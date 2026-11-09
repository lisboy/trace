"""ComfyUI 本地生图客户端。

通过 ComfyUI HTTP API（默认 http://127.0.0.1:8188）提交 API 格式工作流、
轮询 /history、从 /view 下载输出图片。任何失败抛 ComfyUIError，
由调用方决定是否回退占位图。

工作流来源二选一：
- COMFYUI_WORKFLOW_PATH 指向 ComfyUI 界面导出的 API 格式 JSON（Save (API Format)）
- 未配置时用内置默认文生图工作流，模型从 /object_info 自动探测：
  * 有 checkpoint（SD1.5/SDXL）→ 经典 CheckpointLoaderSimple 工作流
  * 无 checkpoint 但有 DiT 三件套（Anima/Qwen 架构：UNET+CLIP+VAE）→ 三件套工作流

全链路异步（httpx + asyncio.sleep 轮询），禁止同步阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

import httpx

from game_agent.assets import gpu_arbiter
from game_agent.assets import media_queue
from game_agent.config import (
    COMFYUI_AUTOSTART, COMFYUI_BASE_URL, COMFYUI_BOOT_HARD_CAP_S, COMFYUI_BOOT_STALL_S,
    COMFYUI_BOOT_TIMEOUT_S, COMFYUI_CHECKPOINT, COMFYUI_CLIP, COMFYUI_LAUNCH_CMD,
    COMFYUI_IMG2IMG_DENOISE, COMFYUI_DIRECT_MAX_MP, COMFYUI_LORA, COMFYUI_LORA_STRENGTH,
    COMFYUI_LORAS_DIR, COMFYUI_TILED_MP, COMFYUI_VAE_TILE, COMFYUI_VAE_TILE_OVERLAP,
    COMFYUI_WORKFLOW_PATH, COMFYUI_VIDEO_CFG, COMFYUI_VIDEO_CLIP, COMFYUI_VIDEO_FPS,
    COMFYUI_VIDEO_HEIGHT, COMFYUI_VIDEO_LENGTH, COMFYUI_VIDEO_REF_BG,
    COMFYUI_VIDEO_SMOOTH, COMFYUI_VIDEO_SMOOTH_FPS, COMFYUI_VIDEO_STEPS,
    COMFYUI_VIDEO_TIMEOUT_S, COMFYUI_VIDEO_UNET, COMFYUI_VIDEO_VAE, COMFYUI_VIDEO_WIDTH,
    LOG_DIR)
from game_agent.logging_setup import get_logger

log = get_logger("assets.comfyui")

POLL_INTERVAL_S = 1.0
IMG2IMG_DENOISE = COMFYUI_IMG2IMG_DENOISE  # 参考图重绘幅度：偏小保风格（笔触/纹理），大改构图需临时调参
# 工作流里「按请求宽高缩放参考图」节点的标记名（写在 _meta.title，ComfyUI 执行时忽略）
TARGET_SIZE_TITLE = "ga_target_size"
# 启动输出落盘文件：以前 DEVNULL 直接丢弃，ComfyUI 起不来时（端口被占/依赖缺失/显存爆）
# 后台一行日志都没有，只能干瞪眼。现在全部落这里，超时/失败时把尾部带回给调用方。
BOOT_LOG_PATH = LOG_DIR / "comfyui_boot.log"
# ComfyUI 自己的日志文件：默认 args.verbose 为空 → app.logger 一个文件都不写，
# 只有控制台。启动时补上 `--verbose INFO <此路径>`，进程输出才真正可回溯，
# 也用来判断「还在加载」还是「卡死」。
COMFY_LOG_PATH = LOG_DIR / "comfyui.log"

# 自动启动：优先直接启动实例服务（绕过桌面壳的实例选择器），找不到再退桌面 exe
# （Comfy Desktop.exe 只开选择器窗口，需手动点实例卡片才真正起服务——不适合无人值守）
_LAUNCH_PY_MAIN = [
    # 实例 .venv（uv 管理，torch 在此）+ main.py 直启；standalone-env 无 torch 仅装基础包
    (r"D:\AI\ComfyUI\modes\ComfyUI\ComfyUI\.venv\Scripts\python.exe",
     r"D:\AI\ComfyUI\modes\ComfyUI\ComfyUI\main.py"),
]
_LAUNCH_CANDIDATES = [
    r"D:\AI\ComfyUI\Comfy Desktop\Comfy Desktop.exe",
    r"C:\Program Files\ComfyUI\Comfy Desktop.exe",
    r"%LOCALAPPDATA%\Programs\ComfyUI\ComfyUI.exe",
    r"%LOCALAPPDATA%\Programs\ComfyUI\Comfy Desktop.exe",
]

_AUTOSTART_LOCK = asyncio.Lock()
# 出图全局串行锁：本地 ComfyUI 单卡单队列，并发提交只会排队 + 叠加显存峰值
# （首图加载模型时最容易 OOM）。所有出图入口都过这把锁 → 一张出完再出下一张。
_GEN_LOCK = asyncio.Lock()


class ComfyUIError(Exception):
    """ComfyUI 调用失败（连接/执行/超时），调用方可回退占位。"""


def _http_err_text(e: Exception) -> str:
    """把 httpx 异常打成能看懂的一句话。

    实测踩坑：httpx 的 ConnectTimeout/ReadTimeout **str() 是空字符串**，以前日志直接
    `{e}` 格式化，于是屏幕上只有「ComfyUI 连接失败（http://…）：」后面什么都没有，
    分不清是没起、端口错、还是它正忙着上一张图没空回话。
    """
    name = type(e).__name__
    msg = str(e).strip() or {
        "ConnectTimeout": "连接超时（端口没人监听，或被防火墙/Windows 保留端口段卡住）",
        "ReadTimeout": "已连上但对方在超时前没返回（服务在，多半正被上一张图拖住）",
        "ConnectError": "连不上（服务没起来或地址/端口不对）",
        "PoolTimeout": "连接池耗尽",
    }.get(name, "无更多信息")
    return f"{name}: {msg}"


async def _comfyui_alive(base_url: str) -> bool:
    """探活走 /queue，别用 /system_stats。

    实测 2026-10-28：/system_stats 在这台机器上**空闲时也要 8~18s**（它要现场问显存），
    正在出图时更慢；而这里原来只给 3s → 活着的服务被判成死的 → ensure 又 Popen 一个
    实例去抢同一张卡（00:06:55 那次重复拉起就是这么来的），正在跑的那张图反而被拖死。
    /queue 不碰 GPU，空闲时 ~50ms；只要 HTTP 层能答（哪怕 4xx）就算活着。
    """
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            r = await client.get(f"{base_url.rstrip('/')}/queue")
            return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


async def comfyui_queue_state(prompt_id: str, base_url: str = "") -> dict:
    """只读查 ComfyUI /queue：本 prompt_id 是「正在跑 / 排队中 / 已不在队列」。
    给工作台的「处理过程可视化」用——出图请求本身是阻塞的，另起一个查询线程靠它报进度阶段。
    /queue 不碰 GPU，正在出图时也能很快返回。任何异常一律返回 unknown（前端按未知处理）。"""
    base = (base_url or COMFYUI_BASE_URL or "").rstrip("/")
    if not base or not prompt_id:
        return {"state": "unknown"}
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            r = await client.get(f"{base}/queue")
            if r.status_code >= 500:
                return {"state": "unknown"}
            d = r.json()
        running = [it for it in (d.get("queue_running") or []) if len(it) > 1 and it[1] == prompt_id]
        pending = [it for it in (d.get("queue_pending") or []) if len(it) > 1 and it[1] == prompt_id]
        if running:
            return {"state": "running"}
        if pending:
            return {"state": "queued",
                    "position": [it[1] for it in (d.get("queue_pending") or [])].index(prompt_id)}
        return {"state": "gone"}  # 已完成/被清理：交给 history 轮询收尾
    except Exception:  # noqa: BLE001
        return {"state": "unknown"}


async def stop_comfyui_service(base_url: str = "") -> dict:
    """停掉监听该端口的 ComfyUI 进程（含子进程），用于把显存让给本地语音服务。

    只杀「监听 COMFYUI 端口」的进程树，不按进程名模糊匹配（避免误杀别的 python）。
    """
    import psutil

    base = (base_url or COMFYUI_BASE_URL or "").strip().rstrip("/")
    if not base:
        return {"stopped": False, "killed": [], "port": 0}
    from urllib.parse import urlparse as _urlparse
    u = _urlparse(base)
    port = u.port or 8188
    if (u.hostname or "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        # 远端 ComfyUI：不在本机，停不了也不该停
        return {"stopped": False, "killed": [], "port": port, "remote": True}

    def _kill() -> list[str]:
        killed: list[str] = []
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    proc = psutil.Process(c.pid)
                    children = proc.children(recursive=True)
                    for p in [proc, *children]:
                        try:
                            p.kill()
                        except psutil.NoSuchProcess:
                            pass
                    killed.append(f"{c.pid}({proc.name()})")
                except psutil.NoSuchProcess:
                    pass
        return killed

    killed = await asyncio.to_thread(_kill)
    return {"stopped": bool(killed), "killed": killed, "port": port}


# 最近一次由本进程发起的 ComfyUI 启动记录：
# {"at": monotonic, "pid": int, "log": str(启动时日志文件当时的字节数), "offset": int}。
# 同轮对话里组长可能已经用 start_service/桌面壳把 ComfyUI 拉起来了（进程还在启动、
# 端口未就绪），此时探活必然失败——如果不看启动记录就再 Popen 一个，就是双实例抢同一块卡。
_LAUNCH_RECORD: dict | None = None


def _read_boot_log_tail(n: int = 40) -> str:
    """读启动日志尾部。优先 ComfyUI 自写的 comfyui.log（有真实内容），
    退回 comfyui_boot.log（常因 ComfyUI 的 stdout 行缓冲而为空，或残留旧 ^C）。"""
    for p in (COMFY_LOG_PATH, BOOT_LOG_PATH):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                return "\n".join(lines[-n:])[-2000:]
        except OSError:
            continue
    return ""


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _log_progress_since(offset: int, log_path: Path = BOOT_LOG_PATH) -> tuple[int, str]:
    """返回 (日志自 offset 起新增的字节数, 新增文本尾部)。

    「还在加载」和「卡死」在进度上一样——都是没有新输出（ComfyUI 加载模型/前端时长时间无日志）。
    所以这里只用它判断「最近有没有动静」，供日志提示使用，不作为超时主依据。
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return 0, ""
    new = max(0, size - max(0, offset))
    if new <= 0:
        return 0, ""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(max(0, offset))
            chunk = fh.read()[-4000:]
        return new, chunk.decode("utf-8", errors="replace")
    except OSError:
        return new, ""


def _find_starting_comfyui() -> dict | None:
    """找「外部已拉起、正在启动中」的 ComfyUI 进程（cmdline 带 comfyui 且是 main.py / 桌面壳）。

    只用于识别启动状态，不做任何杀进程动作（杀进程在 stop_comfyui_service 里按端口精确匹配）。
    """
    try:
        import psutil
    except ImportError:
        return None
    me = psutil.Process().pid
    for p in psutil.process_iter(["pid", "cmdline"]):
        if p.info["pid"] == me:
            continue
        cl = " ".join(p.info.get("cmdline") or [])
        if not cl:
            continue
        low = cl.lower()
        if "comfyui" not in low:
            continue
        # 排除自己人（game_agent 的脚本/测试命令行里可能引用 comfyui 字样）
        if "game_agent" in low or "cargame" in low or "pytest" in low:
            continue
        if "main.py" in low or "comfy desktop" in low:
            return {"pid": p.info["pid"], "cmdline": cl[:200]}
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return True  # 探测不了就按还活着处理，宁可多等不双开


def _boot_timeout_s(cold_start: bool) -> int:
    """本次启动给多久：首次冷启动（上次运行久远/从没跑过）给 COMFYUI_BOOT_TIMEOUT_S，
    否则给 COMFYUI_BOOT_TIMEOUT_S 的一个较紧值（热启动通常几十秒）。

    「卡在一半」最常见是首次启动：要扫模型目录、解包前端、JIT 编译内核，几分钟正常。
    固定 180s 会在尾巴上把正常的冷启动判死，于是整批图回退占位。
    """
    if cold_start:
        return max(60, COMFYUI_BOOT_TIMEOUT_S)
    return max(60, min(COMFYUI_BOOT_TIMEOUT_S, 240))


async def _wait_alive(base: str, deadline: float, record: dict | None = None) -> dict:
    """轮询探活直到就绪、进程退出或超时。返回 {"ok": bool, "reason": str, "tail": str}。

    与旧实现的区别（都是「卡在一半」的实际踩坑）：
    - 自己拉起的进程若**退出**就立刻失败，不必干等到超时（旧 docstring 承诺了但没实现）；
    - 进程还活着就**不轻言失败**：加载模型/编译内核时长时间无输出是正常的，
      只要没到硬上限（COMFYUI_BOOT_HARD_CAP_S）就继续等；
    - 无进展超过 COMFYUI_BOOT_STALL_S 时在日志里记一条（便于事后复盘），但不打断等待；
    - 超时/失败时把**新增日志**尾部带回，能直接看出是卡在哪个 import/哪步。
    """
    import psutil  # 局部导入，避免无 psutil 环境下模块导入失败

    pid = (record or {}).get("pid")
    has_own_pid = pid is not None  # 只有本进程拉起的才有 pid；外部/桌面壳起的不该按 pid 判死
    log_offset = (record or {}).get("offset", 0)
    started_at = time.monotonic()
    hard_deadline = started_at + max(60, COMFYUI_BOOT_HARD_CAP_S)
    last_progress_at = started_at
    last_size = -1
    stall_warned = False

    while True:
        now = time.monotonic()
        if await _comfyui_alive(base):
            return {"ok": True, "reason": "", "tail": ""}

        # 进程已经退出（或崩了）→ 立即失败，不空等
        if has_own_pid:
            try:
                alive = psutil.pid_exists(pid) and psutil.Process(pid).is_running()
            except Exception:  # noqa: BLE001
                alive = True  # 探测不了按活着处理
            if not alive:
                _, tail = _log_progress_since(log_offset)
                return {"ok": False, "reason": "启动进程已退出（多半是依赖/端口/显存错误，见日志）",
                        "tail": tail}

        # 进度：日志有没有新增长。一直没有任何日志增长，说明可能根本没在往日志写
        # （桌面壳/外部实例），此时进度只看日志会误判——用「已耗时」兜底，不打断。
        size = _file_size(COMFY_LOG_PATH)
        if size > last_size:
            last_size = size
            last_progress_at = now
            stall_warned = False
        elif (not stall_warned and now - last_progress_at > COMFYUI_BOOT_STALL_S
              and now - started_at > COMFYUI_BOOT_STALL_S):
            anchor = "日志" if last_size > 0 else "启动"
            log.warning("ComfyUI %s后 %.0fs 无新日志（加载模型/编译内核时属正常，继续等待）",
                        anchor, now - last_progress_at)
            stall_warned = True

        if now >= deadline or now >= hard_deadline:
            _, tail = _log_progress_since(log_offset)
            return {"ok": False, "reason": "等待超时", "tail": tail}

        await asyncio.sleep(3)


def _align_launch_cmd(cmd: str, base: str) -> str:
    """把启动命令里的 --port / --listen 对齐到本次要探活的 base URL。

    为什么必须做（实测 2026-10-27）：.env 的 COMFYUI_LAUNCH_CMD 写死 --port 8588，
    而美术档位 profile 里的 base_url 可能还是旧端口 8188 —— 结果启动了一个绑 8588
    的实例，却对着 8188 探活，180s 永远等不到，整批图全部回退占位。
    只对 server 型启动命令（python + main.py）改写；桌面壳 exe 不认识这些参数，不动。
    """
    if "main.py" not in cmd:
        return cmd
    from urllib.parse import urlparse as _up
    u = _up(base)
    port = u.port or 8188
    host = u.hostname or "127.0.0.1"
    out = cmd
    if re.search(r"--port\s+\S+", out):
        out = re.sub(r"--port\s+\S+", f"--port {port}", out)
    else:
        out = f"{out} --port {port}"
    if re.search(r"--listen\s+\S+", out):
        out = re.sub(r"--listen\s+\S+", f"--listen {host}", out)
    else:
        out = f"{out} --listen {host}"
    if out != cmd:
        log.info("启动命令端口对齐：%s -> 探活地址 %s", cmd, base)
    return out


# 启动失败快速失败缓存：base -> (monotonic, error, pid_at_fail)。同 base 60s 内的
# 重复 ensure 直接返回上次失败，不再各自空等（实测 20 张图 × 每张 ensure = 1 小时灾难）。
# 但如果那次失败时拉起的进程**还活着**（其实只是加载慢），不能熔断——否则明明它在
# 起来，后面每张图都秒失败。所以熔断前先查 pid 是否还活着。
_ENSURE_FAIL_CACHE: dict[str, tuple[float, str, int | None]] = {}
_ENSURE_FAIL_TTL_S = 60.0


def _inject_log_args(cmd: str) -> str:
    """给 server 型启动命令补上 ComfyUI 自己的日志参数。

    ComfyUI 默认 args.verbose=[] → app.logger 不写任何文件，只有控制台；
    而控制台输出又被它自己的 LogInterceptor 行缓冲（重定向到文件时 line_buffering=False）
    → 我们的 comfyui_boot.log 基本是空的，出事只能干瞪眼。
    这里加 `--log-stdout`（让 INFO 走 stdout 而非 stderr，经同一文件句柄立即落盘）
    + `--verbose INFO <comfyui.log>`（ComfyUI 自己写日志文件）。
    桌面壳 exe 不认这些参数，只在含 main.py 时注入。
    """
    if "main.py" not in cmd:
        return cmd
    extra: list[str] = []
    if "--log-stdout" not in cmd:
        extra.append("--log-stdout")
    if "--verbose" not in cmd:
        COMFY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        extra.append(f'--verbose INFO "{COMFY_LOG_PATH}"')
    if not extra:
        return cmd
    out = f"{cmd} {' '.join(extra)}"
    log.info("启动命令补日志参数：%s", " ".join(extra))
    return out


def _is_cold_start() -> bool:
    """这次启动是不是「首次/久没跑过」的冷启动——冷启动慢是常态，给足超时。

    依据（任一成立即算冷启动）：
    - 从没成功启动过（没有 comfyui.log）；
    - 上次成功启动距今 ≥ 6 小时；
    - 日志文件很大（>8MB，说明历史运行久、模型/内核缓存可能已被系统回收）。
    """
    try:
        st = COMFY_LOG_PATH.stat()
    except OSError:
        return True
    if st.st_size > 8 * 1024 * 1024:
        return True
    return (time.time() - st.st_mtime) >= 6 * 3600


async def ensure_comfyui_running(base_url: str = "", timeout_s: int = 0) -> dict:
    """确保 ComfyUI API 可用；未运行则自动启动并轮询等待就绪。

    启动状态校验（同轮对话实测踩坑后加的，优先级从高到低）：
    1. 探活成功 → 直接返回（不再走任何启动流程）；
    2. 本进程最近发起过启动且进程还活着 → 只等它就绪，**绝不重复 Popen**；
    3. 外部（组长 start_service / 用户手动 / 桌面壳）已拉起的实例还在启动 → 也只等；
    4. 都不是 → 才真正启动。启动输出落 comfyui_boot.log，ComfyUI 自写 comfyui.log，
       超时/退出时把新增日志尾部带回。
    超时是动态的：首次/久未运行的冷启动给 COMFYUI_BOOT_TIMEOUT_S（默认 600s），
    热启动给较短值；只要进程还活着且没到硬上限就继续等，进程退出则立即失败。
    熔断：启动失败后 60s 内重复调用快速失败——但进程还活着（只是慢）时不熔断。

    返回 {started: bool, waited_ms: int, error: str|None}；error 非空表示无法拉起。
    """
    global _LAUNCH_RECORD
    base = (base_url or COMFYUI_BASE_URL).rstrip("/")
    if not base or not COMFYUI_AUTOSTART:
        return {"started": False, "waited_ms": 0, "error": None}
    if await _comfyui_alive(base):
        if _LAUNCH_RECORD is not None:
            log.info("ComfyUI 已就绪（%s），清除启动记录", base)
            _LAUNCH_RECORD = None
        _ENSURE_FAIL_CACHE.pop(base, None)
        return {"started": False, "waited_ms": 0, "error": None}
    cached = _ENSURE_FAIL_CACHE.get(base)
    if cached and time.monotonic() - cached[0] < _ENSURE_FAIL_TTL_S:
        # 上次失败时拉起的进程还活着 → 不是真失败（多半是加载慢），不熔断，继续等
        if not _pid_alive(cached[2]):
            log.warning("ComfyUI %s 刚启动失败过（%.0fs 前），熔断快速失败：%s",
                        base, time.monotonic() - cached[0], cached[1][:120])
            return {"started": False, "waited_ms": 0, "error": cached[1]}
        log.info("ComfyUI %s 上次判失败但进程 pid=%s 仍存活，继续等待不熔断", base, cached[2])
        _ENSURE_FAIL_CACHE.pop(base, None)

    # 起 ComfyUI 前先停语音服务并等显存释放（与 tts.ensure_tts_running 对称）。
    # 同样必须在 _AUTOSTART_LOCK 之外：GPU 锁持有者会走进来抢 _AUTOSTART_LOCK。
    room = await gpu_arbiter.make_room("image")
    if room.get("stopped"):
        log.info("启动 ComfyUI 前已腾出显存：%s", room["stopped"])

    async with _AUTOSTART_LOCK:
        if await _comfyui_alive(base):  # 并发生成时双检
            _LAUNCH_RECORD = None
            _ENSURE_FAIL_CACHE.pop(base, None)
            return {"started": False, "waited_ms": 0, "error": None}
        t0 = time.monotonic()
        started_here = False
        spawn_cold = False
        boot_log_path = BOOT_LOG_PATH

        ours = _LAUNCH_RECORD
        if ours is not None and _pid_alive(ours.get("pid")):
            log.info("ComfyUI 正在启动中（本进程 %.0fs 前发起，pid=%s，%s），等待就绪不重复拉起",
                     t0 - ours["at"], ours["pid"], base)
        else:
            ext = await asyncio.to_thread(_find_starting_comfyui)
            if ext is not None:
                log.info("检测到外部 ComfyUI 实例正在启动（pid=%s：%s），等待就绪不重复拉起",
                         ext["pid"], ext["cmdline"])
                # 外部实例的 pid 不在我们手上，清掉可能残留的旧记录，避免 _wait_alive
                # 拿一个已退出的旧 pid 误判成「进程退出」立刻失败
                _LAUNCH_RECORD = None
            else:
                if ours is not None:
                    log.info("上次启动的 ComfyUI 进程（pid=%s）已退出且服务未就绪，重新拉起",
                             ours.get("pid"))
                _LAUNCH_RECORD = None
                cmd = COMFYUI_LAUNCH_CMD
                if not cmd:
                    # 优先：实例 python + main.py 直启服务（无人值守可用）。
                    # 端口跟随 COMFYUI_BASE_URL，避免硬编码端口落在 Windows 保留端口段
                    # （如 8188 属于 8155-8254）导致 WinError 10013 绑定失败。
                    from urllib.parse import urlparse as _urlparse
                    _u = _urlparse(base)
                    _host = _u.hostname or "127.0.0.1"
                    _port = _u.port or 8188
                    for py, main in _LAUNCH_PY_MAIN:
                        if Path(py).is_file() and Path(main).is_file():
                            cmd = f'"{py}" "{main}" --listen {_host} --port {_port}'
                            break
                if not cmd:
                    # 兜底：桌面壳 exe（可能需手动点实例卡片）
                    for cand in _LAUNCH_CANDIDATES:
                        p = Path(cand.replace("%LOCALAPPDATA%", str(Path.home() / "AppData" / "Local")))
                        if p.is_file():
                            cmd = f'"{p}"'
                            break
                if not cmd:
                    log.error("找不到 ComfyUI 启动程序，且 %s 探活失败", base)
                    return {"started": False, "waited_ms": 0,
                            "error": "未找到 ComfyUI 启动程序，请在 .env 配置 COMFYUI_LAUNCH_CMD"}
                cmd = _align_launch_cmd(cmd, base)
                cmd = _inject_log_args(cmd)
                # 必须在 Popen 之前判定冷热：ComfyUI 一起来就会刷新日志 mtime，
                # 之后再判会永远当成热启动。
                spawn_cold = _is_cold_start()
                try:
                    # 同步 Popen(shell)：支持完整命令行参数；仅创建进程（瞬时返回，不 wait），
                    # 兼容 Selector 事件循环（asyncio.create_subprocess_* 需要 Proactor）
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                    boot_log_path.parent.mkdir(parents=True, exist_ok=True)
                    # 记录启动前的日志位置：只读「本次新增」的输出，避免把上次的老日志当成本次进度
                    offset = max(0, _file_size(COMFY_LOG_PATH))
                    boot_fh = open(boot_log_path, "ab")  # noqa: SIM115 - 进程生命周期内持有
                    proc = subprocess.Popen(
                        cmd, shell=True,
                        cwd=str(Path(cmd.strip().strip('"').split('"')[0]).parent)
                        if cmd.strip().startswith('"') else None,
                        stdout=boot_fh, stderr=subprocess.STDOUT,
                        creationflags=flags, close_fds=True)
                    boot_fh.close()  # 句柄交给子进程后即可关闭本进程侧的副本
                    started_here = True
                    _LAUNCH_RECORD = {"at": time.monotonic(), "pid": proc.pid,
                                      "offset": offset}
                    log.info("ComfyUI 启动命令已发起（pid=%s，端口=%s，启动日志=%s，自写日志=%s）",
                             proc.pid, base, boot_log_path, COMFY_LOG_PATH)
                except Exception as e:  # noqa: BLE001
                    log.exception("ComfyUI 启动命令执行失败")
                    err = f"启动 ComfyUI 失败：{e}"
                    _ENSURE_FAIL_CACHE[base] = (time.monotonic(), err, None)
                    return {"started": False, "waited_ms": 0, "error": err}

        # 无论谁启动的，统一等到就绪；自己拉起的进程若中途退出则提前失败（带日志尾部）
        cold = started_here and spawn_cold
        budget = timeout_s or _boot_timeout_s(cold)
        if started_here:
            log.info("ComfyUI %s启动，等待上限 %ds（硬上限 %ds）",
                     "冷" if cold else "热", budget, COMFYUI_BOOT_HARD_CAP_S)
        wait = await _wait_alive(base, t0 + budget, _LAUNCH_RECORD)
        if wait["ok"]:
            waited = int((time.monotonic() - t0) * 1000)
            log.info("ComfyUI 就绪（%s，等待 %dms，本次%s发起）",
                     base, waited, "由本进程" if started_here else "外部/早前")
            _LAUNCH_RECORD = None
            return {"started": started_here, "waited_ms": waited, "error": None}
        tail = (wait.get("tail") or "").strip() or _read_boot_log_tail()
        err = (f"ComfyUI（{base}）未就绪：{wait['reason']}（已等 {budget}s）"
               + (f"，日志尾部：{tail[-500:]}" if tail else
                  "，且无日志（可能是外部拉起的实例或输出被缓冲）"))
        _ENSURE_FAIL_CACHE[base] = (time.monotonic(), err, (_LAUNCH_RECORD or {}).get("pid"))
        log.error("ComfyUI 未就绪（%s）：%s\n启动日志尾部：\n%s",
                  base, wait["reason"], tail or "（无）")
        return {"started": False, "waited_ms": int((time.monotonic() - t0) * 1000), "error": err}


def _default_workflow() -> dict:
    """经典 SD 文生图工作流（API 格式）。ckpt_name 留空由自动探测填充。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": 20, "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "game_agent", "images": ["8", 0]}},
    }


def _target_size_node(image_link: list) -> dict:
    """参考图缩放节点：把参考图拉到「本次请求的宽高」再进 latent。

    为什么必须有：img2img 链路的 latent 来自参考图本身，而 _patch_workflow 原来
    只往 Empty*LatentImage 写宽高（该链路根本没有该节点），于是提示词里写
    1792x1024 横构图、实际按参考图出成 1024x1536 竖图。
    （风格参考链路不再经过本节点：它的采样画布是请求宽高的空 latent，参考图只做条件。）
    标记靠 _meta.title 传递（ComfyUI 执行时忽略 _meta，只做 UI 用）。
    """
    return {
        "class_type": "ImageScale",
        "_meta": {"title": TARGET_SIZE_TITLE},
        "inputs": {"image": image_link, "upscale_method": "lanczos",
                   "width": 1024, "height": 1024, "crop": "center"},
    }


def _img2img_workflow() -> dict:
    """经典 SD 图生图工作流：LoadImage + VAEEncode 作 latent，denoise 由 generate 填。
    参考图经 /upload/image 上传后以文件名引用。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": 20, "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": IMG2IMG_DENOISE,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["15", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "game_agent", "images": ["8", 0]}},
        "14": {"class_type": "LoadImage", "inputs": {"image": ""}},
        "16": _target_size_node(["14", 0]),
        "15": {"class_type": "VAEEncode", "inputs": {"pixels": ["16", 0], "vae": ["4", 2]}},
    }


def _dit_img2img_workflow() -> dict:
    """Anima/Qwen 架构 DiT 图生图三件套工作流。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": 20, "cfg": 2.5,
            "sampler_name": "euler", "scheduler": "simple", "denoise": IMG2IMG_DENOISE,
            "model": ["10", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["15", 0]}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "", "weight_dtype": "default"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["12", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "game_agent", "images": ["8", 0]}},
        "10": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.1, "model": ["4", 0]}},
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": "", "type": "qwen_image"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": ""}},
        "14": {"class_type": "LoadImage", "inputs": {"image": ""}},
        "16": _target_size_node(["14", 0]),
        "15": {"class_type": "VAEEncode", "inputs": {"pixels": ["16", 0], "vae": ["12", 0]}},
    }


def _style_ref_workflow() -> dict:
    """Qwen 架构「风格参考」工作流：TextEncodeQwenImage21 把参考图作为 VAE latent
    拼进文本编码序列——模型看到画风/质感/配色但不锁人物与构图，按提示词生成全新内容。
    仅 Qwen 三件套可用（SD checkpoint 无原生对应节点）。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": 20, "cfg": 2.5,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
            "model": ["10", 0], "positive": ["21", 0], "negative": ["21", 1],
            # 采样画布必须是请求宽高的空 latent。以前接 ["21", 2]（编码节点的 latent 出口
            # = 第一张参考图的尺寸），参考图被 ImageScale center 裁成请求比例后，采样就等于
            # 在“参考图的中段裁切带”上作画——1792x1024 请求配 1024x1536 立绘，出得只剩
            # 半截躯干（2026-10-29 teng-ultimate-n3）。
            "latent_image": ["5", 0]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": 1024, "height": 1024, "batch_size": 1}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "", "weight_dtype": "default"}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["12", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "game_agent", "images": ["8", 0]}},
        "10": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.1, "model": ["4", 0]}},
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": "", "type": "qwen_image"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": ""}},
        "20": {"class_type": "LoadImage", "inputs": {"image": ""}},
        "21": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["11", 0], "prompt": "", "negative_prompt": "",
            # resolution：参考图只作为条件（画风/质感/配色）进编码序列，按长边 1024
            # 等比缩放后 VAE 编码，token 数可控且保留原比例。0 = 原图尺寸。
            "resolution": 1024,
            # images 是 COMFY_AUTOGROW_V3 动态输入：提交时必须用**扁平点号键**
            # images.image_1（执行端再组装成 dict）。传 [["16",0]] 或嵌套 dict 都会被
            # 静默丢弃——提示词和参考图都没进模型，输出自由发挥的随机照片（2026-10-29 青蛙图）
            "images.image_1": ["20", 0], "vae": ["12", 0]}},
    }


STYLE_PROMPT_PREFIX = "沿用参考图的画风、厚涂质感、光影处理与配色氛围，按以下描述生成全新内容："


def _dit_workflow() -> dict:
    """Qwen 架构 DiT 文生图三件套工作流（Anima 等动漫特化模型）：
    UNETLoader + ModelSamplingAuraFlow(shift) + CLIPLoader(type=qwen_image) + VAELoader。
    模型名留空由自动探测填充。"""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": 20, "cfg": 2.5,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
            "model": ["10", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0]}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "", "weight_dtype": "default"}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["11", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["12", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": "game_agent", "images": ["8", 0]}},
        "10": {"class_type": "ModelSamplingAuraFlow", "inputs": {"shift": 3.1, "model": ["4", 0]}},
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": "", "type": "qwen_image"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": ""}},
    }


def _follow_text_node(wf: dict, node_id, depth: int = 0) -> str | None:
    """从 KSampler positive/negative 链接回溯到可写文本的节点。

    两种编码节点都要认：CLIPTextEncode 用 ``text``；
    Qwen 风格参考链路的 TextEncodeQwenImage21 用 ``prompt``/``negative_prompt``
    （以前只认 text，风格参考工作流的提示词一直没被写进去，采样等于空提示词
    无条件生成——2026-10-29 实测出青蛙/废墟照片就是这么来的）。"""
    node = wf.get(str(node_id))
    if node is None or depth > 3:
        return None
    inputs = node.get("inputs", {})
    if isinstance(inputs.get("text"), str) or isinstance(inputs.get("prompt"), str):
        return str(node_id)
    link = inputs.get("text")  # text 由上游节点提供时跟进一跳
    if isinstance(link, list) and len(link) >= 1:
        return _follow_text_node(wf, link[0], depth + 1)
    return None


def _patch_workflow(wf: dict, prompt: str, negative: str,
                    width: int, height: int, batch: int = 1) -> dict:
    """对工作流打补丁：prompt / 负面词 / 尺寸 / 随机 seed；缺失节点保持模板原值。"""
    wf = json.loads(json.dumps(wf))  # deep copy，避免污染模板缓存
    patched_positive = False
    for node in wf.values():
        ct = node.get("class_type", "")
        if ct in ("KSampler", "KSamplerAdvanced"):
            inputs = node.setdefault("inputs", {})
            if "seed" in inputs:
                inputs["seed"] = random.randint(0, 2**32 - 1)
            if ct == "KSamplerAdvanced" and "noise_seed" in inputs:
                inputs["noise_seed"] = random.randint(0, 2**32 - 1)
            for slot, text in (("positive", prompt), ("negative", negative)):
                link = inputs.get(slot)
                if isinstance(link, list) and len(link) >= 1:
                    tid = _follow_text_node(wf, link[0])
                    if tid is not None:
                        tinputs = wf[tid]["inputs"]
                        if isinstance(tinputs.get("text"), str):
                            tinputs["text"] = text
                        elif isinstance(tinputs.get("prompt"), str):
                            # TextEncodeQwenImage21：正负词是两个独立字段
                            tinputs["prompt" if slot == "positive" else "negative_prompt"] = text
                        if slot == "positive":
                            patched_positive = True
    if not patched_positive:
        # 回退：第一个 text 为字符串的 CLIPTextEncode 当正向提示词
        for node in wf.values():
            if (node.get("class_type") == "CLIPTextEncode"
                    and isinstance(node.get("inputs", {}).get("text"), str)):
                node["inputs"]["text"] = prompt
                break
    for node in wf.values():
        if node.get("class_type") in ("EmptyLatentImage", "EmptySD3LatentImage"):
            inputs = node.setdefault("inputs", {})
            inputs["width"], inputs["height"] = width, height
            inputs["batch_size"] = max(1, int(batch))
            break
        # img2img 链路没有 Empty*LatentImage，宽高写在 ImageScale 上
        if node.get("class_type") == "ImageScale" and \
                (node.get("_meta") or {}).get("title") == TARGET_SIZE_TITLE:
            inputs = node.setdefault("inputs", {})
            inputs["width"], inputs["height"] = width, height
    return wf


# 高分辨率出图：latent 采样步长与像素输出对齐步长
_LATENT_STEP = 16          # EmptySD3LatentImage 宽高按 16 对齐（Qwen VAE 16x 下采样）
_PIXEL_STEP = 8            # ImageScale 目标尺寸按 8 对齐（2K/竖图/横图常用尺寸都是 8 的倍数）


def _snap(v: int, step: int) -> int:
    """把尺寸对齐到 step 的整数倍（就近取整，最小 1 个 step）。"""
    return max(step, int(round(int(v) / step)) * step)


def _is_hires(width: int, height: int) -> bool:
    """目标像素超过分块解码阈值（默认 1.6MP）——大图才启用 tiled 解码/基准采样等特殊处理。"""
    return (max(1, width) * max(1, height)) / 1_000_000.0 > COMFYUI_TILED_MP


def _plan_sizes(width: int, height: int) -> dict:
    """规划采样尺寸 / 解码方式 / 最终尺寸，返回一个计划字典。

    目标像素 ≤ COMFYUI_DIRECT_MAX_MP（默认 4MP）：
      - 直接按目标（VAE 16 对齐）采样，全画质；若 16 对齐的采样尺寸与用户要求的 8 对齐目标
        不一致（如 2368 vs 2360），解码后在像素态缩放精确落位（不重采样）。
    目标更大（如 4K≈8MP，8G 显存单次采样放不下）：
      - 先按阈值内基准分辨率等比采样 → tiled 解码 → **像素态** Lanczos 放大到目标尺寸。
        快，但只是插值放大，不新增细节。
    """
    w, h = max(1, int(width)), max(1, int(height))
    out_w, out_h = _snap(w, _PIXEL_STEP), _snap(h, _PIXEL_STEP)
    mp = (out_w * out_h) / 1_000_000.0
    if mp <= COMFYUI_DIRECT_MAX_MP:
        sample_w, sample_h = _snap(out_w, _LATENT_STEP), _snap(out_h, _LATENT_STEP)
        return {"two_stage": False, "sample_w": sample_w, "sample_h": sample_h,
                "out_w": out_w, "out_h": out_h}
    scale = (COMFYUI_DIRECT_MAX_MP / mp) ** 0.5
    # 向下取整到 16 的倍数，保证基准采样像素严格不超过阈值（round 可能多出一格越界）
    base_w = max(_LATENT_STEP, int(out_w * scale) // _LATENT_STEP * _LATENT_STEP)
    base_h = max(_LATENT_STEP, int(out_h * scale) // _LATENT_STEP * _LATENT_STEP)
    return {"two_stage": True, "sample_w": base_w, "sample_h": base_h,
            "out_w": out_w, "out_h": out_h}


def _tiled_decode_node(samples_link: list, vae_link: list) -> dict:
    """分块 VAE 解码节点：大图解码显存峰值远低于一次性解码（8G 显存必用）。"""
    return {"class_type": "VAEDecodeTiled", "inputs": {
        "samples": samples_link, "vae": vae_link,
        "tile_size": int(COMFYUI_VAE_TILE), "overlap": int(COMFYUI_VAE_TILE_OVERLAP),
        "temporal_size": 8, "temporal_overlap": 4}}


def _find_node(wf: dict, class_type: str) -> tuple[str, dict] | tuple[None, None]:
    for k, n in wf.items():
        if n.get("class_type") == class_type:
            return k, n
    return None, None


def _new_id(wf: dict) -> str:
    return str(max((int(k) for k in wf if str(k).isdigit()), default=0) + 1)


def _apply_hires(wf: dict, width: int, height: int) -> tuple[int, int]:
    """把工作流改造成「大图安全」形态，返回实际目标输出尺寸 (out_w, out_h)。

    改动（只对「采样器 → VAEDecode → SaveImage」的文生图/风格参考拓扑深度改造）：
    1) latent 采样尺寸按 _plan_sizes 写回 Empty*LatentImage / ImageScale；
    2) VAEDecode 换成 VAEDecodeTiled（大图分块解码，防 8G 解码 OOM）；
    3) 解码后在**像素态**用 ImageScale(Lanczos) 落到目标尺寸：
       - 直接出图但 16 对齐零头（如采样 2368×1632 → 目标 2360×1640）：做一次极小下采样精确落位；
       - 两段式（目标超阈值）：把基准分辨率解码图放大到目标尺寸。
       用像素态缩放而非 LatentUpscale：Qwen VAE 是 16x 下采样，而 LatentUpscale 按 8x 语义
       解释 width/height（实测会放大 2 倍到 4720×3280），像素态缩放与 VAE 倍率无关，稳定。
    图生图（VAEEncode 供 latent）等复杂拓扑只把 VAEDecode 换成 tiled，其余不动。
    """
    plan = _plan_sizes(width, height)
    sw, sh = plan["sample_w"], plan["sample_h"]
    out_w, out_h = plan["out_w"], plan["out_h"]

    # 小图（≤ 分块阈值）：完全不改工作流，保持历史行为（原尺寸原样返回）。
    if not _is_hires(width, height):
        return int(width), int(height)

    # 1) 采样尺寸写回
    for node in wf.values():
        ct = node.get("class_type")
        if ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
            node.setdefault("inputs", {})["width"] = sw
            node["inputs"]["height"] = sh
            break
        if ct == "ImageScale" and (node.get("_meta") or {}).get("title") == TARGET_SIZE_TITLE:
            node.setdefault("inputs", {})["width"] = sw
            node["inputs"]["height"] = sh

    # 2) tiled 解码 + 3) 像素态尺寸落位
    dec_id, dec = _find_node(wf, "VAEDecode")
    if dec is not None:
        samples_link = (dec.get("inputs") or {}).get("samples")
        vae_link = (dec.get("inputs") or {}).get("vae")
        if isinstance(samples_link, list) and isinstance(vae_link, list):
            wf[dec_id] = _tiled_decode_node(samples_link, vae_link)
            if (sw, sh) != (out_w, out_h):
                _insert_pixel_scale_after(wf, dec_id, out_w, out_h)
    return out_w, out_h


def _insert_pixel_scale_after(wf: dict, dec_id: str, out_w: int, out_h: int) -> None:
    """在解码节点之后、其所有 IMAGE 消费者之前插入 ImageScale(Lanczos) 落到目标尺寸。"""
    scale_id = _new_id(wf)
    wf[scale_id] = {"class_type": "ImageScale", "inputs": {
        "image": [dec_id, 0], "upscale_method": "lanczos",
        "width": out_w, "height": out_h, "crop": "disabled"}}
    for k, node in wf.items():
        if k == scale_id:
            continue
        for key, val in list((node.get("inputs") or {}).items()):
            if isinstance(val, list) and len(val) == 2 and str(val[0]) == dec_id and val[1] == 0:
                node["inputs"][key] = [scale_id, 0]


def _apply_lora(wf: dict, lora_name: str, strength: float,
                clip_strength: float | None = None) -> dict:
    """在采样器的模型链上插入 LoRA（可选，lora_name 为空则原样返回）。

    兼容两种拓扑：CheckpointLoaderSimple 直接供模型 (SD/Illustrious 链路),
    以及 UNETLoader→ModelSamplingAuraFlow 供模型 (Qwen/DiT 链路)。
    LoRA 只挂模型输出，不动 latent，因此对文生图/图生图/风格参考都通用。
    """
    if not lora_name:
        return wf
    wf = json.loads(json.dumps(wf))
    sampler = next((n for n in wf.values()
                    if n.get("class_type") in ("KSampler", "KSamplerAdvanced")), None)
    if sampler is None:
        return wf
    model_link = sampler["inputs"].get("model")
    if not (isinstance(model_link, list) and len(model_link) == 2):
        return wf
    model_src = str(model_link[0])
    # LoRA 的 clip 输入:checkpoint 的 CLIP 出口，或独立的 CLIPLoader 节点
    if wf.get(model_src, {}).get("class_type") == "CheckpointLoaderSimple":
        clip_link = [model_src, 1]
    else:
        clip_src = next((k for k, n in wf.items()
                         if n.get("class_type") in ("CLIPLoader", "DualCLIPLoader",
                                                    "SD3CLIPLoader", "QwenImageCLIPLoader")), None)
        clip_link = [clip_src, 0] if clip_src else None
    lora_id = str(max((int(k) for k in wf if str(k).isdigit()), default=0) + 1)
    inputs: dict = {"model": [model_src, 0], "lora_name": lora_name,
                    "strength_model": float(strength)}
    if clip_link:
        inputs["clip"] = clip_link
        inputs["strength_clip"] = float(clip_strength if clip_strength is not None else strength)
    wf[lora_id] = {"class_type": "LoraLoader", "inputs": inputs}
    # 把原来接在 model_src 上的 model / clip 消费者改接到 LoRA 出口 (跳过 LoRA 自身，
    # 否则它自己的 model/clip 入参也会被改写，形成自引用死循环)
    for node in wf.values():
        if node is wf[lora_id]:
            continue
        for key, val in list(node.get("inputs", {}).items()):
            if not (isinstance(val, list) and len(val) == 2):
                continue
            if str(val[0]) == model_src and val[1] == 0:
                node["inputs"][key] = [lora_id, 0]
            elif clip_link and str(val[0]) == str(clip_link[0]) and val[1] == clip_link[1]:
                node["inputs"][key] = [lora_id, 1]
    return wf


async def _resolve_lora_name(client: httpx.AsyncClient, base: str, lora_name: str) -> str:
    """把配置里的 LoRA 路径对齐成 ComfyUI 实际列出的文件名。

    Windows 版 ComfyUI 的 lora_name 列表用反斜杠（qwen21_style\\xxx.safetensors），
    而 .env 里手写的一般是正斜杠；原名直接提交会被 400 value_not_in_list。
    这里按分隔符归一化后匹配，回填列表里的原样字符串。
    """
    if not lora_name:
        return ""
    names = list(map(str, await _fetch_names(client, base, "LoraLoader", "lora_name")))
    if lora_name in names:
        return lora_name

    def _canon(s: str) -> str:
        return s.replace("\\", "/").lower().strip("/")

    want = _canon(lora_name)
    for n in names:
        if _canon(n) == want:
            return n
    # 兜底：只写了文件名（没带子目录）也认
    for n in names:
        if _canon(n).rsplit("/", 1)[-1] == want.rsplit("/", 1)[-1]:
            return n
    avail = ", ".join(names[:8])
    raise ComfyUIError(f"LoRA「{lora_name}」不在 ComfyUI 的 models/loras 列表中。可用：{avail}…")


async def _fetch_names(client: httpx.AsyncClient, base: str,
                       node_class: str, field: str) -> list[str]:
    """取某加载节点在 /object_info 里的可选模型名列表。"""
    try:
        resp = await client.get(f"{base}/object_info/{node_class}")
        resp.raise_for_status()
        info = resp.json().get(node_class, {}).get("input", {}).get("required", {})
        return info.get(field, [[], {}])[0]
    except ComfyUIError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise ComfyUIError(
            f"连不上 ComfyUI（{base}）：{e}。请确认 ComfyUI 已启动、地址端口正确"
            f"（.env COMFYUI_BASE_URL）；若本机开了系统代理，本地地址必须走直连（已内置）。") from e
    except Exception as e:  # noqa: BLE001
        raise ComfyUIError(f"探测 ComfyUI 可用模型失败（{node_class}）：{e}") from e


def _pick(names: list[str], prefer: str = "") -> str:
    """COMFYUI_CHECKPOINT 显式指定时校验并使用；否则优先 prefer 前缀，再取第一个。"""
    if prefer:
        if prefer not in names:
            raise ComfyUIError(
                f"COMFYUI_CHECKPOINT={prefer} 不在可用列表：{', '.join(map(str, names[:8]))}…")
        return prefer
    for n in names:
        if str(n).lower().startswith("qwen"):
            return str(n)
    return str(names[0])


def _pick_clip(names: list[str]) -> str:
    """从 text_encoders 列表挑与 qwen_image_2.1 扩散模型（hidden=4096）匹配的文本编码器。

    - 显式配置 COMFYUI_CLIP：校验存在即用。
    - 否则优先「8b」（Qwen3-VL-8B → 4096 维，与 Qwen-Image-2.1 对齐）；
    - 其次排除「4b/2b/06b 等小模型」（2560 等更小维度，会与扩散模型维度不符）；
    - 最后兜底普通 _pick。
    不能再用 _pick 的「取第一个含 qwen 的文件」：text_encoders 里现在同时躺着给
    MiniMax-H3/ClipProj 用的 qwen3vl_4b（2560 维），选中它会让 KSampler 报
    "normalized_shape=[4096] ... got [1,N,2560]"（2026-11-05）。
    """
    if COMFYUI_CLIP:
        if COMFYUI_CLIP not in names:
            raise ComfyUIError(
                f"COMFYUI_CLIP={COMFYUI_CLIP} 不在可用列表：{', '.join(map(str, names[:8]))}…")
        return COMFYUI_CLIP
    for n in names:
        low = str(n).lower()
        if low.startswith("qwen") and "8b" in low:
            return str(n)
    filtered = [n for n in names
                if not any(tag in str(n).lower() for tag in ("4b", "2b", "06b", "0.6b"))]
    if filtered:
        return _pick(list(map(str, filtered)))
    return _pick(list(map(str, names)))


async def _resolve_models(client: httpx.AsyncClient, base: str, wf: dict) -> str:
    """探测可用模型并填充工作流里留空的加载节点。
    返回模式：'ckpt'（SD 经典）/ 'dit'（Anima/Qwen 三件套）；无空节点返回 ''。
    模板由调用方按探测结果选择（checkpoint 空时自动切 DiT 工作流）。"""
    def _empty(wf_: dict, class_type: str, field: str) -> bool:
        return any(n.get("class_type") == class_type
                   and n.get("inputs", {}).get(field) in ("", None)
                   for n in wf_.values())

    # 1) checkpoint 路径
    if _empty(wf, "CheckpointLoaderSimple", "ckpt_name"):
        names = await _fetch_names(client, base, "CheckpointLoaderSimple", "ckpt_name")
        if not names:
            return "no_ckpt"  # 无 checkpoint：调用方决定是否切 DiT 三件套
        name = _pick(list(map(str, names)), COMFYUI_CHECKPOINT)
        for n in wf.values():
            if (n.get("class_type") == "CheckpointLoaderSimple"
                    and n.get("inputs", {}).get("ckpt_name") in ("", None)):
                n["inputs"]["ckpt_name"] = name
        return "ckpt"

    # 2) DiT 三件套路径（Anima/Qwen 架构）
    if _empty(wf, "UNETLoader", "unet_name"):
        unets = list(map(str, await _fetch_names(client, base, "UNETLoader", "unet_name")))
        clips = list(map(str, await _fetch_names(client, base, "CLIPLoader", "clip_name")))
        vaes = list(map(str, await _fetch_names(client, base, "VAELoader", "vae_name")))
        if not (unets and clips and vaes):
            raise ComfyUIError(
                "ComfyUI 未找到可用模型：无 checkpoint 且 DiT 三件套不全"
                f"（UNET {len(unets)} / CLIP {len(clips)} / VAE {len(vaes)}），请先安装模型")
        unet = _pick(unets, COMFYUI_CHECKPOINT)
        clip = _pick_clip(clips)
        vae = _pick(vaes)
        for n in wf.values():
            ct, inputs = n.get("class_type"), n.setdefault("inputs", {})
            if ct == "UNETLoader" and inputs.get("unet_name") in ("", None):
                inputs["unet_name"] = unet
            elif ct == "CLIPLoader" and inputs.get("clip_name") in ("", None):
                inputs["clip_name"] = clip
            elif ct == "VAELoader" and inputs.get("vae_name") in ("", None):
                inputs["vae_name"] = vae
        return "dit"

    return ""


async def _abort_prompt(client: httpx.AsyncClient, base: str, prompt_id: str) -> None:
    """放弃等待时把任务从 ComfyUI 队列里撤掉（排队中）并打断正在跑的这一张。

    不撤的后果：客户端已经判超时，GPU 还在为一张没人要的图继续采样；下一次重试
    排在它后面再等一个超时时长。用户看到的就是「ComfyUI 控制台进度条一遍遍刷，
    素材条一张不涨」（2026-10-29 反馈）。/interrupt 是全局的，可能打断别的会话的
    任务，所以只在 queue_running 的第一项确实是本次 prompt_id 时才发。
    """
    try:
        resp = await client.get(f"{base}/queue", timeout=10)
        resp.raise_for_status()
        q = resp.json()
        def _has(items: list, pid: str) -> bool:
            return any(isinstance(it, list) and len(it) > 1 and str(it[1]) == pid
                       for it in (items or []))
        pending = _has(q.get("queue_pending") or [], prompt_id)
        running = _has(q.get("queue_running") or [], prompt_id)
        if pending:
            await client.post(f"{base}/queue", json={"delete": [prompt_id]}, timeout=10)
        if running:
            await client.post(f"{base}/interrupt", timeout=10)
        if pending or running:
            log.warning("已撤销超时任务 %s（排队中=%s 正在跑=%s），把显卡让给下一次尝试",
                        prompt_id, pending, running)
    except Exception as e:  # noqa: BLE001 - 撤销失败不影响主流程的报错
        log.warning("撤销超时任务 %s 失败：%s", prompt_id, _http_err_text(e))


async def _upload_image(client: httpx.AsyncClient, base: str, data: bytes) -> str:
    """上传参考图到 ComfyUI input 目录，返回 LoadImage 可引用的文件名。"""
    name = f"game_agent_ref_{uuid.uuid4().hex[:8]}.png"
    resp = await client.post(
        f"{base}/upload/image",
        files={"image": (name, data, "image/png")},
        data={"overwrite": "true"})
    resp.raise_for_status()
    r = resp.json()
    sub = r.get("subfolder", "")
    fname = r.get("name") or name
    return f"{sub}/{fname}" if sub else fname


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """'#ebebe7' / '235,234,231' → (235,234,231)；解析失败回退到中性浅灰。"""
    v = (value or "").strip()
    try:
        if v.startswith("#"):
            v = v[1:]
            if len(v) == 6:
                return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        elif "," in v:
            parts = [int(x) for x in v.split(",")[:3]]
            if len(parts) == 3:
                return max(0, min(255, parts[0])), max(0, min(255, parts[1])), max(0, min(255, parts[2]))
    except Exception:  # noqa: BLE001 - 配置写错就退回默认
        pass
    return (235, 234, 231)


def flatten_reference_bg(ref_bytes: bytes) -> bytes:
    """把带透明通道的参考图**合成到中性浅灰底**并去掉 alpha，返回 PNG 字节。

    为什么必须做（2026-11-04 实测定位）：
    - 漫剧角色参考图常是「抠好的 RGBA 透明底 PNG」。ComfyUI 的 LoadImage 走
      VideoFromFile 路径取首帧，**会丢掉 alpha 通道**，透明区域变成纯黑 (0,0,0)。
    - Wan 拿到「人物 + 大片纯黑背景」的首帧后，为了自洽会自己脑补背景（深色/墨绿/
      模糊光斑），并把 2D 立绘往 3D 手办/塑料人偶方向推——人物僵硬、手臂张开、
      脸部发白发油。这正是用户看到的「奇怪」效果。
    - 也不能简单合成到纯白：参考图本身是浅色白裙，白底 + 白裙 → 模型分不清边界，
      实测约 50% 像素被烧成近纯白、裙子糊成一团。
    - 合成到中性浅灰（默认 #ebebe7）实测：0% 过曝、背景稳定、2D 画风与配色保住、
      人物五官服装前后一致。取灰不要取黑/白，灰的亮度与白裙拉开差距又不过暗。

    非透明图（RGB/JPEG）原样返回，不做任何处理。
    """
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(ref_bytes)) as im:
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info)
            if not has_alpha:
                return ref_bytes
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, _hex_to_rgb(COMFYUI_VIDEO_REF_BG) + (255,))
            bg.alpha_composite(im)
            out = io.BytesIO()
            bg.convert("RGB").save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - 处理失败就原样上传，不阻断出片
        return ref_bytes


async def fetch_model_options(base_url: str) -> dict:
    """从 ComfyUI /object_info 拉可选手动模型列表。
    返回 {available, checkpoints, diffusion, loras, default_lora, default_lora_strength,
    loras_dir, error}；ComfyUI 不可达时 available=False，但默认值与 LoRA 安装目录照常返回。"""
    base = (base_url or "").rstrip("/")
    ld = loras_dir()
    defaults = {
        "default_lora": COMFYUI_LORA,
        "default_lora_strength": COMFYUI_LORA_STRENGTH,
        "loras_dir": str(ld) if ld else None,
    }
    if not base:
        return {"available": False, "checkpoints": [], "diffusion": [], "loras": [],
                "error": "未配置 ComfyUI 地址", **defaults}
    def _opts(spec) -> list[str]:
        try:
            first = spec[0]  # ComfyUI 格式：[["a.safetensors", "b.safetensors"], {...}]
            if first and isinstance(first[0], list):
                first = first[0]
            return [str(x) for x in first]
        except Exception:  # noqa: BLE001
            return []
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            r = await client.get(f"{base}/object_info")
            r.raise_for_status()
            info = r.json()
        ck = _opts(info.get("CheckpointLoaderSimple", {}).get("input", {})
                   .get("required", {}).get("ckpt_name", []))
        un = _opts(info.get("UNETLoader", {}).get("input", {})
                   .get("required", {}).get("unet_name", []))
        lo = _opts(info.get("LoraLoader", {}).get("input", {})
                   .get("required", {}).get("lora_name", []))
        return {"available": True, "checkpoints": ck, "diffusion": un,
                "loras": lo, "error": None, **defaults}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "checkpoints": [], "diffusion": [], "loras": [],
                "error": str(e), **defaults}


def loras_dir() -> Path | None:
    """ComfyUI 的 models/loras 目录（工作台「上传 LoRA」写这里）。

    优先 COMFYUI_LORAS_DIR；否则从 COMFYUI_LAUNCH_CMD 里的 main.py 反推
    <ComfyUI>/models/loras，再退内置启动路径。解析不到返回 None（禁用上传）。
    """
    if COMFYUI_LORAS_DIR:
        return Path(COMFYUI_LORAS_DIR)
    cmd = COMFYUI_LAUNCH_CMD or ""
    candidates: list[str] = []
    m = re.search(r'"([^"]*[\\/]main\.py)"|(\S*[\\/]main\.py)', cmd)
    if m:
        candidates.append(m.group(1) or m.group(2))
    candidates.extend(py_main for _, py_main in _LAUNCH_PY_MAIN)
    for main_py in candidates:
        d = Path(main_py).parent / "models" / "loras"
        if d.is_dir():
            return d
    return None


def _apply_model_override(wf: dict, model: str) -> None:
    """手动指定模型：填入工作流里的 CheckpointLoaderSimple.ckpt_name / UNETLoader.unet_name。"""
    if not model:
        return
    for n in wf.values():
        ct = n.get("class_type")
        if ct == "CheckpointLoaderSimple":
            n.setdefault("inputs", {})["ckpt_name"] = model
        elif ct == "UNETLoader":
            n.setdefault("inputs", {})["unet_name"] = model


@gpu_arbiter.gpu_image
async def _generate_images_bytes_once(prompt: str, *, negative_prompt: str = "",
                                      width: int = 512, height: int = 512,
                                      base_url: str, timeout_s: int = 300,
                                      reference: bytes | None = None,
                                      reference_mode: str = "style",
                                      reference_denoise: float = IMG2IMG_DENOISE,
                                      workflow_mode: str = "auto",
                                      model: str = "", batch: int = 1,
                                      lora: str | None = None,
                                      lora_strength: float | None = None,
                                      on_prompt_id: "Callable[[str], None] | None" = None) -> list[bytes]:
    """提交工作流并等待完成，返回 PNG 字节列表（batch>1 时一次出多张）。失败抛 ComfyUIError。
    reference 提供参考图字节时的两种模式：
    - style（默认）：风格参考换内容——参考图只提供画风/质感/配色，
      人物构图全新（TextEncodeQwenImage21，需 Qwen 三件套），输出尺寸跟随请求宽高；
    - img2img：以参考图为底图重绘（denoise=reference_denoise），保留构图与人物骨架。
    无参考图走文生图；workflow_mode 手动指定生成方式：
    - auto（默认）：模型探测决定经典 SD（CheckpointLoaderSimple）或 DiT 三件套（UNETLoader）；
    - checkpoint：强制经典工作流（style 参考退化为 img2img）；
    - diffusion：强制 DiT 三件套工作流。
    model 非空时手动指定模型文件名（覆盖探测结果）。
    batch > 1 时通过 latent 的 batch_size 一次生成多张，按 ComfyUI 输出顺序返回全部图片。"""
    base = base_url.rstrip("/")
    workflow_mode = (workflow_mode or "auto").lower()
    if workflow_mode not in ("auto", "checkpoint", "diffusion"):
        workflow_mode = "auto"
    try:
        if COMFYUI_WORKFLOW_PATH:
            wf = json.loads(Path(COMFYUI_WORKFLOW_PATH).read_text(encoding="utf-8"))
        elif workflow_mode == "checkpoint":
            wf = _img2img_workflow() if reference else _default_workflow()
        elif workflow_mode == "diffusion":
            if reference and reference_mode == "style":
                wf = _style_ref_workflow()
                if STYLE_PROMPT_PREFIX not in prompt and "参考图" not in prompt:
                    prompt = STYLE_PROMPT_PREFIX + prompt
            else:
                wf = _dit_img2img_workflow() if reference else _dit_workflow()
        elif reference and reference_mode == "style":
            wf = _style_ref_workflow()
            if STYLE_PROMPT_PREFIX not in prompt and "参考图" not in prompt:
                prompt = STYLE_PROMPT_PREFIX + prompt
        elif reference:
            wf = _img2img_workflow()
        else:
            wf = _default_workflow()
    except Exception as e:  # noqa: BLE001
        raise ComfyUIError(f"加载工作流模板失败：{e}") from e

    try:
        # trust_env=False：本地 ComfyUI 必须直连，绝不能走系统代理。Windows 上 httpx 默认
        # 读 Internet 设置（LAN 代理），本机若开着代理（如 127.0.0.1:12334 之类、可能已停），
        # 发往 127.0.0.1 的请求会被转给代理 → ConnectError「All connection attempts failed」，
        # 界面表现为「探测 ComfyUI 可用模型失败」而出不了图（2026-11-06 定位）。
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=90),
                                     trust_env=False) as client:
            # 先探测/切换模板（no_ckpt 时会整体换 wf），再上传填 LoadImage，避免填好的名字被新模板重置
            mode = await _resolve_models(client, base, wf)
            if (not COMFYUI_WORKFLOW_PATH and workflow_mode == "auto" and mode == "no_ckpt"):
                # 无 checkpoint → 切 Anima/Qwen 架构 DiT 三件套工作流重新探测
                wf = _dit_img2img_workflow() if reference else _dit_workflow()
                mode = await _resolve_models(client, base, wf)
            if mode == "no_ckpt":
                # 显式选了 checkpoint 模式但一个 checkpoint 都没有：直接给可读报错，
                # 否则会发一个 ckpt_name="" 的请求，被 ComfyUI 以 400 value_not_in_list 拒绝
                raise ComfyUIError(
                    "SD（checkpoint）模式需要可用的 checkpoint 模型，但 ComfyUI 的 "
                    "models/checkpoints 目录下没有找到任何模型。请放入 .safetensors，"
                    "或把生成方式改成「自动探测 / Diffusion Models（DiT 三件套）」。")
            _apply_model_override(wf, model)
            if reference:
                ref_name = await _upload_image(client, base, reference)
                img2img = reference_mode != "style"
                for n in wf.values():
                    ct = n.get("class_type")
                    inputs = n.setdefault("inputs", {})
                    if ct == "LoadImage" and inputs.get("image") in ("", None):
                        inputs["image"] = ref_name
                    elif img2img and ct in ("KSampler", "KSamplerAdvanced"):
                        inputs["denoise"] = reference_denoise
            if mode == "dit" and (not reference or reference_mode == "style") \
                    and min(width, height) < 768:
                # 风格参考现在也走空 latent 采样，尺寸规则和文生图一致
                width = height = 1024
            wf = _patch_workflow(wf, prompt, negative_prompt, width, height, batch=batch)
            # 高分辨率：>COMFYUI_TILED_MP 的大图改用分块解码 + 必要时基准采样/像素放大，
            # 8G 显存才能稳定出 2K~4K 图。小图不受影响（内部判断，行为不变）。
            actual_w, actual_h = _apply_hires(wf, width, height)
            if (actual_w, actual_h) != (width, height):
                log.info("高分辨率出图：请求 %dx%d → 实际输出 %dx%d（分块解码）",
                         width, height, actual_w, actual_h)
            # 风格 LoRA：lora=None 用 .env 默认；""=明确不挂；具名=用指定 LoRA。
            # 文件缺失时 _resolve_lora_name 直接报错，不静默出无 LoRA 的图。
            wanted = COMFYUI_LORA if lora is None else lora
            strength = COMFYUI_LORA_STRENGTH if lora_strength is None else lora_strength
            if wanted:
                lora_name = await _resolve_lora_name(client, base, wanted)
                wf = _apply_lora(wf, lora_name, strength)

            resp = await client.post(f"{base}/prompt",
                                     json={"prompt": wf, "client_id": uuid.uuid4().hex})
            if resp.status_code >= 400:
                # ComfyUI 的 400 会在 body 里给出 node_errors（如 ckpt_name 不在列表），
                # raise_for_status 会丢掉这段信息，导致只看到一句无用的 "400 Bad Request"
                raise ComfyUIError(
                    f"ComfyUI 拒绝了工作流（HTTP {resp.status_code}）：{resp.text[:800]}")
            resp.raise_for_status()
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                raise ComfyUIError(f"ComfyUI 未返回 prompt_id：{resp.text[:300]}")
            if on_prompt_id:
                try:
                    on_prompt_id(prompt_id)
                except Exception:  # noqa: BLE001 - 进度回调绝不能影响出图
                    pass

            deadline = time.monotonic() + timeout_s
            poll_fails = 0
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_INTERVAL_S)
                try:
                    resp = await client.get(f"{base}/history/{prompt_id}")
                    resp.raise_for_status()
                    poll_fails = 0
                except httpx.HTTPError as e:
                    # 单次轮询超时≠任务死了：ComfyUI 正在跑图时它的 HTTP 线程会被拖住
                    # （实测 /system_stats 空闲都要十几秒），以前这里一次 ReadTimeout 就把
                    # 整张图判失败并回退占位，而服务端其实还在正常出图。
                    poll_fails += 1
                    if poll_fails >= 3:
                        await _abort_prompt(client, base, prompt_id)
                        raise ComfyUIError(
                            f"连续 {poll_fails} 次查不到任务进度（{_http_err_text(e)}），"
                            f"ComfyUI 可能已崩溃或被重启") from e
                    log.warning("查询出图进度失败（%d/3，%s），继续等",
                                poll_fails, _http_err_text(e))
                    continue
                entry = resp.json().get(prompt_id)
                if not entry:
                    continue
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = json.dumps(status.get("messages", []), ensure_ascii=False)[:500]
                    raise ComfyUIError(f"ComfyUI 执行出错：{msgs}")
                outputs = entry.get("outputs") or {}
                images = [img for out in outputs.values()
                          for img in (out.get("images") or [])]
                if images:
                    # 只取正式输出（排除 preview/temp），保持 ComfyUI 返回顺序
                    ordered = [i for i in images if i.get("type") == "output"] or images
                    picked: list[bytes] = []
                    for pick in ordered:
                        resp = await client.get(f"{base}/view", params={
                            "filename": pick.get("filename", ""),
                            "subfolder": pick.get("subfolder", ""),
                            "type": pick.get("type", "output"),
                        })
                        resp.raise_for_status()
                        picked.append(resp.content)
                    if picked:
                        return picked
                if status.get("completed"):
                    raise ComfyUIError("ComfyUI 工作流执行完成但无输出图片")
            await _abort_prompt(client, base, prompt_id)
            raise ComfyUIError(f"等待 ComfyUI 出图超时（{timeout_s}s），"
                               f"可能是模型太大首次加载或显存不足；"
                               f"已撤销该任务，避免它继续占着显卡拖累重试")
    except httpx.HTTPError as e:
        raise ComfyUIError(f"ComfyUI 请求失败（{base}）：{_http_err_text(e)}") from e


async def generate_images_bytes(prompt: str, **kwargs) -> list[bytes]:
    """出图总入口：**全局串行**，一次只跑一张（跨会话、跨类型共用 media_queue）。

    为什么必须串行：本地 ComfyUI 是单队列 + 单卡，并发提交不会更快，只会把请求堆在
    服务端排队、把显存峰值叠加（首图加载模型时尤其明显），最后表现为超时 / OOM / 卡死。
    进程内先过 media_queue（图片/语音/视频共用一条队列，别的会话在生成语音时这里要等），
    再过 _GEN_LOCK（同类型内再兜一层）；服务本身由 ensure_comfyui_running 只拉起一次。
    """
    base = (kwargs.get("base_url") or COMFYUI_BASE_URL or "").rstrip("/")
    ref = "参考图" if kwargs.get("reference") else "文生图"
    t0 = time.monotonic()
    log.info("出图开始：%s %sx%s %s prompt=%r",
             ref, kwargs.get("width", 512), kwargs.get("height", 512), base,
             (prompt or "")[:80])
    async with media_queue.media_slot("image"):
        async with _GEN_LOCK:
            try:
                imgs = await _generate_images_bytes_once(prompt, **kwargs)
            except ComfyUIError as e:
                log.warning("出图失败（耗时 %.1fs）：%s", time.monotonic() - t0, e)
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("出图异常（耗时 %.1fs）：%s: %s",
                              time.monotonic() - t0, type(e).__name__, e)
                raise
    log.info("出图完成：%d 张，耗时 %.1fs", len(imgs), time.monotonic() - t0)
    return imgs


async def generate_image_bytes(prompt: str, **kwargs) -> bytes:
    """单张出图（兼容旧调用方）：返回第一张 PNG 字节，失败抛 ComfyUIError。"""
    imgs = await generate_images_bytes(prompt, **kwargs)
    if not imgs:
        raise ComfyUIError("ComfyUI 工作流执行完成但无输出图片")
    return imgs[0]


# ============================================================================
# 视频生成（本地 ComfyUI · Wan2.2 TI2V 5B）
#
# 8GB 显存只能跑 5B 视频模型（14B/28B 放不下）。Wan2.2 TI2V 5B 是「文生视频 + 图生视频」
# 二合一：不传参考图走 t2v（空 latent 采样），传参考图走 i2v（首帧作为起始条件）。
# 工作流完全由内置模板拼装（节点见 comfyui_extras/nodes_wan.py / nodes_video.py），
# 不依赖用户自备 workflow JSON。提交→轮询 /history→从 /view 取回 mp4 字节。
# ============================================================================

# 视频输出节点在 /history 里可能落在 images/gifs/videos 等不同键下，统一按扩展名找。
_VIDEO_OUT_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif")


def fit_video_size(width: int, height: int, reference: bytes | None = None) -> tuple[int, int]:
    """统一的出片尺寸计算：宽高取 32 的整数倍；图生视频时再贴合参考图长宽比。

    调用方与 ComfyUI 工作流都走这一个函数，保证「上报的尺寸」和「实际渲染的尺寸」一致。"""
    width = max(256, (int(width) // 32) * 32)
    height = max(256, (int(height) // 32) * 32)
    if reference:
        width, height = _ref_fit_size(reference, width, height)
    return width, height


# i2v 出片尺寸的像素预算（宽×高）。参考图是竖图（如 1024x1536）时，若沿用 832x480 的
# 预算会把竖图压成 480x736 这种小方幅，脸部像素太少、细节糊。这里提到约 0.4MP
# （2:3 竖图 → 512x768），8G 显存 L=49 帧实测稳定（约 2 分钟）；再往上（832x1248≈1MP）
# VAE 解码阶段会 OOM 崩溃，本机 8G 跑不动，别调太高。
COMFYUI_VIDEO_I2V_PIXEL_BUDGET = 400000


def _ref_fit_size(ref_bytes: bytes, width: int, height: int) -> tuple[int, int]:
    """图生视频时让出片宽高**贴合参考图长宽比**，避免把人物拉伸变形。

    为什么：Wan22ImageToVideoLatent 内部会对首帧做 common_upscale(crop=center)。
    - 若出片比例≠参考图比例，要么被中心裁切（可能切掉脸），要么被拉伸变形——都是「变脸」。
    - 这里按参考图真实长宽比，在预算内求出最接近的画幅（宽高为 32 倍数）。
    竖版角色图走更大的像素预算（COMFYUI_VIDEO_I2V_PIXEL_BUDGET），保证脸部细节。
    参考图读不出尺寸时原样返回目标宽高。"""
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(ref_bytes)) as im:
            rw, rh = im.size
    except Exception:  # noqa: BLE001 - 读不到尺寸就退回请求宽高
        return width, height
    if not rw or not rh:
        return width, height
    aspect = rw / rh
    # 竖图（高>宽）用更大的像素预算，避免脸太小；横幅沿用调用方预算。
    budget = COMFYUI_VIDEO_I2V_PIXEL_BUDGET if rh > rw else max(1, int(width) * int(height))
    # 在预算内、宽高都是 32 倍数的候选里，选**长宽比最接近原图**的一个，避免脸被轻微压扁。
    target_h = (budget / aspect) ** 0.5
    base_h = int(target_h // 32) * 32
    best = None
    for dh in (-64, -32, 0, 32, 64):
        cand_h = base_h + dh
        if cand_h < 256:
            continue
        cand_w = int(round(cand_h * aspect) // 32) * 32
        if cand_w < 256:
            continue
        area = cand_w * cand_h
        if area > budget * 1.12:
            continue
        err = abs(cand_w / cand_h - aspect)
        key = (round(err, 4), -area)
        if best is None or key < best[0]:
            best = (key, cand_w, cand_h)
    if best is not None:
        return best[1], best[2]
    # 兜底：老算法
    new_h = max(256, base_h)
    new_w = max(256, (int(round(new_h * aspect)) // 32) * 32)
    return new_w, new_h


def _video_workflow(*, prompt: str, negative: str, width: int, height: int,
                    length: int, fps: int, steps: int, cfg: float,
                    ref_name: str | None) -> dict:
    """拼装 Wan2.2 TI2V 5B 的 API 格式工作流。

    节点：
      1 UNETLoader(wan2.2_ti2v_5B) 2 CLIPLoader(umt5,type=wan) 3 VAELoader(wan2.2_vae)
      4/5 CLIPTextEncode 正/负   6 ModelSamplingSD3(shift=8)   7 Wan22ImageToVideoLatent
      8 KSampler   9 VAEDecode   11 CreateVideo   12 SaveVideo(→mp4/h264)
      10 LoadImage（仅 i2v 时存在，接 7.start_image）
    length 必须是 4n+1（Wan 的帧数约束）。

    ⚠️ 关于「首尾帧」：本节点只支持首帧（start_image）。实测（2026-11-04，8G/3060Ti）
    Wan2.2 TI2V 5B 的官方 WanFirstLastFrameToVideo 节点以及「latent 尾帧替换」都不生效
    ——首帧能锁住，尾帧条件被模型忽略（Wan2.1 FLF 才支持首尾双条件，但那是 14B，
    24G 显存，本机 8G 跑不了）。所以这里只做首帧，不假装支持尾帧。"""
    wf: dict = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": COMFYUI_VIDEO_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": COMFYUI_VIDEO_CLIP, "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": COMFYUI_VIDEO_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 8.0, "model": ["1", 0]}},
        "8": {"class_type": "KSampler", "inputs": {
            "seed": random.randint(0, 2**32 - 1), "steps": steps, "cfg": cfg,
            "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0,
            "model": ["6", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": float(fps)}},
        "12": {"class_type": "SaveVideo", "inputs": {
            "video": ["11", 0], "filename_prefix": "game_agent/video",
            # 注意：本版 ComfyUI 的 SaveVideo 里 format 传字符串，codec 必须传
            #   {"codec": ...} 字典（execute 里读 codec["codec"]，传字符串会 TypeError）。
            "format": "mp4", "codec": {"codec": "h264"}}},
    }
    latent_inputs: dict = {"vae": ["3", 0], "width": width, "height": height,
                           "length": length, "batch_size": 1}
    if ref_name:
        # i2v：参考图作首帧（LoadImage 必须放在 input 目录，由 _upload_image 上传）。
        # 出片宽高已在调用方按参考图长宽比算好（见 _ref_fit_size），此处直接等比喂入，
        # 不做额外裁切/拉伸——避免把人物脸部切掉或拉变形的「变脸」。
        wf["10"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        latent_inputs["start_image"] = ["10", 0]
    wf["7"] = {"class_type": "Wan22ImageToVideoLatent", "inputs": latent_inputs}
    return wf


def smooth_video_bytes(data: bytes, target_fps: int, orig_fps: int,
                       denoise: bool = False) -> tuple[bytes, dict]:
    """视频后处理：时间域降噪（可选）+ 运动补偿补帧到 target_fps（可选），让播放顺滑、少闪烁。

    为什么需要：
    - Wan2.2 5B 原生 24fps，直接播放在 60Hz 屏上仍有轻微顿感 → minterpolate 补到 60fps。
    - 漫剧片段容易出现「帧间色彩/线条轻微跳变、画面割裂」→ 补帧前先过一遍 hqdn3d
      很轻的时间+空间降噪（denoise=True），压掉闪烁又不糊掉漫画锐利线条。

    绝不阻断出片：目标帧率不高于原帧率且无需降噪 / 没装 ffmpeg / 转码失败或超时
    → 原样返回。返回 (视频字节, {"fps_raised": bool, "denoised": bool})。
    """
    try:
        target_fps = int(target_fps)
        orig_fps = int(orig_fps or 0)
    except (TypeError, ValueError):
        return data, {"fps_raised": False, "denoised": False}
    if not data:
        return data, {"fps_raised": False, "denoised": False}

    raise_fps = target_fps > 0 and target_fps > orig_fps
    if not raise_fps and not denoise:
        return data, {"fps_raised": False, "denoised": False}

    import os
    import shutil
    import tempfile

    vf_parts: list[str] = []
    if denoise:
        # hqdn3d=亮度空间:亮度时间:色度空间:色度时间，取轻档（≈0.15–0.25 强度效果）。
        vf_parts.append("hqdn3d=2:1:3:3")
    if raise_fps:
        vf_parts.append(f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:"
                        f"me_mode=bidir:vsbmc=1")
    vf = ",".join(vf_parts)

    tmp_dir = tempfile.mkdtemp(prefix="ga_vsmooth_")
    src = os.path.join(tmp_dir, "src.mp4")
    dst = os.path.join(tmp_dir, "dst.mp4")
    try:
        with open(src, "wb") as fh:
            fh.write(data)
        cp = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", vf,
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", dst],
            capture_output=True, timeout=600)
        if cp.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            with open(dst, "rb") as fh:
                out = fh.read()
            log.info("视频后处理（补帧=%s→%dfps，降噪=%s）：%d→%d 字节",
                     raise_fps, target_fps if raise_fps else orig_fps, denoise,
                     len(data), len(out))
            return out, {"fps_raised": raise_fps, "denoised": denoise}
        log.warning("视频后处理失败，保留原片：%s", (cp.stderr or b"")[:200])
    except Exception as exc:  # noqa: BLE001 - 后处理失败绝不能影响出片
        log.warning("视频后处理异常，保留原片：%s", exc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return data, {"fps_raised": False, "denoised": False}


@gpu_arbiter.gpu_image
async def _generate_video_bytes_once(prompt: str, *, negative_prompt: str = "",
                                     width: int = 0, height: int = 0, length: int = 0,
                                     fps: int = 0, steps: int = 0, cfg: float = 0.0,
                                     base_url: str = "", timeout_s: int = 0,
                                     reference: bytes | None = None,
                                     on_prompt_id: "Callable[[str], None] | None" = None) -> bytes:
    """提交一条 Wan2.2 TI2V 5B 视频工作流并等待完成，返回视频文件字节。失败抛 ComfyUIError。

    reference 提供时走 i2v（参考图作首帧，并按目标宽高**等比缩放、不裁脸**）；否则 t2v。
    帧数按 Wan 的 4n+1 约束收敛；尺寸取 32 的整数倍。"""
    base = (base_url or COMFYUI_BASE_URL).rstrip("/")
    width = width or COMFYUI_VIDEO_WIDTH
    height = height or COMFYUI_VIDEO_HEIGHT
    length = length or COMFYUI_VIDEO_LENGTH
    fps = fps or COMFYUI_VIDEO_FPS
    steps = steps or COMFYUI_VIDEO_STEPS
    cfg = cfg or COMFYUI_VIDEO_CFG
    timeout_s = timeout_s or COMFYUI_VIDEO_TIMEOUT_S
    # Wan 帧数约束：4n+1。向下取整到合法的 4n+1，最少 17 帧。
    length = max(17, ((int(length) - 1) // 4) * 4 + 1)
    # 视频尺寸：32 倍数；图生视频时贴合参考图长宽比，避免拉伸/裁切变形（防变脸）。
    width, height = fit_video_size(width, height, reference)

    try:
        # trust_env=False：本地 ComfyUI 视频链路同样必须直连，不能走系统代理（见出图链路的说明）。
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=120),
                                     trust_env=False) as client:
            ref_name = None
            if reference:
                # 透明底 PNG 必须先合成到浅灰底：否则 LoadImage 丢 alpha 变纯黑，
                # Wan 会脑补出诡异深色背景 + 把 2D 立绘做成塑料 3D 人偶（已实测定位）。
                ref_name = await _upload_image(client, base, flatten_reference_bg(reference))
            wf = _video_workflow(prompt=prompt, negative=negative_prompt,
                                 width=width, height=height, length=length, fps=fps,
                                 steps=steps, cfg=cfg, ref_name=ref_name)
            log.info("Wan 视频工作流提交：%s %dx%d 帧数=%d fps=%d 模式=%s",
                     base, width, height, length, fps,
                     "i2v" if ref_name else "t2v")
            resp = await client.post(f"{base}/prompt",
                                     json={"prompt": wf, "client_id": uuid.uuid4().hex})
            if resp.status_code >= 400:
                raise ComfyUIError(
                    f"ComfyUI 拒绝了视频工作流（HTTP {resp.status_code}）：{resp.text[:800]}")
            resp.raise_for_status()
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                raise ComfyUIError(f"ComfyUI 未返回 prompt_id：{resp.text[:300]}")
            if on_prompt_id:
                try:
                    on_prompt_id(prompt_id)
                except Exception:  # noqa: BLE001 - 进度回调绝不能影响出片
                    pass

            deadline = time.monotonic() + timeout_s
            poll_fails = 0
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_INTERVAL_S)
                try:
                    resp = await client.get(f"{base}/history/{prompt_id}")
                    resp.raise_for_status()
                    poll_fails = 0
                except httpx.HTTPError as e:
                    poll_fails += 1
                    if poll_fails >= 3:
                        await _abort_prompt(client, base, prompt_id)
                        raise ComfyUIError(
                            f"连续 {poll_fails} 次查不到视频进度（{_http_err_text(e)}），"
                            f"ComfyUI 可能已崩溃或被重启") from e
                    log.warning("查询视频进度失败（%d/3，%s），继续等",
                                poll_fails, _http_err_text(e))
                    continue
                entry = resp.json().get(prompt_id)
                if not entry:
                    continue
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = json.dumps(status.get("messages", []), ensure_ascii=False)[:500]
                    raise ComfyUIError(f"ComfyUI 视频执行出错：{msgs}")
                # 从各输出节点的 images/gifs/videos 里挑视频文件
                outputs = entry.get("outputs") or {}
                candidates: list[dict] = []
                for out in outputs.values():
                    for key in ("images", "gifs", "videos", "video"):
                        for item in (out.get(key) or []):
                            if isinstance(item, dict) and item.get("filename"):
                                candidates.append(item)
                picks = [c for c in candidates
                         if c.get("type") in (None, "output")
                         and str(c.get("filename", "")).lower().endswith(_VIDEO_OUT_EXTS)]
                if not picks:
                    picks = [c for c in candidates
                             if str(c.get("filename", "")).lower().endswith(_VIDEO_OUT_EXTS)]
                if picks:
                    pick = picks[0]
                    vresp = await client.get(f"{base}/view", params={
                        "filename": pick.get("filename", ""),
                        "subfolder": pick.get("subfolder", ""),
                        "type": pick.get("type", "output"),
                    })
                    vresp.raise_for_status()
                    if not vresp.content:
                        raise ComfyUIError("ComfyUI 视频输出为空")
                    return vresp.content
                if status.get("completed"):
                    raise ComfyUIError("ComfyUI 视频工作流执行完成但无输出文件")
            await _abort_prompt(client, base, prompt_id)
            raise ComfyUIError(f"等待 ComfyUI 出片超时（{timeout_s}s），"
                               f"已撤销该任务，避免它继续占着显卡拖累重试")
    except ComfyUIError:
        raise
    except httpx.HTTPError as e:
        raise ComfyUIError(f"ComfyUI 视频请求失败（{base}）：{_http_err_text(e)}") from e


async def generate_video_bytes(prompt: str, **kwargs) -> bytes:
    """出片总入口：**全局串行**（与出图/配音共用 media_queue 的 video 通道），
    独占 GPU（gpu_image：视频同样吃 ComfyUI，和出图互斥，与语音互斥）。"""
    base = (kwargs.get("base_url") or COMFYUI_BASE_URL or "").rstrip("/")
    t0 = time.monotonic()
    log.info("出片开始：%s %sx%s prompt=%r",
             base, kwargs.get("width") or COMFYUI_VIDEO_WIDTH,
             kwargs.get("height") or COMFYUI_VIDEO_HEIGHT, (prompt or "")[:80])
    async with media_queue.media_slot("video"):
        async with _GEN_LOCK:
            try:
                data = await _generate_video_bytes_once(prompt, **kwargs)
            except ComfyUIError as e:
                log.warning("出片失败（耗时 %.1fs）：%s", time.monotonic() - t0, e)
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("出片异常（耗时 %.1fs）：%s: %s",
                              time.monotonic() - t0, type(e).__name__, e)
                raise
    log.info("出片完成：%d bytes，耗时 %.1fs", len(data), time.monotonic() - t0)
    return data


# ---------------------------------------------------------------------------
# 长视频「接龙」：把上一段最后一帧当下一段首帧，串成任意长且连贯的视频
# ---------------------------------------------------------------------------
# 为什么这样做：Wan2.2 5B 是首帧 conditioning 模型，没法一次生成很长（8G 也不允许）。
# 把每个短段（默认 1 秒）的**最后一帧**喂回当下一段**首帧**，模型就能在动作/画面上
# 无缝续写——实测 3 段接龙：接缝帧差 ≈1.6（等同压缩噪声）、整体亮度 std≈1.9（无漂移）。
# 长视频=多个短段接龙，越长越省显存、越不容易整段崩，失败也只丢一段。

def _ffmpeg_exe() -> str:
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def _extract_frames_to_dir(mp4_path: str, out_dir: str) -> list[str]:
    """用 ffmpeg 把 mp4 解成 0001.png… 序列，返回按序排列的 png 路径。"""
    import os
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.endswith(".png"):
            os.remove(os.path.join(out_dir, f))
    subprocess.run([_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", mp4_path,
                    os.path.join(out_dir, "%04d.png")], check=True, timeout=600)
    return [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir)) if f.endswith(".png")]


def _last_frame_png_bytes(mp4_path: str) -> bytes:
    """取 mp4 最后一帧的 PNG 字节（作为下一段的首帧 start_image）。"""
    import os
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ga_lastf_")
    try:
        frames = _extract_frames_to_dir(mp4_path, tmp)
        if not frames:
            raise ComfyUIError("无法从上一段视频提取最后一帧（接龙中断）")
        with open(frames[-1], "rb") as fh:
            return fh.read()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _normalize_seg_len(seconds: float, fps: int) -> int:
    """秒数 → 合法帧数（4n+1，最少 17），取**最接近**目标的 4n+1。
    1 秒@24fps → 25 帧（4*6+1，正好 1.0s）；2 秒 → 49 帧。"""
    n = max(17, int(round(float(seconds) * int(fps))))
    k = max(4, int(round((n - 1) / 4.0)))
    return 4 * k + 1


async def generate_video_chain_bytes(prompt: str, *, segments: int = 4,
                                     segment_seconds: float = 1.0,
                                     reference: bytes | None = None,
                                     target_fps: int = 0, denoise: bool = True,
                                     on_segment: "Callable[[int, int], None] | None" = None,
                                     **kwargs) -> tuple[bytes, dict]:
    """接龙出长视频：逐段生成，每段以上一段最后一帧为首帧，最后拼接并补到 target_fps。

    返回 (最终 mp4 字节, meta{segments, seg_seconds, seg_frames, native_frames, fps_raised, denoised})。
    失败抛 ComfyUIError（不假装成功）。reference 为第一段的首帧（第一段是 i2v，后续都是接龙 i2v）。
    """
    import os
    import tempfile

    fps = int(kwargs.get("fps") or COMFYUI_VIDEO_FPS)
    segments = max(1, min(int(segments), 30))          # 硬上限 30 段（≈30s@1s）防失控
    seg_len = _normalize_seg_len(segment_seconds, fps)
    target_fps = int(target_fps or 0)

    base = (kwargs.get("base_url") or COMFYUI_BASE_URL or "").rstrip("/")
    t0 = time.monotonic()
    log.info("接龙出片：%d 段 × %d 帧(@%dfps) 首帧参考=%s prompt=%r",
             segments, seg_len, fps, bool(reference), (prompt or "")[:60])

    cur_start = reference
    tmp = tempfile.mkdtemp(prefix="ga_chain_")
    try:
        native_frames: list[str] = []
        seg_files: list[str] = []
        async with media_queue.media_slot("video"):
            async with _GEN_LOCK:
                for i in range(segments):
                    seg_kwargs = dict(kwargs)
                    seg_kwargs.update(length=seg_len, fps=fps, reference=cur_start)
                    if on_segment:
                        try:
                            on_segment(i, segments)
                        except Exception:  # noqa: BLE001
                            pass
                    data = await _generate_video_bytes_once(prompt, **seg_kwargs)
                    seg_path = os.path.join(tmp, f"seg{i:02d}.mp4")
                    with open(seg_path, "wb") as fh:
                        fh.write(data)
                    seg_files.append(seg_path)
                    frames = _extract_frames_to_dir(seg_path, os.path.join(tmp, f"f{i:02d}"))
                    if not frames:
                        raise ComfyUIError(f"第 {i + 1}/{segments} 段没有解出任何帧")
                    # 第 2 段起丢掉首帧（它就是上一段的最后一帧，避免同一帧重复出现卡一下）
                    use = frames if i == 0 else frames[1:]
                    native_frames.extend(use)
                    # 下一段首帧=本段最后一帧
                    cur_start = open(frames[-1], "rb").read()
                    log.info("接龙第 %d/%d 段完成（%d 帧），已取尾帧续接",
                             i + 1, segments, len(frames))
        if not native_frames:
            raise ComfyUIError("接龙出片没有任何帧")

        # 组装原生 fps 视频（png 序列），再统一补帧/降噪到目标帧率
        seq_dir = os.path.join(tmp, "seq")
        os.makedirs(seq_dir, exist_ok=True)
        for idx, fp in enumerate(native_frames):
            os.replace(fp, os.path.join(seq_dir, f"{idx:04d}.png"))
        native_mp4 = os.path.join(tmp, "native.mp4")
        subprocess.run([_ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", str(fps),
                        "-i", os.path.join(seq_dir, "%04d.png"),
                        "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", native_mp4],
                       check=True, timeout=1200)
        with open(native_mp4, "rb") as fh:
            native_bytes = fh.read()

        final_bytes, pp = await asyncio.to_thread(
            smooth_video_bytes, native_bytes, target_fps, fps, denoise)
        meta = {"segments": segments, "seg_seconds": seg_len / max(1, fps),
                "seg_frames": seg_len, "native_frames": len(native_frames),
                "fps_raised": pp.get("fps_raised"), "denoised": pp.get("denoised"),
                "elapsed_s": round(time.monotonic() - t0, 1)}
        log.info("接龙出片完成：%d 段 %d 帧 → %d bytes，耗时 %.1fs",
                 segments, len(native_frames), len(final_bytes), time.monotonic() - t0)
        return final_bytes, meta
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
