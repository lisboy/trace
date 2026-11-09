"""解析「团队 main 角色绑定的对话档位」→ Ctx.model_profile，供工作台类的
纯文本辅助功能（提示词增强/补全、文案润色）复用。

为什么需要它：这些功能以前直接 ``build_llm("main")``（不传 ctx），会回退到 .env
全局端点；而全局订阅常已失效（403 SUBSCRIPTION_NOT_FOUND），只有聊天能用是因为
它走的是团队绑定的档位。这里把 project_memory._main_profile 的取值链路抽出来公用：
Agent.model_profile_key → ModelProfile →（provider_id）供应商 base_url/api_key
（档位 inline 优先）。拿不到就返回 {}，调用方再让 build_llm 回退全局默认。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from game_agent.db.models import Agent, LlmProvider, ModelProfile
from game_agent.db.session import SessionLocal
from game_agent.logging_setup import get_logger

log = get_logger("game_agent.llm.team_profile")


def _u(v):  # type: ignore[no-untyped-def]
    try:
        return uuid.UUID(str(v))
    except Exception:  # noqa: BLE001
        return v


async def team_main_profile(team_id: str) -> dict:
    """返回 {model, base_url, api_key, protocol, user_agent, params}；取不到返回 {}。"""
    if not team_id:
        return {}
    try:
        async with SessionLocal() as s:
            agent = await s.scalar(select(Agent).where(
                Agent.team_id == _u(team_id), Agent.key == "main"))
            profile_key = agent.model_profile_key if agent else None
            if not profile_key:
                return {}
            p = await s.scalar(select(ModelProfile).where(
                ModelProfile.team_id == _u(team_id), ModelProfile.key == profile_key))
            if p is None:
                return {}
            params = p.params_json or {}
            base_url, api_key, user_agent = p.base_url, None, None
            protocol = p.provider or "openai"
            if p.provider_id:
                pv = await s.get(LlmProvider, p.provider_id)
                if pv is not None and pv.enabled:
                    base_url = base_url or pv.base_url
                    api_key = pv.api_key
                    user_agent = pv.user_agent
                    protocol = pv.protocol or protocol
            return {
                "model": p.model, "base_url": base_url,
                "api_key": params.get("api_key") or p.api_key_ref or api_key,
                "protocol": protocol, "user_agent": user_agent, "params": params,
            }
    except Exception as e:  # noqa: BLE001
        log.warning("解析团队 main 档位失败，回退全局默认：%s", e)
        return {}
