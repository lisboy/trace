"""对话机制瘦身的两处回归：
1) 旧的可再生工具结果（read_file/run_command…）在「最近一轮之前」降级成存根，
   写操作/结论类结果不动，且幂等；
2) 同一张图短间隔重复 view_image（文件未变化）不再重复回灌像素，文件变化后允许重看。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from game_agent.agents import runtime as R  # noqa: E402
from game_agent.agents.tools import ToolContext  # noqa: E402
from game_agent.agents.tools.service import view_image  # noqa: E402
from game_agent.core.context import RunContext  # noqa: E402


def _ctx(workspace: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request="验收")
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


class _RT:
    """只给 _shrink_old_tool_results 用的极简 self：它只读 llm.context_window 与 tools。"""

    def __init__(self, window: int) -> None:
        import types
        self.llm = types.SimpleNamespace(context_window=window)
        self.tools = []


def test_shrink_old_tool_results() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        # 第 1 轮：read_file（可再生，应降级）
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "read_file", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "A" * 3000},
        # 第 2 轮：run_command（最近一轮，保留原文）
        {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "name": "run_command", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "2", "content": "B" * 3000},
        {"role": "user", "content": "[视觉回灌]..."},
    ]
    rt = _RT(2000)  # 小窗口 → 一定超过压力门槛
    out = R.AgentRuntime._shrink_old_tool_results(rt, msgs)
    assert out[3]["content"].startswith(R._SHRUNK_MARK)
    assert len(out[3]["content"]) < 1600
    assert out[5]["content"] == "B" * 3000  # 最近一轮不动
    # 幂等
    out2 = R.AgentRuntime._shrink_old_tool_results(rt, out)
    assert out2[3]["content"] == out[3]["content"]


def test_shrink_skipped_when_context_is_roomy() -> None:
    """上下文还宽松时不能降级：降级会让模型「忘掉」刚读的内容 → 反复整读同一个文件。

    实测（2026-11-14 02:06 那次「看看这个项目」）：16k 上下文 / 192k 预算（8%）时，
    README.md 仍被按 1-150、150-254、18-147、205-254 读了 4 遍。
    """
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "read_file", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "A" * 5000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "name": "grep", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "2", "content": "B" * 100},
    ]
    out = R.AgentRuntime._shrink_old_tool_results(_RT(256000), msgs)
    assert out[1]["content"] == "A" * 5000, "宽松上下文里不该动历史"
    assert out is msgs or out[1]["content"] == "A" * 5000


def test_shrink_stops_when_pressure_relieved() -> None:
    """有压力时从旧到新削，削到达标就停：越新的内容越有用，不该跟着一起削。"""
    msgs = []
    for i in range(6):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": str(i), "name": "read_file", "arguments": {}}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": f"{i}" + "A" * 8000})
    # 最近一轮要保留（构造一个尾部 assistant）
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": "z", "name": "grep", "arguments": {}}]})
    msgs.append({"role": "tool", "tool_call_id": "z", "content": "tail"})
    rt = _RT(24000)  # 预算 18k，门槛 10.8k：6×8k 字符 ≈ 每条约 4k token，必然超
    out = R.AgentRuntime._shrink_old_tool_results(rt, msgs)
    shrunk = [i for i, m in enumerate(out)
              if m.get("role") == "tool" and str(m.get("content", "")).startswith(R._SHRUNK_MARK)]
    assert shrunk, "有压力时必须削"
    assert shrunk == sorted(shrunk) and shrunk[0] == 1, "应从最旧的开始削"
    # 靠后的（较新）结果应被保留为原文
    assert any(m.get("role") == "tool" and str(m.get("content", "")).startswith(("4", "5"))
               for m in out[1:-1]), "削到达标就停，较新的原文应保留"


def test_shrink_keeps_non_regenerable_and_small() -> None:
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "9", "name": "write_file", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "9", "content": "C" * 3000},  # 写回执不该长这样，但即便长也不动
        {"role": "assistant", "content": "", "tool_calls": [{"id": "10", "name": "glob", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "10", "content": "small"},
    ]
    out = R.AgentRuntime._shrink_old_tool_results(_RT(2000), msgs)
    assert out[1]["content"] == "C" * 3000


def test_repeat_view_suppresses_reattach(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    Image.new("RGB", (64, 64), (30, 60, 90)).save(img)
    t = _ctx(tmp_path)

    async def flow():
        t.call_seq += 1
        r1 = await view_image({"path": "shot.png"}, t)
        t.call_seq += 1
        r2 = await view_image({"path": "shot.png"}, t)
        return r1, r2

    r1, r2 = asyncio.run(flow())
    assert len(r1.images) == 1
    assert len(r2.images) == 0  # 未变化 → 不再回灌
    assert "不再重复回灌" in r2.text

    # 文件变化后允许重看
    time.sleep(0.01)
    Image.new("RGB", (64, 64), (120, 0, 0)).save(img)
    t.call_seq += 1
    r3 = asyncio.run(view_image({"path": "shot.png"}, t))
    assert len(r3.images) == 1
