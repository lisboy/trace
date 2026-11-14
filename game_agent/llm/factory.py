"""LLM 工厂：按配置与角色选择大脑（设计 §2.4 统一适配层）。

优先级：Agent 绑定的 ModelProfile（经 ctx.model_profile 传入）> GA_ROLE_MODELS > GA_LLM_MODEL。
协议：profile["protocol"]（来自供应商）决定用哪套适配器——
  - anthropic → AnthropicMessagesLLM（/v1/messages 原生协议）
  - 其它/缺省 → OpenAICompatibleLLM（/v1/chat/completions，OpenAI 兼容）
档位 params_json 支持：context_window（上下文窗口 tokens）、vision（是否支持图片输入）、
reasoning_effort（推理强度：none/minimal/low/medium/high，空=不传走端点默认）、
max_tokens（仅 anthropic：单次回复上限）。
"""
from __future__ import annotations

import logging

from game_agent.config import (
    LLM_MAX_OUTPUT_TOKENS, LLM_MODEL, LLM_SUB_MAX_OUTPUT_TOKENS,
    LLM_SUB_REASONING_EFFORT, ROLE_MODELS,
)
from game_agent.llm.anthropic_compat import AnthropicMessagesLLM
from game_agent.llm.base import Ctx, LLM
from game_agent.llm.openai_compat import OpenAICompatibleLLM

log = logging.getLogger("game_agent.llm.factory")


def _effort_for(role: str, params: dict) -> str:
    """推理强度：档位显式配了就用档位的；否则 sub 角色走 GA_SUB_REASONING_EFFORT。

    sub（只读调研）实测把思考关掉后延迟 -32%、输出 token -61%，而它的产物只是
    「查到的证据 + 结论」，由主agent 复核——这是整条链上最划算的一档。
    主agent / 其它角色保持端点默认（不传），不替用户做质量取舍。
    """
    explicit = str(params.get("reasoning_effort") or "").strip().lower()
    if explicit:
        return explicit
    if role == "sub":
        return (LLM_SUB_REASONING_EFFORT or "").strip().lower()
    return ""


def build_llm(role: str, ctx: Ctx | None = None) -> LLM:
    """始终构建真实大模型适配器。没有 mock/离线演示模式：
    有绑定档位/供应商就走它，否则回退到 .env 全局兜底。"""
    if ctx is not None and isinstance(ctx.model_profile, LLM):
        # 测试/调用方直接注入了一个假大脑：原样返回（本仓库冒烟测试靠它离线跑整轮）。
        return ctx.model_profile
    profile = (ctx.model_profile if ctx else None) or {}
    params = profile.get("params") or {}
    model = profile.get("model") or ROLE_MODELS.get(role) or LLM_MODEL
    protocol = str(profile.get("protocol") or "openai").lower()
    if not (profile.get("api_key") or profile.get("api_key_ref")):
        # 档位没带 Key 时适配器回退到 OPENAI_API_KEY；两者都没有，请求注定 401/403。
        from game_agent.config import OPENAI_API_KEY
        if not OPENAI_API_KEY:
            log.warning("角色 %r 没有可用档位、.env 也未配 OPENAI_API_KEY："
                        "本轮请求会因缺鉴权失败（model=%r, base=%r）",
                        role, model, profile.get("base_url"))
    common = dict(
        role=role, model=model,
        api_key=(profile.get("api_key") or profile.get("api_key_ref")),
        base_url=profile.get("base_url"),
        context_window=int(params.get("context_window") or 0),
        vision=bool(params.get("vision")),
        user_agent=profile.get("user_agent"),
        reasoning_effort=_effort_for(role, params),
    )
    if protocol == "anthropic":
        return AnthropicMessagesLLM(max_tokens=int(params.get("max_tokens") or 0), **common)
    # 输出上限：档位显式配了就用它；否则 sub 角色默认走 LLM_SUB_MAX_OUTPUT_TOKENS
    # （只读调研，产出只是一份报告，长尾 60k tokens 的报告纯属浪费，见 config 注释），
    # 其余角色仍用全局 GA_LLM_MAX_TOKENS（默认 0 = 不设，避免截断代码写入/工具调用）。
    max_out = int(params.get("max_tokens") or 0)
    if not max_out:
        max_out = LLM_SUB_MAX_OUTPUT_TOKENS if role == "sub" else LLM_MAX_OUTPUT_TOKENS
    return OpenAICompatibleLLM(max_tokens=max_out, **common)
