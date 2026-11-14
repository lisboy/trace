"""命令行工具（打包版与源码通用）。

这个版本**没有账号密码**，所以不再有「重置密码」这类命令。留着这个入口只为两件事：
  1. 老用户/老脚本还会敲 ``Trace.exe --reset-password``，要能给出人话，
     而不是 argparse 的报错；
  2. 需要一个不开服务、只查配置的地方时（数据目录 / 库文件 / 端口在哪），
     ``--info`` 能直接打印。
"""
from __future__ import annotations

import argparse
import sys

_NO_ACCOUNTS = (
    "本版本不需要账号密码：没有登录，也没有注册/找回。\n"
    "打开浏览器访问服务地址即可直接使用（数据全部保存在本机数据目录里）。"
)


def _safe_print(text: str = "") -> None:
    """Windows 控制台编码各异，打印失败也绝不能让命令崩掉。"""
    try:
        print(text)
    except Exception:  # noqa: BLE001
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


def _print_info() -> int:
    from game_agent import config as cfg

    _safe_print("循迹（Trace）配置")
    _safe_print(f"  数据目录：{cfg.DATA_DIR}")
    _safe_print(f"  数据库  ：{cfg.DB_PATH}")
    _safe_print(f"  项目目录：{cfg.PROJECTS_ROOT}")
    _safe_print(f"  监听    ：{cfg.HOST}:{cfg.PORT}")
    _safe_print("  账号    ：无（本版本没有登录/账号密码）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="trace",
        description="Trace 命令行工具（本版本无账号密码，仅保留信息查询）。",
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("info", help="打印数据目录 / 数据库 / 端口等配置")
    sub.add_parser("reset-password", help="（已废弃）本版本没有账号密码")
    ap.add_argument("--info", action="store_true", help="等价于 info 子命令")
    ap.add_argument("--list", action="store_true", help="（已废弃）本版本没有账号")
    return ap


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("--reset-password", "reset_password", "-rp"):
        _safe_print(_NO_ACCOUNTS)
        return 0
    ap = build_parser()
    args = ap.parse_args(raw)
    if args.info or args.cmd == "info":
        return _print_info()
    if args.cmd == "reset-password" or args.list:
        _safe_print(_NO_ACCOUNTS)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
