"""项目长期记忆 v2：结构化条目 + 模型融合 + 渐进式披露。

为什么升级（v1 的问题）：
- v1 只有一条 auto 记忆，每次 run 用模型把「旧记忆 + 本轮结果」压成一段 900 字摘要，
  跨对话的信息被反复重写、互相挤掉（记了角色定位就丢了素材规格）；
- 注入时把最多 20 条正文整段塞进上下文，长会话里记忆块本身就成了负担。

v2：
- 记忆是**一组结构化条目**（title / body / tags / 来源会话 / 更新时间），
  每次 run 收尾由模型在「已有条目 + 本次结果 + 其他对话的结论」里做融合：
  合并重复、以新为准、保留关键数值，上限 MAX_ENTRIES 条；
- 对话上下文只注入**标题索引**（渐进式披露），需要细节时主智能体调
  ``memory_read`` 工具按关键词或 id 取正文；
- 手动补充的约定（kind=manual）不被模型改写，注入时给全文。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from game_agent.config import LLM_MODEL, ROLE_MODELS
from game_agent.db.models import Agent, LlmProvider, ModelProfile, Project, ProjectMemory, Run
from game_agent.db.session import SessionLocal
from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm
from game_agent.logging_setup import get_logger

log = get_logger("orchestration.memory")

MAX_ENTRIES = 12          # 融合后保留的自动记忆条数上限
MAX_TITLE = 40
MAX_BODY = 900            # 单条正文上限（索引里另有一句概括）
MAX_TAGS = 6
OTHER_TALK_RUNS = 8       # 融合时参考的其他对话结论条数
LLM_TIMEOUT_S = 90


def _u(v: Any) -> Any:
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _derive_title(body: str) -> str:
    """老数据没有标题：取正文第一行的前 24 字。"""
    first = ""
    for line in (body or "").splitlines():
        line = line.strip().lstrip("-·* ").strip()
        if line:
            first = line
            break
    return (first or "项目要点")[:24]


def _split_tags(raw: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[,，、;；]+", raw or "") if t.strip()][:MAX_TAGS]


def _entry(m: ProjectMemory) -> dict[str, Any]:
    body = (m.content or "").strip()
    return {
        "id": str(m.id),
        "title": (m.title or "").strip() or _derive_title(body),
        "body": body,
        "tags": _split_tags(m.tags),
        "kind": m.kind,
        "source_run_id": str(m.source_run_id) if m.source_run_id else None,
        "session_id": m.session_id,
        "updated_at": str(m.updated_at or m.created_at),
        "created_at": str(m.created_at),
    }


def _gist(body: str, n: int = 64) -> str:
    text = " ".join(x.strip() for x in (body or "").splitlines() if x.strip())
    text = re.sub(r"^[-·*]\s*", "", text)
    return text[:n] + ("…" if len(text) > n else "")


async def load_entries(project_id: str, limit: int = 60) -> list[dict[str, Any]]:
    """按更新时间倒序取记忆条目；老行没有 updated_at 时退回 created_at。"""
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(ProjectMemory)
            .where(ProjectMemory.project_id == _u(project_id))
            .order_by(ProjectMemory.created_at.desc())
            .limit(limit)
        )).scalars().all()
    items = [_entry(m) for m in rows]
    items.sort(key=lambda e: e["updated_at"], reverse=True)
    return items


async def read_entries(project_id: str, query: str = "", ids: list[str] | None = None,
                       limit: int = 3) -> list[dict[str, Any]]:
    """memory_read 的实现：先按 id 前缀精确取，再按关键词打分取；都没有就给最新的几条。"""
    entries = await load_entries(project_id, limit=80)
    picks: list[dict[str, Any]] = []
    if ids:
        prefixes = [str(i).strip().lower() for i in ids if str(i).strip()]
        picks = [e for e in entries if any(e["id"].lower().startswith(p) for p in prefixes)]
    if not picks and query.strip():
        tokens = [t for t in re.split(r"[\s,，、;；/|]+", query.lower()) if t]
        scored: list[tuple[int, dict[str, Any]]] = []
        for e in entries:
            hay = f'{e["title"]} {" ".join(e["tags"])} {e["body"]}'.lower()
            score = sum(hay.count(t) for t in tokens)
            if score:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        picks = [e for _, e in scored]
    if not picks:
        picks = entries
    return picks[:limit]


def index_block(entries: list[dict[str, Any]], manual_limit: int = 8) -> str:
    """注入编排上下文的记忆索引：自动条目只给标题+概括，手动约定给全文。"""
    auto = [e for e in entries if e["kind"] != "manual"][:MAX_ENTRIES]
    manual = [e for e in entries if e["kind"] == "manual"][:manual_limit]
    if not auto and not manual:
        return ""
    lines: list[str] = []
    if auto:
        lines.append("## 项目记忆索引（历次对话沉淀，正文没注入）")
        for e in auto:
            tags = f" · {'/'.join(e['tags'][:3])}" if e["tags"] else ""
            lines.append(f'- [{e["id"][:8]}] {e["title"]}{tags} — {_gist(e["body"])}')
        lines.append('命中当前任务的条目，用 memory_read 取全文（query=关键词，或 ids=["xxxxxxxx"]）；'
                     "与本轮无关的不要读，省上下文。")
    if manual:
        if lines:
            lines.append("")
        lines.append("## 手动约定（必须遵守，全文）")
        for e in manual:
            lines.append(f'- {e["body"][:400]}')
    return "\n".join(lines) + "\n\n"


async def _other_talk_summaries(project_id: str, skip_run_id: str | None) -> list[str]:
    """同一项目下**其他对话**最近的结论（run.final_summary），用于跨会话融合。"""
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Run)
            .where(Run.project_id == _u(project_id), Run.final_summary.is_not(None))
            .order_by(Run.finished_at.desc())
            .limit(OTHER_TALK_RUNS + 2)
        )).scalars().all()
    out: list[str] = []
    seen_sessions: set[str] = set()
    for r in rows:
        if skip_run_id and str(r.id) == str(skip_run_id):
            continue
        text = (r.final_summary or "").strip()
        if not text:
            continue
        sid = str(r.session_id)
        if sid in seen_sessions:
            continue
        seen_sessions.add(sid)
        stamp = (r.finished_at.strftime("%m-%d") if r.finished_at else "")
        out.append(f"[{stamp}] {text[:500]}")
        if len(out) >= OTHER_TALK_RUNS:
            break
    return out


def _prompt(manual: list[dict], auto: list[dict], new_text: str,
            other: list[str], single: bool = False) -> str:
    existing = "\n".join(
        f'[{e["id"][:8]}] {e["title"]}\n{e["body"][:600]}' for e in auto
    ) or "（空）"
    manual_block = "\n".join(f"- {e['body'][:400]}" for e in manual) or "（无）"
    other_block = "\n".join(other) or "（无）"
    if single:
        # 用户手动点「AI 融合」：不管原来有多少条，全部重写成一份「项目现状」。
        merge_rule = ("4. 本次是用户手动点击「AI 融合」：必须把全部自动记忆合并成 **1 条**，"
                      "标题固定为「项目现状」，正文用要点分行写全所有仍然成立的事实；\n")
    else:
        merge_rule = f"4. 最多 {MAX_ENTRIES} 条，正文每条不超过 {MAX_BODY} 字；\n"
    return (
        "你是项目记忆整理器。请把「已有记忆」「本次运行结果」「其他对话的结论」融合成一份"
        "去重后的项目记忆库，供后续开发该项目的智能体使用。\n"
        "规则：\n"
        "1. 一条记忆只讲一个主题；标题不超过 20 字；正文只写「当前仍然成立的事实/约定/结论」，"
        "不写过程流水，不写「本次/之前改了什么」；\n"
        "2. 重复或相近的条目必须合并；互相冲突的以最新（本次运行结果 > 其他对话 > 已有记忆）为准；\n"
        "3. 关键路径、文件名、规格数值（尺寸/张数/端口/命令/命名口径）必须原样保留；\n"
        f"{merge_rule}"
        "5. 已有记忆里带 [id] 且仍保留的，输出时带上同一个 id（原地更新）；合并时沿用信息更全的那个 id；"
        "新条目不要填 id；\n"
        "6. 只输出 JSON，不要解释：\n"
        '{"memories":[{"id":"a1b2c3d4","title":"...","body":"...","tags":["...","..."]}]}\n\n'
        f"【手动约定（只读，不可改写，也不要重复记录）】\n{manual_block}\n\n"
        f"【已有记忆】\n{existing}\n\n"
        f"【本次运行结果】\n{new_text[:2500]}\n\n"
        f"【其他对话的结论（可能相关，择优合并）】\n{other_block}"
    )


def _parse_json(text: str, max_body: int = MAX_BODY) -> list[dict[str, Any]] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    items = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip().lstrip("【[").strip()
        body = str(it.get("body") or "").strip()
        if not body:
            continue
        tags = [str(t).strip() for t in (it.get("tags") or []) if str(t).strip()][:MAX_TAGS]
        out.append({
            "id": str(it.get("id") or "").strip()[:8],
            "title": (title or _derive_title(body))[:MAX_TITLE],
            "body": body[:max_body],
            "tags": tags,
        })
        if len(out) >= MAX_ENTRIES:
            break
    return out


async def _main_profile(project_id: str) -> dict[str, Any]:
    """解析项目所属团队 main 角色绑定的 ModelProfile → Ctx.model_profile。

    以前这里传空 ctx，build_llm 回退到环境变量默认端点（该订阅已失效时永远 403），
    记忆融合就永远走 ``_fallback``，用户看到「没有把所有记忆重新融合成一份」。
    取值链路与 orchestrator._load_profile 保持一致：Agent.model_profile_key → ModelProfile
    → 供应商 base_url/api_key（档位 inline 优先）。
    """
    async with SessionLocal() as s:
        proj = await s.get(Project, _u(project_id))
        if proj is None:
            return {}
        agent = await s.scalar(select(Agent).where(
            Agent.team_id == proj.team_id, Agent.key == "main"))
        profile_key = agent.model_profile_key if agent else None
        if not profile_key:
            return {}
        p = await s.scalar(select(ModelProfile).where(
            ModelProfile.team_id == proj.team_id, ModelProfile.key == profile_key))
        if p is None:
            log.warning("记忆融合：main 角色绑定的档位 key=%r 不存在，将回退环境默认", profile_key)
            return {}
        params = p.params_json or {}
        base_url, api_key, user_agent = p.base_url, None, None
        protocol = p.provider or "openai"
        if p.provider_id:
            pv = await s.get(LlmProvider, p.provider_id)
            if pv is not None and pv.enabled:
                base_url = base_url or pv.base_url
                api_key = pv.api_key
                user_agent = pv.user_agent
                protocol = pv.protocol or protocol
        return {"model": p.model, "base_url": base_url,
                "api_key": params.get("api_key") or p.api_key_ref or api_key,
                "protocol": protocol, "user_agent": user_agent, "params": params}


async def _call_llm(prompt: str, profile: dict[str, Any] | None = None) -> str:
    ctx = Ctx(role="main", run_type="memory", requirement=prompt,
              model=ROLE_MODELS.get("main") or LLM_MODEL,
              model_profile=profile or {})
    llm = build_llm("main", ctx)
    import asyncio
    resp = await asyncio.wait_for(
        llm.chat([{"role": "user", "content": prompt}], [], ctx),
        timeout=LLM_TIMEOUT_S,
    )
    return (getattr(resp, "text", "") or "").strip()


async def _write_consolidated(project_id: str, items: list[dict[str, Any]],
                              run_id: str | None, session_id: str | None) -> None:
    """按 id 前缀原地更新、无 id 新增、模型没保留的旧 auto 条目删除。来源信息只增不改。"""
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(ProjectMemory).where(
                ProjectMemory.project_id == _u(project_id),
                ProjectMemory.kind == "auto",
            )
        )).scalars().all()
        by_prefix = {str(r.id)[:8]: r for r in rows}
        keep: set[str] = set()
        now = _now()
        for it in items:
            row = by_prefix.get((it.get("id") or "").lower())
            tags = ",".join(it["tags"])
            if row is not None:
                row.title = it["title"]
                row.content = it["body"]
                row.tags = tags
                row.updated_at = now
                keep.add(str(row.id))
            else:
                row = ProjectMemory(
                    project_id=_u(project_id), kind="auto", title=it["title"],
                    content=it["body"], tags=tags,
                    source_run_id=_u(run_id) if run_id else None,
                    session_id=(session_id or "")[:32] or None, updated_at=now,
                )
                s.add(row)
                await s.flush()
                keep.add(str(row.id))
        for r in rows:
            if str(r.id) not in keep:
                await s.delete(r)
        await s.commit()


async def _fallback(project_id: str, new_text: str, run_id: str | None,
                    session_id: str | None) -> int:
    """模型不可用：保留旧条目，把本次结果并入一条「项目现状」，绝不静默丢记忆。"""
    text = (new_text or "").strip()
    if not text:
        return 0
    async with SessionLocal() as s:
        row = (await s.execute(
            select(ProjectMemory).where(
                ProjectMemory.project_id == _u(project_id),
                ProjectMemory.kind == "auto",
            ).order_by(ProjectMemory.created_at.desc()).limit(1)
        )).scalars().first()
        now = _now()
        if row is None:
            row = ProjectMemory(
                project_id=_u(project_id), kind="auto", title="项目现状",
                content=text[:MAX_BODY], updated_at=now,
                source_run_id=_u(run_id) if run_id else None,
                session_id=(session_id or "")[:32] or None,
            )
            s.add(row)
        else:
            merged = f"{row.content}\n- {text}" if row.content else text
            row.content = merged[-MAX_BODY * 2:]
            row.title = row.title or "项目现状"
            row.updated_at = now
        await s.commit()
        n = (await s.execute(
            select(ProjectMemory.id).where(
                ProjectMemory.project_id == _u(project_id),
                ProjectMemory.kind == "auto",
            )
        )).all()
        return len(n)


def _merge_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """本地兜底：把多条记忆并成一份「项目现状」（去重标题前缀，正文按要点分行）。"""
    seen: set[str] = set()
    lines: list[str] = []
    tags: list[str] = []
    for it in items:
        body = str(it.get("body") or "").strip()
        title = str(it.get("title") or "").strip()
        if not body:
            continue
        key = body[:80]
        if key in seen:
            continue
        seen.add(key)
        # 单条正文本身可能已经带要点；不再重复加标题前缀时保持可读
        lines.append(f"- {title}：{body}" if title and title not in body[:20] else f"- {body}")
        for t in it.get("tags") or []:
            t = str(t).strip()
            if t and t not in tags:
                tags.append(t)
    return {"id": "", "title": "项目现状", "body": "\n".join(lines)[:MAX_BODY * 4],
            "tags": tags[:MAX_TAGS]}


async def consolidate(project_id: str, *, run_id: str | None = None,
                      session_id: str | None = None, new_text: str = "",
                      force: bool = False) -> dict[str, Any]:
    """融合一次：模型判断保留/合并哪些记忆。force=True 供界面「立即融合」用。

    手动「AI 融合」（force=True 且没有新的 run 结果）按用户口径把全部自动记忆
    **重写成一份**「项目现状」；模型不可用时也走本地合并，而不是原样不动。
    """
    entries = await load_entries(project_id, limit=60)
    auto = [e for e in entries if e["kind"] != "manual"]
    manual = [e for e in entries if e["kind"] == "manual"]
    single = bool(force and not new_text.strip())  # 手动「AI 融合」= 全部合成一份
    if not (new_text or auto) and not force:
        return {"before": len(auto), "after": len(auto), "used_ai": False}
    if not session_id and run_id:
        async with SessionLocal() as s:
            r = await s.get(Run, _u(run_id))
            session_id = str(r.session_id) if r and r.session_id else None

    other = await _other_talk_summaries(project_id, run_id)
    items: list[dict[str, Any]] | None = None
    try:
        profile = await _main_profile(project_id)
        text = await _call_llm(_prompt(manual, auto, new_text, other, single=single), profile)
        items = _parse_json(text, max_body=MAX_BODY * 4 if single else MAX_BODY)
        if single and items and len(items) > 1:
            # 模型没听话拆成多条：本地再兜一层，全部并成一份
            items = [_merge_items(items)]
    except Exception as e:  # noqa: BLE001 - 融合失败退化为追加
        log.warning("记忆融合失败（%s），退化为追加", str(e)[:200],
                    extra={"project_id": str(project_id)})
    if items is None:
        if single and auto:
            # 手动融合且模型不可用：本地把现有自动记忆并成一份，绝不「点了没反应」
            merged = _merge_items([{"title": e["title"], "body": e["body"], "tags": e["tags"]}
                                   for e in auto])
            await _write_consolidated(project_id, [merged], run_id, session_id)
            return {"before": len(auto), "after": 1, "used_ai": False}
        if not new_text:
            # 只是点了「融合」但模型不可用：没有新内容可写，保持原样，别报 0 条
            return {"before": len(auto), "after": len(auto), "used_ai": False}
        after = await _fallback(project_id, new_text, run_id, session_id)
        return {"before": len(auto), "after": after, "used_ai": False}
    if not items:
        # 模型认为没有可保留的记忆：不动旧数据，避免误清空
        log.warning("记忆融合返回空结果，保留旧记忆", extra={"project_id": str(project_id)})
        return {"before": len(auto), "after": len(auto), "used_ai": True}
    await _write_consolidated(project_id, items, run_id, session_id)
    log.info("project memory consolidated",
             extra={"project_id": str(project_id), "before": len(auto), "after": len(items)})
    return {"before": len(auto), "after": len(items), "used_ai": True}
