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

from game_agent.config import LLM_MODEL, ROLE_MODELS
from game_agent.llm.anthropic_compat import AnthropicMessagesLLM
from game_agent.llm.base import Ctx, LLM
from game_agent.llm.openai_compat import OpenAICompatibleLLM

log = logging.getLogger("game_agent.llm.factory")


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
        reasoning_effort=str(params.get("reasoning_effort") or ""),
    )
    if protocol == "anthropic":
        return AnthropicMessagesLLM(max_tokens=int(params.get("max_tokens") or 0), **common)
    return OpenAICompatibleLLM(**common)
