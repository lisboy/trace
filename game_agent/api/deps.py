"""FastAPI 依赖：解析出「本机身份」并按权限闸门。

这个版本没有登录：身份来自 ``game_agent.auth.local``（本机单用户），
请求里的 Cookie / ``Authorization`` 一律不看。``require(perm)`` 的签名保留，
是为了让路由层不用改——owner 拥有全部权限，闸门永远放行；
将来若要恢复多用户，只需让 :func:`get_current_context` 重新从凭证解析。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from game_agent.auth.local import ensure_local_identity
from game_agent.core.context import RequestContext
from game_agent.db.models import Project
from game_agent.db.session import SessionLocal


async def local_identity() -> tuple[str, str, str]:
    """(user_id, team_id, role)：本机唯一身份，库还没有就建（幂等）。"""
    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
        return str(user.id), str(team.id), "owner"


async def get_current_context(request: Request) -> RequestContext:
    """当前请求的身份：本机默认用户，无需任何凭证。"""
    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
        proj_rows = await s.execute(select(Project.id).where(Project.team_id == team.id))
        project_ids = [str(r[0]) for r in proj_rows.all()]
    return RequestContext(
        user_id=str(user.id), team_id=str(team.id), role="owner",
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


async def get_optional_context(request: Request) -> Optional[RequestContext]:
    """历史签名：给「签名 URL 自授权」的读接口用。现在恒有身份，等价于 get_current_context。"""
    try:
        return await get_current_context(request)
    except Exception:  # noqa: BLE001 - 拿不到身份也不该让静态读取 500
        return None


def _u(v):  # type: ignore[no-untyped-def]
    """宽松 UUID 转换：字符串能转就转，转不了原样返回（路由层大量使用）。"""
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _rid(request: Request) -> str:
    return request.headers.get("X-Request-Id") or "local"
