"""运行上下文：认证上下文（贯穿日志/审计）与单次 run 的执行上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from game_agent.core.abstractions import (
    EventBus,
    Fencing,
    LockManager,
    ObjectStore,
    Repository,
)


@dataclass
class RequestContext:
    """认证上下文（设计 §3.4）。所有查询强制用 team_id 过滤，不收前端传值。"""
    user_id: str
    team_id: str
    role: str  # owner|admin|developer|viewer
    project_ids: list[str] = field(default_factory=list)
    request_id: str = ""
    ip: Optional[str] = None
    user_agent: Optional[str] = None

    def can(self, perm: str) -> bool:
        from game_agent.auth.rbac import check_permission
        return check_permission(self.role, perm)


@dataclass
class RunContext:
    """单次 run 的执行上下文，注入到 AgentRuntime / 工具 / 编排器。"""
    run_id: str
    team_id: str
    project_id: str
    session_id: str
    triggered_by: str
    run_type: str  # modify|analyze
    worker_id: str
    repo: Repository
    bus: EventBus
    locks: LockManager
    store: ObjectStore
    fencing: Optional[Fencing] = None
    payload: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    # 用户本轮的原话（不含系统注入的提示/历史）。工具层的路由硬规则要靠它判断
    # 「用户是否明确要求走某条特殊路径」，例如「按之前 python 脚本方式生成配音」。
    user_request: str = ""
