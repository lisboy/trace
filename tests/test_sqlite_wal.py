"""SQLite 落盘参数（WAL / synchronous / busy_timeout）的回归测试。

默认的 rollback journal 每次提交都要 fsync 主库并建/删日志文件，与正在跑的 run 抢同一把写锁：
6.5GB 的库上实测单次 300KB 提交 7.1ms（delete）vs 2.7ms（WAL），长 run 一轮要写十几条
事件/消息，还常撞「database is locked」。这里钉住两件事：连上来就是 WAL，且外键约束没丢。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def test_sqlite_connect_pragmas_applied(tmp_path) -> None:
    """任何通过本仓库引擎栈建出来的 SQLite 连接都应带 WAL + busy_timeout + 外键。"""
    db = tmp_path / "pragma.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")

    async def _check() -> dict[str, str]:
        async with engine.connect() as conn:
            out = {}
            for pragma in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys"):
                out[pragma] = str((await conn.execute(text(f"PRAGMA {pragma}"))).scalar())
            await conn.execute(text("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"))
            await conn.execute(text("INSERT INTO t (v) VALUES ('x')"))
            await conn.commit()
            return out

    try:
        got = asyncio.run(_check())
    finally:
        asyncio.run(engine.dispose())

    assert got["journal_mode"].lower() == "wal", got
    # synchronous: 1 = NORMAL（WAL 下只在 checkpoint fsync），2 = FULL
    assert got["synchronous"] in ("1", "2"), got
    assert int(got["busy_timeout"]) >= 5000, "并发写撞锁时要等一会儿而不是立刻报错"
    assert got["foreign_keys"] == "1", "开 WAL 不能把外键约束丢掉"


def test_wal_survives_reopen_and_allows_concurrent_reader(tmp_path) -> None:
    """WAL 是持久化在库文件里的：重开还是 wal，且读不会被写卡住。"""
    db = tmp_path / "reopen.db"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"

    async def _write() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"))
            await conn.execute(text("INSERT INTO t (v) VALUES ('a')"))
        await engine.dispose()

    async def _read_while_writing() -> str:
        w = create_async_engine(url)
        r = create_async_engine(url)

        async def _hold_write() -> None:
            async with w.begin() as conn:
                await conn.execute(text("INSERT INTO t (v) VALUES ('b')"))
                # 事务还没提交时，另一个连接必须仍能读到旧快照（WAL 的读不阻塞写）
                async with r.connect() as rc:
                    n = (await rc.execute(text("SELECT COUNT(*) FROM t"))).scalar()
                assert n == 1, f"WAL 下读被写阻塞或读到了未提交数据: {n}"

        await _hold_write()
        async with r.connect() as conn:
            mode = str((await conn.execute(text("PRAGMA journal_mode"))).scalar())
        await w.dispose()
        await r.dispose()
        return mode

    asyncio.run(_write())
    mode = asyncio.run(_read_while_writing())
    assert mode.lower() == "wal", f"重开后 journal_mode 变了: {mode}"


@pytest.mark.parametrize("sync_mode,expected", [("FULL", "2"), ("NORMAL", "1")])
def test_sync_mode_env_respected(tmp_path, monkeypatch, sync_mode, expected) -> None:
    """GA_SQLITE_SYNC=FULL 时回到「每事务 fsync」的保守档（掉电零丢失的取舍要能选）。"""
    import importlib

    import game_agent.db.session as sess_mod

    monkeypatch.setenv("GA_SQLITE_SYNC", sync_mode)
    reloaded = importlib.reload(sess_mod)
    assert reloaded._SQLITE_SYNC == sync_mode

    db = tmp_path / f"sync-{sync_mode}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")

    async def _check() -> str:
        async with engine.connect() as conn:
            return str((await conn.execute(text("PRAGMA synchronous"))).scalar())

    try:
        got = asyncio.run(_check())
    finally:
        asyncio.run(engine.dispose())
        monkeypatch.delenv("GA_SQLITE_SYNC", raising=False)
        importlib.reload(sess_mod)   # 复原，别影响其它测试的引擎
    assert got == expected, f"synchronous={got}，期望 {expected}"
