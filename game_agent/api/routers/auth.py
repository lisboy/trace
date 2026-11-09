"""认证路由：密码登录 / 会话 / PAT / 首启 / OIDC 配置口（设计 §3.4）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import get_current_context
from game_agent.api.state import state
from game_agent.auth.rbac import PERM_MEMBER
from game_agent.config import AUTH_MODE
from game_agent.core.context import RequestContext
from game_agent.db.session import SessionLocal
from game_agent.db.models import AuditLog, Team, TeamMember, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _u(v):  # type: ignore[no-untyped-def]
    import uuid

    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _ip(request: Request | None) -> str | None:
    return request.client.host if request and request.client else None


def _ua(request: Request | None) -> str | None:
    return request.headers.get("user-agent") if request else None


def _user_payload(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "display_name": u.display_name,
            "avatar": u.avatar or None,
            "is_active": u.is_active, "role": None}


async def require_member(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
    if not ctx.can(PERM_MEMBER):
        raise HTTPException(status_code=403, detail="权限不足")
    return ctx


class LoginForm(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(email: str = Form(...), password: str = Form(...), request: Request = None):
    if AUTH_MODE == "oidc":
        raise HTTPException(status_code=400, detail="当前仅允许 OIDC 登录")
    auth = state.auth
    user = await auth.authenticate(email, password, ip=_ip(request))
    if user is None:
        # 失败也记一条：多人团队里"谁在试密码"只有在这里留痕才查得到
        await _audit_login(email, "auth.login_failed", request)
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    token = await auth.create_session(str(user.id), _ip(request), _ua(request))
    pat = await auth.create_pat(str(user.id), "web-session", scopes=["run:create", "run:read", "view", "download", "comment"])
    await _audit_login(email, "auth.login", request, user_id=str(user.id))
    resp = JSONResponse(content={**_user_payload(user), "access_token": pat})
    resp.set_cookie("ga_session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


async def _audit_login(email: str, action: str, request: Request,
                       user_id: str | None = None) -> None:
    """登录痕迹写入审计：team_id 取该用户第一个团队，匿名失败的尝试挂不到任何团队下。"""
    try:
        async with SessionLocal() as s:
            team_id = None
            if user_id:
                team_id = await s.scalar(select(TeamMember.team_id).where(
                    TeamMember.user_id == _u(user_id)).order_by(TeamMember.joined_at))
            elif email:
                u = await s.scalar(select(User).where(
                    User.email == email.strip().lower()))
                if u is not None:
                    team_id = await s.scalar(select(TeamMember.team_id).where(
                        TeamMember.user_id == u.id).order_by(TeamMember.joined_at))
            s.add(AuditLog(team_id=team_id, actor_id=_u(user_id) if user_id else None,
                           action=action, target=(email or "").strip().lower() or None,
                           ip=_ip(request), user_agent=_ua(request), detail_json={}))
            await s.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能挡登录
        pass


@router.post("/logout")
async def logout(ga_session: str | None = None):
    if ga_session:
        await state.auth.revoke_session(ga_session)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie("ga_session")
    return resp


@router.get("/me")
async def me(ctx: RequestContext = Depends(get_current_context)):
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
    """修改个人资料：显示名称 / 头像地址（头像文件走 /api/auth/me/avatar 上传）。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
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


class PasswordIn(BaseModel):
    old_password: str = ""
    new_password: str


@router.post("/me/password")
async def change_password(body: PasswordIn, ctx: RequestContext = Depends(get_current_context)):
    """修改账号密码。OIDC-only 账号（无本地密码）可直接设置新密码。"""
    from game_agent.auth.passwords import hash_password, verify_password

    new = body.new_password or ""
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    if len(new) > 200:
        raise HTTPException(status_code=400, detail="新密码过长")
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if user.password_hash:
            if not verify_password(body.old_password or "", user.password_hash):
                raise HTTPException(status_code=400, detail="当前密码不正确")
        user.password_hash = hash_password(new)
        user.failed_logins = 0
        user.locked_until = None
        await s.commit()
    return {"ok": True}


@router.get("/status")
async def status():
    """公开的初始化状态：登录页据此区分「首次使用」与「已有账号」。
    全新安装（0 个账号）不应预填/引导一个并不存在的默认账号，否则只会得到
    「邮箱或密码错误」——直接引导到 /setup 用启动日志里的引导 token 建首个 owner。"""
    auth = state.auth
    count = await auth.count_users()
    return {"initialized": count > 0, "has_bootstrap_token": bool(state.bootstrap_token)}


class SetupForm(BaseModel):
    token: str = ""
    email: str
    display_name: str
    password: str


@router.post("/setup")
async def setup(form: SetupForm):
    auth = state.auth
    if await auth.count_users() > 0:
        raise HTTPException(status_code=400, detail="已初始化，不能重复创建 owner")
    if state.bootstrap_token and form.token != state.bootstrap_token:
        raise HTTPException(status_code=403, detail="引导 token 不正确")
    user = await auth.bootstrap_owner(form.email, form.display_name, form.password)
    token = await auth.create_session(str(user.id), None, None)
    pat = await auth.create_pat(str(user.id), "web-session", scopes=["run:create", "run:read", "view", "download", "comment"])
    resp = JSONResponse(content={**_user_payload(user), "access_token": pat})
    resp.set_cookie("ga_session", token, httponly=True, samesite="lax")
    return resp


@router.get("/oidc/providers")
async def oidc_providers(ctx: RequestContext = Depends(require_member)):
    # OIDC provider 配置存于 settings；lite 默认空。M1 配置口。
    async with SessionLocal() as s:
        from sqlalchemy import select as _select
        from game_agent.db.models import Settings
        row = await s.scalar(_select(Settings).where(Settings.key == "oidc_providers"))
        providers = row.value_json.get("items", []) if row else []
    return {"providers": providers}
