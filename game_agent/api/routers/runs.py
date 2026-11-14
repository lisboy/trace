"""run 详情 / 取消 / 事件补拉 / diff 评审（设计 §5 / §8）。"""
from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.api.deps import get_current_context, require
from game_agent.api.state import state
from game_agent.auth.rbac import (PERM_COMMENT, PERM_RUN_CANCEL_OTHERS, PERM_RUN_CANCEL_SELF,
                                  PERM_RUN_CREATE, PERM_VIEW)
from game_agent.core.context import RequestContext
from game_agent.core.notices import post_session_notice
from game_agent.db.models import (Artifact, Comment, Event, LlmCall, LlmTranscript,
                                  Message, Run, RunQuestion, Session, Task, WorkOrder)
from game_agent.db.session import SessionLocal
from game_agent.logging_setup import get_logger

log = get_logger("api.runs")

router = APIRouter(prefix="/api", tags=["runs"])


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        run = await s.get(Run, _u(run_id))
        if run is None or str(run.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        tasks = (await s.execute(select(Task).where(Task.run_id == _u(run_id))
                  .order_by(Task.created_at))).scalars().all()
        wo = await s.scalar(select(WorkOrder).where(WorkOrder.run_id == _u(run_id)))
        arts = (await s.execute(select(Artifact).where(Artifact.run_id == _u(run_id)))).scalars().all()
        u = (await s.execute(
            select(func.count(), func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cached_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0),
                   func.coalesce(func.sum(LlmCall.cache_reported), 0))
            .where(LlmCall.run_id == _u(run_id)))).one()
        prompt, completion, cached, total_ms = int(u[1]), int(u[2]), int(u[3]), int(u[4])
        reported = int(u[5])
        # 当前是否停在一个待回答的提问上（同一 run 内暂停）：让前端在刷新/重连后也能
        # 正确显示「等你回答」而不误以为「执行中」。
        pending_q = await s.scalar(
            select(RunQuestion).where(
                RunQuestion.run_id == _u(run_id),
                RunQuestion.status == "pending").order_by(RunQuestion.created_at.desc()).limit(1))
        usage = {
            "calls": int(u[0]), "prompt": prompt, "completion": completion,
            "cached": cached, "total": prompt + completion,
            "latency_ms": total_ms,
            "tokens_per_sec": round(completion / (total_ms / 1000), 1)
            if total_ms > 0 and completion > 0 else None,
            "cache_hit_rate": round(cached / prompt * 100, 1)
            if (prompt > 0 and reported > 0) else None,
            "cache_reported": reported > 0,
        }
    return {
        "id": str(run.id), "status": run.status, "run_type": run.run_type,
        "project_id": str(run.project_id), "session_id": str(run.session_id),
        "has_placeholder": run.has_placeholder, "final_summary": run.final_summary,
        "change_report": run.change_report_json, "error": run.error,
        "created_at": str(run.created_at), "finished_at": str(run.finished_at) if run.finished_at else None,
        "usage": usage,
        "tasks": [{"id": str(t.id), "kind": t.kind, "agent_key": t.agent_key,
                   "title": t.title, "status": t.status, "attempt": t.attempt,
                   "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
                   "cancel_requested": bool(getattr(t, "cancel_requested", False)),
                   "result_json": t.result_json} for t in tasks],
        "work_order": {
            "goal": wo.goal, "checklist": wo.checklist_json, "art_direction": wo.art_direction,
        } if wo else None,
        "artifacts": [{"id": str(a.id), "asset_kind": a.asset_kind, "slug": a.slug,
                       "rel_path": a.rel_path, "is_placeholder": a.is_placeholder,
                       "provider": a.provider} for a in arts],
        "plan": run.plan_json or [],
        "awaiting_question": (pending_q.question if pending_q is not None else None),
    }


@router.get("/runs/{run_id}/transcripts")
async def run_transcripts(run_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """这次运行的逐轮「上下文请求」清单（不含大字段，点开某轮再取全文）。

    每轮把 llm_calls 的真实 token 按 call_id 拼回来：stats_json 里是按字符估的占比，
    真实 prompt_tokens 才知道端点到底收了多少（两者差距本身就是有用的信息）。
    """
    async with SessionLocal() as s:
        run = await s.get(Run, _u(run_id))
        if run is None or str(run.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        rows = (await s.execute(
            select(LlmTranscript.id, LlmTranscript.call_id, LlmTranscript.agent_key,
                   LlmTranscript.model, LlmTranscript.round_no, LlmTranscript.purpose,
                   LlmTranscript.compressed, LlmTranscript.dropped_msgs,
                   LlmTranscript.req_bytes, LlmTranscript.resp_bytes,
                   LlmTranscript.stats_json, LlmTranscript.truncated,
                   LlmTranscript.created_at)
            .where(LlmTranscript.run_id == run.id)
            .order_by(LlmTranscript.created_at, LlmTranscript.id))).all()
        call_ids = [r[1] for r in rows if r[1]]
        usage_by_call: dict[str, dict] = {}
        if call_ids:
            for c in (await s.execute(select(LlmCall).where(LlmCall.call_id.in_(call_ids)))
                      ).scalars().all():
                usage_by_call[c.call_id] = {
                    "prompt_tokens": c.prompt_tokens, "completion_tokens": c.completion_tokens,
                    "cached_tokens": c.cached_tokens, "latency_ms": c.latency_ms,
                    "cache_reported": bool(c.cache_reported),
                }
    items = []
    for (tid, cid, agent_key, model, round_no, purpose, compressed, dropped,
         req_b, resp_b, stats, trunc, created) in rows:
        u = usage_by_call.get(cid or "", {})
        real = int(u.get("prompt_tokens") or 0)
        est = int((stats or {}).get("est_prompt_tokens") or 0)
        items.append({
            "id": str(tid), "call_id": cid, "agent_key": agent_key, "model": model,
            "round_no": round_no, "purpose": purpose,
            "compressed": bool(compressed), "dropped_msgs": dropped,
            "req_bytes": req_b, "resp_bytes": resp_b, "truncated": bool(trunc),
            "created_at": str(created),
            "stats": stats or {},
            "prompt_tokens": real or est,
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "cached_tokens": int(u.get("cached_tokens") or 0),
            "latency_ms": int(u.get("latency_ms") or 0),
            # 估算与真实的偏离倍数：>1.5 说明启发式低估了（图片/二进制类内容最容易低估）
            "est_vs_real": round(real / est, 2) if real and est else None,
        })
    return {"run_id": str(run.id), "rounds": items}


@router.get("/llm-transcripts/{transcript_id}")
async def llm_transcript_detail(transcript_id: str,
                                ctx: RequestContext = Depends(require(PERM_VIEW))):
    """单轮往返的完整现场：发给模型的 messages（图片已换成占位）+ 模型返回的内容。"""
    async with SessionLocal() as s:
        row = (await s.execute(
            select(LlmTranscript, Run.team_id)
            .join(Run, Run.id == LlmTranscript.run_id)
            .where(LlmTranscript.id == _u(transcript_id)))).first()
        if row is None or str(row[1]) != ctx.team_id:
            raise HTTPException(status_code=404, detail="记录不存在")
        t = row[0]
    try:
        request = json.loads(t.request_json or "{}")
    except json.JSONDecodeError:
        request = {"parse_error": True, "raw": t.request_json[:2000]}
    try:
        response = json.loads(t.response_json or "{}")
    except json.JSONDecodeError:
        response = {"parse_error": True, "raw": t.response_json[:2000]}
    # 逐条消息估算 token 存在 stats_json 里；提到 request 顶层给前端直接用，
    # 这样「每条消息的 tok 之和」与头部「X tok 估算」严格一致，不再各算一份。
    stats = t.stats_json or {}
    if isinstance(request, dict) and isinstance(stats.get("msg_tokens"), list):
        request["msg_tokens"] = stats["msg_tokens"]
    elif isinstance(request, dict) and isinstance(request.get("messages"), list):
        # 老记录没存 msg_tokens：按同一口径从落库消息现算一份（图片已被替换成占位文本，
        # 含图轮次会略偏小，但绝大多数轮次与 est_prompt_tokens 一致，比前端另算一份强）。
        try:
            from game_agent.llm.transcript import _msg_tokens
            request["msg_tokens"] = [_msg_tokens(m) for m in request["messages"]]
        except Exception:  # noqa: BLE001 - 现算失败就退回前端估算
            pass
    return {
        "id": str(t.id), "call_id": t.call_id, "agent_key": t.agent_key, "model": t.model,
        "round_no": t.round_no, "purpose": t.purpose, "compressed": bool(t.compressed),
        "dropped_msgs": t.dropped_msgs, "truncated": bool(t.truncated),
        "created_at": str(t.created_at), "stats": stats,
        "req_bytes": t.req_bytes, "resp_bytes": t.resp_bytes,
        "request": request, "response": response,
    }


@router.get("/runs/{run_id}/plan")
async def run_plan(run_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """计划机制：读取本轮待办清单（UI 刷新/重连后恢复勾选进度）。"""
    async with SessionLocal() as s:
        run = await s.get(Run, _u(run_id))
        if run is None or str(run.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        items = run.plan_json or []
    from game_agent.agents.tools.plan import plan_progress
    return {"run_id": str(run.id), "items": items, "progress": plan_progress(items)}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, ctx: RequestContext = Depends(get_current_context)):
    async with SessionLocal() as s:
        run = await s.get(Run, _u(run_id))
        if run is None or str(run.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        is_self = str(run.triggered_by) == ctx.user_id
        if is_self:
            if not ctx.can(PERM_RUN_CANCEL_SELF):
                raise HTTPException(status_code=403, detail="无权取消")
        else:
            if not ctx.can(PERM_RUN_CANCEL_OTHERS):
                raise HTTPException(status_code=403, detail="无权取消他人的 run")
        run.cancel_requested = True
        was_queued = run.status == "queued"
        if was_queued:
            run.status = "cancelled"
            # 与后端其余部分一致用 Python UTC（不用 func.now()：那是数据库本地时区）
            run.finished_at = datetime.now(timezone.utc)
        # 会在会话里补提示：提交后 ORM 对象可能过期，先把需要的标量取出来
        snap = {"session_id": run.session_id, "team_id": run.team_id,
                "project_id": run.project_id, "status": run.status}
        await s.commit()
    if was_queued:
        await state.services.queue.cancel(run_id)
        # 排队中的 run 由本接口直接落终态（不经 orchestrator），必须自己补一条会话提示，
        # 否则该会话只剩用户那句话 —— 聊天页看起来就是「对话内容空白」。
        await post_session_notice(
            session_id=snap["session_id"], run_id=run_id,
            team_id=snap["team_id"], project_id=snap["project_id"],
            text="已按你的要求取消本次执行（排队中，未开始执行）。")
    return {"ok": True, "status": snap["status"]}


@router.post("/runs/{run_id}/tasks/{task_id}/cancel")
async def cancel_task(run_id: str, task_id: str,
                      ctx: RequestContext = Depends(get_current_context)):
    """单独停掉一个子agent（主agent 与其它子agent 继续跑）。

    语义与整轮「停止」不同：整轮停止会掐断主agent 和全部子agent（见 cancel_run），
    这里只给**某一个子 task** 置取消位，它自己的看门狗看到就只掐断它，把「已停止」当成
    该子任务的结果回给主agent——主agent 既能继续等其它子agent，也能拿这条失败/停止信息
    自己接手或再派一个新子agent（用户 2026-11-11 要求）。
    """
    async with SessionLocal() as s:
        run = await s.get(Run, _u(run_id))
        if run is None or str(run.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        is_self = str(run.triggered_by) == ctx.user_id
        if is_self:
            if not ctx.can(PERM_RUN_CANCEL_SELF):
                raise HTTPException(status_code=403, detail="无权停止子agent")
        else:
            if not ctx.can(PERM_RUN_CANCEL_OTHERS):
                raise HTTPException(status_code=403, detail="无权停止他人的 run")
        task = await s.get(Task, _u(task_id))
        if task is None or str(task.run_id) != str(run.id):
            raise HTTPException(status_code=404, detail="子任务不存在")
        if task.status in ("done", "failed", "cancelled"):
            # 已经收口的子任务不用再停：如实告诉前端（前端据此刷新状态）
            return {"ok": True, "status": task.status, "already": True}
        task.cancel_requested = True
        snap = {"status": task.status, "session_id": run.session_id,
                "project_id": run.project_id}
        await s.commit()
    # 广播一条状态，前端收到立刻拉事件流（task_done(cancelled) 由看门狗落库后推送）
    try:
        from game_agent.core.abstractions import EventEnvelope
        await state.services.bus.emit(EventEnvelope(
            team_id=str(ctx.team_id), project_id=str(snap["project_id"]),
            run_id=str(run_id), type="status",
            payload={"status": "running", "note": "已停止一个子agent"}))
    except Exception:  # noqa: BLE001 - 广播失败不影响取消
        pass
    return {"ok": True, "status": snap["status"]}


class SteerIn(BaseModel):
    content: str = ""
    images: list[str] = []  # data:image/... 的 dataURL（与聊天发送同一套校验/压缩）
    files: list[dict] = []  # 已在 .trace/uploads 的附件：{path,name,size}
    # 前端生成的幂等键：连点/重试同一条插话时只落一条消息（以前双击会发两遍）
    client_msg_id: str = ""
    # 运行中「切换模型」：带上就不只是插话，而是把这一轮之后的 LLM 换成新档位。
    # 空串 = 显式回到「跟随角色默认」；不传(None) = 本次不动模型。
    model_profile_key: str | None = None
    reasoning_effort: str | None = None
    # 运行中「改运行选项」：能力开关 / 会话权限 / 仅分析。带上即改这一轮后续的硬约束
    # （重新裁剪工具白名单、覆盖权限作用域、换系统提示里的约束块）。不传 = 本次不动。
    caps: dict | None = None            # {test,web,media}
    permission_scope: str | None = None  # workspace | full
    analyze: bool | None = None          # 仅分析（运行中可开/可关）


@router.post("/runs/{run_id}/steer")
async def steer_run(run_id: str, body: SteerIn,
                    ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """插话 / 运行中切换模型：并进**正在执行**的这一轮，不取消、不重开。

    与会话里正常发消息的区别：那会开一个新 run（组长从零开始，还得重新探路）；
    插话只是往当前这轮的对话里追加一条 user 消息，下一轮 LLM 调用前由 runtime 取走，
    已完成的步骤/工具结果/计划全部保留。

    带图插话（2026-10-29）：图片与聊天发送共用 `_validate_images`（同样压到进上下文那一版、
    原图另存 .trace/uploads），runtime 拼成带 image parts 的 user 消息。图不走视觉回灌，
    所以不会下一轮就过期。

    运行中切换模型（2026-11-01）：body 带 model_profile_key / reasoning_effort 时，除插话外
    还要把这一轮后续的 LLM 换成新档位。实现上：校验档位 → 写回 run.options_json（这样
    「失败后继续 / 清单未完成自动续跑」不会悄悄退回角色默认）→ 推进 steering 邮箱，
    runtime 下一轮开头用 _load_profile 重新解析并重建 LLM。只切模型、不带文字/图的请求
    不落聊天消息，只广播一条状态提示。
    """
    from game_agent.api.routers.sessions import (FileIn, _resolve_files, _validate_files,
                                                 _validate_images)
    from game_agent.orchestration import steering

    text = (body.content or "").strip()
    # 只切模型的请求：没有文字/图/附件，但有模型字段（model_profile_key 可能是空串=跟随默认）
    is_model_switch = body.model_profile_key is not None or body.reasoning_effort is not None
    # 改运行选项的请求：能力开关 / 会话权限 / 仅分析，任一带上即算（可叠加在插话上）
    is_settings_switch = (body.caps is not None or body.permission_scope is not None
                          or body.analyze is not None)
    if not text and not body.images and not body.files and not is_model_switch \
            and not is_settings_switch:
        raise HTTPException(status_code=400, detail="插话内容为空")
    model_switch: dict | None = None
    if is_model_switch:
        effort = body.reasoning_effort
        if effort:
            if effort not in ("none", "minimal", "low", "medium", "high"):
                raise HTTPException(status_code=400, detail="推理等级不合法")
        else:
            effort = ""
        pkey = (body.model_profile_key or "").strip()
        # 档位是否存在在下面开事务后再校验（要用到 team_id 与 DB 会话）
        model_switch = {"profile_key": pkey, "effort": effort}
    # 运行选项：只归一化，不在此校验（permission_scope 走 normalize_scope 的容错默认）
    settings_switch: dict | None = None
    if is_settings_switch:
        from game_agent.orchestration import policy as policy_mod
        from game_agent.orchestration.caps import normalize_caps
        settings_switch = {}
        if body.caps is not None:
            settings_switch["caps"] = normalize_caps(body.caps)
        if body.permission_scope is not None:
            settings_switch["permission_scope"] = policy_mod.normalize_scope(body.permission_scope)
        if body.analyze is not None:
            settings_switch["analyze"] = bool(body.analyze)
    client_msg_id = (body.client_msg_id or "").strip()[:64]
    switch_label: str | None = None
    async with SessionLocal() as s:
        run, _project, workspace = await _run_workspace(s, run_id, ctx)
        snap = {"status": run.status, "session_id": run.session_id,
                "project_id": run.project_id}
        if snap["status"] in ("succeeded", "failed", "cancelled", "blocked", "orphaned"):
            return {"ok": False, "delivered": False,
                    "detail": f"该任务已结束（{snap['status']}），按新消息发送即可"}
        # 切换模型：档位必须存在（本团队、kind=llm），否则报错；随后写回 options_json
        if model_switch is not None:
            pkey = model_switch["profile_key"]
            if pkey:
                from game_agent.db.models import ModelProfile
                from sqlalchemy import select as _select
                prof = await s.scalar(_select(ModelProfile).where(
                    ModelProfile.team_id == _u(ctx.team_id),
                    ModelProfile.key == pkey,
                    ModelProfile.kind == "llm"))
                if prof is None:
                    raise HTTPException(status_code=400, detail="所选模型档位不存在")
                switch_label = prof.model or prof.label or pkey
            opts = dict(run.options_json or {})
            if pkey:
                opts["model_profile_key"] = pkey
            else:
                opts.pop("model_profile_key", None)  # 空串=回到跟随角色默认
            if model_switch["effort"]:
                opts["reasoning_effort"] = model_switch["effort"]
            else:
                opts.pop("reasoning_effort", None)
            # 关键：续跑（continue_run）与自动续跑都从 options_json 继承模型，
            # 不回写这里的话，切完模型再点「继续」会悄悄回到角色默认档。
            run.options_json = opts
        # 改运行选项：写回 options_json（续跑 / 自动续跑继承新值）。用 SQLAlchemy 的
        # flag_modified 标记 JSON 列已变更——原地改 dict 有时不触发 UPDATE。
        if settings_switch is not None:
            from sqlalchemy.orm.attributes import flag_modified
            opts = dict(run.options_json or {})
            if "caps" in settings_switch:
                opts["caps"] = settings_switch["caps"]
            if "permission_scope" in settings_switch:
                opts["permission_scope"] = settings_switch["permission_scope"]
            if "analyze" in settings_switch:
                # 仅分析以 run_type 落地（续跑按 run.run_type 继承）；这里同步一份到 options
                opts["analyze"] = settings_switch["analyze"]
                run.run_type = "analyze" if settings_switch["analyze"] else "modify"
            run.options_json = opts
            flag_modified(run, "options_json")
        # 幂等：同一个 client_msg_id 已经在同一个 run 下落地过就直接返回成功，
        # 不再写第二条、也不再往 steering 邮箱里塞第二遍（连点/网络重试安全）。
        if client_msg_id:
            recent = (await s.execute(
                select(Message).where(
                    Message.session_id == _u(snap["session_id"]),
                    Message.run_id == _u(run_id),
                    Message.author_type == "user",
                ).order_by(Message.created_at.desc()).limit(10))).scalars().all()
            for m in recent:
                if str((m.meta_json or {}).get("client_msg_id") or "") == client_msg_id:
                    return {"ok": True, "delivered": True, "duplicate": True,
                            "images": len((m.meta_json or {}).get("images") or []),
                            "files": len((m.meta_json or {}).get("files") or [])}
        images, image_paths = await run_in_threadpool(_validate_images, body.images, ctx.team_id, workspace)
        raw_files = [FileIn(path=str(f.get("path") or ""), name=str(f.get("name") or ""),
                            size=int(f.get("size") or 0))
                     for f in body.files if isinstance(f, dict)]
        files = await run_in_threadpool(
            _resolve_files, workspace, _validate_files(raw_files))
        # 落成会话里的用户消息：刷新后仍可见，之后的轮次也会从历史里读到
        # （纯切模型请求没有文字/图/附件，不落消息，避免聊天里冒出一条空气泡）
        msg = None
        if text or images or files:
            meta: dict = {"steer": True}
            if client_msg_id:
                meta["client_msg_id"] = client_msg_id
            if images:
                meta["images"] = images
                meta["image_paths"] = image_paths
            if files:
                meta["files"] = files
            msg = await state.services.repo.create_message(
                session_id=snap["session_id"], run_id=run_id, author_type="user",
                author_id=ctx.user_id, content=text, meta_json=meta)
        await s.commit()
    # 给模型的这一版补上附件位置：附件只传路径，模型得知道去哪读
    noted = text or ("（这条插话只有附件）" if (images or files) else "")
    if files:
        noted += ("\n（附件已在工作区：" + "、".join(f["path"] for f in files[:6])
                  + "；需要内容用 read_file / parse_document 读取）")
    # 广播：所有打开该项目的窗口都能马上看到这条插话，不用等 4s 轮询兜底。
    # 以前 steer 只写库不广播，用户会感觉「我发了插话，对话半天不刷新」。
    try:
        from game_agent.core.abstractions import EventEnvelope
        if msg is not None:
            await state.services.bus.emit(EventEnvelope(
                team_id=str(ctx.team_id), project_id=str(snap["project_id"]),
                run_id=str(run_id), type="message_new",
                payload={"session_id": str(snap["session_id"]), "message_id": str(msg.id),
                         "author_type": "user", "kind": "steer"},
            ))
        if model_switch is not None:
            target = f"「{switch_label}」" if switch_label else "跟随角色默认"
            if model_switch["effort"]:
                target += f"，推理等级 {model_switch['effort']}"
            await state.services.bus.emit(EventEnvelope(
                team_id=str(ctx.team_id), project_id=str(snap["project_id"]),
                run_id=str(run_id), type="status",
                payload={"status": "running",
                         "note": f"已收到模型切换 → {target}；当前这一轮仍用原模型跑完，"
                                 "下一个思考轮次生效"},
            ))
    except Exception:  # noqa: BLE001 - 广播失败不影响投递，前端仍会轮询补拉
        pass
    steering.push(run_id, noted, images, model=model_switch)
    if settings_switch is not None:
        steering.push_settings(run_id, settings_switch)
    # 插话分派（用户 2026-11-11 要求）：如果这一轮有正在跑的子agent，由**大模型**判断这条
    # 插话该不该同步给其中哪几个，命中的子任务各自从自己的信箱取走。不阻塞主返回：
    # 判定要一次短 LLM 调用，放到后台跑，主agent 的插话立即照旧投递（即便判定失败也只是
    # 不给子agent 派，主agent 不受影响）。
    if text and state.orchestrator is not None:
        try:
            async with SessionLocal() as _s2:
                running_subs = (await _s2.execute(select(Task).where(
                    Task.run_id == _u(run_id),
                    Task.agent_key == "sub",
                    Task.status == "running").order_by(Task.created_at))).scalars().all()
        except Exception:  # noqa: BLE001 - 查询失败不影响插话投递
            running_subs = []
        if running_subs:
            subs_info = [{"index": i, "name": (t.title or "").replace("子agent · ", ""),
                          "question": str((t.result_json or {}).get("question") or t.title or "")}
                         for i, t in enumerate(running_subs)]
            task_ids = [str(t.id) for t in running_subs]

            async def _dispatch(note_text: str, subs: list[dict], tids: list[str],
                                team_id: str) -> None:
                from game_agent.orchestration import interject
                try:
                    pkey = await state.orchestrator._agent_profile_key(team_id, "main")
                    profile = await state.orchestrator._load_profile(team_id, pkey)
                except Exception:  # noqa: BLE001
                    profile = {}
                try:
                    targets = await interject.decide_targets(note_text, subs, profile=profile)
                except Exception:  # noqa: BLE001 - 判定异常不派
                    targets = []
                for idx in targets:
                    if 0 <= idx < len(tids):
                        steering.push_to_task(tids[idx], note_text)
                        log.info("interjection dispatched to subagent %s", tids[idx][:8])

            import asyncio as _asyncio
            _asyncio.create_task(_dispatch(noted, subs_info, task_ids, str(ctx.team_id)))
    return {"ok": True, "delivered": True, "images": len(images), "files": len(files)}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, since: int = Query(0),
                     ctx: RequestContext = Depends(require(PERM_VIEW))):
    events = await state.services.repo.get_events_since(run_id, since)
    return [{"seq": e.seq, "type": e.type, "payload": e.payload_json,
             # task_id：子agent 的事件带自己的 task，前端靠它把行归进对应的子agent 分组
             "task_id": str(e.task_id) if e.task_id else None,
             "created_at": str(e.created_at)} for e in events]


# ---------------- diff 评审（设计 §8.4：自建 diff 评审 + 行内评论） ----------------
@router.get("/runs/{run_id}/diff")
async def run_diff(run_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """评审用行级 diff。数据源是工具层写前快照（平台不接管 git，工作区里有没有
    .git 都不看），所以任何项目都能评审，且与「改动审阅/回滚」看到的是同一份事实。"""
    from game_agent.orchestration import snapshots
    async with SessionLocal() as s:
        run, project, workspace = await _run_workspace(s, run_id, ctx)
    changes = snapshots.list_changes(str(run.id), workspace)
    files = [{"file": f["path"], "status": f["status"],
              "hunks": [{"header": f"{f['status']} · +{f['added']} −{f['deleted']}",
                         "lines": f["diff"]}]}
             for f in changes["files"]]
    # 附上已有行内评论
    async with SessionLocal() as s:
        rows = (await s.execute(select(Comment).where(
            Comment.team_id == _u(ctx.team_id),
            Comment.target_id == _u(run_id)))).scalars().all()
    comments = {}
    for c in rows:
        for m in (c.mentions or []):
            comments.setdefault(m, []).append({
                "id": str(c.id), "body": c.body, "resolved": c.resolved,
                "author_id": str(c.author_id), "created_at": str(c.created_at),
            })
    return {"base": "写前快照", "files": files, "comments": comments}


# ---------------- 改动审阅 / 回滚（不依赖 git：工具层写前快照） ----------------
async def _run_workspace(s, run_id: str, ctx: RequestContext):
    """取 run 对应的工作区绝对路径（repo_path 兼容绝对路径）。"""
    from game_agent.config import PROJECTS_ROOT
    from game_agent.db.models import Project
    run = await s.get(Run, _u(run_id))
    if run is None or str(run.team_id) != ctx.team_id:
        raise HTTPException(status_code=404, detail="run 不存在")
    project = await s.get(Project, run.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return run, project, (PROJECTS_ROOT / project.repo_path)


@router.get("/runs/{run_id}/changes")
async def run_changes(run_id: str, diff: bool = Query(True),
                      ctx: RequestContext = Depends(require(PERM_VIEW))):
    """本次 run 改过哪些文件 + 前后差异（可直接用于审阅与回滚）。"""
    from game_agent.orchestration import snapshots
    async with SessionLocal() as s:
        run, project, workspace = await _run_workspace(s, run_id, ctx)
    data = snapshots.list_changes(str(run.id), workspace, with_diff=bool(diff))
    data["status"] = run.status
    data["project_id"] = str(project.id)
    data["change_report"] = run.change_report_json
    return data


# ---------------- 单文件查看：全部内容 / 改动前快照 / 原始字节（预览用） ----------------
TEXT_PREVIEW_MAX = 2 * 1024 * 1024      # 文本预览上限，超过只提示下载
RAW_MAX_BYTES = 24 * 1024 * 1024        # 直出字节上限（图片 / HTML / PDF）
# 可长缓存的直出类型（截图/回灌视觉的本地图等，路径固定、内容基本不变）
_RAW_CACHEABLE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
                       ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
_RAW_CACHEABLE_EXTS_IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}

_MD_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        line-height: 1.65; color: #1f2329; background: #fff; margin: 0; }}
  .layout {{ display: flex; min-height: 100vh; align-items: flex-start; }}
  /* 左侧目录：随页面滚动钉住，独立滚动 */
  nav#md-nav {{ width: 224px; flex: 0 0 224px; position: sticky; top: 0;
        max-height: 100vh; overflow-y: auto; border-right: 1px solid #f0f1f3;
        background: #fafbfc; padding: 16px 8px; box-sizing: border-box; }}
  .toc-title {{ font-size: 11px; font-weight: 500; letter-spacing: .04em;
        color: #9aa0a6; padding: 0 8px 6px; }}
  .toc-item {{ display: block; font-size: 11.5px; line-height: 1.35;
        color: #57606a; text-decoration: none; padding: 3px 8px; border-radius: 5px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .toc-item:hover {{ background: #eef1f4; color: #24292f; }}
  .toc-item.active {{ background: #eef3ff; color: #2f54eb; font-weight: 500; }}
  main {{ flex: 1; min-width: 0; padding: 24px 28px 60px; }}
  h1,h2,h3,h4 {{ line-height: 1.3; margin: 1.4em 0 .5em; scroll-margin-top: 16px; }}
  h1 {{ font-size: 1.7em; border-bottom: 1px solid #e5e7eb; padding-bottom: .3em; }}
  h2 {{ font-size: 1.4em; border-bottom: 1px solid #f0f1f3; padding-bottom: .25em; }}
  table {{ border-collapse: collapse; margin: 1em 0; display: block;
         overflow-x: auto; max-width: 100%; }}
  th,td {{ border: 1px solid #d0d7de; padding: 6px 10px; font-size: .93em; }}
  th {{ background: #f6f8fa; }}
  code {{ background: #f2f3f5; padding: 1px 5px; border-radius: 3px;
        font-family: ui-monospace, Consolas, monospace; font-size: .9em; }}
  pre {{ background: #f6f8fa; padding: 12px 14px; border-radius: 6px;
       overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ color: #57606a; border-left: 4px solid #d0d7de;
              margin: 1em 0; padding: 0 12px; }}
  img {{ max-width: 100%; }}
  @media (max-width: 820px) {{ nav#md-nav {{ display: none; }} }}
</style>
</head><body>
<div class="layout">
  <nav id="md-nav" style="display:none">
    <div class="toc-title">目录</div>
    <div id="md-nav-list"></div>
  </nav>
  <main>
    <div id="md-src" style="display:none">{src}</div>
    <div id="md-out"></div>
  </main>
</div>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script>
(function () {{
  var raw = document.getElementById("md-src").textContent;
  var out = document.getElementById("md-out");
  var nav = document.getElementById("md-nav");
  var navList = document.getElementById("md-nav-list");
  var headingEls = [];
  var activeLink = null;

  function render() {{
    if (window.marked) {{
      marked.setOptions({{ gfm: true, breaks: false }});
      out.innerHTML = marked.parse(raw);
    }} else {{
      // CDN 不可达（离线服务器）：退回等宽排版，至少表格逐行可读，不比纯文本差
      var pre = document.createElement("pre");
      pre.style.cssText = "white-space:pre-wrap;font-size:13px";
      pre.textContent = raw;
      out.appendChild(pre);
    }}
    buildToc();
  }}

  // 渲染后从真实 DOM 抓 h1~h4 建目录（同名标题去重 -1/-2，代码块里的 # 不会误收）
  function buildToc() {{
    var hs = Array.prototype.slice.call(out.querySelectorAll("h1, h2, h3, h4"));
    if (hs.length < 2) return; // 0~1 个标题没有目录价值，正文铺满
    var seen = {{}};
    hs.forEach(function (el) {{
      var base = (el.textContent || "").trim() || "（无标题）";
      var n = seen[base] || 0;
      seen[base] = n + 1;
      var id = n > 0 ? base + "-" + n : base;
      el.id = id;
      var a = document.createElement("a");
      a.href = "#" + encodeURIComponent(id);
      a.textContent = base;
      a.className = "toc-item";
      a.title = base;
      a.style.paddingLeft = ((Number(el.tagName[1]) - 1) * 10 + 8) + "px";
      a.addEventListener("click", function (ev) {{
        ev.preventDefault();
        scrollToHeading(el, a);
      }});
      navList.appendChild(a);
      headingEls.push({{ el: el, link: a }});
    }});
    nav.style.display = "";
  }}

  function scrollToHeading(el, a) {{
    setActive(a);
    var startY = window.scrollY;
    el.scrollIntoView({{ behavior: "smooth", block: "start" }});
    // 无合成器环境（headless / 远程桌面）smooth 可能静默不动：退回瞬时跳转
    window.setTimeout(function () {{
      if (Math.abs(window.scrollY - startY) < 4) {{
        el.scrollIntoView({{ behavior: "auto", block: "start" }});
      }}
    }}, 300);
  }}

  function setActive(a) {{
    if (activeLink) activeLink.classList.remove("active");
    activeLink = a;
    a.classList.add("active");
  }}

  // 滚动高亮：找判定线之上最近的一个标题
  var ticking = false;
  function onScroll() {{
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function () {{
      var top = 56;
      var cur = null;
      headingEls.forEach(function (h) {{
        if (h.el.getBoundingClientRect().top <= top) cur = h.link;
      }});
      if (cur) setActive(cur);
      ticking = false;
    }});
  }}
  window.addEventListener("scroll", onScroll, {{ passive: true }});

  render();
}})();
</script>
</body></html>"""


def _md_as_html(target: Path) -> str:
    """Markdown 包成可渲染的 HTML 壳。文本先做 HTML 转义再塞进 DOM，
    由 marked 解析——不直接拼 HTML，文件内容里的 <script> 不会被执行。"""
    import html as _html
    text = target.read_bytes().decode("utf-8", errors="replace")
    return _MD_HTML.format(title=_html.escape(target.name),
                           src=_html.escape(text))


def _resolve_in_workspace(workspace, rel: str) -> Path:
    """把「工作区相对路径」解析成绝对路径；越界一律 400。"""
    ws = Path(workspace).resolve()
    target = (ws / str(rel).replace("\\", "/").lstrip("/")).resolve()
    if target != ws and not str(target).startswith(str(ws) + os.sep):
        raise HTTPException(status_code=400, detail="非法路径")
    return target


def _sniff_kind(target: Path, rel: str) -> str:
    """扩展名认不出来时看内容：含 NUL 字节当二进制，否则按文本。"""
    from game_agent.orchestration import snapshots
    kind = snapshots.preview_kind(rel)
    if kind != "unknown":
        return kind
    try:
        head = target.read_bytes()[:4096]
    except OSError:
        return "binary"
    return "binary" if b"\x00" in head else "text"


@router.get("/runs/{run_id}/file")
async def run_file(run_id: str, path: str = Query(...), mode: str = Query("text"),
                   ctx: RequestContext = Depends(require(PERM_VIEW))):
    """看本次 run 改动里的单个文件。

    mode=text → 当前内容（文本预览走它，带 kind 让前端选渲染方式）；
    mode=orig → 改动前快照（删掉的文件也能看原来长什么样）；
    mode=raw  → 原始字节，交给 <img> / <iframe> / <audio> 用。
    """
    from game_agent.orchestration import snapshots
    async with SessionLocal() as s:
        run, project, workspace = await _run_workspace(s, run_id, ctx)

    rel = path.replace("\\", "/").lstrip("/")
    if mode == "orig":
        target = snapshots.blob_path(str(run.id), rel)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="本次没有该文件的改动前快照")
    else:
        target = _resolve_in_workspace(workspace, rel)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    size = target.stat().st_size
    kind = _sniff_kind(target, rel)
    if mode == "raw":
        if size > RAW_MAX_BYTES:
            raise HTTPException(status_code=400, detail="文件过大，不支持预览")
        media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return FileResponse(str(target), media_type=media)
    if kind in ("image", "audio", "pdf", "binary"):
        return {"path": rel, "kind": kind, "size": size, "content": "", "truncated": False}
    if size > TEXT_PREVIEW_MAX:
        return {"path": rel, "kind": kind, "size": size, "content": "", "truncated": True}
    return {"path": rel, "kind": kind, "size": size, "truncated": False,
            "content": target.read_bytes().decode("utf-8", errors="replace")}


class RevealBody(BaseModel):
    path: str


@router.post("/runs/{run_id}/reveal")
async def run_reveal(run_id: str, body: "RevealBody",
                     ctx: RequestContext = Depends(require(PERM_VIEW))):
    """在系统文件管理器里定位工作区里的文件（点击文件查看器顶部的路径触发）。

    Windows 用 explorer /select（选中该文件），文件已不存在（如已删/已回滚）则退回
    打开工作区根目录。必须走 to_thread：Windows 上的 Selector 事件循环里
    asyncio.create_subprocess_exec 永远 NotImplementedError（见项目记忆）。
    """
    import sys
    import subprocess

    async with SessionLocal() as s:
        _, _, workspace = await _run_workspace(s, run_id, ctx)

    try:
        target_abs = _resolve_in_workspace(workspace, body.path)
        exists = target_abs.is_file()
    except HTTPException:
        raise
    if not exists:
        # 文件已被删/回滚：退回定位到工作区根目录
        target_abs = Path(workspace).resolve()

    def _open(abs_path: Path) -> None:
        if sys.platform == "win32":
            # explorer /select 选中后自身退出码是 1，不能当失败
            subprocess.run(["explorer", "/select,", str(abs_path)], check=False)
        else:  # linux / mac：打开所在目录
            subprocess.run(["xdg-open" if sys.platform != "darwin" else "open",
                            str(abs_path.parent)], check=False)

    await run_in_threadpool(_open, target_abs)
    return {"ok": True}


@router.get("/runs/{run_id}/raw/{file_path:path}")
async def run_raw(run_id: str, file_path: str,
                  thumb: int = Query(0),
                  ctx: RequestContext = Depends(require(PERM_VIEW))):
    """按工作区相对路径直出文件字节，给 HTML / 图片预览用。

    走「路径式」URL 而不是 ?path=：预览 HTML 时同目录的 css/js/图片都是相对路径，
    按 URL 层级解析才能跟着加载出来（浏览器会带上 ga_session cookie 完成鉴权）。

    thumb=长边像素：图片按需缩放成 JPEG 并缓存到 .trace/thumbnails/（截图缩略图用）。"""
    async with SessionLocal() as s:
        run, project, workspace = await _run_workspace(s, run_id, ctx)
    target = _resolve_in_workspace(workspace, file_path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    # Markdown 不能按字节直出：浏览器只显示纯文本，表格/标题全糊成一行
    # （用户截图：/raw/.../review.md 打开是一坨）。包一层 HTML 壳交给
    # marked 渲染；离线环境 CDN 失败时退回等宽 <pre>，不比原来差。
    if target.suffix.lower() == ".md":
        return Response(content=_md_as_html(target), media_type="text/html")
    is_img = target.suffix.lower() in _RAW_CACHEABLE_EXTS_IMG
    if thumb and is_img:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_raw_thumb, workspace, target, thumb)
    if target.stat().st_size > RAW_MAX_BYTES:
        raise HTTPException(status_code=400, detail="文件过大，不支持预览")
    media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    # 图片/音频（截图、回灌视觉的本地图）按路径固定 → 给短缓存，重复渲染时少下一遍；
    # HTML 预览不带缓存（它的相对资源/内容会随改动变）。
    if target.suffix.lower() in _RAW_CACHEABLE_EXTS:
        st = target.stat()
        etag = f"{st.st_mtime_ns:x}_{st.st_size:x}"
        return FileResponse(str(target), media_type=media,
                            headers={"Cache-Control": "public, max-age=0, must-revalidate",
                                     "ETag": etag})
    return FileResponse(str(target), media_type=media)


@router.get("/run-shots")
async def serve_run_shot(ref: str = Query(...), thumb: int = Query(0),
                         ctx: RequestContext = Depends(require(PERM_VIEW))):
    """run 过程图（截图/回灌/合图）稳定目录直出：DATA_DIR/run_shots/<team>/<run>/<file>。

    为什么单独一条：这些图以前只落在工作区 .trace/screenshots|previews，run 收尾就清空，
    用户回看轨迹时缩略图全变「图片已清理」。现在工具落图时另存稳定目录，文本里写
    ``固定预览：run-shot/<team>/<run>/<file>``，前端在此取图（带 Bearer 头 → blob URL，
    不受 cookie 方案影响）。强制团队校验：ref 内嵌的 team 必须等于登录者团队。"""
    from game_agent.core import run_shots

    if run_shots.norm_id(run_shots.team_of(ref)) != run_shots.norm_id(ctx.team_id):
        raise HTTPException(status_code=403, detail="无权查看该图")
    target = run_shots.resolve_ref(ref)
    if target is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    ext = target.suffix.lower()
    if thumb and ext in _RAW_CACHEABLE_EXTS_IMG:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_run_shot_thumb, target, thumb)
    st = target.stat()
    media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    etag = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    return FileResponse(str(target), media_type=media, headers={
        "Cache-Control": "public, max-age=0, must-revalidate", "ETag": etag})


def _run_shot_thumb(target: Path, w: int):  # type: ignore[no-untyped-def]
    """run 稳定图缩略图：缓存到 DATA_DIR/run_shots/.thumbs/<hash>_<w>_<版本>.jpg。"""
    import hashlib

    from game_agent.config import DATA_DIR
    w = max(64, min(int(w), 1280))
    st = target.stat()
    token = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    h = hashlib.sha1(str(target.name).encode("utf-8")).hexdigest()[:20]
    cache = DATA_DIR / "run_shots" / ".thumbs"
    dest = cache / f"{h}_{w}_{token}.jpg"
    cache_hdr = {"Cache-Control": "public, max-age=0, must-revalidate", "ETag": token}
    if dest.is_file() and dest.stat().st_size > 0:
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    try:
        from PIL import Image
        im = Image.open(target)
        im.load()
        if im.mode != "RGB":
            im = im.convert("RGB")
        if max(im.size) > w:
            im.thumbnail((w, w), Image.LANCZOS)
        cache.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        im.save(tmp, format="JPEG", quality=80, optimize=True)
        tmp.replace(dest)
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    except Exception:  # noqa: BLE001 - 生成失败回原图
        return FileResponse(str(target), headers=cache_hdr)


def _raw_thumb(workspace, target: Path, w: int):  # type: ignore[no-untyped-def]
    """run 原始文件的按需缩略图：缓存到 <workspace>/.trace/thumbnails/<hash>_<w>.jpg。"""
    import hashlib

    w = max(64, min(int(w), 1280))
    try:
        rel = target.relative_to(workspace)
    except ValueError:
        rel = target
    h = hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:20]
    st = target.stat()
    token = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    dest = workspace / ".trace" / "thumbnails" / f"{h}_{w}_{token}.jpg"
    # 原图被重新写过后 token 变 → 缩略图重建；ETag 让浏览器用对版本
    cache_hdr = {"Cache-Control": "public, max-age=0, must-revalidate", "ETag": token}
    if dest.is_file() and dest.stat().st_size > 0:
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    png_dest = dest.with_suffix(".png")
    if png_dest.is_file() and png_dest.stat().st_size > 0:
        return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
    try:
        if st.st_size > 30 * 1024 * 1024:
            return FileResponse(str(target), headers=cache_hdr)
        from PIL import Image
        im = Image.open(target)
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if max(im.size) > w:
            im.thumbnail((w, w), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if has_alpha:
            tmp = png_dest.with_suffix(".png.tmp")
            im.save(tmp, format="PNG", optimize=True)
            tmp.replace(png_dest)
            return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
        if im.mode != "RGB":
            im = im.convert("RGB")
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        im.save(tmp, format="JPEG", quality=80, optimize=True)
        tmp.replace(dest)
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    except Exception:  # noqa: BLE001 - 生成失败回原图
        return FileResponse(str(target), headers=cache_hdr)


class RevertIn(BaseModel):
    paths: list[str] | None = None   # 为空 = 全部回滚


@router.post("/runs/{run_id}/revert")
async def revert_run(run_id: str, body: RevertIn,
                     ctx: RequestContext = Depends(get_current_context)):
    """审阅不通过 → 把本 run 改过的文件恢复成 run 之前的样子（可单个/全部）。"""
    from sqlalchemy import select as _select

    from game_agent.core.abstractions import EventEnvelope
    from game_agent.db.models import Project
    from game_agent.orchestration import snapshots

    async with SessionLocal() as s:
        run, project, workspace = await _run_workspace(s, run_id, ctx)
        is_self = str(run.triggered_by) == ctx.user_id
        if is_self:
            if not ctx.can(PERM_RUN_CANCEL_SELF):
                raise HTTPException(status_code=403, detail="无权回滚")
        elif not ctx.can(PERM_RUN_CANCEL_OTHERS):
            raise HTTPException(status_code=403, detail="无权回滚他人的 run")
        if run.status in ("queued", "running"):
            raise HTTPException(status_code=400,
                                detail="该任务还在执行，先「取消」再回滚，避免写出半成品")
        # 同一项目还有别的 run 在跑时不回滚：工作区是共享检出，会把别人正在改的文件覆盖掉
        others = (await s.execute(_select(Run).where(
            Run.project_id == run.project_id, Run.id != run.id,
            Run.status.in_(("queued", "running"))))).scalars().all()
        if others:
            raise HTTPException(status_code=409,
                                detail=f"项目还有 {len(others)} 个任务在跑，请等它们结束再回滚")
        snap = {"team_id": str(run.team_id), "project_id": str(run.project_id),
                "session_id": str(run.session_id), "status": run.status}

    result = snapshots.revert(str(run_id), workspace, body.paths)
    if result["reverted"]:
        try:
            await state.services.bus.emit(EventEnvelope(
                team_id=snap["team_id"], project_id=snap["project_id"], run_id=str(run_id),
                type="run_reverted",
                payload={"paths": result["reverted"], "failed": result["failed"]}))
        except Exception:  # noqa: BLE001 - 事件推送失败不影响回滚结果
            pass
        await post_session_notice(
            session_id=snap["session_id"], run_id=str(run_id), team_id=snap["team_id"],
            project_id=snap["project_id"],
            text="已回滚本次改动：" + "、".join(f"`{p}`" for p in result["reverted"][:12])
                 + (f" 等 {len(result['reverted'])} 个文件" if len(result["reverted"]) > 12 else ""))
    return result


class ContinueIn(BaseModel):
    note: str | None = None        # 用户额外补一句（可空）
    # 续跑沿用上一轮模型；但前端若带着当前选择器里的模型（用户可能刚在面板切过），
    # 以它为准，避免「切了模型 → 点继续 → 又回到角色默认」的坑。
    model_profile_key: str | None = None
    reasoning_effort: str | None = None


def _is_answer_msg(content: str) -> bool:
    """ask_user 的回答消息（前端以「【回答】」开头）——续跑时不能把它当成新需求。"""
    return (content or "").lstrip().startswith("【回答】")


# 点「继续」时后端合成的续跑说明开头。它本身已经是「包装过的需求」，
# 再被当成「上次请求原文」就会套出【继续…】→【继续…】→【继续…】三层。
_BRIEF_PREFIX = "【继续上次被中断的任务】"


def _is_brief_msg(content: str) -> bool:
    return (content or "").lstrip().startswith(_BRIEF_PREFIX)


# 会话里给用户看的续跑消息只有这一句人话；完整续跑说明放在 meta["brief"] 里，
# 交给 orchestrator._load_context 读给模型（见 continue_run / _auto_continue）。
# 注意：手动「继续」已不再写这条消息（改为把上下文挂在 run.options 上、对话里零新增），
# 这里仅保留给历史数据和自动续跑消息的正文约定。
CONTINUE_SHORT = "继续完成任务目标"


def _is_cont_msg(m) -> bool:  # type: ignore[no-untyped-def]
    """这条用户消息是不是「系统合成的续跑说明」（手动继续 / 自动续跑）。

    续跑说明的正文现在挪进了 meta["brief"]，content 只有一句短话——不能再靠
    正文前缀判断。优先看 meta 标记，老数据（正文仍是整段说明）再退回看前缀。
    """
    meta = m.meta_json or {}
    if meta.get("continue_from") or meta.get("auto_continue_from"):
        return True
    return _is_brief_msg(m.content)


def _err_brief(err: str, limit: int = 300) -> str:
    """把 run.error 压成一行可读的原因。

    异常字符串里可能混着整页 HTML（CDN/WAF 拦截页会被塞进 LlmAuthError），
    直接截断等于把「Just a moment…」的网页源码贴进对话。
    """
    # 拦截页还常被从 HTML 中间切断（标签都不闭合），认出这种就只留前面那句人话
    if re.search(r"<!doctype|<html|just a moment", err, re.I):
        err = err.split("<", 1)[0]
    one = re.sub(r"<[^>]+>", " ", err)
    one = re.sub(r"&[A-Za-z#0-9]{1,8};", " ", one)
    one = re.sub(r"\s+", " ", one).strip()
    if not one:
        return ""
    return one[:limit] + ("…" if len(one) > limit else "")


# 「继续 / 重试 / 接着做」这类没信息量的消息：本身不是需求，续跑时要越过它找真正的需求原文
_TRIVIAL_REQ = {"继续", "继续吧", "继续。", "继续啊", "接着", "接着做", "接着干", "重试", "重试吧",
                "再来", "再来一次", "继续执行", "继续干", "go", "go on", "ok", "好的", "嗯", "在吗", "?"}


def _is_trivial_req(m) -> bool:  # type: ignore[no-untyped-def]
    txt = (m.content or "").strip()
    return len(txt) <= 12 and txt.lower() in _TRIVIAL_REQ


def _plan_lines(plan: list | None) -> str:
    items = [p for p in (plan or []) if isinstance(p, dict)]
    if not items:
        return "（上次没列计划）"
    done = sum(1 for p in items if str(p.get("status")) == "completed")
    lines = [f"{'✓' if str(p.get('status')) == 'completed' else '•'} {p.get('content') or p.get('title') or ''}"
             for p in items[:12]]
    return f"{done}/{len(items)} 完成\n" + "\n".join(lines)


_RESULT_MIN_LEN = 40      # 短于这个长度的结果多半是「(空目录)」「已写入」这类废话
_RESULT_CHARS = 200
_THINKING_CHARS = 300
_THINKING_MIN_LEN = 40
_FINDINGS_BUDGET = 2200   # 全文超过它就只留最近的若干条（含已读文件清单，给足预算才不用重读）
_READ_TOOLS = ("read_file", "grep", "glob", "list_dir", "parse_document", "view_image")


def _one_line(s: str, limit: int) -> str:
    one = re.sub(r"\s+", " ", str(s or "")).strip()
    return one[:limit] + ("…" if len(one) > limit else "")


def _tail_head(s: str, limit: int, head: int = 80) -> str:
    """尾部优先截断：前 head 字作锚点 + 尾部主体（续跑摘要用）。

    模型/工具的关键结论多在尾部，旧逻辑取头会把根因/行号丢掉，续跑时二次丢失。
    """
    one = _one_line(s, limit * 4)
    if len(one) <= limit:
        return one
    tail = one[-(limit - head):]
    return one[:head] + "…[中略]…" + tail


async def _touched_files_brief(s, run_id) -> str:
    """上次 run 已经读过/改过的文件清单。

    现场（用户截图）：续跑后助手又「读取了 26 个文件」，把上一次已经查清的代码
    从头读了一遍——续跑说明里只有「最后 8 个动作」且没带路径，模型根本不知道
    哪些文件已经看过。这里把整条 run 的读/写轨迹按文件去重，明确告诉它「这些
    已经读过，别重读；只有要确认现状时才看具体几行」。
    """
    rows = (await s.execute(
        select(Event).where(Event.run_id == _u(str(run_id)),
                            Event.type == "tool_call")
        .order_by(Event.seq.asc()).limit(2000)
    )).scalars().all()
    read_paths: list[str] = []
    write_paths: list[str] = []
    for e in rows:
        p = e.payload_json or {}
        tool = str(p.get("tool") or "")
        args = p.get("args") or {}
        path = str(args.get("path") or "").strip()
        if not path:
            continue
        if tool in ("write_file", "edit_file", "delete_file"):
            if path not in write_paths:
                write_paths.append(path)
        elif tool in _READ_TOOLS:
            if path not in read_paths:
                read_paths.append(path)
    # 同一文件先读后改：只算「已改过」，不能再列进「已读到」，否则模型会以为还要去读旧的
    read_paths = [x for x in read_paths if x not in write_paths]
    parts: list[str] = []
    if read_paths:
        shown = read_paths[:60]
        parts.append(
            "上次已经读过的文件（共 %d 个，**不要再整篇重读**；只有在要改某处、"
            "需要确认行号时用 read_file 看那几行）：\n%s"
            % (len(read_paths), "\n".join(f"- {x}" for x in shown)
               + (f"\n- …还有 {len(read_paths) - len(shown)} 个" if len(read_paths) > len(shown) else "")))
    if write_paths:
        parts.append(
            "上次已经改过的文件（**已经写进工作区，不要重写**）：\n"
            + "\n".join(f"- {x}" for x in write_paths[:30]))
    return "\n\n".join(parts)


async def _findings_brief(s, run_id) -> str:
    """从中断 run 的轨迹里启发式抽出「已经量到的事实」，供续跑时直接沿用。

    现场（2026-10-28「首页为啥还有滚动条」）：根因在第 7 分钟就已经测出来了
    （scrollHeight 1029 > clientHeight 911），但服务重启把 run 打断，续跑说明里只有
    「最后几个工具名」——测量结果一条不带，下一个 run 从第 0 步重新量了 8 分钟。
    这里不叫 LLM、不查库外数据：工具输出本来就逐条落在 events 里，捞回来截断即可。
    """
    rows = (await s.execute(
        select(Event).where(Event.run_id == _u(str(run_id)), Event.type == "tool_result")
        .order_by(Event.seq.desc()).limit(40)
    )).scalars().all()
    think_rows = (await s.execute(
        select(Event).where(Event.run_id == _u(str(run_id)), Event.type == "agent_thinking")
        .order_by(Event.seq.desc()).limit(3)
    )).scalars().all()
    facts: list[str] = []
    think: list[str] = []
    seen: set[str] = set()
    for e in reversed(rows):  # 时间正序，越靠后越接近中断点
        p = e.payload_json or {}
        when = str(e.created_at or "")[11:19]
        body = _one_line(p.get("result"), _RESULT_CHARS)
        if len(body) < _RESULT_MIN_LEN or body in seen:
            continue
        # 工具结果尾部优先：错误码/行号/关键输出多在中后部，取尾部更接近「结论」
        body = _tail_head(p.get("result"), _RESULT_CHARS)
        seen.add(body)
        facts.append(f"- {when} {p.get('tool') or '?'}：{body}")
    for e in reversed(think_rows):
        p = e.payload_json or {}
        when = str(e.created_at or "")[11:19]
        body = _tail_head(p.get("reasoning"), _THINKING_CHARS)
        if len(body) >= _THINKING_MIN_LEN:
            think.append(f"- {when}：{body}")
    facts, think = facts[-40:], think[-3:]
    if sum(len(x) for x in facts) + sum(len(x) for x in think) > _FINDINGS_BUDGET:
        facts = facts[-15:]
    parts = []
    if facts:
        parts.append("上次已经量到的事实（工具输出原文，旧→新，最后一条最接近中断点；"
                     "这些是上一次真跑出来的结果，直接沿用，不要重新测一遍）：\n"
                     + "\n".join(facts))
    if think:
        parts.append("上次的阶段性推理（模型自己当时的结论，未必正确，需复核）：\n"
                     + "\n".join(think))
    return "\n\n".join(parts)


@router.post("/runs/{run_id}/continue")
async def continue_run(run_id: str, body: ContinueIn | None = None,
                       ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """从中断处继续上次任务（重试的正确语义）。

    不把「上一条用户消息」当新需求重发——上次如果是 ask_user 的回答，重发回答只会
    让组长当成新任务从头问一遍。这里做四件事：
      1. 找回这次 run 真正的原需求（跳过「【回答】…」消息）；
      2. 把原需求的图片原样重新附上（视觉模型能再次看图；额度上限同聊天接口）；
      3. 把「看到的图/已改文件/计划进度/最后动作」写成文字塞进新消息，非视觉模型也不丢上下文；
      4. 用这条「续跑说明」建新 run，历史消息照旧进入组长视野。
    """
    from game_agent.orchestration import snapshots

    async with SessionLocal() as s:
        run, project, workspace = await _run_workspace(s, run_id, ctx)
        if run.status not in ("failed", "cancelled", "orphaned"):
            raise HTTPException(
                status_code=400,
                detail=f"该任务状态是「{run.status}」，只有中断/失败/取消的任务才需要「继续」")
        # 注意：这里故意不再拦「同项目还有别的 run 在跑」。
        # 工作区是共享检出，并发继续确实可能互相踩文件——这条风险交给用户判断
        # （被 409 挡在门外时，用户看到的是「继续」按钮无效，比冲突更难理解）。
        msgs = (await s.execute(select(Message).where(
            Message.session_id == run.session_id).order_by(Message.created_at))).scalars().all()
        user_msgs = [m for m in msgs if m.author_type == "user"]
        # 原需求 = 本条 run 之前（含）最后一条「不是回答、不是续跑说明、也不是『继续/重试』空话」的用户消息。
        # 实测坑 1：用户点过「继续」或者手动敲「继续」时，那条消息本身没信息量，若直接用它当
        # 「上次请求原文」，续跑说明就变成「上次请求原文：继续」——真正的需求和图片全丢了。
        # 实测坑 2：上一次点「继续」合成的续跑说明里就带着「上次请求原文」，把它当原需求会套娃
        # （实测会话里出现过【继续…】→【继续…】→【继续…】三层，且中断原因的 HTML 被复制了三遍）。
        non_brief = [m for m in user_msgs if not _is_cont_msg(m)]
        upto = [m for m in non_brief
                if not _is_answer_msg(m.content)
                and (run.created_at is None or m.created_at <= run.created_at)]
        substantive = [m for m in upto if not _is_trivial_req(m)]
        orig = (substantive or upto or non_brief or user_msgs or [None])[-1]
        # 图片：优先取原需求自带的；原需求没图时再挑最近一条带图的（并在文字里说明来源）。
        # 只在非续跑说明里找——续跑说明的图本来就是从更早的真消息复制来的。
        img_src = orig if orig is not None and (orig.meta_json or {}).get("images") else None
        if img_src is None:
            for m in reversed(non_brief):
                if (m.meta_json or {}).get("images"):
                    img_src = m
                    break
        images = list((img_src.meta_json or {}).get("images") or [])[:4] if img_src else []
        # 原图路径跟着图走：像素超预算被省略时，组长还能按路径 view_image 重看
        img_paths = (list((img_src.meta_json or {}).get("image_paths") or [])[:4]
                     if img_src else [])
        # 附件同理：文件一直躺在工作区 .trace/uploads/，但要让组长知道「有哪些、在哪」
        file_src = orig if orig is not None and (orig.meta_json or {}).get("files") else None
        if file_src is None:
            for m in reversed(non_brief):
                if (m.meta_json or {}).get("files"):
                    file_src = m
                    break
        files = list((file_src.meta_json or {}).get("files") or [])[:6] if file_src else []

        # 上次最后几个动作（让组长知道已经做到哪一步，别从头再来）
        evs = (await s.execute(select(Event).where(
            Event.run_id == run.id, Event.type == "tool_call").order_by(Event.seq.desc()).limit(8)
        )).scalars().all()
        acts = []
        for e in reversed(evs):
            p = e.payload_json or {}
            a = p.get("args") or {}
            hint = str(a.get("path") or a.get("pattern") or a.get("command") or a.get("text") or "")
            acts.append("- " + str(p.get("tool") or "?") + (f" · {hint[:90]}" if hint else ""))
        changed: list[str] = []
        try:
            changed = [f["path"] for f in snapshots.list_changes(
                str(run.id), workspace, with_diff=False)["files"]]
        except Exception:  # noqa: BLE001 - 没快照不影响续跑
            changed = []
        if not changed:
            cr = run.change_report_json or {}
            changed = [f.get("file") for f in (cr.get("files") or []) if f.get("file")]

        # 上次已经产出的素材：这些文件已经躺在工作区里，续跑时最容易被「重新画一遍」，
        # 所以直接列给组长看（类型 + slug + 相对路径），明确说不要重画。
        made: list[str] = []
        try:
            arts = (await s.execute(
                select(Artifact).where(Artifact.run_id == run.id)
                .order_by(Artifact.created_at))).scalars().all()
            for a in arts:
                tag = "（占位图）" if a.is_placeholder else ""
                made.append(f"- {a.asset_kind}/{a.slug}: {a.rel_path}{tag}")
        except Exception:  # noqa: BLE001 - 查不到素材不影响续跑
            made = []

        err = _err_brief((run.error or "").strip())
        brief_parts = [_BRIEF_PREFIX]
        brief_parts.append(
            "硬性要求：这是**从中断处继续**，不是重新开始。下面「上次已确认的事实」「上次已经读过的文件」"
            "「上次已改过的文件」都是上一次真跑出来的结果，已经成立——不要重新勘察、不要整篇重读、"
            "不要重写已改文件；直接从「上次最后的动作」之后的下一步接着做。")
        brief_parts.append("上次请求原文：\n" + ((orig.content or "").strip() if orig else "（找不到原需求）"))
        if images:
            extra = ""
            if img_src is not None and orig is not None and str(img_src.id) != str(orig.id):
                snippet = (img_src.content or "").strip().replace("\n", " ")[:60]
                extra = f"（图来自更早的一条消息：「{snippet}」，图已重新附上）"
            brief_parts.append(
                f"上次请求附带 {len(images)} 张图片{extra}：已随本条消息重新附上（能看图就直接看；"
                "看不到图就以「上次已改文件」和下面的动作为准，不要猜图里的内容）"
                + (f"。原图在工作区：{'、'.join(img_paths)}"
                   "，需要再看用 view_image 单看或 view_images 合并看。" if img_paths else "）"))
        if files:
            brief_parts.append(
                f"上次请求附带 {len(files)} 个附件，仍存放在工作区 .trace/uploads/（清单见下方文件注入），"
                "需要内容时用 parse_document 解析或 read_file 读取，不需要用户重传。")
        if err:
            brief_parts.append("上次中断原因：" + err)
        brief_parts.append("上次计划进度：\n" + _plan_lines(run.plan_json))
        if changed:
            brief_parts.append("上次已改动的文件（已经写进工作区，不要重写）：\n"
                               + "\n".join(f"- {p}" for p in changed[:20]))
        if made:
            brief_parts.append(
                f"上次已产出的素材（共 {len(made)} 个，文件已经在工作区里，**不要重画**，"
                "缺哪张只补哪张）：\n" + "\n".join(made[:40]))
        if (run.final_summary or "").strip():
            brief_parts.append("上次组长的结论：" + run.final_summary.strip()[:800])
        findings = await _findings_brief(s, run.id)
        if findings:
            brief_parts.append(findings)
        touched = await _touched_files_brief(s, run.id)
        if touched:
            brief_parts.append(touched)
        if acts:
            brief_parts.append("上次最后的动作：\n" + "\n".join(acts))
        if body and (body.note or "").strip():
            brief_parts.append("用户补充：" + body.note.strip())
        brief_parts.append(
            "要求：**从中断处接着做**——已完成的不重做，已改的文件先读一遍确认现状再改；"
            "**不要重新提问**（除非确实缺关键信息，且一次只问 1-2 个）。做完照常汇报改了什么、怎么验证的。")
        brief = "\n\n".join(brief_parts)

        cont_meta = {"continue_from": str(run.id), "brief": brief}
        if images:
            cont_meta["images"] = images
        if img_paths:
            cont_meta["image_paths"] = img_paths
        if files:
            cont_meta["files"] = files
        # 「继续」不再往聊天里发任何消息：不写 Message，也不新增用户气泡。
        # 续跑所需的全部上下文（原需求原文、图片、附件、已改文件、计划进度…）放进
        # 新 run 的 options_json（continue_from / brief / images / image_paths / files），
        # 由 orchestrator 开跑时读出当成「本轮需求」交给模型。trigger_msg_id 留空 =
        # 本 run 没有触发消息，于是 _load_context 走 brief 分支、_history_block 正常
        # 带回会话历史——等于「带着原有上下文继续」，而对话里一条新消息都不多。
        # 续跑同样算会话活跃：侧栏应在点击「继续」后立即把它排到前面
        cont_sess = await s.get(Session, run.session_id)
        if cont_sess is not None:
            cont_sess.last_active = datetime.now(timezone.utc)
        # 续跑沿用上次的模型/推理等级与能力开关：否则中途点「继续」会悄悄换回角色默认模型
        cont_opts = {**(run.options_json or {}), **cont_meta}
        # 前端明确带了当前选择器里的模型 → 覆盖（用户刚切过、或想换模型再续跑）
        if body is not None and (body.model_profile_key is not None
                                 or body.reasoning_effort is not None):
            pkey = (body.model_profile_key or "").strip()
            if pkey:
                from game_agent.db.models import ModelProfile
                prof = await s.scalar(select(ModelProfile).where(
                    ModelProfile.team_id == _u(ctx.team_id),
                    ModelProfile.key == pkey, ModelProfile.kind == "llm"))
                if prof is None:
                    raise HTTPException(status_code=400, detail="所选模型档位不存在")
                cont_opts["model_profile_key"] = pkey
            else:
                cont_opts.pop("model_profile_key", None)
            effort = (body.reasoning_effort or "").strip()
            if effort:
                if effort not in ("none", "minimal", "low", "medium", "high"):
                    raise HTTPException(status_code=400, detail="推理等级不合法")
                cont_opts["reasoning_effort"] = effort
            else:
                cont_opts.pop("reasoning_effort", None)
        new_run = Run(team_id=run.team_id, project_id=run.project_id, session_id=run.session_id,
                      triggered_by=_u(ctx.user_id), trigger_msg_id=None,
                      run_type=run.run_type, options_json=cont_opts,
                      status="queued")
        s.add(new_run)
        await s.commit()
        await s.refresh(new_run)
        new_id = str(new_run.id)
        session_id = str(run.session_id)

    await state.services.queue.enqueue(new_id, priority=0)
    return {"run_id": new_id, "session_id": session_id, "status": "queued",
            "continued_from": str(run_id), "images": len(images), "changed_files": changed[:20]}


class CommentIn(BaseModel):
    body: str
    file: str
    line: int  # diff 中的新行号；0 表示对整个文件

@router.post("/runs/{run_id}/comments")
async def add_comment(run_id: str, body: CommentIn,
                      ctx: RequestContext = Depends(require(PERM_COMMENT))):
    async with SessionLocal() as s:
        run = await s.get(Run, _u(run_id))
        if run is None or str(run.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="run 不存在")
        anchor = f"diff:{body.file}#L{body.line}"
        c = Comment(team_id=_u(ctx.team_id), target_type="diff_line", target_id=_u(run_id),
                    author_id=_u(ctx.user_id), body=body.body.strip()[:2000],
                    mentions=[anchor])
        s.add(c)
        await s.commit()
        return {"id": str(c.id), "anchor": anchor}


@router.post("/comments/{comment_id}/resolve")
async def resolve_comment(comment_id: str, ctx: RequestContext = Depends(require(PERM_COMMENT))):
    async with SessionLocal() as s:
        c = await s.get(Comment, _u(comment_id))
        if c is None or str(c.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="评论不存在")
        c.resolved = True
        await s.commit()
    return {"ok": True}


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
