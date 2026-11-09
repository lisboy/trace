"""把 roles.py 里的默认系统提示词同步到已存在的 agents 行。

team 级 prompt 是落库的（agents.system_prompt），改 roles.py 只影响新团队；
本脚本只覆盖「看起来还是默认值」的行（含旧默认特征串且缺本轮新增特征串），
用户自定义过的 prompt 一律不动。

用法：
    .venv\\Scripts\\python.exe scripts\\sync_role_prompts.py --dry-run
    .venv\\Scripts\\python.exe scripts\\sync_role_prompts.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from game_agent.agents.roles import ROLE_PROMPTS  # noqa: E402
from game_agent.db.migrate import init_db  # noqa: E402
from game_agent.db.models import Agent  # noqa: E402
from game_agent.db.session import SessionLocal  # noqa: E402

# 角色 → (旧默认特征串, 本轮新增特征串)
# 判定规则：库里文本含「旧默认特征串」且不含「本轮新增特征串」= 还是老默认 → 覆盖。
# 每次改 roles.py 的默认提示词时，把上一轮的新增特征串挪到左边、把这一轮的新特征串写到右边，
# 这样连续升级也不会把用户自定义的 prompt 冲掉。
SIGNS: dict[str, tuple[str, str]] = {
    # v2 三角色架构：旧提示词里必有 write_work_order / 开发派发痕迹
    "planner": ("write_work_order", "唯一的执行者"),
    "artist": ("无可用供应商时产出占位素材", "没有可用供应商时产出占位图"),
    "voice": ("生成不了就产出占位音频并标注", "生成不了就产出占位音频并标注原因"),
    # v3：起服务自测加了「不许按进程名全杀浏览器」的硬提醒
    "main": ("验收完 stop_service 收干净。", "不要用 `taskkill /IM chrome.exe`"),
}


async def main(dry: bool) -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    await init_db()
    async with SessionLocal() as s:
        rows = (await s.execute(select(Agent))).scalars().all()
        changed: list[tuple[str, str]] = []
        skipped: list[tuple[str, str, str]] = []
        for r in rows:
            newp = ROLE_PROMPTS.get(r.key)
            if not newp:
                continue
            cur = r.system_prompt or ""
            if cur.strip() == newp.strip():
                continue
            old_sign, new_sign = SIGNS.get(r.key, ("", ""))
            # 空 prompt 也是「没自定义」；含旧默认特征串且缺新特征串 = 还是老默认
            is_default = (not cur.strip()) or (
                bool(old_sign) and old_sign in cur and (not new_sign or new_sign not in cur))
            if is_default:
                r.system_prompt = newp
                changed.append((str(r.team_id), r.key))
            else:
                skipped.append((str(r.team_id), r.key, cur[:40].replace("\n", " ")))
        if changed and not dry:
            await s.commit()
        print(f"updated={len(changed)} skipped={len(skipped)} dry_run={dry}")
        for team, key in changed:
            print(f"  updated  team={team} role={key}")
        for team, key, head in skipped:
            print(f"  skipped  team={team} role={key} （疑似自定义：{head}…）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
    # 直接退出：aiomysql 连接对象在 loop 关闭后 __del__ 会刷一屏 RuntimeError，无害但吓人
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
