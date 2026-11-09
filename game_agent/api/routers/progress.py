"""处理过程可视化：进程内的长任务进度表（供前端轮询）。

背景：音/图/视频工作台里的一次生成都是几秒到几分钟的**阻塞**请求——后端要等全部算完
才一次性返回，中途用户只看到按钮上「生成中…」，完全没有过程感，像纯等待。这里用一个
进程内的轻量任务表，让各路由在执行过程中随时汇报「当前阶段 / 第几张 / 估算进度」，
前端另开轮询读它，把真实的处理阶段显示出来。

要点：
- 单进程 uvicorn（无 workers），进程内 dict 即可跨请求共享；
- OrderedDict 限量（默认 200 条），超出淘汰最旧，避免无限增长；
- stage / stage_label 是**真实状态**（来自代码执行到的位置、子服务的真实状态），
  percent 只是按经验耗时折算的**估算**值（前端必须明确标注「估算」）。
"""
from __future__ import annotations

import time
from collections import OrderedDict

_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_MAX = 200

_SNAPSHOT_KEYS = (
    "job_id", "stage", "stage_label", "detail", "image_index", "image_count",
    "prompt_id", "queue_state", "queue_position", "elapsed_ms", "percent",
    "done", "error", "log", "previews")

_LOG_MAX = 24  # 时间线最多保留的条目数（过程可视化用）


def new_job(job_id: str, image_count: int = 1, est_total_s: float = 300.0) -> dict | None:
    """登记一个新任务（无 job_id 返回 None，调用方可忽略）。"""
    if not job_id:
        return None
    job = {
        "job_id": job_id, "stage": "queued", "stage_label": "已排队，等待开始",
        "detail": "", "image_index": 0, "image_count": max(1, int(image_count or 1)),
        "prompt_id": "", "queue_state": "", "queue_position": None,
        "sample_started_ms": 0, "stage_started_ms": int(time.time() * 1000),
        "elapsed_ms": 0, "percent": 0, "done": False, "error": "",
        "started_at": time.time(), "est_total_s": float(est_total_s or 300.0),
        "log": [{"label": "已排队，等待开始", "at": int(time.time() * 1000)}],
        "previews": [],  # 边生成边质检后「已完成」的图（含 verdict），供前端即时显示
    }
    _JOBS[job_id] = job
    _JOBS.move_to_end(job_id)
    while len(_JOBS) > _JOBS_MAX:
        _JOBS.popitem(last=False)
    return job


def _append_log(job: dict, label: str, now_ms: int) -> None:
    """把阶段文案追加到时间线（相邻重复去重、限量保留最新）。"""
    if not label:
        return
    lg = job.setdefault("log", [])
    if lg and lg[-1].get("label") == label:
        return
    lg.append({"label": label, "at": now_ms})
    if len(lg) > _LOG_MAX:
        del lg[: len(lg) - _LOG_MAX]


def set_stage(job_id: str, stage: str, label: str, detail: str | None = None,
              **extra) -> None:
    """更新任务阶段（无 job_id / 任务已被回收时静默跳过，保证不开进度也完全不受影响）。

    detail=None 表示「沿用上一次的 detail」；显式传 "" 可清空（避免上一张的错误残留）。
    """
    if not job_id:
        return
    job = _JOBS.get(job_id)
    if job is None:
        return
    now = int(time.time() * 1000)
    job.update(stage=stage, stage_label=label, stage_started_ms=now, **extra)
    if detail is not None:
        job["detail"] = detail
    if stage in ("done", "failed"):
        job["done"] = True
    _append_log(job, label, now)


def set_est_total(job_id: str, est_total_s: float) -> None:
    """根据实际画幅/时长调整估算总耗时（仅影响 percent 估算）。"""
    job = _JOBS.get(job_id) if job_id else None
    if job is not None and est_total_s and est_total_s > 0:
        job["est_total_s"] = float(est_total_s)


def set_queue(job_id: str, state: str, position: int | None = None) -> None:
    job = _JOBS.get(job_id) if job_id else None
    if job is not None:
        job["queue_state"] = state or ""
        job["queue_position"] = position


def get(job_id: str) -> dict | None:
    return _JOBS.get(job_id) if job_id else None


def add_preview(job_id: str, entry: dict) -> None:
    """登记一张「已完成」的图（含 verdict），让前端边生成边显示。
    entry 形如 {index, total, file, url, size, view, verdict, issues, ok}。
    """
    job = _JOBS.get(job_id) if job_id else None
    if job is None or not isinstance(entry, dict):
        return
    lst = job.setdefault("previews", [])
    # 同 index 覆盖（修复重生成会用新文件替换同一张）
    idx = entry.get("index")
    lst[:] = [e for e in lst if e.get("index") != idx]
    lst.append(entry)
    lst.sort(key=lambda e: (e.get("index") is None, e.get("index") or 0))
    if len(lst) > 100:
        del lst[: len(lst) - 100]


def clear_previews(job_id: str) -> None:
    """清空已完成预览（生成回退重试时，丢弃上一轮已推送的图）。"""
    job = _JOBS.get(job_id) if job_id else None
    if job is not None:
        job["previews"] = []


def _estimate_percent(job: dict) -> int:
    """按已耗时 / 经验总耗时折算百分比（封顶 95%，仅 done 时 100%）。纯估算。"""
    if job.get("done"):
        return 100 if job.get("stage") == "done" else 0
    elapsed = time.time() - job.get("started_at", time.time())
    total = float(job.get("est_total_s") or 300.0)
    return max(1, min(95, int(elapsed / total * 100)))


def snapshot(job_id: str) -> dict:
    """返回可直接 JSON 化的进度快照；任务不存在时 stage='unknown'。"""
    job = _JOBS.get(job_id) if job_id else None
    if job is None:
        return {"job_id": job_id, "stage": "unknown", "stage_label": "", "detail": "",
                "image_index": 0, "image_count": 0, "prompt_id": "", "queue_state": "",
                "queue_position": None, "elapsed_ms": 0, "percent": 0, "done": False,
                "error": "", "log": [], "previews": []}
    job["elapsed_ms"] = int((time.time() - job.get("started_at", time.time())) * 1000)
    job["percent"] = _estimate_percent(job)
    out = {k: job.get(k) for k in _SNAPSHOT_KEYS}
    out["log"] = list(job.get("log") or [])
    out["previews"] = list(job.get("previews") or [])
    return out
