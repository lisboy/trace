"""一键停止：关掉本服务（后端）+ 本地 ComfyUI（出图/出视频）+ 本地语音 IndexTTS2。

给 stop.cmd 用，也可单独跑：  python scripts\\stop_all.py [--app-only] [--quiet]

为什么不用 taskkill 直接按名字杀：机器上可能还有别的 python / 别的会话起的进程，
按名字杀会误伤。这里只用两条可靠线索：
  1) <DATA_DIR>/game_agent.pid —— 服务启动时自己写的 PID（main.py 的 _write_pidfile）；
  2) 监听固定端口的进程 —— 端口来自 .env（COMFYUI_BASE_URL / TTS_BASE_URL）。
命中进程连同其子进程一起结束；找不到就如实说「没在运行」，绝不谎报成功。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil  # noqa: E402

from game_agent.config import (  # noqa: E402
    COMFYUI_BASE_URL, DATA_DIR, TTS_BASE_URL, is_sqlite,
)


def _port_of(url: str) -> int | None:
    from urllib.parse import urlparse
    u = urlparse((url or "").strip())
    if not u.hostname or u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None  # 远端服务不归我们管
    return u.port


def _listeners_on(port: int) -> list[psutil.Process]:
    out: list[psutil.Process] = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    out.append(psutil.Process(c.pid))
                except psutil.NoSuchProcess:
                    pass
    except Exception:  # noqa: BLE001 - 某些系统 net_connections 需要权限
        pass
    return out


def _kill_tree(proc: psutil.Process, label: str, quiet: bool) -> list[str]:
    killed: list[str] = []
    try:
        targets = proc.children(recursive=True) + [proc]
    except psutil.NoSuchProcess:
        return killed
    for p in targets:
        try:
            name = p.name()
        except psutil.NoSuchProcess:
            continue
        try:
            p.kill()
            killed.append(f"{p.pid}({name})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed and not quiet:
        print(f"  已结束 {label}：{', '.join(killed)}")
    return killed


def _kill_one(proc: psutil.Process, label: str, quiet: bool) -> bool:
    """只结束这一个进程，不连坐子进程。

    后端会通过 webbrowser.open 把浏览器拉成自己的子进程——若按进程树杀，会把
    用户正在用的浏览器一起关掉，非常突兀。所以后端只杀它自己。
    """
    try:
        name = proc.name()
    except psutil.NoSuchProcess:
        return False
    try:
        proc.kill()
        proc.wait(timeout=8)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except psutil.TimeoutExpired:
        pass
    if not quiet:
        print(f"  已结束 {label}：{proc.pid}({name})")
    return True


def stop_app(quiet: bool) -> bool:
    """停后端：优先用 pidfile；pidfile 缺失时退化为「谁在监听服务的端口」。"""
    pidfile = DATA_DIR / "game_agent.pid"
    pid = None
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text(encoding="ascii").strip() or "0") or None
        except Exception:  # noqa: BLE001
            pid = None

    stopped = False
    if pid and psutil.pid_exists(pid):
        try:
            proc = psutil.Process(pid)
            # 再用命令行确认一下确实是我们的服务，避免 PID 被别的进程复用后误杀
            cmdline = " ".join(proc.cmdline()).lower()
            if "game_agent" in cmdline or "game_agent.main" in cmdline:
                stopped = _kill_one(proc, "服务(后端)", quiet)
            else:
                if not quiet:
                    print(f"  pidfile 里的 PID {pid} 现在是别的程序（{cmdline[:60]}），跳过")
        except psutil.NoSuchProcess:
            pass

    if not stopped:
        # 兜底：谁在监听 GA_PORT（config.PORT）就杀谁
        try:
            from game_agent.config import PORT
        except Exception:  # noqa: BLE001
            PORT = 18888
        for proc in _listeners_on(int(PORT)):
            stopped = _kill_one(proc, f"服务(后端 :{PORT})", quiet) or stopped

    try:
        if pidfile.exists():
            pidfile.unlink()
    except Exception:  # noqa: BLE001
        pass
    return stopped


def stop_by_port(url: str, label: str, quiet: bool) -> bool:
    port = _port_of(url)
    if port is None:
        return False
    procs = _listeners_on(port)
    if not procs:
        return False
    ok = False
    for proc in procs:
        ok = bool(_kill_tree(proc, f"{label}(:{port})", quiet)) or ok
    return ok


def stop_comfy_children(quiet: bool) -> bool:
    """ComfyUI 若由本服务的常驻拉起，端口可能已随父进程退出而消失；这里再按端口兜一层。"""
    return stop_by_port(COMFYUI_BASE_URL, "ComfyUI", quiet)


def main() -> None:
    ap = argparse.ArgumentParser(description="一键停止：服务 + ComfyUI + 语音")
    ap.add_argument("--app-only", action="store_true", help="只停本服务，不动 ComfyUI/语音")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    args = ap.parse_args()
    quiet = args.quiet
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not quiet:
        print("正在停止……")

    results: list[tuple[str, bool]] = []
    results.append(("服务(后端)", stop_app(quiet)))
    if not args.app_only:
        results.append(("ComfyUI", stop_comfy_children(quiet)))
        results.append(("语音 IndexTTS2", stop_by_port(TTS_BASE_URL, "语音 IndexTTS2", quiet)))

    # 给进程一点时间真正退出（端口释放），再复查
    time.sleep(1.5)
    if not quiet:
        print()
        for name, ok in results:
            print(f"  {name}：{'已停止' if ok else '未在运行（或已随父进程退出）'}")

    # 复查后端端口是否真的没了
    try:
        from game_agent.config import PORT
        still = _listeners_on(int(PORT))
    except Exception:  # noqa: BLE001
        still = []
    if still:
        print(f"\n[警告] 端口 {PORT} 仍被占用：{[p.pid for p in still]}，请手动结束。")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"\n停止时出错：{e}")
        sys.exit(1)
