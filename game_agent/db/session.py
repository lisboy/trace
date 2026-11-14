"""异步引擎与会话工厂。个人版只跑 SQLite（单文件、零运维、随包即用）。"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from game_agent.config import DATABASE_URL
from game_agent.db.models import Base

# SQLite 落盘强度：NORMAL（默认，WAL 下只在 checkpoint fsync）| FULL（每事务 fsync，最安全）
_SQLITE_SYNC = os.environ.get("GA_SQLITE_SYNC", "NORMAL").strip().upper()
if _SQLITE_SYNC not in ("NORMAL", "FULL"):
    _SQLITE_SYNC = "NORMAL"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

# SQLite 单连接即可；开启外键约束
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine as SyncEngine  # noqa: E402


@event.listens_for(SyncEngine, "connect")
def _fk_pragma(dbapi_con, con_record):  # type: ignore[no-untyped-def]
    cur = dbapi_con.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL：默认的 rollback journal（journal_mode=delete）每次提交都要 fsync 主库 + 建/删
    # 日志文件，与正在跑的 run 抢同一把写锁。实测单次 300KB 提交 7.1ms（delete）vs 2.7ms（WAL），
    # 而长 run 一轮要写十几条事件/消息，6.5GB 库上还常撞「database is locked」。
    # WAL 允许读不阻塞写，synchronous=NORMAL 把每次提交的 fsync 降到「只在 checkpoint 时」，
    # 断电最多丢最后几个事务（这是本地单机库可接受的取舍；要极致安全可设 GA_SQLITE_SYNC=FULL）。
    for pragma in ("journal_mode=WAL", f"synchronous={_SQLITE_SYNC}",
                   "wal_autocheckpoint=1000", "busy_timeout=15000",
                   "temp_store=MEMORY", "cache_size=-64000"):
        cur.execute(f"PRAGMA {pragma}")
    cur.close()

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
