"""spawn_agents 子agent 并行的单元测试（docs/DESIGN-OPTIMIZATION.md「子 agent 并行」节）。

2026-11-12 起 spawn_agents 改为**派发即返回**：子agent 在后台跑，结论由
AgentRuntime 在后续轮次回收注入（见 runtime._harvest_spawns）。所以这里测：
- items 校验（空数组/非法项报 ToolError）；
- 立即返回 + 批次挂到 ToolContext（不再阻塞主agent 等整批结束）；
- 后台任务确实并发跑、指令完整注入、汇总含每个子agent 的结论；
- 单个子任务失败不影响其他任务回收；
- 超过 MAX_SUBAGENTS 自动截断。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.spawn import MAX_SUBAGENTS, spawn_agents
from game_agent.core.context import RunContext


def _ctx(orch) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request="并行核对")
    t = ToolContext(run_ctx=rc, workspace=Path("."), role="main", task_id="t-main")
    t.orchestrator = orch  # type: ignore[assignment]
    return t


def _run(coro):
    return asyncio.run(coro)


async def _collect(t: ToolContext) -> str:
    """等后台批次收工并取汇总（与运行时回收同一路径）。"""
    assert t.spawn_batches, "spawn_agents 应把批次挂到 ToolContext.spawn_batches 上"
    batch = t.spawn_batches[0]
    await asyncio.gather(*batch.tasks, return_exceptions=True)
    return batch.aggregate()


class _FakeOrch:
    """假编排器：记录 run_agent 调用，并发时用 sleep 验证重叠。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._active = 0
        self._max_active = 0

    async def run_agent(self, *, role, parent_task_id, instruction, run_ctx, **_):
        assert role == "sub"
        assert parent_task_id == "t-main"
        self._active += 1
        self._max_active = max(self._max_active, self._active)
        try:
            await asyncio.sleep(0.02)
            self.calls.append(instruction)
            return SimpleNamespace(text=f"答：{instruction[:20]}")
        finally:
            self._active -= 1


class _FailOrch(_FakeOrch):
    async def run_agent(self, *, role, parent_task_id, instruction, run_ctx, **_):
        self._active += 1
        self._max_active = max(self._max_active, self._active)
        try:
            await asyncio.sleep(0.02)
            if "会失败" in instruction:
                raise RuntimeError("boom")
            return SimpleNamespace(text="ok")
        finally:
            self._active -= 1


def test_spawn_requires_items() -> None:
    with pytest.raises(ToolError):
        _run(spawn_agents({"items": []}, _ctx(_FakeOrch())))
    with pytest.raises(ToolError):
        _run(spawn_agents({}, _ctx(_FakeOrch())))
    with pytest.raises(ToolError):
        _run(spawn_agents({"items": "not-list"}, _ctx(_FakeOrch())))


def test_spawn_returns_immediately_and_runs_in_background() -> None:
    """主agent 不能被子agent 拖住：工具调用立刻返回，任务挂在批次上后台跑。"""
    async def _flow() -> tuple[str, str, _FakeOrch]:
        orch = _FakeOrch()
        t = _ctx(orch)
        ack = await spawn_agents({
            "items": [{"name": "A", "question": "查文件 a"},
                      {"name": "B", "question": "查文件 b", "note": "重点看行号"},
                      {"name": "C", "question": "查文件 c"}],
        }, t)
        assert orch.calls == [], "返回时子agent 不应已经跑完（说明它在同步等）"
        out = await _collect(t)
        return ack, out, orch

    ack, out, orch = _run(_flow())
    assert "已派出 3 个只读子agent" in ack
    assert "后台" in ack
    assert "已并行派出 3 个只读子agent" in out
    assert "## A" in out and "## B" in out and "## C" in out
    assert "（备注：重点看行号）" in out


def test_spawns_run_concurrently() -> None:
    async def _flow() -> _FakeOrch:
        orch = _FakeOrch()
        t = _ctx(orch)
        await spawn_agents({
            "items": [{"name": f"S{i}", "question": f"q{i}"} for i in range(4)],
        }, t)
        await _collect(t)
        return orch

    orch = _run(_flow())
    assert orch._max_active >= 4, "4 个子任务应并发派发，而非串行"


def test_spawn_one_failure_does_not_drop_others() -> None:
    async def _flow() -> str:
        t = _ctx(_FailOrch())
        await spawn_agents({
            "items": [{"name": "good", "question": "没问题"},
                      {"name": "bad", "question": "会失败"},
                      {"name": "good2", "question": "也没问题"}],
        }, t)
        return await _collect(t)

    out = _run(_flow())
    assert "## good\nok" in out
    assert "## bad" in out
    assert "执行出错：RuntimeError: boom" in out


def test_spawn_caps_at_max_subagents() -> None:
    async def _flow() -> tuple[_FakeOrch, str]:
        orch = _FakeOrch()
        t = _ctx(orch)
        items = [{"name": f"s{i}", "question": f"q{i}"} for i in range(MAX_SUBAGENTS + 2)]
        ack = await spawn_agents({"items": items}, t)
        out = await _collect(t)
        return orch, ack + out

    orch, out = _run(_flow())
    assert len(orch.calls) == MAX_SUBAGENTS
    assert f"已派出 {MAX_SUBAGENTS} 个" in out


def test_spawn_missing_question_skipped() -> None:
    async def _flow() -> tuple[_FakeOrch, str]:
        orch = _FakeOrch()
        t = _ctx(orch)
        await spawn_agents({
            "items": [{"name": "无问题", "question": ""},
                      {"name": "有", "question": "查一下"},
                      {"name": "还有", "question": "再查一个"}],
        }, t)
        return orch, await _collect(t)

    orch, out = _run(_flow())
    assert len(orch.calls) == 2
    assert "没有给 question，跳过" in out


def test_spawn_accepts_two_items() -> None:
    """2 条互不依赖的线索值得并行派（与 1 条分界）。实测 2026-11-11：两条较重核对被旧
    「至少3条」门槛逼成串行、主agent 反复试探 30s+，故门槛降到 2。"""
    async def _flow() -> _FakeOrch:
        orch = _FakeOrch()
        t = _ctx(orch)
        await spawn_agents({"items": [{"name": "A", "question": "查 a"},
                                      {"name": "B", "question": "查 b"}]}, t)
        await _collect(t)
        return orch

    orch = _run(_flow())
    assert len(orch.calls) == 2


def test_spawn_rejects_single_item() -> None:
    """只有 1 条线索：自己直接读更快，省下建 task / 单独走 LLM 的开销。"""
    orch = _FakeOrch()
    with pytest.raises(ToolError):
        _run(spawn_agents({
            "items": [{"name": "A", "question": "查 a"}],
        }, _ctx(orch)))
    assert orch.calls == []
