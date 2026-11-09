"""重启本服务（后端）：等价于「关掉 start.cmd 那个窗口 + 重新 start.cmd run」。

给 restart.cmd（双击）用，也可单独跑：
    python scripts\\restart_app.py stop      # 停：结束整棵 start.cmd 进程树
    python scripts\\restart_app.py wait      # 等：轮询 /health 直到新实例就绪

为什么需要「整棵树」：双击 start.cmd 后进程是这样挂的——
    cmd.exe(start.cmd 窗口) ─┬─ python.exe -m game_agent.main   (venv 启动器, 父)
                            └─(孙) python.exe -m game_agent.main (真正跑服务/监听端口)
stop_all.py 的 stop_app() 只杀「监听端口的那一个」，是为了不误伤它用 webbrowser
拉起的浏览器；但重启时，若只杀孙进程，那个空转的 cmd 窗口 + venv 父进程要等你
到窗口里敲 Ctrl+C，start.cmd 也就不会再往下走。所以重启走这条：从监听端口的进程
往上找到 start.cmd 的 cmd.exe，只结束「python 后端 + 它们之间的 cmd 壳」，
**绝不碰浏览器**（浏览器是孙进程的子节点，但不在这条父链上）。

找不到服务就说「没在运行」，绝不谎报成功。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil  # noqa: E402

from game_agent.config import DATA_DIR, PORT  # noqa: E402


def _backend_pids() -> list[psutil.Process]:
    """定位后端进程：pidfile 优先，其次「谁在监听 PORT」。"""
    found: list[psutil.Process] = []
    pidfile = DATA_DIR / "game_agent.pid"
    pid = None
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text(encoding="ascii").strip() or "0") or None
        except Exception:  # noqa: BLE001
            pid = None
    if pid and psutil.pid_exists(pid):
        try:
            p = psutil.Process(pid)
            cmd = " ".join(p.cmdline()).lower()
            if "game_agent" in cmd:
                found.append(p)
        except psutil.NoSuchProcess:
            pass
    if not found:
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == int(PORT) and c.status == psutil.CONN_LISTEN and c.pid:
                    try:
                        found.append(psutil.Process(c.pid))
                    except psutil.NoSuchProcess:
                        pass
        except Exception:  # noqa: BLE001
            pass
    # 去重
    uniq: dict[int, psutil.Process] = {p.pid: p for p in found}
    return list(uniq.values())


def _is_python(proc: psutil.Process) -> bool:
    try:
        return "python" in proc.name().lower()
    except psutil.NoSuchProcess:
        return False


def _is_start_cmd(proc: psutil.Process) -> bool:
    try:
        if proc.name().lower() != "cmd.exe":
            return False
        cmd = " ".join(proc.cmdline()).lower()
        return "start.cmd" in cmd
    except psutil.NoSuchProcess:
        return False


def _collect_tree(backend: psutil.Process) -> list[psutil.Process]:
    """从后端往上找到 start.cmd 的 cmd 壳；沿父链只收 python 后端和 cmd 壳。

    父链典型：后端python -> 父python(venv) -> cmd.exe(start.cmd) -> explorer
    只收「python 后端」和「cmd 壳」，到非 python 且非 start.cmd 的进程就停，
    这样绝不会把浏览器（它是后端的子节点，不在父链上）卷进来。
    """
    targets: list[psutil.Process] = []
    cur = backend
    for _ in range(8):  # 防御性上限
        targets.append(cur)
        try:
            parent = cur.parent()
        except psutil.NoSuchProcess:
            break
        if parent is None:
            break
        if _is_start_cmd(parent):
            targets.append(parent)
            # start.cmd 的父（explorer）到此为止，不收
            break
        if _is_python(parent):
            cur = parent
            continue
        # 父既不是 python 也不是 start.cmd（如 conhost/explorer）→ 停在上一层
        break
    return targets


def stop(quiet: bool = False) -> bool:
    backends = _backend_pids()
    if not backends:
        if not quiet:
            print("  没有发现正在运行的本服务。")
        return False
    killed: list[str] = []
    for backend in backends:
        for proc in _collect_tree(backend):
            try:
                proc.kill()
                killed.append(f"{proc.pid}({proc.name()})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    # 清 pidfile，避免新实例读到旧 pid
    try:
        p = DATA_DIR / "game_agent.pid"
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass
    if killed and not quiet:
        print(f"  已结束进程树：{', '.join(killed)}")
    return bool(killed)


def wait_ready(timeout_s: int = 90) -> int:
    """轮询 http://127.0.0.1:PORT/health，直到返回 200 或超时。"""
    import urllib.request
    url = f"http://127.0.0.1:{int(PORT)}/health"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    print(f"  新实例已就绪：{url} -> {r.status}")
                    return 0
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    print(f"  [警告] {timeout_s}s 内未等到 {url} 就绪。请查看 start.cmd 窗口里的报错。")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description="重启本服务（后端）")
    ap.add_argument("action", choices=["stop", "wait"], help="stop=结束整棵进程树；wait=等 /health 就绪")
    ap.add_argument("--timeout", type=int, default=90, help="wait 的超时秒数")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.action == "stop":
        ok = stop(args.quiet)
        # 停之前服务在跑、现在停了 = 成功；本就没跑 = 也算成功（可继续启动）
        sys.exit(0 if ok or not _backend_pids() else 1)
    else:
        sys.exit(wait_ready(args.timeout))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"重启辅助脚本出错：{e}")
        sys.exit(1)
