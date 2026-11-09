"""媒体生成全局串行队列（跨会话、跨类型）。

需求（2026-11-01）：同一时刻只允许一个会话在「调用生成服务」——
A 会话正在生成语音时，B 会话要生成语音必须等 A 完成；
A 在生成图片时，B 要生成语音/视频同样排队；反之亦然。
排队依据是**是否有生成调用正在进行**，与服务进程是否已启动无关。

与 gpu_arbiter 的分工：
- gpu_arbiter 解决「本地 ComfyUI / IndexTTS2 共用一张显卡，两个服务不能同时驻留」；
- media_queue 解决「任何来源（本地/远程 openai_image / openai_tts）的生成调用都全局串行」。
两者锁序固定：先 media_queue，再 gpu_arbiter（生成函数内层才碰 GPU），避免死锁。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

# 单把全局锁：图片、语音、视频全部共用一条生成队列
_LOCK = asyncio.Lock()
_owner: asyncio.Task | None = None
_depth = 0
_current: dict[str, Any] = {}
_waiting = 0
_served = 0
_last: dict[str, Any] = {}


def _state() -> dict[str, Any]:
    return {
        "busy": _LOCK.locked(),
        "waiting": _waiting,
        "served": _served,
        **({"current": dict(_current)} if _current else {}),
    }


def status() -> dict[str, Any]:
    """诊断用快照：当前谁在生成、还有几个在排队。"""
    return _state()


def busy() -> bool:
    return _LOCK.locked()


def current_kind() -> str:
    return str(_current.get("kind") or "")


async def wait_until_idle(timeout_s: float = 0.2) -> bool:
    """测试/诊断辅助：非阻塞确认队列是否空闲（不用来抢锁）。"""
    if not _LOCK.locked():
        return True
    try:
        await asyncio.wait_for(_LOCK.acquire(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return False
    _LOCK.release()
    return True


@asynccontextmanager
async def media_slot(kind: str, owner: str = "") -> AsyncIterator[dict[str, Any]]:
    """占用全局生成队列；同一任务重入只加层（generate 入口/底层函数可能嵌套装饰）。

    kind: image | voice | video（video 与图片同队列，预留给后续视频生成接入口）。
    owner: 可选标记（如 session_id / run_id），仅用于日志和 status 诊断。
    """
    global _owner, _depth, _waiting, _served
    me = asyncio.current_task()
    if _owner is me and _depth > 0:
        _depth += 1
        try:
            yield _state()
        finally:
            _depth -= 1
        return

    _waiting += 1
    t0 = time.monotonic()
    try:
        await _LOCK.acquire()
    except BaseException:
        _waiting -= 1
        raise
    waited = time.monotonic() - t0
    _waiting -= 1
    _owner, _depth = me, 1
    _current.update({"kind": kind, "owner": owner, "started_at": time.time(),
                     "waited_ms": int(waited * 1000)})
    if waited >= 0.5:
        from game_agent.logging_setup import get_logger
        get_logger("media.queue").info("媒体生成排队 %.1fs 后轮到：kind=%s owner=%s",
                                       waited, kind, owner or "-")
    try:
        yield _state()
    finally:
        _depth = 0
        _owner = None
        _current.clear()
        _served += 1
        _last.update({"kind": kind, "owner": owner, "waited_ms": int(waited * 1000)})
        if _LOCK.locked():
            _LOCK.release()


def serialized(kind: str):
    """装饰器：让一个协程的所有调用都先进全局媒体生成队列。"""
    def _deco(fn):  # noqa: ANN001, ANN202
        import functools

        @functools.wraps(fn)
        async def _wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            async with media_slot(kind):
                return await fn(*args, **kwargs)
        return _wrapped
    return _deco
