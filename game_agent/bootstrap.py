"""首启引导与示例播种（设计 §3.4 首启 / §16 示例项目）。

两种首启行为，二选一（以 GA_SEED_DEMO 区分）：
- GA_SEED_DEMO=1（Windows 单机打包版默认）：自动建示例 owner 账号 demo@local / demo1234，
  可选地把 examples/cardgame 拷贝为可运行的团队项目，开箱即可登录体验。
  这是打包版避免「预填一个根本不存在的账号 → 邮箱或密码错误」的正解。
- GA_SEED_DEMO 未设（源码/git/团队部署）：不做任何账号播种，打印一次性引导 token，
  由用户访问 /setup 创建第一个 owner。
"""
from __future__ import annotations

import secrets
import shutil
from pathlib import Path

from sqlalchemy import select

from game_agent.config import BOOTSTRAP_TOKEN, PROJECTS_ROOT, SEED_DEMO, default_python_bin
from game_agent.core.registry import seed_default_agents
from game_agent.db.models import Project, Team, TeamMember
from game_agent.db.session import SessionLocal
from game_agent.logging_setup import get_logger

log = get_logger("bootstrap")

DEMO_EMAIL = "demo@local"
DEMO_PASSWORD = "demo1234"
DEMO_TEAM = "demo-team"


async def bootstrap(auth) -> None:  # type: ignore[no-untyped-def]
    count = await auth.count_users()
    if count != 0:
        return
    if SEED_DEMO:
        # 打包版开箱即用：建示例 owner + 示例项目，控制台打印可登录的账号
        await seed_demo(auth)
        _print_demo_banner()
        return
    token = BOOTSTRAP_TOKEN or secrets.token_hex(8)
    from game_agent.api.state import state
    state.bootstrap_token = token
    print("\n" + "=" * 60)
    print(" 循迹（Trace） 首次启动：创建第一个 owner")
    print(f"  访问 /setup 并使用引导 token：{token}")
    print("  或在 .env 设置 GA_BOOTSTRAP_TOKEN 后重启")
    print("=" * 60 + "\n")


def _safe_print(text: str) -> None:
    """控制台输出兜底：Windows 中文控制台是 GBK，遇到 ⚠/emoji 等字符会
    UnicodeEncodeError 直接把进程带崩（打包版首启实测）。统一兜底成可替换，
    任何编码问题都只影响这一行显示，绝不阻断服务启动。"""
    try:
        print(text)
    except Exception:  # noqa: BLE001 - 打印失败不允许影响启动
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


def _print_demo_banner() -> None:
    _safe_print("\n" + "=" * 60)
    _safe_print(" 循迹（Trace） 首次启动：已创建示例账号，可直接登录")
    _safe_print(f"   邮箱：{DEMO_EMAIL}")
    _safe_print(f"   密码：{DEMO_PASSWORD}")
    _safe_print("   注意：这是打包版默认账号，请登录后在「设置 / 团队」里修改密码或新建正式成员")
    _safe_print("=" * 60 + "\n")


async def seed_demo(auth) -> None:  # type: ignore[no-untyped-def]
    log.info("seeding demo team + project")
    # 可选：拷贝示例项目到 PROJECTS_ROOT/cardgame（源码与打包版都随包提供 examples/）
    src = Path(__file__).resolve().parent.parent / "examples" / "cardgame"
    dst = PROJECTS_ROOT / "cardgame"
    if src.exists() and not dst.exists():
        try:
            shutil.copytree(src, dst)
        except Exception:  # noqa: BLE001 - 示例项目拷贝失败不应阻断首启
            log.exception("示例项目拷贝失败（忽略）")

    async with SessionLocal() as s:
        # owner 已由 bootstrap 创建；复用其所在的首个工作区团队，避免重复建团队
        user = await auth.get_user_by_email(DEMO_EMAIL)
        if user is None:
            user = await auth.bootstrap_owner(DEMO_EMAIL, "Demo Owner", DEMO_PASSWORD)
        tm = (await s.execute(
            select(TeamMember).where(TeamMember.user_id == user.id)
        )).scalars().first()
        team = await s.get(Team, tm.team_id) if tm else None
        if team is None:
            team = Team(name="Demo 工作区", slug=DEMO_TEAM)
            s.add(team)
            await s.flush()
            s.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))

        if dst.exists():
            existing_proj = await s.scalar(
                select(Project).where(Project.team_id == team.id, Project.slug == "cardgame"))
            if existing_proj is None:
                proj = Project(team_id=team.id, name="CardGame 示例",
                              slug="cardgame", repo_path="cardgame",
                              project_type="python",
                              python_bin=default_python_bin() or None,
                              commands_json={"build": "python -m compileall -q .",
                                             "test": "python -m pytest -q"})
                s.add(proj)
                await s.commit()
        await seed_default_agents(s, team.id)
        log.info("demo ready", extra={"email": DEMO_EMAIL})
