"""应用级单例状态：会话工厂、5 接口服务、编排器。

main 启动时装配；router 通过本模块访问，避免循环依赖。

这个版本没有登录/会话/PAT，所以这里没有 auth / bootstrap_token 这类字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker

from game_agent.core.lite.container import CoreServices
from game_agent.orchestration.orchestrator import Orchestrator


@dataclass
class AppState:
    session_factory: Optional[async_sessionmaker] = None
    services: Optional[CoreServices] = None
    orchestrator: Optional[Orchestrator] = None
    worker_id: str = "lite-1"
    frontend_dir: Optional[str] = None


state = AppState()
