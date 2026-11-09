"""认证服务：密码登录、会话 Cookie、PAT、OIDC 账号绑定（设计 §3.4）。

首启引导：users 为空时打印一次性引导 token，访问 /setup 创建第一个 owner。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from game_agent.auth.passwords import hash_password, verify_password
from game_agent.db.models import ApiToken, AuthSession, Team, TeamMember, User
from game_agent.logging_setup import get_logger

log = get_logger("auth")

SESSION_TTL_DAYS = 30
PAT_TTL_DAYS = 365


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _aware(dt: datetime) -> datetime:
    """SQLite 会返回 naive datetime；统一按 UTC 处理后再比较。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _gen_token(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


class AuthService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ---------- 用户 ----------
    async def get_user_by_email(self, email: str) -> Optional[User]:
        async with self._sf() as s:
            return await s.scalar(select(User).where(User.email == email.lower()))

    async def get_user(self, user_id: str) -> Optional[User]:
        async with self._sf() as s:
            return await s.get(User, _u(user_id))

    async def count_users(self) -> int:
        async with self._sf() as s:
            from sqlalchemy import func
            return (await s.scalar(select(func.count()).select_from(User))) or 0

    # ---------- 密码登录 ----------
    async def authenticate(self, email: str, password: str,
                           ip: Optional[str] = None) -> Optional[User]:
        user = await self.get_user_by_email(email)
        if user is None or not user.is_active:
            return None
        # 锁定判断
        if user.locked_until and _aware(user.locked_until) > datetime.now(timezone.utc):
            return None
        if not user.password_hash or not verify_password(password, user.password_hash):
            async with self._sf() as s:
                u = await s.get(User, user.id)
                u.failed_logins += 1
                if u.failed_logins >= 5:
                    u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
                await s.commit()
            return None
        async with self._sf() as s:
            u = await s.get(User, user.id)
            u.failed_logins = 0
            u.locked_until = None
            u.last_login_at = datetime.now(timezone.utc)
            await s.commit()
        return user

    # ---------- 会话 ----------
    async def create_session(self, user_id: str, ip: Optional[str], ua: Optional[str]) -> str:
        raw = _gen_token()
        async with self._sf() as s:
            s.add(AuthSession(
                user_id=_u(user_id), token_hash=_sha256(raw),
                ip=ip, user_agent=ua,
                expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS),
            ))
            await s.commit()
        return raw

    async def get_user_by_session(self, raw: str) -> Optional[User]:
        h = _sha256(raw)
        async with self._sf() as s:
            sess = await s.scalar(select(AuthSession).where(AuthSession.token_hash == h))
            if sess is None or sess.revoked_at is not None:
                return None
            if _aware(sess.expires_at) < datetime.now(timezone.utc):
                return None
            return await s.get(User, sess.user_id)

    async def revoke_session(self, raw: str) -> None:
        h = _sha256(raw)
        async with self._sf() as s:
            sess = await s.scalar(select(AuthSession).where(AuthSession.token_hash == h))
            if sess:
                sess.revoked_at = datetime.now(timezone.utc)
                await s.commit()

    # ---------- PAT ----------
    async def create_pat(self, user_id: str, name: str, scopes: list[str],
                        ttl_days: int = PAT_TTL_DAYS) -> str:
        raw = f"ga_{secrets.token_urlsafe(8)}_{secrets.token_urlsafe(24)}"
        async with self._sf() as s:
            s.add(ApiToken(
                user_id=_u(user_id), name=name, token_hash=_sha256(raw),
                scopes=scopes, expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
            ))
            await s.commit()
        return raw

    async def get_user_by_pat(self, raw: str) -> Optional[User]:
        h = _sha256(raw)
        async with self._sf() as s:
            tok = await s.scalar(select(ApiToken).where(ApiToken.token_hash == h))
            if tok is None or tok.revoked_at is not None:
                return None
            if tok.expires_at and _aware(tok.expires_at) < datetime.now(timezone.utc):
                return None
            tok.last_used_at = datetime.now(timezone.utc)
            await s.commit()
            return await s.get(User, tok.user_id)

    # ---------- 创建用户 / 团队 ----------
    async def bootstrap_owner(self, email: str, display_name: str, password: str) -> User:
        """首启：创建第一个 owner + 默认团队。"""
        async with self._sf() as s:
            user = User(
                email=email.lower(), display_name=display_name,
                password_hash=hash_password(password), is_active=True,
            )
            s.add(user)
            await s.flush()
            team = Team(name=f"{display_name} 的工作区", slug=_slug(email))
            s.add(team)
            await s.flush()
            s.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
            await s.commit()
            await s.refresh(user)
            return user

    async def create_user_with_team(self, email: str, display_name: str, password: str,
                                   team_id: str, role: str = "developer") -> User:
        async with self._sf() as s:
            user = User(
                email=email.lower(), display_name=display_name,
                password_hash=hash_password(password), is_active=True,
            )
            s.add(user)
            await s.flush()
            s.add(TeamMember(team_id=_u(team_id), user_id=user.id, role=role))
            await s.commit()
            await s.refresh(user)
            return user


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _slug(email: str) -> str:
    base = email.split("@")[0].lower().replace(".", "-").replace("_", "-")
    return f"{base}-{secrets.token_hex(3)}"
