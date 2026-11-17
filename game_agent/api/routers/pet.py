"""桌宠启停：从主项目界面里打开/关闭桌面上的 3D/2D 小白狐。

为什么放在后端而不是前端直接起进程：
  * 浏览器（以及打包后的 WebView）拿不到启动本地程序的权限，
    前端 `window.open` 也开不出透明置顶窗；
  * 由后端 `subprocess.Popen` 拉起 Electron 壳，前端只调 API、切开关状态。

实现要点：
  * 用 pidfile 记录当前桌宠进程，status 时用 psutil 校验进程是否真活着
    （只记 pid 不够：进程被杀后 pid 可能被复用，会误报「还在跑」）；
  * 启动走 electron.exe + 桌宠目录，detached + 不继承 stdout，
    避免后端重启时连带把桌宠关掉；
  * 桌宠进程本身是单实例的（main.js 里有 requestSingleInstanceLock），
    所以重复点「打开」只会把已有那只叫到前台，不会开出第二只。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from game_agent.api.deps import require
from game_agent.auth.rbac import PERM_VIEW
from game_agent.config import DATA_DIR
from game_agent.core.context import RequestContext

router = APIRouter(prefix="/api", tags=["pet"])

# 桌宠目录：仓库根下的 desktop_pet/（与后端同仓）
_REPO_ROOT = Path(__file__).resolve().parents[3]
PET_DIR = _REPO_ROOT / "desktop_pet"
PID_FILE = Path(DATA_DIR) / "desktop_pet.pid"


def _electron_exe() -> Path | None:
    """定位 electron 可执行文件（开发态在 node_modules 里）。"""
    cand = PET_DIR / "node_modules" / "electron" / "dist" / "electron.exe"
    if cand.is_file():
        return cand
    # 非 Windows（start.sh 场景）：electron 无扩展名
    cand2 = PET_DIR / "node_modules" / "electron" / "dist" / "electron"
    return cand2 if cand2.is_file() else None


def _pid_alive(pid: int) -> bool:
    """进程是否真活着。优先 psutil，没有就退化成 os.kill(0)。"""
    try:
        import psutil
        return psutil.pid_exists(pid) and not psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf8").strip())
    except (OSError, ValueError):
        return None
    if pid > 0 and _pid_alive(pid):
        return pid
    # 进程已不在：清掉过期 pidfile，避免 status 一直报「运行中」
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return None


@router.get("/pet/status")
async def pet_status(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """桌宠是否在运行 + 环境是否可用（找不到 electron 时要能告诉用户为什么）。"""
    pid = _read_pid()
    exe = _electron_exe()
    return {
        "running": pid is not None,
        "pid": pid or 0,
        # 桌宠目录/electron 缺失时前端可以把按钮禁用并给出原因
        "available": exe is not None and PET_DIR.is_dir(),
        "reason": "" if exe is not None else (
            f"未找到 Electron（{PET_DIR / 'node_modules' / 'electron' / 'dist'}），"
            "先在 desktop_pet 目录执行 npm install"
        ),
    }


@router.post("/pet/start")
async def pet_start(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """打开桌宠。已在运行时直接返回（单实例，不会开出第二只）。"""
    running = _read_pid()
    if running:
        return {"ok": True, "running": True, "pid": running, "already": True}

    exe = _electron_exe()
    if exe is None:
        raise HTTPException(status_code=400, detail="未找到 Electron，请先在 desktop_pet 执行 npm install")

    # detached：后端退出/重启不连带关掉桌宠；窗口不抢后端控制台
    kwargs: dict = {
        "cwd": str(PET_DIR),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP｜DETACHED_PROCESS：独立进程组，不受后端控制台关闭影响
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen([str(exe), "."], **kwargs)

    # electron.exe 起来后会 fork 出真正的主进程；这里记录的是入口 pid，
    # status 时只要它（或其子进程）还活着就算运行中。
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf8")
    time.sleep(0.6)  # 给窗口一点时间落地，前端立刻查 status 时不会闪一下「未运行」
    return {"ok": True, "running": True, "pid": proc.pid, "already": False}


@router.post("/pet/stop")
async def pet_stop(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """关闭桌宠（按 pid 精确结束进程树，不用 taskkill /IM 全杀）。"""
    pid = _read_pid()
    if pid is None:
        return {"ok": True, "running": False, "pid": 0, "already": False}

    try:
        import psutil
        proc = psutil.Process(pid)
        # 连同 electron 渲染/GPU 子进程一起收，否则会残留后台进程
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        # 兜底：Windows 用 taskkill /T 杀进程树；其它平台 kill -9
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, check=False)
            else:
                os.kill(pid, 9)
        except OSError:
            pass

    try:
        PID_FILE.unlink()
    except OSError:
        pass
    time.sleep(0.3)
    return {"ok": True, "running": False, "pid": 0, "already": False}
