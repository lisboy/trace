"""手工补种示例项目（等价于设 ``GA_SEED_DEMO=1`` 后启动一次）。

这个版本没有账号：脚本只负责「准备本机身份 + 拷贝示例工作区 + 登记成项目」。
"""
from __future__ import annotations

import asyncio

from game_agent.auth.local import ensure_local_identity
from game_agent.bootstrap import seed_demo
from game_agent.core.lite.container import build_lite_services
from game_agent.db.migrate import init_db
from game_agent.db.session import SessionLocal


async def main() -> None:
    await init_db()
    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
        print(f"本机身份已就绪：user={user.id} team={team.id}")
    await build_lite_services(SessionLocal, "lite-1")
    await seed_demo()
    print("示例工作区 + cardgame 项目已就绪")


if __name__ == "__main__":
    asyncio.run(main())
