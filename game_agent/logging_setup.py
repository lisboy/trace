"""结构化日志（JSON 可选，这里用可读文本 + request_id 注入）。

除了 stdout，还会：
  * 把每条记录塞进内存环形缓冲（`snapshot_logs`），供「系统日志」弹窗轮询——
    不读磁盘、不受文件滚动影响，看到的就是当前进程正在发生的事；
  * 落一份到 `<DATA_DIR>/logs/app.log`（10MB 滚动 3 份），进程重启后还能翻旧账。
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import sys
import threading
import time
from typing import Any

_CONFIGURED = False

# ---- 内存环形缓冲（系统日志弹窗的数据源）----
BUFFER_SIZE = 5000
_buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=BUFFER_SIZE)
_buffer_lock = threading.Lock()
_seq = 0
_log_file: str | None = None

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class BufferHandler(logging.Handler):
    """把日志同时写入内存环形缓冲，供 /api/system/logs 近实时读取。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        global _seq
        try:
            msg = record.getMessage()
            if record.exc_info:
                tail = logging.Formatter().formatException(record.exc_info)
                if tail:
                    msg = f"{msg}\n{tail}"
            with _buffer_lock:
                _seq += 1
                _buffer.append(
                    {
                        "seq": _seq,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": msg[:8000],
                    }
                )
        except Exception:  # noqa: BLE001 - 日志处理器绝不能把主流程带崩
            pass


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED, _log_file
    if _CONFIGURED:
        return
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    buffered = BufferHandler()
    buffered.setFormatter(fmt)
    root.addHandler(buffered)

    # 文件落盘（目录可能还没建：configure_logging 早于 ensure_dirs，自己兜一下）
    try:
        from game_agent.config import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "app.log"
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        _log_file = str(path)
    except Exception as exc:  # noqa: BLE001 - 写不了文件也要能跑
        root.warning("日志文件不可用，仅内存缓冲：%s", exc)

    root.setLevel(level)
    # 第三方降噪
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def last_seq() -> int:
    with _buffer_lock:
        return _seq


def log_file_path() -> str | None:
    return _log_file


def snapshot_logs(
    since: int = 0,
    limit: int = 500,
    min_level: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """取内存缓冲里 seq > since 的日志（按时间正序）。

    返回 `truncated=True` 表示 since 太老、中间那截已经被环形缓冲挤掉了。
    """
    floor = _LEVEL_ORDER.get((min_level or "").upper(), 0)
    needle = (query or "").strip().lower()
    with _buffer_lock:
        items = list(_buffer)
        total = _seq
    first_seq = items[0]["seq"] if items else total + 1
    truncated = bool(items) and since + 1 < first_seq

    out: list[dict[str, Any]] = []
    for item in items:
        if item["seq"] <= since:
            continue
        if floor and _LEVEL_ORDER.get(item["level"], 0) < floor:
            continue
        if needle and needle not in item["message"].lower() and needle not in item["logger"].lower():
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return {
        "lines": out,
        "last_seq": total,
        "buffered": len(items),
        "buffer_size": BUFFER_SIZE,
        "truncated": truncated,
        "file": _log_file,
    }


def tail_log_file(tail: int = 300) -> list[str]:
    """读 app.log 末尾若干行（重启后想翻之前进程的日志时用）。"""
    if not _log_file:
        return []
    try:
        with open(_log_file, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = min(size, max(4096, tail * 400))
            fh.seek(size - block)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[-tail:]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"game_agent.{name}")
