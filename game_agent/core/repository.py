"""DB 版 Repository 实现（lite/server 共用，靠方言自动切换）。

满足 core.abstractions.Repository 接口；业务代码只依赖接口。
内部用 session_factory，每次操作开闭独立 session（简单且对两种模式都正确）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from game_agent.core.abstractions import EventEnvelope, Repository
from game_agent.db.models import (
    Approval,
    Artifact,
    AuditLog,
    Comment,
    Event,
    LlmCall,
    LlmTranscript,
    Message,
    Run,
    Session,
    Task,
    UsageLedger,
    WorkOrder,
)


def _u(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


class DbRepository(Repository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ---------- runs ----------
    async def create_run(self, **kw) -> Run:
        for k in ("id", "team_id", "project_id", "session_id", "triggered_by"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            obj = Run(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            await s.commit()
            return obj

    async def get_run(self, run_id: str) -> Optional[Run]:
        async with self._sf() as s:
            return await s.get(Run, _u(run_id))

    async def update_run(self, run_id: str, **fields) -> None:
        async with self._sf() as s:
            await s.execute(update(Run).where(Run.id == _u(run_id)).values(**fields))
            await s.commit()

    # ---------- tasks ----------
    async def create_task(self, **kw) -> Task:
        for k in ("id", "run_id", "parent_task_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            obj = Task(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            await s.commit()
            return obj

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self._sf() as s:
            return await s.get(Task, _u(task_id))

    async def update_task(self, task_id: str, **fields) -> None:
        async with self._sf() as s:
            await s.execute(update(Task).where(Task.id == _u(task_id)).values(**fields))
            await s.commit()

    async def list_tasks(self, run_id: str) -> list[Task]:
        async with self._sf() as s:
            res = await s.scalars(
                select(Task).where(Task.run_id == _u(run_id)).order_by(Task.created_at)
            )
            return list(res)

    # ---------- work orders ----------
    async def create_work_order(self, **kw) -> WorkOrder:
        if "run_id" in kw and isinstance(kw["run_id"], str):
            kw["run_id"] = _u(kw["run_id"])
        async with self._sf() as s:
            obj = WorkOrder(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            await s.commit()
            return obj

    async def get_work_order(self, run_id: str) -> Optional[WorkOrder]:
        async with self._sf() as s:
            return await s.scalar(select(WorkOrder).where(WorkOrder.run_id == _u(run_id)))

    # ---------- artifacts ----------
    async def create_artifact(self, **kw) -> Artifact:
        for k in ("team_id", "project_id", "run_id", "task_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            obj = Artifact(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            await s.commit()
            return obj

    async def list_artifacts(self, run_id: str) -> list[Artifact]:
        async with self._sf() as s:
            res = await s.scalars(
                select(Artifact).where(Artifact.run_id == _u(run_id)).order_by(Artifact.created_at)
            )
            return list(res)

    async def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        async with self._sf() as s:
            return await s.get(Artifact, _u(artifact_id))

    async def update_artifact(self, artifact_id: str, **fields) -> None:
        async with self._sf() as s:
            await s.execute(update(Artifact).where(Artifact.id == _u(artifact_id)).values(**fields))
            await s.commit()

    # ---------- messages ----------
    async def create_message(self, **kw) -> Message:
        for k in ("session_id", "run_id", "author_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            obj = Message(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            # 任何消息落库都算会话活跃：侧栏排序与「N 分钟前」才能跟着后台处理刷新，
            # 而不是永远显示创建时间（用户反馈「会话没有自动刷新」的一部分）。
            sess = await s.get(Session, obj.session_id)
            if sess is not None:
                sess.last_active = datetime.now(timezone.utc)
            await s.commit()
            return obj

    async def list_messages(self, session_id: str) -> list[Message]:
        async with self._sf() as s:
            res = await s.scalars(
                select(Message).where(Message.session_id == _u(session_id)).order_by(Message.created_at)
            )
            return list(res)

    # ---------- events ----------
    async def append_event(self, env: EventEnvelope) -> int:
        rid = _u(env.run_id) if env.run_id else None
        pid = _u(env.project_id) if env.project_id else None
        tid = _u(env.team_id) if env.team_id else None
        task_id = _u(env.task_id) if env.task_id else None
        # seq 按「项目」单调递增；WS 全项目流与 REST 按 run 补拉共用同一序，避免丢/重。
        # 多 worker 并发下 max+1 可能撞号（唯一索引兜底），撞号后重读 max 重试。
        for attempt in range(6):
            try:
                async with self._sf() as s:
                    cur = 0
                    if pid is not None:
                        cur = await s.scalar(
                            select(func.coalesce(func.max(Event.seq), 0)).where(Event.project_id == pid)
                        ) or 0
                    seq = cur + 1
                    s.add(Event(
                        team_id=tid, project_id=pid, run_id=rid, task_id=task_id,
                        seq=seq, type=env.type, payload_json=env.payload,
                    ))
                    await s.commit()
                    return seq
            except IntegrityError:
                await asyncio.sleep(0.02 * (attempt + 1))
        raise RuntimeError("append_event: seq 冲突重试耗尽")

    async def get_events_since(self, run_id: str, seq: int) -> list[Event]:
        async with self._sf() as s:
            res = await s.scalars(
                select(Event)
                .where(Event.run_id == _u(run_id), Event.seq > seq)
                .order_by(Event.seq)
            )
            return list(res)

    # ---------- projects / approvals / comments ----------
    async def get_project(self, project_id: str):  # noqa: ANN201
        from game_agent.db.models import Project
        async with self._sf() as s:
            return await s.get(Project, _u(project_id))

    async def create_approval(self, **kw):  # noqa: ANN201
        for k in ("team_id", "project_id", "run_id", "requested_by", "decided_by"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            obj = Approval(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            await s.commit()
            return obj

    async def get_approval(self, approval_id: str):  # noqa: ANN201
        async with self._sf() as s:
            return await s.get(Approval, _u(approval_id))

    async def update_approval(self, approval_id: str, **fields) -> None:
        async with self._sf() as s:
            await s.execute(update(Approval).where(Approval.id == _u(approval_id)).values(**fields))
            await s.commit()

    async def create_comment(self, **kw):  # noqa: ANN201
        for k in ("team_id", "author_id", "target_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            obj = Comment(**kw)
            s.add(obj)
            await s.flush()
            await s.refresh(obj)
            await s.commit()
            return obj

    # ---------- audit / usage ----------
    async def audit(self, **kw) -> None:
        for k in ("team_id", "actor_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            s.add(AuditLog(**kw))
            await s.commit()

    async def record_usage(self, **kw) -> None:
        for k in ("team_id", "run_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            s.add(UsageLedger(**kw))
            await s.commit()

    async def record_llm_call(self, **kw) -> None:
        import uuid as _uuid
        for k in ("team_id", "run_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        kw.setdefault("call_id", _uuid.uuid4().hex)
        async with self._sf() as s:
            s.add(LlmCall(**kw))
            await s.commit()

    # ---------- 上下文现场（发给模型什么 / 模型返回什么）----------
    async def record_llm_transcript(self, **kw) -> None:
        for k in ("team_id", "run_id"):
            if k in kw and isinstance(kw[k], str):
                kw[k] = _u(kw[k])
        async with self._sf() as s:
            s.add(LlmTranscript(**kw))
            await s.commit()

    async def list_transcripts(self, run_id: str, *, with_blobs: bool = False,
                               limit: int = 400) -> list[dict]:
        """某次运行的逐轮往返现场。默认不取 LONGTEXT 两列——列表页只要骨架，
        点开某一轮再单独取全文（把 200KB×N 拖进一次响应会把页面卡死）。"""
        cols: list[Any] = [
            LlmTranscript.id, LlmTranscript.call_id, LlmTranscript.agent_key,
            LlmTranscript.model, LlmTranscript.round_no, LlmTranscript.purpose,
            LlmTranscript.compressed, LlmTranscript.dropped_msgs,
            LlmTranscript.req_bytes, LlmTranscript.resp_bytes,
            LlmTranscript.stats_json, LlmTranscript.truncated, LlmTranscript.created_at,
        ]
        if with_blobs:
            cols += [LlmTranscript.request_json, LlmTranscript.response_json]
        async with self._sf() as s:
            rows = (await s.execute(
                select(*cols)
                .where(LlmTranscript.run_id == _u(run_id))
                .order_by(LlmTranscript.created_at, LlmTranscript.id)
                .limit(limit))).all()
        keys = ["id", "call_id", "agent_key", "model", "round_no", "purpose",
                "compressed", "dropped_msgs", "req_bytes", "resp_bytes",
                "stats_json", "truncated", "created_at"]
        if with_blobs:
            keys += ["request_json", "response_json"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["id"] = str(d["id"])
            d["created_at"] = str(d["created_at"])
            out.append(d)
        return out

    async def get_transcript(self, transcript_id: str) -> Optional[dict]:
        async with self._sf() as s:
            row = (await s.execute(
                select(LlmTranscript).where(LlmTranscript.id == _u(transcript_id)))).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": str(row.id), "call_id": row.call_id, "agent_key": row.agent_key,
                "model": row.model, "round_no": row.round_no, "purpose": row.purpose,
                "compressed": row.compressed, "dropped_msgs": row.dropped_msgs,
                "req_bytes": row.req_bytes, "resp_bytes": row.resp_bytes,
                "stats_json": row.stats_json or {}, "truncated": row.truncated,
                "created_at": str(row.created_at),
                "request_json": row.request_json, "response_json": row.response_json,
            }

    async def transcript_stats(self, run_ids: list[str]) -> list[dict]:
        """若干运行的现场聚合（只取轻列，供会话级上下文统计）。"""
        if not run_ids:
            return []
        async with self._sf() as s:
            return [dict(r._mapping) for r in (await s.execute(
                select(LlmTranscript.run_id, LlmTranscript.agent_key, LlmTranscript.purpose,
                       LlmTranscript.compressed, LlmTranscript.dropped_msgs,
                       LlmTranscript.stats_json, LlmTranscript.created_at)
                .where(LlmTranscript.run_id.in_([_u(x) for x in run_ids]))
                .order_by(LlmTranscript.created_at))).all()]
