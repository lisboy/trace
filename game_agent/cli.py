"""命令行工具（打包版与源码通用）。

用途：忘记登录密码时重置账号密码（无需旧密码）。

用法：
    python -m game_agent.cli --list                      # 只列出所有账号
    python -m game_agent.cli reset-password              # 交互式：选账号 -> 输入新密码
    python -m game_agent.cli reset-password --email demo@local --password 新密码

打包版（Trace.exe）：
    Trace.exe --reset-password                           # 等价于 reset-password
    start.cmd 菜单里选 [2] 会调起它

连接的是 .env 里 DATABASE_URL 指向的那套库（与 start.cmd 启动的服务同一套数据）。
故意用纯文本交互（getpass 隐藏输入 + 普通 input 兜底），不依赖任何第三方 CLI 库，
以免打进 exe 后受终端/编码影响。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys


# ---------------------------------------------------------------- 输出兜底
def _safe_print(text: str = "") -> None:
    """Windows 控制台编码各异，打印失败也绝不能让命令崩掉。"""
    try:
        print(text)
    except Exception:  # noqa: BLE001
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


def _ask(prompt: str) -> str:
    """读一行密码：真实控制台用 getpass 隐藏输入；stdin 不是终端（管道/自动化）
    时退回普通 input，否则 getpass 会一直等一个不存在的 tty 而挂住。"""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass(prompt)
    except Exception:  # noqa: BLE001
        pass
    try:
        return input(prompt)
    except EOFError:
        return ""


def _prompt_new_password() -> str:
    while True:
        p1 = _ask("请输入新密码（至少 8 位）：")
        if len(p1) < 8:
            _safe_print("  密码太短，至少 8 位，请重来。\n")
            continue
        if len(p1) > 200:
            _safe_print("  密码过长（>200），请重来。\n")
            continue
        p2 = _ask("请再输入一次确认：")
        if p1 != p2:
            _safe_print("  两次输入不一致，请重来。\n")
            continue
        return p1


# ---------------------------------------------------------------- DB 操作
async def _collect_users():
    from sqlalchemy import select

    from game_agent.db.models import User
    from game_agent.db.session import SessionLocal

    async with SessionLocal() as s:
        return list((await s.execute(select(User).order_by(User.email))).scalars())


async def _do_reset(email: str, password: str) -> bool:
    from sqlalchemy import select

    from game_agent.auth.passwords import hash_password
    from game_agent.db.models import User
    from game_agent.db.session import SessionLocal

    async with SessionLocal() as s:
        user = await s.scalar(select(User).where(User.email == email))
        if user is None:
            return False
        user.password_hash = hash_password(password)
        user.failed_logins = 0
        user.locked_until = None
        user.is_active = True
        await s.commit()
    return True


def _print_users(users) -> None:
    if not users:
        _safe_print("  库中还没有任何账号（说明服务还没完成首启初始化）。")
        return
    _safe_print(f"  共有 {len(users)} 个账号：")
    for u in users:
        flags = []
        if not getattr(u, "is_active", True):
            flags.append("已停用")
        if getattr(u, "locked_until", None):
            flags.append("锁定中")
        if getattr(u, "is_superuser", False):
            flags.append("超管")
        tail = ("  [" + "/".join(flags) + "]") if flags else ""
        _safe_print(f"    - {u.email:<32} {u.display_name}{tail}")


# ---------------------------------------------------------------- 命令实现
async def _run_reset(args: argparse.Namespace) -> int:
    users = await _collect_users()

    if args.list:
        _print_users(users)
        return 0

    if not users:
        _safe_print("库中还没有任何账号，先启动一次服务完成初始化，再来重置。")
        return 2

    email = (args.email or "").strip().lower()
    if not email:
        _print_users(users)
        _safe_print()
        email = input("要重置哪个账号？输入上面的邮箱后回车：").strip().lower()
        if not email:
            _safe_print("没有输入邮箱，已取消。")
            return 1

    password = args.password or ""
    if not password:
        password = _prompt_new_password()

    ok = await _do_reset(email, password)
    if not ok:
        _safe_print(f"找不到账号：{email}")
        _safe_print("可用账号：")
        _print_users(users)
        return 3

    _safe_print(f"\n已重置 {email} 的密码，并已解锁/启用该账号。现在可以用新密码登录了。")
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    from game_agent.db.session import engine

    async def _wrap() -> int:
        try:
            return await _run_reset(args)
        finally:
            # 必须在同一个事件循环里 dispose，否则 aiomysql 在另一个 loop 里关连接会炸
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001
                pass

    try:
        if sys.platform == "win32":
            # aiomysql 在 Windows 上要求 Selector 事件循环（默认 Proactor 不兼容）
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        return asyncio.run(_wrap())
    except Exception as e:  # noqa: BLE001
        _safe_print(f"\n连接数据库失败或执行出错：{e}")
        _safe_print("请确认 .env 的 DATABASE_URL 正确；用 SQLite 时无需数据库服务，"
                    "用 MySQL 时请先启动数据库。")
        return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="trace",
        description="Trace 命令行工具（重置账号密码）。",
    )
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("reset-password", help="重置某个账号的密码（无需旧密码）")
    rp.add_argument("--list", action="store_true", help="只列出所有账号")
    rp.add_argument("--email", help="要重置的账号邮箱")
    rp.add_argument("--password", help="新密码（不传则交互式输入，更安全）")
    return ap


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # 允许不带子命令的便捷写法：
    #   Trace.exe --reset-password
    #   Trace.exe --list / --email x@y --password z
    if raw and raw[0] in ("--reset-password", "reset_password", "-rp"):
        raw = ["reset-password", *raw[1:]]
    elif raw and raw[0].startswith("-"):
        raw = ["reset-password", *raw]
    ap = build_parser()
    args = ap.parse_args(raw)
    if args.cmd != "reset-password":
        ap.print_help()
        return 2
    return _dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
