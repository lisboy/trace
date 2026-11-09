"""造演示数据：首启 owner + demo 团队 + cardgame 示例项目（设计 §16）。

等价于设 ``GA_SEED_DEMO=1`` 后首次启动；本脚本可在已有库上手动补种。
"""
from __future__ import annotations

import asyncio

from game_agent.auth.service import AuthService
from game_agent.bootstrap import seed_demo
from game_agent.core.lite.container import build_lite_services
from game_agent.db.migrate import init_db
from game_agent.db.session import SessionLocal


async def main() -> None:
    await init_db()
    auth = AuthService(SessionLocal)
    if await auth.count_users() == 0:
        await auth.bootstrap_owner("demo@local", "Demo Owner", "demo1234")
        print("已创建 demo owner (demo@local / demo1234)")
    await build_lite_services(SessionLocal, "lite-1")
    await seed_demo(auth)
    print("demo 团队 + cardgame 项目已就绪")


if __name__ == "__main__":
    asyncio.run(main())
