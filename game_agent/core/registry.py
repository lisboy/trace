"""智能体注册表：团队级配置载入与初始化（设计 §4.1，配置粒度为团队级）。

5 个角色都是同一个 AgentRuntime 的实例，差异来自 system prompt / 工具白名单 / 模型档位 / 参数。
新增角色 = 插一行配置。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.agents.roles import ROLE_PROMPTS, ROLE_TOOLS
from game_agent.db.models import Agent


@dataclass
class AgentConfig:
    key: str
    system_prompt: str
    tool_whitelist: list[str]
    model_profile_key: Optional[str] = None
    params_json: dict = None  # type: ignore[assignment]


async def seed_default_agents(session: AsyncSession, team_id: str) -> None:
    """为新团队插入 5 个角色的默认配置（若不存在）。"""
    for key, prompt in ROLE_PROMPTS.items():
        existing = await session.scalar(
            select(Agent).where(Agent.team_id == _uuid(team_id), Agent.key == key)
        )
        if existing is None:
            session.add(
                Agent(
                    team_id=team_id,
                    key=key,
                    system_prompt=prompt,
                    model_profile_key=None,
                    params_json={},  # 只放媒体档位等扩展参数；执行次数不再有上限
                )
            )
    await session.commit()


def _uuid(v):  # type: ignore[no-untyped-def]
    """把 team_id 统一成 UUID。

    为什么必须转：agents.team_id 是 UUID 列，SQLAlchemy 的 UUID 处理器要求
    参数带 .hex；调用方（run_ctx.team_id / 编排器）传的是 str，直接进查询会抛
    "'str' object has no attribute 'hex'"。而 resolve_media_profile 用
    try/except 包了这段，异常被吞成"未绑定档位"——表现就是明明配好了音色，
    agent 配音却一直回退到静音占位。这里统一收口，别再让 str 漏进来。
    """
    import uuid

    if isinstance(v, uuid.UUID):
        return v
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


async def get_agent_config(session: AsyncSession, team_id: str, key: str) -> AgentConfig:
    row = await session.scalar(
        select(Agent).where(Agent.team_id == _uuid(team_id), Agent.key == key)
    )
    if row is None:
        # 兜底：用默认（理论上 seed 已建）
        return AgentConfig(
            key=key,
            system_prompt=ROLE_PROMPTS.get(key, ""),
            tool_whitelist=ROLE_TOOLS.get(key, []),
            model_profile_key=None,
            params_json={},
        )
    return AgentConfig(
        key=row.key,
        system_prompt=row.system_prompt,
        tool_whitelist=ROLE_TOOLS.get(row.key, []),
        model_profile_key=row.model_profile_key,
        params_json=row.params_json or {},
    )
