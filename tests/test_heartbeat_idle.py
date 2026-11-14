"""等待心跳的秒数必须是「静默时长」，不是本轮调用的累计时长（2026-11-12 用户反馈）。

现场：模型流式吐了很长的思考（有增量就不该算「等待」），中间停了十几秒，
心跳却报「已等待 156 秒」——那是本轮从开始到现在的总时长，把正常生成时间也算进去了。
口径应为：距上一次增量过去了几秒；增量一来就归零，持续流式时根本不该发心跳。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from game_agent.agents.roles import ROLE_TOOLS
from game_agent.agents.runtime import AgentRuntime
from game_agent.agents.tools import ToolContext
from game_agent.core.context import RunContext
from game_agent.llm.base import Ctx


class _Bus:
    def __init__(self) -> None:
        self.notes: list[str] = []

    async def emit(self, env) -> None:
        p = env.payload or {}
        if p.get("waiting"):
            self.notes.append(f"{p.get('waited')}|{p.get('note')}")


def _runtime(tmp_path: Path):
    rc = RunContext(run_id="run-hb", team_id="t1", project_id="p1", session_id="s1",
                    triggered_by="u1", run_type="modify", worker_id="w",
                    repo=None, bus=_Bus(), locks=None, store=None)  # type: ignore[arg-type]
    tctx = ToolContext(run_ctx=rc, workspace=Path(tmp_path), role="main", task_id="t")
    ctx = Ctx(role="main", model="m", model_profile={"model": "m"})
    rt = AgentRuntime("main", ROLE_TOOLS["main"], "sys", "instr", tctx, ctx)
    return rt, rc


class _Clock:
    """可手动推进的 time.monotonic 替身（真实等待只在真实时间里走秒级拍子）。"""
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _collect(rt, messages, on_delta) -> list[str]:
    await rt._chat_with_heartbeat(messages, [], on_delta=on_delta, label="模型")
    # _emit 用 create_task 投递：让出几拍，事件才会落到 fake bus 上
    for _ in range(4):
        await asyncio.sleep(0)
    return rt.tctx.run_ctx.bus.notes  # type: ignore[union-attr]


def test_heartbeat_reports_idle_not_cumulative(tmp_path, monkeypatch) -> None:
    import game_agent.agents.runtime as rt_mod
    clock = _Clock()
    # 只替换 runtime 模块内的 time 引用；绝不能 patch 全局 time.monotonic，
    # 否则 asyncio 事件循环自己的时钟也被冻结，sleep/wait_for 永不醒来（实测卡死）。
    monkeypatch.setattr(rt_mod, "time", SimpleNamespace(monotonic=clock))
    rt, _rc = _runtime(tmp_path)

    async def chat(messages, tools, ctx, on_delta=None, summarizer=None, **kw):
        # 第 1 拍：流式吐增量（真实等待 ~0.05s，虚拟时钟走到 100）
        await asyncio.sleep(0.05)
        clock.now = 100.0
        on_delta("思考", "")
        # 第 2 拍：之后静默——虚拟时钟走到 130（=30 秒没动静），再真实等 3.2s
        # 让心跳在 ~6s 那拍醒来（beats 每 3 真实秒一拍）
        await asyncio.sleep(3.4)
        clock.now = 130.0
        await asyncio.sleep(3.2)
        # 增量恢复：心跳必须立即闭嘴，不能再报累计值
        clock.now = 131.0
        on_delta("继续", "")
        await asyncio.sleep(0.2)
        return type("R", (), {"text": "ok"})()

    rt.llm = type("L", (), {"chat": staticmethod(chat)})()
    notes = asyncio.run(_collect(rt, [{"role": "user", "content": "hi"}],
                                 lambda r, t: None))
    assert notes, "静默 30 秒应至少发出一条心跳"
    for waited, note in (n.split("|", 1) for n in notes):
        assert int(waited) <= 32, f"心跳报了累计时长: {note}"
        assert "已等待" in note
    # 最关键：绝不能出现「131 秒」（=从本轮开始算的累计时长）
    assert all(not n.startswith("131|") for n in notes)


def test_heartbeat_silent_while_streaming(tmp_path, monkeypatch) -> None:
    import game_agent.agents.runtime as rt_mod
    clock = _Clock()
    monkeypatch.setattr(rt_mod, "time", SimpleNamespace(monotonic=clock))
    rt, _rc = _runtime(tmp_path)

    async def chat(messages, tools, ctx, on_delta=None, summarizer=None, **kw):
        for i in range(12):
            await asyncio.sleep(0.35)  # 真实 ~4.2s，够心跳醒来好几拍
            clock.now = float(i + 1) * 5  # 增量一直每 5 虚拟秒来一次，从未静默 8 秒
            on_delta("思考", "")
        return type("R", (), {"text": "ok"})()

    rt.llm = type("L", (), {"chat": staticmethod(chat)})()
    notes = asyncio.run(_collect(rt, [{"role": "user", "content": "hi"}],
                                 lambda r, t: None))
    assert notes == [], f"持续流式期间不该有心跳: {notes}"
