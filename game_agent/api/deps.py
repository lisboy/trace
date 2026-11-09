"""FastAPI 依赖：从 Cookie / PAT 解析出 RequestContext，并按权限闸门。"""
from __future__ import annotations

from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.api.state import state
from game_agent.auth.rbac import check_permission
from game_agent.core.context import RequestContext
from game_agent.db.models import Team, TeamMember, User
from game_agent.db.session import get_session


async def get_current_context(
    request: Request,
    ga_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> RequestContext:
    auth = state.auth
    user: Optional[User] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
        user = await auth.get_user_by_pat(token)
    elif ga_session:
        user = await auth.get_user_by_session(ga_session)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已禁用")

    team_id = request.headers.get("X-Team-Id")
    # 取成员关系：指定 team 或默认第一个
    async with state.session_factory() as s:
        if team_id:
            tm = await s.scalar(select(TeamMember).where(
                TeamMember.team_id == _u(team_id), TeamMember.user_id == user.id))
        else:
            tm = await s.scalar(select(TeamMember).where(TeamMember.user_id == user.id))
        if tm is None:
            raise HTTPException(status_code=403, detail="不在任何团队")
        role = tm.role
        team = await s.get(Team, tm.team_id)
        # 项目可见性：本团队全部项目
        from game_agent.db.models import Project
        proj_rows = await s.execute(select(Project.id).where(Project.team_id == tm.team_id))
        project_ids = [str(r[0]) for r in proj_rows.all()]
    return RequestContext(
        user_id=str(user.id), team_id=str(tm.team_id), role=role,
        project_ids=project_ids, request_id=_rid(request),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def require(perm: str):
    async def _dep(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not ctx.can(perm):
            raise HTTPException(status_code=403, detail=f"权限不足：需要 {perm}")
        return ctx
    return _dep


async def get_optional_context(
    request: Request,
    ga_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> Optional[RequestContext]:
    """有登录态就返回 RequestContext，没有则返回 None（不抛 401）。

    用于「签名 URL 自授权」的读接口：<img> 可能带不上任何 cookie/头，此时靠 URL 签名放行；
    若恰好有登录态，则额外做团队权限校验（双保险）。"""
    try:
        return await get_current_context(request, ga_session, authorization)
    except HTTPException:
        return None


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _rid(request: Request) -> str:
    return request.headers.get("X-Request-Id") or "local"
