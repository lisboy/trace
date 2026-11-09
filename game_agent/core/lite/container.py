"""把 5 个接口的实现装配成一套可用的「引擎」（lite 或 server）。

业务代码拿到的永远是 CoreServices 这个门面，不感知具体实现。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from game_agent.core.abstractions import (
    EventBus,
    LockManager,
    ObjectStore,
    Queue,
    Repository,
)
from game_agent.core.lite.eventbus import LiteEventBus
from game_agent.core.lite.lock import LiteLockManager
from game_agent.core.lite.queue import LiteQueue
from game_agent.core.lite.store import LiteObjectStore
from game_agent.core.repository import DbRepository


@dataclass
class CoreServices:
    repo: Repository
    bus: EventBus
    queue: Queue
    locks: LockManager
    store: ObjectStore


async def build_lite_services(
    session_factory: async_sessionmaker,
    worker_id: str,
    ttl: int = 120,
) -> CoreServices:
    """装配 lite 模式全套实现。"""
    repo = DbRepository(session_factory)
    bus = LiteEventBus(repo)
    queue = LiteQueue(session_factory)
    locks = LiteLockManager(session_factory, worker_id, ttl)
    store = LiteObjectStore()
    return CoreServices(repo=repo, bus=bus, queue=queue, locks=locks, store=store)
