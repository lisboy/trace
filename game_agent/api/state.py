"""应用级单例状态：会话工厂、5 接口服务、认证服务、编排器。

main 启动时装配；router 通过本模块访问，避免循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker

from game_agent.auth.service import AuthService
from game_agent.core.lite.container import CoreServices
from game_agent.orchestration.orchestrator import Orchestrator


@dataclass
class AppState:
    bootstrap_token: str = ""
    session_factory: Optional[async_sessionmaker] = None
    services: Optional[CoreServices] = None
    auth: Optional[AuthService] = None
    orchestrator: Optional[Orchestrator] = None
    worker_id: str = "lite-1"
    frontend_dir: Optional[str] = None


state = AppState()
