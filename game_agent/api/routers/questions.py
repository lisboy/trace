"""提问机制 API：组长「拿不准就问」的问题列表 / 作答。

作答语义：把用户的答案作为一条 **user 消息** 写进同一会话，并重新入队一个 run——
组长会被带着这条答案重新唤起（会话历史会注入），相当于「从刚才那步继续」。
这样不会有一个 worker 被"等人回答"长期占住（worker 池只有 3 个）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import RequestContext, require
from game_agent.api.state import state
from game_agent.auth.rbac import PERM_RUN_CREATE, PERM_VIEW
from game_agent.db.models import Message, Run, RunQuestion, Session

router = APIRouter(prefix="/api", tags=["questions"])


def _same_team(a, b) -> bool:
    """UUID 归一比较：库里 Uuid 列与 ctx.team_id 的横线/大小写可能不同，直接比字符串会误判 404。"""
    return _u(str(a)).hex == _u(str(b)).hex if isinstance(_u(str(a)), uuid.UUID) else str(a) == str(b)


def _u(v):  # type: ignore[no-untyped-def]
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _q_dict(q: RunQuestion) -> dict:
    return {
        "id": str(q.id),
        "run_id": str(q.run_id) if q.run_id else None,
        "session_id": str(q.session_id),
        "agent_key": q.agent_key,
        "question": q.question,
        "options": q.options_json or [],
        "allow_text": bool(q.allow_text),
        "status": q.status,
        "answer_index": q.answer_index,
        "answer_text": q.answer_text,
        "created_at": str(q.created_at),
        "answered_at": str(q.answered_at) if q.answered_at else None,
    }


@router.get("/sessions/{session_id}/questions")
async def list_questions(session_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """会话问题列表：前端用它渲染「待回答」面板与「已回答」记录（刷新/重连后仍在）。"""
    async with state.session_factory() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or not _same_team(sess.team_id, ctx.team_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        rows = (await s.execute(
            select(RunQuestion).where(RunQuestion.session_id == sess.id)
            .order_by(RunQuestion.created_at.asc()))).scalars().all()
    items = [_q_dict(q) for q in rows]
    pending = [q for q in items if q["status"] == "pending"]
    answered = [q for q in items if q["status"] != "pending"]
    return {"session_id": session_id, "items": items, "pending": pending,
            "answered": answered}


class AnswerIn(BaseModel):
    option_index: int | None = None  # 0-based；-1 表示"跳过/按你说的来"
    text: str | None = None


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: str, body: AnswerIn,
                          ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    async with state.session_factory() as s:
        q = await s.get(RunQuestion, _u(question_id))
        if q is None or not _same_team(q.team_id, ctx.team_id):
            raise HTTPException(status_code=404, detail="问题不存在")
        if q.status != "pending":
            raise HTTPException(status_code=409, detail="该问题已回答")
        sess = await s.get(Session, q.session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        # 续跑类型跟着提问那条 run 走：analyze 会被编排器剔掉写码/起服务/派发的工具，
        # 组长带着答案却出不了图、改不了代码（见 orchestrator.run_agent 的 WRITE_TOOLS 过滤）
        origin = await s.get(Run, q.run_id) if q.run_id else None
        cont_run_type = (origin.run_type if origin else "") or "modify"

        opts = q.options_json or []
        idx = body.option_index
        text = (body.text or "").strip()
        if idx is not None and not (0 <= idx < len(opts)) and idx != -1:
            raise HTTPException(status_code=400, detail="选项越界")
        if idx is None and not text:
            raise HTTPException(status_code=400, detail="请选择选项或输入答案")
        if idx is not None and 0 <= idx < len(opts) and not text:
            text = str(opts[idx].get("label") or "")
        if idx == -1 and not text:
            text = "跳过（按你的判断处理）"

        q.status = "answered" if idx != -1 else "skipped"
        q.answer_index = idx
        q.answer_text = text
        q.answered_at = _now()
        sess.last_active = _now()  # 回答也算会话活跃：侧栏排序/相对时间即时刷新

        # 答案作为一条 user 消息写进会话（组长重新被唤起时能在历史里看到）
        msg = Message(session_id=sess.id, run_id=None, author_type="user",
                      author_id=_u(ctx.user_id),
                      content=f"【回答】{text}" if q.question else text,
                      meta_json={"kind": "answer", "question_id": str(q.id),
                                 "option_index": idx})
        s.add(msg)
        await s.flush()
        await s.refresh(msg)

        run = Run(team_id=_u(ctx.team_id), project_id=sess.project_id,
                  session_id=sess.id, triggered_by=_u(ctx.user_id),
                  trigger_msg_id=msg.id, run_type=cont_run_type,
                  # 沿用提问那条 run 的模型/推理等级与能力开关（否则回答后模型悄悄换回默认）
                  options_json=dict(origin.options_json or {}) if origin else None,
                  status="queued")
        s.add(run)
        await s.commit()
        await s.refresh(run)
        run_id = str(run.id)
        session_id = str(sess.id)

    await state.services.queue.enqueue(run_id, priority=0)
    return {"ok": True, "run_id": run_id, "session_id": session_id,
            "question": _q_dict(q)}


@router.get("/runs/{run_id}/questions")
async def list_run_questions(run_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with state.session_factory() as s:
        rows = (await s.execute(
            select(RunQuestion).where(RunQuestion.run_id == _u(run_id))
            .order_by(RunQuestion.created_at.asc()))).scalars().all()
        if rows and not _same_team(rows[0].team_id, ctx.team_id):
            raise HTTPException(status_code=404, detail="问题不存在")
    return {"run_id": run_id, "items": [_q_dict(q) for q in rows]}
