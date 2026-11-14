"""运行中切换模型：下一轮换大脑，且后续派出的子agent 也要继承新档位（2026-11-12）。

现场：用户在长任务里切了更快的模型，下一个思考轮次看起来还是旧模型。除了「当前
这一轮已经在跑、只能等它跑完」这个天然窗口，代码里还有一个真洞：子agent 派发时
按 run_ctx.meta["model_profile_key"] 重新解析档位（orchestrator._resolve_profile_key），
而切换只改了 ctx/self.llm，没写回 meta —— 切完之后新派出的子agent 仍拿旧档位。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_agent.agents.roles import ROLE_TOOLS
from game_agent.agents.runtime import AgentRuntime
from game_agent.agents.tools import ToolContext
from game_agent.core.context import RunContext
from game_agent.llm.base import Ctx
from game_agent.orchestration import steering


class _Brain:
    async def chat(self, *args, **kwargs):  # pragma: no cover - 本测试不真的调模型
        raise AssertionError("不应发起真实 LLM 调用")


class _Bus:
    def __init__(self) -> None:
        self.notes: list[str] = []

    async def emit(self, env) -> None:
        note = (env.payload or {}).get("note")
        if note:
            self.notes.append(str(note))


def _runtime(tmp_path: Path, run_id: str, meta: dict | None = None):
    rc = RunContext(run_id=run_id, team_id="t1", project_id="p1", session_id="s1",
                    triggered_by="u1", run_type="modify", worker_id="w",
                    repo=None, bus=_Bus(), locks=None, store=None,  # type: ignore[arg-type]
                    meta=meta if meta is not None else {})
    tctx = ToolContext(run_ctx=rc, workspace=Path(tmp_path), role="main", task_id="task1")
    ctx = Ctx(role="main", model="old-model", model_profile={"model": "old-model"})
    return AgentRuntime("main", ROLE_TOOLS["main"], "sys", "instr", tctx, ctx), rc, tctx


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    import game_agent.agents.runtime as rt_mod
    monkeypatch.setattr(rt_mod, "build_llm", lambda role, ctx=None: _Brain())


def _orchestrator(default_effort: str = ""):
    async def _load_profile(team_id, key):
        return {"model": "fast-model" if key == "fast" else "bound-model",
                "base_url": "http://example.invalid", "protocol": "openai", "params": {}}

    async def _agent_profile_key(team_id, role):
        return "bound"

    async def _agent_defaults(team_id, role):
        # 角色默认：绑定档位 + 默认推理等级（agents.params_json.reasoning_effort）
        return "bound", default_effort

    return SimpleNamespace(_load_profile=_load_profile, _agent_profile_key=_agent_profile_key,
                           _agent_defaults=_agent_defaults)


def _apply(rt) -> None:
    async def run():
        await rt._apply_model_switch()
        # _emit 用 create_task 投递：让出几拍，事件才会真正落到 fake bus 上
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())


def test_switch_rebuilds_llm_and_propagates_to_run_meta(tmp_path) -> None:
    rt, rc, tctx = _runtime(tmp_path, "run-switch-a",
                            meta={"caps": {"test": True, "web": True, "media": True}})
    tctx.orchestrator = _orchestrator()
    try:
        steering.clear("run-switch-a")
        steering.push("run-switch-a", "", [], model={"profile_key": "fast", "effort": "low"})

        _apply(rt)

        # 下一个 LLM 往返换新大脑
        assert rt.ctx.model == "fast-model"
        assert rt.ctx.model_profile["model"] == "fast-model"
        # 之后 spawn_agents 派生子agent 时按 meta 重新解析 → 继承新档位
        assert rc.meta["model_profile_key"] == "fast"
        assert rc.meta["reasoning_effort"] == "low"
        # 其它运行期选项不能被顺手清掉
        assert rc.meta["caps"] == {"test": True, "web": True, "media": True}
        assert any("下一个思考轮次生效" in n for n in tctx.run_ctx.bus.notes)
    finally:
        steering.clear("run-switch-a")


def test_switch_back_to_default_clears_meta_override(tmp_path) -> None:
    rt, rc, tctx = _runtime(tmp_path, "run-switch-b",
                            meta={"model_profile_key": "fast", "reasoning_effort": "high"})
    tctx.orchestrator = _orchestrator()
    try:
        steering.clear("run-switch-b")
        steering.push("run-switch-b", "", [], model={"profile_key": "", "effort": ""})

        _apply(rt)

        # 空串 = 跟随角色绑定默认：meta 里的显式覆盖必须撤掉，子agent 才会重新跟随
        assert "model_profile_key" not in rc.meta
        assert "reasoning_effort" not in rc.meta
        assert rt.ctx.model == "bound-model"
    finally:
        steering.clear("run-switch-b")


def test_switch_back_to_default_uses_agent_default_effort(tmp_path) -> None:
    """切回「跟随默认」时推理强度也回角色默认（2026-11-14）。

    输入框切换会把「模型 + 推理等级」写回设置页的角色默认；切回默认时必须把这份默认
    强度一起读回来，否则设置页显示 High、实际却跑端点默认。
    """
    rt, rc, tctx = _runtime(tmp_path, "run-switch-c", meta={})
    tctx.orchestrator = _orchestrator(default_effort="high")
    try:
        steering.clear("run-switch-c")
        steering.push("run-switch-c", "", [], model={"profile_key": "", "effort": ""})

        _apply(rt)

        assert rt.ctx.model == "bound-model"
        assert rt.ctx.model_profile["params"]["reasoning_effort"] == "high"
        # 角色默认不是「本轮显式覆盖」：meta 保持干净，子agent 仍走自己的默认
        assert "reasoning_effort" not in rc.meta
    finally:
        steering.clear("run-switch-c")
