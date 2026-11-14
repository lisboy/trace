"""首启：保证本机身份与（可选的）示例项目存在。

这个版本没有注册/登录，所以「首启」不再是「建第一个账号 + 打印引导 token」，
而是「把这台机器的默认身份和工作区准备好」：

  · 身份：``auth.local.ensure_local_identity`` 幂等保证 users/teams/team_members
    各有恰好一行（老库复用最早那个用户，不丢原有项目与会话）；
  · 示例：``GA_SEED_DEMO=1``（Windows 单机打包版默认）时把 ``examples/cardgame``
    拷成可直接运行的示例工作区，开箱就能点开一个项目试。

个人版：一个人 = 一个工作区，没有团队/成员/邀请概念。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select

from game_agent.auth.local import ensure_local_identity
from game_agent.config import PROJECTS_ROOT, SEED_DEMO, default_python_bin
from game_agent.core.registry import seed_default_agents
from game_agent.db.models import Project
from game_agent.db.session import SessionLocal
from game_agent.logging_setup import get_logger

log = get_logger("bootstrap")

DEMO_PROJECT_SLUG = "cardgame"


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


async def bootstrap(auth=None) -> None:  # type: ignore[no-untyped-def]
    """准备本机身份（+ 示例项目）。``auth`` 参数只为兼容旧调用方，已不再使用。"""
    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
    log.info("local identity ready", extra={"user": str(user.id), "team": str(team.id)})
    if SEED_DEMO:
        await seed_demo()
    _safe_print("\n" + "=" * 60)
    _safe_print(" 循迹（Trace）已就绪 —— 无需登录，直接在浏览器里使用")
    _safe_print("=" * 60 + "\n")


async def seed_demo() -> None:
    """拷贝示例项目并登记到本机工作区（幂等：已存在就跳过）。"""
    src = Path(__file__).resolve().parent.parent / "examples" / "cardgame"
    dst = PROJECTS_ROOT / DEMO_PROJECT_SLUG
    if src.exists() and not dst.exists():
        try:
            shutil.copytree(src, dst)
        except Exception:  # noqa: BLE001 - 示例项目拷贝失败不应阻断首启
            log.exception("示例项目拷贝失败（忽略）")

    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
        if dst.exists():
            existing = await s.scalar(select(Project).where(
                Project.team_id == team.id, Project.slug == DEMO_PROJECT_SLUG))
            if existing is None:
                s.add(Project(team_id=team.id, name="CardGame 示例",
                              slug=DEMO_PROJECT_SLUG, repo_path=DEMO_PROJECT_SLUG,
                              project_type="python",
                              python_bin=default_python_bin() or None,
                              commands_json={"build": "python -m compileall -q .",
                                             "test": "python -m pytest -q"}))
                await s.commit()
                log.info("示例项目已登记")
        await seed_default_agents(s, team.id)
        log.info("demo ready")
