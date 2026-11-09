"""lite Queue：基于 runs 表的 queued/claimed 状态，多 worker 并行领取。

- 并行规则：优先领取「所属项目无活跃 run」的排队 run（跨项目/多会话并行）；
  SAME_PROJECT_PARALLEL 开启时（默认），同项目也允许并行领取；关掉则同项目串行。
- 领取用「UPDATE ... WHERE status='queued'」原子防双领，多个 worker 竞争同一条时
  只有拿到 rowcount=1 的那个赢，其余顺延下一条——单机 SQLite 下也安全。
- MySQL/MariaDB（server 模式）额外用 ``SELECT ... FOR UPDATE SKIP LOCKED`` 先在事务里
  锁住候选行：并发 worker 直接跳过已被别人锁住的行，不必每次都撞一遍 UPDATE 再回退。
  这就是 server 模式多进程编排的正确姿势（业务代码不变，仅领取实现按方言切换）。
- SQLite 并发写会偶发 ``database is locked``（默认 busy_timeout 之外的情况），
  claim 整体带几次退避重试，避免一次抖动就让某个 worker 空转、run 没人领。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from game_agent.config import SAME_PROJECT_PARALLEL
from game_agent.core.abstractions import Queue
from game_agent.db.models import Run


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


class LiteQueue(Queue):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def enqueue(self, run_id: str, priority: int = 0) -> None:
        async with self._sf() as s:
            await s.execute(
                update(Run).where(Run.id == _u(run_id)).values(status="queued", priority=priority)
            )
            await s.commit()

    async def claim(self) -> Optional[str]:
        # SQLite 偶发 database is locked：整次领取退避重试，别让 worker 空转。
        for attempt in range(4):
            try:
                return await self._claim_once()
            except OperationalError as e:  # noqa: PERF203 - 只在 SQLite 锁冲突时命中
                if "locked" not in str(e).lower() or attempt == 3:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        return None

    async def _claim_once(self) -> Optional[str]:
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else ""
            # MySQL：事务内 SELECT ... FOR UPDATE SKIP LOCKED，先锁住候选行再改状态。
            # 并发 worker 会跳过已被锁住的行，天然不会抢同一条。
            use_skip_locked = dialect in ("mysql", "mariadb")
            stmt = (
                select(Run)
                .where(Run.status == "queued")
                .order_by(Run.priority.desc(), Run.created_at)
                .limit(20)
            )
            if use_skip_locked:
                stmt = stmt.with_for_update(skip_locked=True)
            rows = await s.execute(stmt)
            for run in rows.scalars().all():
                # 平台不接管 git，同项目要不要串行只剩 SAME_PROJECT_PARALLEL 这一条规则。
                # 但同一条会话内的 run 必须串行：用户同一个对话里连发几条，应该按顺序一条条做，
                # 不能两条同时改同一个上下文（前端已排队，这里是后端兜底：两个标签页/刷新竞态）。
                # 跨会话不受这条影响 —— 不同会话的 run 照旧可以同时执行。
                busy = await s.scalar(
                    select(func.count()).select_from(Run).where(
                        Run.session_id == run.session_id,
                        Run.status.in_(("claimed", "running")),
                        Run.id != run.id,
                    ))
                if busy:
                    continue
                if not SAME_PROJECT_PARALLEL:
                    busy = await s.scalar(
                        select(func.count()).select_from(Run).where(
                            Run.project_id == run.project_id,
                            Run.status.in_(("claimed", "running")),
                            Run.id != run.id,
                        ))
                    if busy:
                        continue
                # 原子领取：带 status 条件防多 worker 双领（SQLite 无 SKIP LOCKED 时靠这条兜底）
                res = await s.execute(
                    update(Run)
                    .where(Run.id == run.id, Run.status == "queued")
                    .values(status="claimed", claimed_at=_now())
                )
                if res.rowcount:
                    await s.commit()
                    return str(run.id)
            # 没有可领的行：提交（释放 FOR UPDATE 读锁）后返回，交给调用方 sleep 再试
            await s.commit()
            return None

    async def cancel(self, run_id: str) -> None:
        async with self._sf() as s:
            await s.execute(
                update(Run).where(Run.id == _u(run_id)).values(status="cancelled")
            )
            await s.commit()

    async def waiting(self, project_id: str) -> list[str]:
        async with self._sf() as s:
            rows = await s.execute(
                select(Run.id)
                .where(Run.project_id == _u(project_id), Run.status == "queued")
                .order_by(Run.priority.desc(), Run.created_at)
            )
            return [str(r[0]) for r in rows.all()]


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
