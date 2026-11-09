"""lite LockManager：进程内 asyncio.Lock 串行化 + 单调递增 fencing token + 落库记账。

设计 §6.3：fencing token 每次转移 +1；所有写操作前校验当前 token；worker 失联后 token 已变则立即自杀。
server 模式换成 Redis SET NX PX + DB 记账（同样返回 fencing token）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from game_agent.core.abstractions import Fencing, LockInfo, LockManager
from game_agent.db.models import ProjectLock


def _u(v):  # type: ignore[no-untyped-def]
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


class LockBusy(Exception):
    pass


class LiteLockManager(LockManager):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession],
                 worker_id: str, ttl: int = 120) -> None:
        self._sf = session_factory
        self._worker_id = worker_id
        self._ttl = ttl
        self._locks: dict[str, asyncio.Lock] = {}
        self._state: dict[str, dict] = {}

    def _al(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    async def acquire(self, project_id: str, run_id: str, mode: str = "exclusive",
                     ttl: int = 120) -> int:
        async with self._al(project_id):
            cur = self._state.get(project_id)
            now = datetime.now(timezone.utc)
            if cur and cur["run_id"] != str(run_id) and cur["expires_at"] > now:
                raise LockBusy(f"project {project_id} 已被 run {cur['run_id']} 占用")
            token = (cur["fencing_token"] + 1) if cur else 1
            self._state[project_id] = {
                "run_id": str(run_id), "mode": mode, "fencing_token": token,
                "holder_worker": self._worker_id,
                "acquired_at": now, "expires_at": _exp(now, ttl), "heartbeat_at": now,
            }
            await self._persist(project_id, run_id, mode, token, now)
            return token

    async def release(self, project_id: str, run_id: str, token: int) -> None:
        async with self._al(project_id):
            cur = self._state.get(project_id)
            if cur and cur["run_id"] == str(run_id) and cur["fencing_token"] == token:
                self._state.pop(project_id, None)
                await self._clear_db(project_id)

    async def heartbeat(self, project_id: str, run_id: str, token: int) -> None:
        async with self._al(project_id):
            cur = self._state.get(project_id)
            if cur and cur["run_id"] == str(run_id) and cur["fencing_token"] == token:
                now = datetime.now(timezone.utc)
                cur["heartbeat_at"] = now
                cur["expires_at"] = _exp(now, self._ttl)
                await self._persist(project_id, run_id, cur["mode"], token, now)

    async def current(self, project_id: str) -> Optional[LockInfo]:
        cur = self._state.get(project_id)
        if not cur:
            return None
        return LockInfo(
            project_id=project_id, run_id=cur["run_id"], mode=cur["mode"],
            fencing_token=cur["fencing_token"], holder_worker=cur["holder_worker"],
            expires_at=cur["expires_at"],
        )

    def make_fencing(self, project_id: str, token: int) -> Fencing:
        return Fencing(project_id, token, self)

    # ---- DB 记账（供 UI 显示"谁在占用"）----
    async def _persist(self, project_id, run_id, mode, token, now):  # type: ignore[no-untyped-def]
        async with self._sf() as s:
            await s.execute(
                update(ProjectLock)
                .where(ProjectLock.project_id == _u(project_id))
                .values(run_id=_u(run_id), mode=mode, fencing_token=token,
                        holder_worker=self._worker_id, acquired_at=now,
                        expires_at=_exp(now, self._ttl), heartbeat_at=now)
            )
            res = await s.execute(
                select(ProjectLock).where(ProjectLock.project_id == _u(project_id))
            )
            if res.scalar_one_or_none() is None:
                s.add(ProjectLock(
                    project_id=_u(project_id), run_id=_u(run_id), mode=mode,
                    fencing_token=token, holder_worker=self._worker_id,
                    acquired_at=now, expires_at=_exp(now, self._ttl), heartbeat_at=now,
                ))
            await s.commit()

    async def _clear_db(self, project_id):  # type: ignore[no-untyped-def]
        async with self._sf() as s:
            await s.execute(
                update(ProjectLock).where(ProjectLock.project_id == _u(project_id)).values(run_id=None)
            )
            await s.commit()


def _exp(now: datetime, ttl: int) -> datetime:
    from datetime import timedelta
    return now + timedelta(seconds=ttl)
