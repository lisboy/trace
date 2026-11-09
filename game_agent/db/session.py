"""异步引擎与会话工厂。MySQL / SQLite 共用模型，靠 URL 方言自动切换。"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from game_agent.config import DATABASE_URL, MAX_CONCURRENT_RUNS, is_sqlite
from game_agent.db.models import Base

if is_sqlite():
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )
else:
    # MySQL（server/公用部署）：长连接需防 wait_timeout 断连，charset 固定 utf8mb4。
    # sort_buffer_size 调大：messages 行里可能带 base64 附图（单行可达数 MB），
    # 默认 256KB 排序缓冲会让任何「整行 + ORDER BY」的查询报 1038 Out of sort memory，
    # 直接把 run 顶死在加载上下文那一步。
    #
    # 连接池大小跟着并发 worker 数走：默认 pool_size=5+overflow=10 只有 15 条连接，
    # 一放开多会话并行（MAX_CONCURRENT_RUNS=64）就会在 LLM 往返高峰排队等连接、
    # 甚至 PoolOverflow。这里按 worker 数放大，同时给 MySQL max_connections 留余量。
    _pool = max(10, min(MAX_CONCURRENT_RUNS + 8, 48))
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=_pool,
        max_overflow=max(10, _pool // 2),
        future=True,
        connect_args={
            "charset": "utf8mb4",
            "init_command": "SET SESSION sort_buffer_size=16777216, max_sort_length=16777216",
        },
    )

if is_sqlite():
    # SQLite 单连接即可；开启外键约束
    from sqlalchemy import event
    from sqlalchemy.engine import Engine as SyncEngine

    @event.listens_for(SyncEngine, "connect")
    def _fk_pragma(dbapi_con, con_record):  # type: ignore[no-untyped-def]
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
