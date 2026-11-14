"""PyInstaller 打包入口（仅用于生成 Trace.exe，源码运行时用不到）。

- --windowed（无控制台）时 stdout/stderr 为 None，先补一个日志文件当输出；
- 从 exe 所在目录导入 game_agent（确保打包进去的代码用的是这份）；
- 复用 game_agent.main.main() 的完整启动流程。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_std_streams() -> None:
    """windowed 模式下 sys.stdout/stderr 可能是 None，写日志会崩；兜底到 DATA_DIR 下的日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    # 数据目录以 config 为准（它认得设置页写下的指针文件）：这里要是只认「默认 ~/.trace」，
    # 用户把数据迁到 D 盘后，每次启动都会在 C 盘重新长出 ~/.trace/logs/exe.out，
    # 看起来就像「迁移根本没生效」。
    try:
        from game_agent.config import DATA_DIR

        data_dir = Path(DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 - config 都导不进来时回到默认位置，别在这里崩
        data_dir = Path(os.environ.get("GA_DATA_DIR") or (Path.home() / ".trace"))
    log_path = data_dir / "logs" / "exe.out"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except Exception:  # noqa: BLE001
        devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        f = devnull
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main() -> None:
    exe_dir = Path(sys.executable).resolve().parent
    # 让 `import game_agent` 命中与 exe 同级的代码（onefile 下指向 _MEIPASS，这里是无害的双保险）
    if str(exe_dir) not in sys.path:
        sys.path.insert(0, str(exe_dir))
    _ensure_std_streams()
    # 子命令入口：`Trace.exe --reset-password` 仍走 CLI，但这个版本没有账号密码，
    # 它只会打印一句「本版本不需要账号密码」。留着是为了老用户/老快捷方式不至于撞上
    # argparse 报错；`Trace.exe --info` 则能不开服务就查到数据目录/端口。
    argv = sys.argv[1:]
    if argv and argv[0] in ("--reset-password", "reset-password", "-rp", "--info", "info"):
        from game_agent.cli import main as cli_main
        raise SystemExit(cli_main(argv))
    from game_agent.main import main as app_main
    app_main()


if __name__ == "__main__":
    main()
