"""回归测试：意图盘查**保留判别、但只当提示**，绝不作闸门（2026-11-14 用户拍板）。

背景（用户原话）：
「是不是可以调整下，不需要什么意图识别，工具权限都开放，然后由模型来当作大脑驱动就行了，
应该做什么是不是应该编辑都是模型来按照用户需要思考就行」
→ 用户随后在方案里选了 B：**保留判别但不作为闸门，只当提示，模型有最终决定权**。

因此本文钉死这几条不变式：
1. 盘查仍会真的判别（关键词短路 + 拿不准走模型），结论有 modify / survey 两种；
2. 盘查结论**只换一句软提示**（`hint_for`），提示里绝不出现「不许修改/本轮禁止」这类禁手词；
3. 编排器**不再**因为 survey 把 run_type 降级成 analyze——只有用户亲手点「仅分析」才走 analyze；
4. 工具白名单不随轮次类型裁剪（analyze 也给全量工具），只有用户手动能力开关才裁剪；
5. `engage` 只对 modify 开启「只勘察不动手」刹车；纯问答轮次不被催着改文件。
"""
from __future__ import annotations

import asyncio

import pytest

from game_agent.orchestration.intent import (IntentResult, classify_intent,
                                             guess_mode, hint_for)
from game_agent.orchestration.caps import effective_whitelist


# ---------------------------------------------------------------- 盘查仍真的判别
def test_classify_keyword_shortcircuits_survey() -> None:
    """纯勘察措辞（无动手动词）→ survey，且由关键词短路、不调模型。"""
    res = asyncio.run(classify_intent("检查一下登录为什么失败，给出结论就行"))
    assert res.intent == "survey" and res.decided_by == "keyword"


def test_classify_keyword_shortcircuits_modify() -> None:
    """明确动手动词 → modify（「移除…导入…」曾是事故根因，必须判动手）。"""
    res = asyncio.run(classify_intent(
        "PmsHistoryBatch 给我移除所有和这个表相关的内容，导入历史项目"))
    assert res.intent == "modify" and res.decided_by == "keyword"


def test_classify_ambiguous_defers_to_model(monkeypatch) -> None:
    """带否定词（先别改）时拿不准，仍交给模型推理。"""
    from game_agent.llm.base import LLM, LLMResponse
    from game_agent.orchestration import intent as intent_mod

    seen: list[str] = []

    class _Fake(LLM):
        async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None, **_):
            seen.append(messages[-1]["content"])
            return LLMResponse(text='{"intent":"survey","mode":"build","reason":"先别改"}')

    monkeypatch.setattr(intent_mod, "build_llm", lambda role, ctx=None: _Fake())
    res = asyncio.run(classify_intent("先别改，先看看问题出在哪"))
    assert res.intent == "survey" and res.decided_by == "model"
    assert seen  # 确实调了模型


def test_classify_falls_back_to_modify_on_error(monkeypatch) -> None:
    """模型不可用 → 兜底 modify（绝不让模型以为本轮被禁止动手）。"""
    from game_agent.llm.base import LLM
    from game_agent.orchestration import intent as intent_mod

    class _Boom(LLM):
        async def chat(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(intent_mod, "build_llm", lambda role, ctx=None: _Boom())
    res = asyncio.run(classify_intent("先别改，先看看问题出在哪"))
    assert res.intent == "modify" and res.decided_by == "fallback"


@pytest.mark.parametrize("text,want", [
    ("这个接口报错了，修掉", "fix"),
    ("加一个导出按钮", "build"),
    ("", "build"),
])
def test_guess_mode_only_affects_wording(text, want) -> None:
    """mode 只影响提示措辞，不影响权限；这里钉住它的取值口径。"""
    assert guess_mode(text) == want


def test_intent_result_flags() -> None:
    modify = IntentResult(intent="modify", mode="fix")
    assert modify.is_survey is False and modify.is_fix is True
    survey = IntentResult(intent="survey", mode="build")
    assert survey.is_survey is True and survey.is_fix is False


# ---------------------------------------------------------------- 只当提示：不出现禁手词
_FORBIDDEN = ("不许", "禁止", "不要修改", "不得修改", "只读")


@pytest.mark.parametrize("res", [
    IntentResult(intent="survey", mode="build"),
    IntentResult(intent="modify", mode="fix"),
    IntentResult(intent="modify", mode="build"),
])
def test_hint_is_soft_never_forbids(res: IntentResult) -> None:
    """任何盘查结论生成的提示都只能是「参考」，不能出现禁手措辞。"""
    hint = hint_for(res)
    for bad in _FORBIDDEN:
        assert bad not in hint, f"提示里出现了禁手词 {bad!r}：{hint}"


def test_survey_hint_still_lets_model_act() -> None:
    """survey 的软提示必须明确「你自己判断，要改就改」，否则等于变相闸门。"""
    hint = hint_for(IntentResult(intent="survey", mode="build"))
    assert "你自己判断" in hint
    assert "直接动手改" in hint


def test_build_modify_hint_is_silent() -> None:
    """新建/生成类（modify+build）不发提示，省上下文。"""
    assert hint_for(IntentResult(intent="modify", mode="build")) == ""


def test_explicit_analyze_has_no_soft_hint() -> None:
    """用户亲手点「仅分析」时，软提示让位给 _inspect_hint（避免两段话打架）。"""
    assert hint_for(IntentResult(intent="survey", mode="build",
                                 decided_by="explicit")) == ""


# ---------------------------------------------------------------- 工具白名单
def test_analyze_run_keeps_full_toolset() -> None:
    """轮次类型不裁工具：analyze 也给全量角色工具，用什么由模型自己判断。"""
    role_tools = ["read_file", "grep", "write_file", "edit_file", "run_command",
                  "start_service", "generate_image"]
    wl = effective_whitelist(role_tools, "analyze", None)
    assert set(wl) == set(role_tools)


def test_caps_still_filter_tools() -> None:
    """用户手动的能力开关仍然硬裁剪（关掉联网就不给 web 工具）。"""
    role_tools = ["read_file", "web_search", "web_fetch", "generate_image"]
    wl = effective_whitelist(role_tools, "modify", {"web": False})
    assert "web_search" not in wl and "web_fetch" not in wl
    assert {"read_file", "generate_image"} <= set(wl)


# ---------------------------------------------------------------- 显式「仅分析」仍生效
def test_inspect_hint_only_for_explicit_analyze() -> None:
    """_inspect_hint 仍保留，但只由用户显式的「仅分析」触发，且措辞点明是用户指定。"""
    from game_agent.orchestration.orchestrator import Orchestrator

    hint = Orchestrator._inspect_hint()
    assert "仅分析" in hint
    assert "不要" in hint and "修改" in hint and "结论" in hint


# ---------------------------------------------------------------- runtime 侧接线
def test_engage_follows_intent_not_hardcoded() -> None:
    """engage 必须由盘查结论决定，不能像半成品那样写死 True。

    纯问答（survey）不该被催着改文件；要动手（modify）才装刹车。
    """
    import inspect as _inspect

    import game_agent.orchestration.orchestrator as orch

    src = _inspect.getsource(orch)
    assert '"engage": intent.intent == "modify"' in src
    assert '"engage": True,' not in src
