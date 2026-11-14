"""每步推理强度 + 输出上限的回归（2026-11-14）。

- OpenAI 兼容适配器：档位配了 max_tokens 才下发；reasoning_effort 支持「本轮 override
  优先、否则用档位默认」。
- 工厂：档位 params.max_tokens 会透传到 OpenAI 适配器（此前只有 anthropic 生效）。
- runtime：长 run 后段按轮次下调档位的阶梯（GA_REASONING_STEP_DOWN_AFTER）。
"""
from __future__ import annotations

import asyncio

from game_agent.llm.base import Ctx, LLMResponse
from game_agent.llm.factory import build_llm
from game_agent.llm.openai_compat import OpenAICompatibleLLM


def _capture(llm: OpenAICompatibleLLM) -> dict:
    cap: dict = {}

    async def fake_request(body, headers, on_delta, t0, on_retry=None,
                           timeout_s=None, max_attempts=None):
        cap["body"] = body
        return LLMResponse(text="ok")

    llm._request = fake_request  # type: ignore[assignment]
    return cap


def test_max_tokens_only_sent_when_configured() -> None:
    off = OpenAICompatibleLLM("main", model="m", api_key="k", base_url="http://x/v1")
    cap = _capture(off)
    asyncio.run(off.chat([{"role": "user", "content": "hi"}], [], Ctx()))
    assert "max_tokens" not in cap["body"]

    onn = OpenAICompatibleLLM("main", model="m", api_key="k", base_url="http://x/v1",
                              max_tokens=2048)
    cap = _capture(onn)
    asyncio.run(onn.chat([{"role": "user", "content": "hi"}], [], Ctx()))
    assert cap["body"]["max_tokens"] == 2048


def test_reasoning_effort_override_beats_configured() -> None:
    llm = OpenAICompatibleLLM("main", model="m", api_key="k", base_url="http://x/v1",
                              reasoning_effort="high")
    cap = _capture(llm)
    ctx = Ctx()
    ctx.reasoning_effort_override = "low"
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], [], ctx))
    assert cap["body"]["reasoning_effort"] == "low"

    ctx.reasoning_effort_override = ""
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], [], ctx))
    assert cap["body"]["reasoning_effort"] == "high"


def test_factory_passes_max_tokens_for_openai() -> None:
    ctx = Ctx(model_profile={"model": "m", "base_url": "http://x/v1", "api_key": "k",
                             "params": {"max_tokens": 1234}})
    llm = build_llm("main", ctx)
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.max_tokens == 1234


def test_runtime_effort_ladder() -> None:
    from game_agent.agents import runtime as R
    old = R.LLM_REASONING_STEP_DOWN_AFTER
    try:
        R.LLM_REASONING_STEP_DOWN_AFTER = 10

        class D:
            llm = type("L", (), {"reasoning_effort": "high"})()

        d = D()
        assert R.AgentRuntime._round_effort_override(d, 9) == ""
        assert R.AgentRuntime._round_effort_override(d, 10) == "medium"
        assert R.AgentRuntime._round_effort_override(d, 20) == "low"
        assert R.AgentRuntime._round_effort_override(d, 30) == "minimal"
        # 未配 reasoning_effort（走端点默认）时一律不动
        class E:
            llm = type("L", (), {"reasoning_effort": ""})()
        assert R.AgentRuntime._round_effort_override(E(), 50) == ""
    finally:
        R.LLM_REASONING_STEP_DOWN_AFTER = old


def test_norm_effort_only_accepts_known_levels() -> None:
    """角色默认推理等级的归一（2026-11-14）：脏值/「Default」都要当成「不指定」。

    设置页与输入框都会往 agents.params_json.reasoning_effort 写值；写进空串表示
    「走端点默认」，此时绝不能把 '' 当档位透传给端点。
    """
    from game_agent.orchestration.orchestrator import REASONING_EFFORTS, _norm_effort

    assert _norm_effort("High") == "high"
    assert _norm_effort(" medium ") == "medium"
    assert _norm_effort("") == ""
    assert _norm_effort(None) == ""
    assert _norm_effort("Default") == ""
    assert _norm_effort("max") == ""
    assert set(REASONING_EFFORTS) == {"none", "minimal", "low", "medium", "high"}
