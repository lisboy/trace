"""单个子agent 的独立停止（tasks.cancel_requested）回归测试。

2026-11-12 实测事故：点任意子agent 旁的「停止」，5 条全部永久停在「停止中…」。
根因是 ``_watch_cancel`` 的参数也叫 ``task``（asyncio.Task），把外层的 DB 任务对象
遮住了：``str(task.id)`` 实际落在 asyncio.Task 上 → AttributeError → 看门狗第一次
轮询就永久退出。这里用一个永不返回的假 runtime 锁住整条链路：
置位 tasks.cancel_requested → 看门狗掐断该子agent → task 落 cancelled → 主流程不被打断。
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

from game_agent.core.context import RunContext
from game_agent.core.lite.container import build_lite_services
from game_agent.orchestration.orchestrator import Orchestrator

from test_smoke_orchestrator import _seed


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 30) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


def test_per_subagent_cancel_stops_only_that_task(session_factory, monkeypatch) -> None:
    async def flow() -> tuple[str, str]:
        started_evt = asyncio.Event()
        runtime_cancelled = asyncio.Event()

        class _HangingRuntime:
            def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
                pass

            async def run(self):
                started_evt.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    runtime_cancelled.set()

        monkeypatch.setattr(
            "game_agent.agents.runtime.AgentRuntime", _HangingRuntime)

        ids = await _seed(session_factory, "并行核对")
        svcs = await build_lite_services(session_factory, "cancel-worker")
        orch = Orchestrator(svcs, "cancel-worker", session_factory)
        rc = RunContext(
            run_id=ids["run_id"], team_id=ids["team_id"], project_id=ids["project_id"],
            session_id=ids["session_id"], triggered_by=ids["user_id"],
            run_type="modify", worker_id="cancel-worker",
            repo=svcs.repo, bus=svcs.bus, locks=svcs.locks, store=svcs.store)

        agent = asyncio.create_task(orch.run_agent("sub", None, "查一条线索", rc))
        await asyncio.wait_for(started_evt.wait(), 10)
        subs = [t for t in await svcs.repo.list_tasks(ids["run_id"]) if t.kind == "sub"]
        assert len(subs) == 1
        await svcs.repo.update_task(str(subs[0].id), cancel_requested=True)

        result = await asyncio.wait_for(agent, 10)
        await asyncio.wait_for(runtime_cancelled.wait(), 5)
        after = await svcs.repo.get_task(str(subs[0].id))
        return result.text or "", after.status

    text, status = _run(flow())
    assert "已按用户要求停止这个子agent" in text, text
    assert status == "cancelled", status
