"""「按意图分流」回归测试：只检查/只排查 ≠ 动手改。

用户抱怨（2026-11-10）：「不管问什么都勘察后落地；我让他检查问题，他就该只检查，
我让他修，他才该检查完落地改」。旧实现里「检查/排查/诊断」等动词被当成动手指令，
导致：
- 过程刹车（_is_fix_task）刚开跑就催着改代码；
- 收尾守卫（_needs_action）因为「只勘察没落地」判 failed / 强制重做。

修法：新增 ``wants_inspect_only`` 识别「先查清楚」意图——
- 收尾/过程两道守卫都对它放行（只出结论，不逼着落地）；
- 编排器把这类 run 按 analyze（只读）跑，并显式提示「只查不改、给结论与建议」。

同时不能把「帮我修复/改好/解决」「检查完顺手改成 X」这类真·动手指令误伤成只读。
"""
from __future__ import annotations

import pytest

from game_agent.agents.runtime import _is_fix_task, wants_inspect_only
from game_agent.orchestration.orchestrator import Orchestrator, WRITE_TOOLS
from game_agent.orchestration.caps import effective_whitelist


# ---------------------------------------------------------------- wants_inspect_only
@pytest.mark.parametrize("text", [
    "帮我检查一下这个报错是什么原因",
    "检查一下登录为什么失败",
    "排查一下这个接口报错",
    "诊断一下卡顿的原因",
    "帮我找出登录失败的原因",
    "看看问题出在哪",
    "先排查一下再说",
    "只分析一下这个报错，不要改代码",
    "帮我查一下这个接口为什么报错",   # 口语化诊断
    "帮我看看这个 bug 在哪",
    "检查下这个报错",
])
def test_inspect_only_detected(text: str) -> None:
    assert wants_inspect_only(text), text


@pytest.mark.parametrize("text", [
    "帮我修复这个报错",
    "把登录失败的问题解决了",
    "改一下结算界面适配手机",
    "优化一下加载速度",
    "帮我排查一下，然后修复",     # 检查 + 明确修复 → 动手
    "检查一下并改成适配手机",     # 检查 + 改成 → 动手
    "帮我看看这个功能怎么加个按钮",  # 检查字样 + 加个 → 动手
    "给主界面加一个卡牌详情弹窗",
    "这个报错怎么回事",           # modify run 里问报错，默认仍是要弄好（只在收尾守卫豁免）
    "帮我看看为什么白屏",         # 既有语义：帮我看看 + 故障 → 动手（别误伤）
])
def test_imperative_fix_not_inspect_only(text: str) -> None:
    assert not wants_inspect_only(text), text


# ---------------------------------------------------------------- 两道守卫都放行
@pytest.mark.parametrize("text", [
    "帮我检查一下这个报错是什么原因",
    "检查一下登录为什么失败",
    "排查一下这个接口报错",
    "诊断一下卡顿的原因",
])
def test_check_only_does_not_arm_guards(text: str) -> None:
    """只检查：收尾守卫不逼落地、过程刹车不催改代码。"""
    assert not Orchestrator._needs_action(text), text
    assert not _is_fix_task(text), text


@pytest.mark.parametrize("text", [
    "帮我修复这个报错",
    "把登录失败的问题解决了",
    "改一下结算界面适配手机",
])
def test_fix_requests_still_arm_guards(text: str) -> None:
    assert Orchestrator._needs_action(text), text
    assert _is_fix_task(text), text


def test_symptom_complaint_still_acts() -> None:
    """「首页为啥还有滚动条」仍要动手，不能被新意图误判成只读。"""
    assert Orchestrator._needs_action("首页为啥还有滚动条")
    assert _is_fix_task("首页为啥还有滚动条")
    assert not wants_inspect_only("首页为啥还有滚动条")


# ---------------------------------------------------------------- 只读运行保障
def test_inspect_run_is_readonly() -> None:
    """只检查按 analyze 跑：写/生成/起服务类工具被剔除，读工具保留。"""
    role_tools = ["read_file", "grep", "write_file", "edit_file", "run_command",
                  "start_service", "generate_image"]
    wl = effective_whitelist(role_tools, "analyze", None)
    assert not (set(wl) & WRITE_TOOLS)
    assert {"read_file", "grep"} <= set(wl)


def test_inspect_hint_tells_model_not_to_edit() -> None:
    hint = Orchestrator._inspect_hint()
    assert "不要" in hint and "修改" in hint and "结论" in hint
