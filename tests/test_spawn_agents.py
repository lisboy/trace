"""spawn_agents 子agent 并行的单元测试（docs/DESIGN-OPTIMIZATION.md「子 agent 并行」节）。

只测工具函数本身的硬行为，不拉起真实 Orchestrator：
- items 校验（空数组/非法项报 ToolError）；
- 并行派发（多个子任务确实并发跑、指令完整注入）；
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


class _FakeOrch:
    """假编排器：记录 run_agent 调用，并发时用 sleep 验证重叠。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._active = 0
        self._max_active = 0

    async def run_agent(self, *, role, parent_task_id, instruction, run_ctx):
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
    async def run_agent(self, *, role, parent_task_id, instruction, run_ctx):
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


def test_spawn_runs_in_parallel_and_aggregates() -> None:
    orch = _FakeOrch()
    out = _run(spawn_agents({
        "items": [
            {"name": "A", "question": "查文件 a"},
            {"name": "B", "question": "查文件 b", "note": "重点看行号"},
            {"name": "C", "question": "查文件 c"},
        ],
    }, _ctx(orch)))

    assert orch._max_active >= 3, "3 个子任务应并发派发，而非串行"
    assert len(orch.calls) == 3
    assert "已并行派出 3 个只读子agent" in out
    assert "## A" in out and "## B" in out and "## C" in out
    assert "（备注：重点看行号）" in orch.calls[1]


def test_spawn_one_failure_does_not_drop_others() -> None:
    orch = _FailOrch()
    out = _run(spawn_agents({
        "items": [
            {"name": "good", "question": "没问题"},
            {"name": "bad", "question": "会失败"},
        ],
    }, _ctx(orch)))
    assert "## good\nok" in out
    assert "## bad" in out
    assert "执行出错：RuntimeError: boom" in out


def test_spawn_caps_at_max_subagents() -> None:
    orch = _FakeOrch()
    items = [{"name": f"s{i}", "question": f"q{i}"} for i in range(MAX_SUBAGENTS + 2)]
    out = _run(spawn_agents({"items": items}, _ctx(orch)))
    assert len(orch.calls) == MAX_SUBAGENTS
    assert f"已并行派出 {MAX_SUBAGENTS} 个" in out


def test_spawn_missing_question_skipped() -> None:
    orch = _FakeOrch()
    out = _run(spawn_agents({
        "items": [{"name": "无问题", "question": ""},
                  {"name": "有", "question": "查一下"}],
    }, _ctx(orch)))
    assert len(orch.calls) == 1
    assert "没有给 question，跳过" in out
