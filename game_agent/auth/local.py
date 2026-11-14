"""本机单用户身份：这个版本**没有登录，也没有账号密码**。

个人版是「一台机器一个人的应用」：服务就跑在用户自己电脑的 127.0.0.1 上，
再要求注册 / 记密码 / 每次打开先登录，只是多一道必输的门——清个 Cookie、换个浏览器
就得重来一遍，而它想保护的「多用户数据隔离」在这个形态里根本不存在。

所以身份不再「由凭证解析」，而是「本机默认身份」：
  · 启动时（``bootstrap.py``）保证库里恰好有一个本地用户 + 一个本地工作区；
  · 每个请求直接用这个身份（owner，全权限），不看 Cookie / Bearer / PAT；
  · ``users`` / ``teams`` / ``team_members`` 这几张表仍然保留——会话、项目、审计、
    运行记录都要挂 ``team_id`` / ``user_id``；它们只是变成内部实现细节，
    用户永远看不到「账号」这个概念。

老库升级：库里已有账号（demo@local 或用户自己建的）时**复用最早那个**，
不新建身份，否则原有项目/会话会因为挂了别的 team_id 而「消失」。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.db.models import Team, TeamMember, User

# 全新安装时建的占位身份（不对外暴露、不参与任何登录）
LOCAL_EMAIL = "local@trace"
LOCAL_NAME = "我"
LOCAL_TEAM_NAME = "我的工作区"
LOCAL_TEAM_SLUG = "local"


async def ensure_local_identity(s: AsyncSession) -> tuple[User, Team]:
    """取本机身份（用户 + 工作区）；库里没有就建，幂等。返回 (user, team)。"""
    # 复用库里最早的用户：老版本装过的人不会丢掉自己的项目/会话
    user = await s.scalar(select(User).order_by(User.created_at))
    if user is None:
        user = User(email=LOCAL_EMAIL, display_name=LOCAL_NAME, is_active=True)
        s.add(user)
        await s.flush()
    elif not user.is_active:
        # 以前可能被停用过；这个版本没有「停用账号」概念，直接恢复正常
        user.is_active = True

    tm = await s.scalar(
        select(TeamMember).where(TeamMember.user_id == user.id).order_by(TeamMember.joined_at))
    team = await s.get(Team, tm.team_id) if tm is not None else None
    if team is None:
        team = Team(name=LOCAL_TEAM_NAME, slug=f"{LOCAL_TEAM_SLUG}-{uuid.uuid4().hex[:8]}")
        s.add(team)
        await s.flush()
        if tm is not None:  # 指向已被手工删掉的工作区：这行成员记录是孤儿，换掉
            await s.delete(tm)
        s.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
    await s.commit()
    return user, team
