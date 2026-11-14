"""「没有登录」的回归测试。

这个版本把账号体系整个去掉了：身份 = 本机默认用户（``game_agent/auth/local.py``），
请求不带任何 Cookie / Bearer 也照样通过。专门锁住几件事，因为这几条一错就是
「打开是一片登录页 / 接口全 401 / 老用户的项目全消失」：

  1. ``ensure_local_identity`` 幂等：反复调用拿到的是同一个用户与工作区；
  2. 老库升级：库里已有更早的用户时**复用**它，不新建身份（否则原有项目挂在别的
     team_id 上，用户看到的是「升级后数据没了」）；
  3. ``get_current_context`` 不看凭证，返回 owner 全权限上下文；
  4. 旧的登录相关路由确实不存在了（不是被隐藏，是删掉了）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from game_agent.api.deps import get_current_context
from game_agent.api.routers import auth as auth_router
from game_agent.auth.local import ensure_local_identity
from game_agent.db.models import Project, Team, TeamMember, User


def test_ensure_local_identity_is_idempotent(session_factory):
    async def run():
        async with session_factory() as s:
            u1, t1 = await ensure_local_identity(s)
            u2, t2 = await ensure_local_identity(s)
            return (str(u1.id), str(t1.id), str(u2.id), str(t2.id))

    a, b, c, d = asyncio.run(run())
    assert (a, b) == (c, d), "本机身份必须是稳定的同一个用户/工作区"


def test_ensure_local_identity_reuses_earliest_user(session_factory):
    """老库升级：库里已有账号时复用它，不新建身份。"""
    async def run():
        async with session_factory() as s:
            old = User(email="someone@old", display_name="老用户",
                       created_at=datetime(2001, 1, 1, tzinfo=timezone.utc))
            s.add(old)
            await s.flush()
            team = Team(name="老工作区", slug="old-ws")
            s.add(team)
            await s.flush()
            s.add(TeamMember(team_id=team.id, user_id=old.id, role="owner"))
            await s.commit()
            old_id, team_id = str(old.id), str(team.id)
        async with session_factory() as s:
            u, t = await ensure_local_identity(s)
            return old_id, team_id, str(u.id), str(t.id)

    old_id, team_id, uid, tid = asyncio.run(run())
    assert uid == old_id, "必须复用库里最早的账号，否则原有数据会「消失」"
    assert tid == team_id


def test_ensure_local_identity_creates_owner_membership(session_factory):
    async def run():
        async with session_factory() as s:
            u, t = await ensure_local_identity(s)
            tm = await s.get(TeamMember, (t.id, u.id))
            return tm.role if tm else None

    assert asyncio.run(run()) == "owner"


def test_context_needs_no_credentials(session_factory):
    """不带 Cookie / Bearer 也拿到身份上下文；带了假凭证也不影响（压根不看它）。"""

    async def ctx_for(headers: dict):
        ctx = await get_current_context(_fake_request(headers))
        return {"role": ctx.role, "user_id": ctx.user_id, "team_id": ctx.team_id}

    bare = asyncio.run(ctx_for({}))
    bogus = asyncio.run(ctx_for({"Cookie": "ga_session=garbage",
                                 "Authorization": "Bearer garbage"}))
    assert bare["role"] == "owner" and bare["user_id"] and bare["team_id"]
    assert bogus["role"] == "owner" and bogus["user_id"] == bare["user_id"]


def _fake_request(headers: dict):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": ("127.0.0.1", 1234),
                    "method": "GET", "path": "/whoami"})


def test_deps_context_lists_local_projects(session_factory):
    """project_ids = 本机工作区里的全部项目（越权判断靠它）。"""
    async def run():
        async with session_factory() as s:
            _u, team = await ensure_local_identity(s)
            p = Project(name="本地项目", slug=f"local-{team.id.hex[:8]}",
                        repo_path="local-ws", team_id=team.id)
            s.add(p)
            await s.commit()
            pid = str(p.id)
        ctx = await get_current_context(_fake_request({}))
        return pid, ctx.project_ids

    pid, ids = asyncio.run(run())
    assert pid in ids


def test_login_routes_are_gone():
    """只有 /me 与 /me/avatar 还在；登录/登出/首启/改密码这些接口已删除。"""
    paths = {getattr(r, "path", "") for r in auth_router.router.routes}
    assert "/api/auth/me" in paths and "/api/auth/me/avatar" in paths
    for dead in ("/api/auth/login", "/api/auth/logout", "/api/auth/setup",
                 "/api/auth/status", "/api/auth/me/password", "/api/auth/oidc/providers"):
        assert dead not in paths, f"{dead} 应该已经删掉（这个版本没有账号密码）"


def test_me_endpoint_returns_identity_without_login(session_factory):
    """/api/auth/me 无凭证可读：返回本机用户 + 工作区（前端顶栏昵称/头像靠它）。"""
    app = FastAPI()
    app.include_router(auth_router.router)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/auth/me")
            assert r.status_code == 200, r.text
            return r.json()

    body = asyncio.run(run())
    assert body["user"]["id"] and body["teams"], body
    assert body["teams"][0]["role"] == "owner"
    assert body["current_team"] == body["teams"][0]["id"]
