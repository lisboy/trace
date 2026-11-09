"""忘记密码时重置账号密码（无需旧密码）。

这是一个薄封装，真正逻辑在 ``game_agent.cli`` —— 这样源码运行、Windows lite 包
（start.cmd 菜单 [2]）、以及 Trace.exe（``Trace.exe --reset-password``）走的是**同一份**
代码，不会出现「开发机能选 [3]、发出去的包没有」这种偏差。

用法（在项目根目录、用 .venv 的 python 跑）：
    python scripts\\reset_password.py --list
    python scripts\\reset_password.py --email demo@local --password 新密码
    python scripts\\reset_password.py                 # 交互式

连接的是 .env 里 DATABASE_URL 指向的那套库（与 start.cmd 启动的服务同一套数据）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许从任意目录 `python scripts\reset_password.py` 运行时导入 game_agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game_agent.cli import main as cli_main  # noqa: E402


def main() -> None:
    code = cli_main(sys.argv[1:] or ["reset-password"])
    # 直接双击运行（非管道）时留一口气，方便看结果；批量/管道调用时不打扰。
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车关闭……")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(code)


if __name__ == "__main__":
    main()
