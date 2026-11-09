"""lite EventBus：进程内 asyncio 扇出 + 经 Repository 落库（DB 是权威源）。

WS 客户端断线重连时先 repo.get_events_since 补拉，再接本总线实时流，保证不丢不重。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from game_agent.core.abstractions import EventBus, EventEnvelope, Repository


class LiteEventBus(EventBus):
    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._subs: dict[str, list[asyncio.Queue]] = {}
        # 事件必须严格有序：seq 由 max+1 生成，并发 emit 会撞号，
        # 进程内用锁串行化「读 max → 插入」临界区
        self._emit_lock = asyncio.Lock()

    async def emit(self, env: EventEnvelope) -> int:
        # 订阅 key 一律为字符串；envelope.project_id 可能是 UUID 对象（Run.project_id），
        # 必须归一化为 str，否则 subs 查不到队列 → ws 实时推送静默失效（全靠 REST 轮询兜底）
        key = str(env.project_id) if env.project_id else ""
        if env.ephemeral:
            # 高频轻量事件（流式思考 delta 等）：只扇出给在线订阅者，不落库不占 seq，
            # 断线重连的补拉不含它——完整版事件随后会正常落库兜底
            for q in self._subs.get(key, []):
                q.put_nowait(env)
            return 0
        async with self._emit_lock:
            seq = await self._repo.append_event(env)
        env.payload.setdefault("_seq", seq)
        for q in self._subs.get(key, []):
            q.put_nowait(env)
        return seq

    def subscribe(self, project_id: str) -> AsyncIterator[EventEnvelope]:
        return self._subscribe(project_id)

    async def _subscribe(self, project_id: str):  # type: ignore[no-untyped-def]
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(project_id, []).append(q)
        try:
            while True:
                env = await q.get()
                yield env
        finally:
            subs = self._subs.get(project_id)
            if subs and q in subs:
                subs.remove(q)

    async def attach_session(self, session) -> None:  # noqa: ARG002
        return None
