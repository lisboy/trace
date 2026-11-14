"""本机资料：昵称 / 头像 / 工作区信息。

这个版本**没有登录、没有账号密码**——所以这里也没有 login / logout / setup /
改密码这些接口。留下的只是「界面怎么称呼我」这类纯展示偏好（顶栏头像与昵称），
它们跟身份校验无关，不会拦住任何操作。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import get_current_context
from game_agent.core.context import RequestContext
from game_agent.db.models import Team, TeamMember, User
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api/auth", tags=["profile"])


def _u(v):  # type: ignore[no-untyped-def]
    import uuid

    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _user_payload(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "display_name": u.display_name,
            "avatar": u.avatar or None, "is_active": u.is_active, "role": None}


@router.get("/me")
async def me(ctx: RequestContext = Depends(get_current_context)):
    """本机身份 + 工作区（个人版恒为一个）。前端用它渲染顶栏昵称/头像。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        teams = await s.execute(
            select(Team.id, Team.name, Team.slug, TeamMember.role)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == _u(ctx.user_id))
        )
        team_list = [{"id": str(t[0]), "name": t[1], "slug": t[2], "role": t[3]} for t in teams.all()]
    return {"user": _user_payload(user), "teams": team_list, "current_team": ctx.team_id}


class ProfileIn(BaseModel):
    display_name: str | None = None
    avatar: str | None = None


@router.patch("/me")
async def update_me(body: ProfileIn, ctx: RequestContext = Depends(get_current_context)):
    """改昵称 / 头像地址（头像文件走 /api/auth/me/avatar 上传）。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="本机身份不存在")
        if body.display_name is not None:
            name = (body.display_name or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="名称不能为空")
            if len(name) > 120:
                raise HTTPException(status_code=400, detail="名称不能超过 120 个字符")
            user.display_name = name
        if body.avatar is not None:
            user.avatar = (body.avatar or "").strip() or None
        await s.commit()
        await s.refresh(user)
        payload = _user_payload(user)
    return {"ok": True, "user": payload}


_AVATAR_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MAX_AVATAR_BYTES = 8 * 1024 * 1024


@router.post("/me/avatar")
async def upload_avatar(file: UploadFile, ctx: RequestContext = Depends(get_current_context)):
    """上传头像（图片，≤8MB）。返回可直接用于 <img src> 的 /uploads/avatars/... 地址。"""
    import time
    from pathlib import Path as _Path
    from game_agent.config import DATA_DIR

    avatars = DATA_DIR / "uploads" / "avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    ext = _Path(file.filename or "").suffix.lower()
    ctype = (file.content_type or "").lower()
    if ext not in _AVATAR_EXTS:
        if ctype.startswith("image/"):
            ext = ".png"
        else:
            raise HTTPException(status_code=400, detail="只支持图片文件（png/jpg/webp/gif）")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="头像不能超过 8MB")
    uid = str(ctx.user_id).replace("-", "")[:16]
    # 文件名带时间戳：同 URL 才不会被浏览器/静态缓存住旧图
    name = f"avatar_{uid}_{int(time.time())}{ext}"
    (avatars / name).write_bytes(data)
    url = f"/uploads/avatars/{name}"
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is not None:
            user.avatar = url
            await s.commit()
    return {"ok": True, "avatar": url}
