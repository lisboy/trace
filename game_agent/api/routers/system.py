"""系统日志：给前端「系统日志」弹窗用（尾部轮询 + 历史文件尾巴）。

设计取舍：
  * 主体走进程内存环形缓冲（见 logging_setup.BufferHandler）——`since` 游标轮询，
    返回的永远是「上次之后新增的行」，前端拼起来就是连续的实时日志；
  * 另外给一个 app.log 尾巴接口：进程刚重启、缓冲还是空的时候能翻上一轮的日志。
"""
from __future__ import annotations

import platform
import sys
import time

from fastapi import APIRouter, Depends, Query

from game_agent.api.deps import require
from game_agent.auth.rbac import PERM_VIEW
from game_agent.core.context import RequestContext
from game_agent.logging_setup import last_seq, log_file_path, snapshot_logs, tail_log_file

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system/media-queue")
async def media_queue_status(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """媒体生成队列状态：当前是否有生成在跑、排了几个（图片/语音/视频共用）。"""
    from game_agent.assets import media_queue
    return media_queue.status()


@router.get("/system/logs")
async def get_logs(
    since: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    level: str | None = Query(None, description="最低级别：DEBUG/INFO/WARNING/ERROR"),
    q: str | None = Query(None, description="消息/logger 包含的子串"),
    ctx: RequestContext = Depends(require(PERM_VIEW)),
):
    snap = snapshot_logs(since=since, limit=limit, min_level=level, query=q)
    snap["now"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return snap


@router.get("/system/logs/file")
async def get_log_file_tail(
    tail: int = Query(300, ge=1, le=5000),
    ctx: RequestContext = Depends(require(PERM_VIEW)),
):
    return {
        "file": log_file_path(),
        "lines": tail_log_file(tail),
        "last_seq": last_seq(),
    }


@router.get("/system/metrics")
async def get_metrics(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """本机 CPU / 内存 / GPU 瞬时状态（会话顶栏轮询用，服务端 4s 缓存）。"""
    from game_agent.core import sysinfo

    m = await sysinfo.snapshot()
    m["now"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return m


@router.get("/system/info")
async def get_system_info(ctx: RequestContext = Depends(require(PERM_VIEW))):
    from game_agent.config import DATA_DIR, MODE

    return {
        "mode": MODE,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
        "data_dir": str(DATA_DIR),
        "log_file": log_file_path(),
        "log_lines": last_seq(),
        "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
