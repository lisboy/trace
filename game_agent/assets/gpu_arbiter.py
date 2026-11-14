"""GPU 互斥调度：本地生图（ComfyUI）与本地语音（IndexTTS2）共用一块显卡，不能同时驻留。

规则（按调用先后排队切换）：
- 要出图 → 先停语音服务 → 等显存真正释放（端口关闭）→ 再拉起 ComfyUI → 出图；
- 要出声 → 先停 ComfyUI   → 等显存真正释放（端口关闭）→ 再拉起 IndexTTS2 → 合成；
- 同一进程内全局一把锁（跨 run、跨会话）：并发生成会排队，而不是两个一起抢显存导致 OOM。

挂载点有两类：
1. 出图/出声函数（comfyui.generate_images_bytes / tts.synthesize 等，见 `gpu_image` / `gpu_voice`）；
2. 服务启动函数（comfyui.ensure_comfyui_running / tts.ensure_tts_running 里的 `make_room`）——
   「只是把服务拉起来」同样要腾卡，否则界面点「启动语音」会让两个服务同时驻留、直接 OOM。
第 2 类会被第 1 类内部调用（`_switch` 要调 `ensure_*`），所以用 `_owner` 记录当前持锁任务：
同一任务重入时放行，既不抢正在用的卡，也不会自锁在 asyncio.Lock 上。
额外用途：`prepare(kind)` 可提前把卡腾给某个服务（例如界面上先点「启动生图」）。
"""
from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from game_agent.config import (COMFYUI_BASE_URL, GPU_EXCLUSIVE, GPU_STOP_TIMEOUT_S,
                               TTS_BASE_URL, TTS_HOME)
from game_agent.logging_setup import get_logger

log = get_logger("gpu")

KINDS = ("image", "voice")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")

# 进程内全局：同一时刻只允许一个 GPU 服务在跑
_LOCK = asyncio.Lock()
_holder: str | None = None   # 最近一次被让进去的服务
_switches = 0                # 切换次数（诊断用）
# 当前持锁的任务、它占用的 kind 与重入深度：ensure_*（自己 Popen 启动服务）内部要调
# make_room，而它也可能是 gpu_slot protecting 的调用链的一环（gpu_slot → prepare →
# _switch → _start → ensure_* → make_room）。没有这套记录，重入会自锁在 asyncio.Lock 上。
_owner: asyncio.Task | None = None
_held_kind: str | None = None
_depth = 0


def enabled() -> bool:
    return GPU_EXCLUSIVE


# 每个会话起过哪些本地媒体服务：会话收尾只关它自己起过的那几个，
# 不替工作台/别的会话收摊（2026-10-30 用户要求）
_USED: dict[str, set[str]] = {}


def _rid(key: Any) -> str:
    """sessions.id 带横线、runs.session_id 是 32 位无横线串：统一后再比较。"""
    return str(key or "").replace("-", "").lower()


def mark_used(kind: str, session_id: Any) -> None:
    """记录「这个会话起过/用过该媒体服务」——由工具层在真正调用本地生成前调用。"""
    sid = _rid(session_id)
    if sid and kind in KINDS:
        _USED.setdefault(sid, set()).add(kind)


def take_used(session_id: Any) -> set[str]:
    """取出并清空该会话用过的服务种类（会话最后一条 run 收尾时调用）。"""
    return _USED.pop(_rid(session_id), set())


_PENDING_STOP: set[str] = set()


def defer_stop(kinds: set[str]) -> None:
    """这个 run 用过的服务这会儿不能停（还有别的 run 在跑），记下来等最后一个 run 收尾。"""
    _PENDING_STOP.update(kinds)


def take_pending() -> set[str]:
    """取出并清空「等最后一个 run 收尾再停」的服务种类。"""
    out = set(_PENDING_STOP)
    _PENDING_STOP.clear()
    return out


_KIND_CN = {"image": "ComfyUI 出图服务", "voice": "IndexTTS2 语音服务"}


def note(info: Any) -> str:
    """把一次腾卡结果压成一句给用户看的话；没发生腾卡就返回空串。

    界面需要知道「点启动顺手把另一个服务干掉了」，否则用户只会觉得
    出图服务莫名其妙下线了。"""
    if not isinstance(info, dict) or info.get("skipped"):
        return ""
    parts: list[str] = []
    stopped = info.get("stopped") or {}
    killed = stopped.get("killed") or []
    if killed:
        label = _KIND_CN.get(str(stopped.get("service")), "另一个本地服务")
        parts.append(f"已停掉{label}（{', '.join(str(x) for x in killed)}）")
    if info.get("warn"):
        parts.append(str(info["warn"]))
    if info.get("stop_error"):
        parts.append(f"停占用服务失败：{info['stop_error']}")
    return "；".join(parts)


def status() -> dict[str, Any]:
    return {"enabled": GPU_EXCLUSIVE, "holder": _holder, "switches": _switches,
            "busy": _LOCK.locked(), "depth": _depth,
            "image_port": _port_of("image"), "voice_port": _port_of("voice")}


def _url(kind: str) -> str:
    if kind == "image":
        # ComfyUI 配置端口被保留/占用时会自动改绑，走实际端口（见 assets/comfyui.py）
        try:
            from game_agent.assets import comfyui as _comfyui
            return _comfyui.actual_base()
        except Exception:  # noqa: BLE001
            return (COMFYUI_BASE_URL or "").strip().rstrip("/")
    # voice 走 tts 的实际端口（配置端口被系统保留时会自动改绑，见 assets/tts.py）
    try:
        from game_agent.assets import tts as _tts
        return _tts.actual_base()
    except Exception:  # noqa: BLE001
        return (TTS_BASE_URL or "").strip().rstrip("/")


def _port_of(kind: str) -> int:
    url = _url(kind)
    try:
        return urlparse(url).port or (10188 if kind == "image" else 10189)
    except Exception:  # noqa: BLE001
        return 10188 if kind == "image" else 10189


def _is_local(kind: str) -> bool:
    url = _url(kind)
    if not url:
        return False
    return (urlparse(url).hostname or "127.0.0.1") in _LOCAL_HOSTS


async def _port_open(kind: str) -> bool:
    """该服务的端口是否有人在监听（= 服务是不是活着）。"""
    if not _url(kind):
        return False
    port = _port_of(kind)
    host = urlparse(_url(kind)).hostname or "127.0.0.1"

    def _try() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex((host, port)) == 0

    try:
        return await asyncio.to_thread(_try)
    except Exception:  # noqa: BLE001
        return False


async def _wait_closed(kind: str, timeout_s: float) -> bool:
    """等端口真正关闭（进程退出、显存释放）。返回是否已关闭。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_s, 1)
    while loop.time() < deadline:
        if not await _port_open(kind):
            return True
        await asyncio.sleep(1.0)
    return False


async def _stop(kind: str) -> dict[str, Any]:
    if kind == "image":
        from game_agent.assets.comfyui import actual_base, stop_comfyui_service
        return await stop_comfyui_service(actual_base())
    from game_agent.assets.tts import stop_tts_service
    return await stop_tts_service(TTS_BASE_URL)


async def _start(kind: str) -> dict[str, Any]:
    if kind == "image":
        from game_agent.assets.comfyui import actual_base, ensure_comfyui_running
        return await ensure_comfyui_running(actual_base())
    from game_agent.assets.tts import ensure_tts_running
    return await ensure_tts_running(TTS_BASE_URL)


async def _switch(kind: str, start: bool = True) -> dict[str, Any]:
    """把 GPU 让给 kind：停掉另一个 → 等它真的退出 → 确保自己在跑。

    `start=False` 只腾卡不拉起（外部命令自己负责启动，见 `make_room`）。
    """
    global _holder, _switches
    other = "voice" if kind == "image" else "image"
    info: dict[str, Any] = {"kind": kind, "stopped": None, "start": None}

    if not _url(kind):
        info["error"] = f"{kind} 未配置"  # 该服务没配，不需要腾卡
        return info

    try:
        if _url(other) and await _port_open(other):
            info["stopped"] = await _stop(other)
            info["stopped"]["service"] = other
            closed = await _wait_closed(other, GPU_STOP_TIMEOUT_S)
            if not closed:
                info["warn"] = (f"{other} 在 {GPU_STOP_TIMEOUT_S}s 内没退出，"
                                f"可能仍在占显存（端口 {_port_of(other)}）")
                log.warning(info["warn"])
            else:
                _switches += 1
                log.info("gpu switched to %s（已停 %s）", kind, other)
    except Exception as e:  # noqa: BLE001
        info["stop_error"] = str(e)[:200]
        log.warning("停 %s 失败：%s", other, e)

    if start:
        try:
            info["start"] = await _start(kind)  # ensure_* 内部先探活，活着就秒返回
        except Exception as e:  # noqa: BLE001
            info["start_error"] = str(e)[:200]
            log.warning("拉起 %s 失败：%s", kind, e)

    _holder = kind
    return info


async def _acquire(kind: str, start: bool = True) -> dict[str, Any]:
    """占住 GPU 并腾给 kind。同任务重入（外层已占同 kind，或 _switch 回调 ensure_*）直接放行。"""
    global _owner, _held_kind, _depth
    if not GPU_EXCLUSIVE:
        return {"skipped": True}
    if not _url(kind):
        return {"skipped": f"{kind} 未配置"}
    me = asyncio.current_task()
    if _owner is me and _held_kind is not None:
        _depth += 1
        if _held_kind != kind:
            # 外层正占着另一个服务（比如生图进行中），此时不能把它的卡抢走
            log.warning("本任务已占用 GPU 给 %s，%s 的腾卡请求跳过（不抢正在用的卡）",
                        _held_kind, kind)
            return {"skipped": f"outer holds {_held_kind}"}
        return {"reentrant": True}
    await _LOCK.acquire()
    try:
        _owner, _held_kind, _depth = me, kind, 1
        return await _switch(kind, start=start)
    except BaseException:
        _drop_owner()
        raise


def _drop_owner() -> None:
    global _owner, _held_kind, _depth
    _owner, _held_kind, _depth = None, None, 0
    if _LOCK.locked():
        _LOCK.release()


def release() -> None:
    """释放 GPU 占用（服务保持运行，下次切换时再停）。重入时逐层退。"""
    global _depth
    if _owner is not asyncio.current_task():
        return
    if _depth > 1:
        _depth -= 1
        return
    _drop_owner()


async def prepare(kind: str, start: bool = True) -> dict[str, Any]:
    """占住 GPU 并腾给 kind（调用方负责随后 `release()`）。用于需要在锁外先起服务的场景。"""
    return await _acquire(kind, start=start)


async def make_room(kind: str) -> dict[str, Any]:
    """腾卡给 kind 但不拉起它（调用方自己 Popen）：`gpu_slot` 链内重入时只加层，不会放掉外层的锁。"""
    info = await _acquire(kind, start=False)
    release()
    return info


def _markers(kind: str) -> tuple[str, ...]:
    """命令里出现这些字样就认为是在起该 kind 的服务（路径/进程名，端口另按 --port 判）。"""
    if kind == "image":
        return ("comfyui",)
    home = str(TTS_HOME or "").lower().replace("\\", "/").rstrip("/")
    return tuple(x for x in (home, "indextts") if x)


def kind_for_command(cmd: str) -> str | None:
    """从启动命令认出它要跑哪个 GPU 服务（认不出返回 None）。

    组长用 start_service 直启本地服务时也要腾卡，否则「语音起来了、ComfyUI 还占着显存」照样 OOM。
    先按 `--port` 精确匹配，再退回安装目录/服务名。
    """
    if not GPU_EXCLUSIVE:
        return None
    low = (cmd or "").lower().replace("\\", "/")
    for kind in KINDS:
        if f"--port {_port_of(kind)}" in low:
            return kind
    for kind in KINDS:
        if any(m in low for m in _markers(kind)):
            return kind
    return None


@asynccontextmanager
async def gpu_slot(kind: str) -> AsyncIterator[dict[str, Any]]:
    """`async with gpu_slot("image"): ...` —— 期间独占 GPU（生图）或（生音）。"""
    info = await prepare(kind)
    try:
        yield info
    finally:
        release()


def gpu_image(fn):  # noqa: ANN001, ANN201
    """装饰器：让这个协程独占 GPU 再跑（生图）。"""
    async def _wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        async with gpu_slot("image"):
            return await fn(*args, **kwargs)

    _wrapped.__name__ = getattr(fn, "__name__", "gpu_image_wrapped")
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    _wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return _wrapped


def gpu_voice(fn):  # noqa: ANN001, ANN201
    """装饰器：让这个协程独占 GPU 再跑（生音）。"""
    async def _wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        async with gpu_slot("voice"):
            return await fn(*args, **kwargs)

    _wrapped.__name__ = getattr(fn, "__name__", "gpu_voice_wrapped")
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    _wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return _wrapped


async def shutdown_media_services(image_base: str = "", voice_base: str = "",
                                  kinds: set[str] | None = None) -> str:
    """会话收尾：把本地生图（ComfyUI）与语音（IndexTTS2）服务都停掉，释放显存。

    GPU 互斥只保证「同一时刻只有一个媒体服务占 GPU」；本次会话结束后两边都该退场，
    不留后台进程吃显存。走全局锁排队：若并行的另一个 run 正在生成，先等它跑完再停，
    不会把别人的生成拦腰砍断。base_url 传团队配置的档位地址（空则用全局配置兜底）；
    只杀本机端口对应的进程树，远端/未启动自然跳过。各自容错，互不影响。
    kinds 给出时只关这些服务：本 run 没起过生图/配音就不替别人收摊
    （服务可能是工作台或别的会话起的）。``None`` = 两种都关（旧行为）。
    返回值得发系统提示的说明（什么都没停则返回空串）。
    """
    killed: list[str] = []
    async with _LOCK:
        for kind, label in (("image", "生图服务（ComfyUI）"), ("voice", "语音服务（IndexTTS2）")):
            if kinds is not None and kind not in kinds:
                continue
            bases: list[str] = []
            for raw in (_url(kind), (image_base if kind == "image" else voice_base) or ""):
                b = (raw or "").strip().rstrip("/")
                if b and b not in bases:
                    bases.append(b)
            for base in bases:
                try:
                    if kind == "image":
                        from game_agent.assets.comfyui import stop_comfyui_service
                        info = await stop_comfyui_service(base)
                    else:
                        from game_agent.assets.tts import stop_tts_service
                        info = await stop_tts_service(base)
                    if info.get("killed"):
                        killed.append(label)
                        log.info("会话收尾：已关闭本地%s（端口 %s）", label, info.get("port"))
                except Exception:  # noqa: BLE001 - 单边失败不影响另一边
                    log.warning("会话收尾关闭 %s 失败", kind, exc_info=True)
    if not killed:
        return ""
    return "本次会话已结束，已关闭本地" + "、".join(killed) + "并释放显存。"
