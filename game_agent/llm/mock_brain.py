"""脚本化假大脑 —— **仅用于自动化测试**（生产环境不存在此路径）。

不调用任何外部 LLM，按角色产出脚本化的 tool_calls，让全链路在零 key、零网络下真实跑通。
本仓库 `tests/conftest.py` 会把 build_llm 打桩成返回它，从而离线跑完整轮编排。
生产代码永远走真实大模型（openai_compat / anthropic_compat），没有「演示模式」开关。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from game_agent.llm.base import Ctx, LLM, LLMResponse, ToolCall


def _tc(name: str, **args: Any) -> ToolCall:  # type: ignore[no-untyped-def]
    return ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)


_FEATURE_MODULE = "feature_agent.py"
_FEATURE_TEST = "test_feature_agent.py"

# 演示思考过程：按角色 × 轮次的口播（真实模型走 reasoning_content）
_DEMO_REASONING: dict[str, list[str]] = {
    "main": [
        "先看工程结构：这是卡牌游戏的 Python 工程，需求要落到可运行的代码上。",
        "自己动手改：新增示例模块与测试，同时直接调 generate_image 出横幅图。",
        "代码已写入，跑一遍 pytest 确认能过。",
        "全部验证通过，汇总改动与产物路径向用户汇报。",
    ],
}


def _feature_module(requirement: str) -> str:
    rq = requirement.replace('"', "'")
    return f'''"""由 循迹（Trace） 在本次 run 中生成的示例模块（需求：{rq}）。

证明模型能在独立 git 分支上产生可运行、可测试的代码。
"""

from __future__ import annotations


def agent_feature_summary(items: list[str]) -> str:
    """把若干条目汇总成一段文本。"""
    if not items:
        return ""
    return "；".join(items)


def rarity_tier(score: int) -> str:
    """示例：把分数映射到稀有度档位。"""
    if score >= 90:
        return "legendary"
    if score >= 70:
        return "epic"
    if score >= 40:
        return "rare"
    return "common"
'''


def _feature_test() -> str:
    return '''from feature_agent import agent_feature_summary, rarity_tier


def test_summary():
    assert agent_feature_summary(["a", "b"]) == "a；b"
    assert agent_feature_summary([]) == ""


def test_rarity_tier():
    assert rarity_tier(95) == "legendary"
    assert rarity_tier(75) == "epic"
    assert rarity_tier(50) == "rare"
    assert rarity_tier(10) == "common"
'''


def _build_report(ctx: Ctx) -> str:
    return (
        f"## ✅ 已完成：{ctx.requirement or '需求'}\n\n"
        f"**本次目标**：{ctx.requirement or '完成用户需求'}\n\n"
        "**代码改动**（2 个文件，新增于独立分支）\n"
        f"| 文件 | 说明 |\n|---|---|\n| `{_FEATURE_MODULE}` | 新增示例功能模块 |\n"
        f"| `{_FEATURE_TEST}` | 新增单元测试 |\n\n"
        "**新增素材（1 张，⚠️ 占位）**\n"
        "- `assets/art/ui/ui_feature_banner_v1.png` — 512×256\n\n"
        "**验证证据**：`python -m pytest test_feature_agent.py -q` → 2 passed\n\n"
        "**⚠️ 占位素材说明**：未配置图像供应商，到「供应商配置」填 key 后"
        "在素材库「批量重画」，文件名不变，代码无需改动。\n\n"
        "**注意事项**：分支待合并，请 admin 审阅 diff。"
    )


class MockBrain(LLM):
    def __init__(self, role: str) -> None:
        self.role = role
        self._round = 0

    async def chat(self, messages: list[dict], tools: list, ctx: Ctx,
                   on_delta=None, summarizer=None, *, timeout_s: int | None = None,
                   max_attempts: int | None = None) -> LLMResponse:
        self._round += 1
        handler = getattr(self, f"_r_{self.role}", None)
        if handler is None:
            return LLMResponse(text=f"[{self.role}] 演示大脑未实现该角色脚本。")
        usage = {"prompt_tokens": 200, "completion_tokens": 80}
        resp = await handler(messages, ctx, usage)
        # 演示用模拟延迟（不真实 sleep）：让速度统计有数据可看
        resp.latency_ms = 300 + self._round * 100
        steps = _DEMO_REASONING.get(self.role) or ["分析当前任务…"]
        resp.reasoning = steps[min(self._round - 1, len(steps) - 1)]
        if ctx.images:
            resp.reasoning = f"已接收 {len(ctx.images)} 张用户附图，结合图片内容分析。\n" + resp.reasoning
        return resp

    # ---------------- main（唯一角色：自己干，媒体直连）----------------
    async def _r_main(self, messages, ctx, usage):  # type: ignore[no-untyped-def]
        if ctx.run_type == "analyze":
            return LLMResponse(
                text=f"【分析】项目 {ctx.project_name} 位于 {ctx.workspace}。"
                     f"这是一个 Python 项目。针对你的问题「{ctx.requirement}」："
                     "当前结构包含一个最小的卡牌数据模型与单测。如需改动代码，"
                     "请取消「只分析」后发起 modify 任务。",
                usage=usage,
            )
        if self._round == 1:
            # 先读后写（write_file 有「未读不许写」护栏）
            return LLMResponse(tool_calls=[
                _tc("list_dir", path="."),
                _tc("read_file", path=_FEATURE_MODULE),
                _tc("read_file", path=_FEATURE_TEST),
            ], usage=usage)
        if self._round == 2:
            return LLMResponse(tool_calls=[
                _tc("write_file", path=_FEATURE_MODULE, content=_feature_module(ctx.requirement)),
                _tc("write_file", path=_FEATURE_TEST, content=_feature_test()),
                _tc("generate_image", prompt="功能展示横幅，厚涂赛璐璐 冷色调",
                    size="512x256", transparent=False, category="ui",
                    slug="ui_feature_banner", count=1),
            ], usage=usage)
        if self._round == 3:
            return LLMResponse(tool_calls=[
                _tc("run_command", command="python -m pytest test_feature_agent.py -q"),
            ], usage=usage)
        if self._round == 4:
            return LLMResponse(tool_calls=[
                _tc("report_to_user", report=_build_report(ctx)),
            ], usage=usage)
        # 终轮：返回汇报文本（工具已记录）
        return LLMResponse(text=_build_report(ctx), usage=usage)
