"""界面直连出片工作台：不走 agent，用户自己在素材库页面生成视频（对齐 imagegen.py）。

- POST /reference      上传首帧参考图（multipart），返回服务端文件名
- GET  /options        返回出片参数（默认分辨率/帧数/fps + 模型名）与 ComfyUI 可用性
- POST /               生成：提示词（可选 LLM 增强）+ 可选首帧参考图 + 尺寸/时长
- GET  /files/{name}   取生成结果视频

出片与出图共用本地 ComfyUI（Wan2.2 TI2V 5B），同样走 GPU 互斥与全局媒体队列。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from game_agent.api.deps import require
from game_agent.api.routers import progress
from game_agent.assets import gpu_arbiter, media_queue
from game_agent.assets.comfyui import (
    ComfyUIError, comfyui_queue_state, ensure_comfyui_running, generate_video_bytes,
    generate_video_chain_bytes, smooth_video_bytes, fit_video_size, _comfyui_alive)
from game_agent.assets.placeholder import _parse_size
from game_agent.auth.rbac import PERM_AGENT_CONFIG, PERM_DOWNLOAD
from game_agent.config import (
    COMFYUI_BASE_URL, COMFYUI_VIDEO_CFG, COMFYUI_VIDEO_FPS,
    COMFYUI_VIDEO_HEIGHT, COMFYUI_VIDEO_LENGTH, COMFYUI_VIDEO_NEGATIVE_PROMPT,
    COMFYUI_VIDEO_SMOOTH,
    COMFYUI_VIDEO_SMOOTH_FPS, COMFYUI_VIDEO_STEPS, COMFYUI_VIDEO_TEMPORAL_DENOISE,
    COMFYUI_VIDEO_TIMEOUT_S, COMFYUI_VIDEO_WIDTH, DATA_DIR)
from game_agent.core.context import RequestContext
from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm
from game_agent.llm.team_profile import team_main_profile

router = APIRouter(prefix="/api/video-gen", tags=["videogen"])

_VIDEO_DIR = DATA_DIR / "objects" / "videogen"
_UPLOAD_DIR = _VIDEO_DIR / "uploads"
_RESULT_DIR = _VIDEO_DIR / "generated"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

_ENHANCE_PROMPT = """你是游戏动态素材的视频提示词专家。把用户输入改写成一段高质量视频生成提示词：
保留用户的关键意图（主体/动作/场景/镜头运动/风格/氛围），补全镜头如何运动、主体如何运动、
光影与节奏细节。输出中文提示词一段话，不要解释、不要列表、不要前后缀。
要求：镜头运动缓和（缓慢推拉/轻移即可），主体动作幅度宁小勿大（眨眼、轻微转头、衣袂飘动就够），
避免剧烈动作和快速运镜；强调保持人物长相、服装、五官前后一致，不要变脸、不要画面撕裂；
**保持原图的二次元插画画风与配色，不要变成 3D/写实/塑料人偶**；**背景保持干净、延续原图背景，不要脑补深色或模糊光斑背景**。
用户输入："""


def _clean(name: str) -> str:
    return _SAFE_NAME.sub("_", name)


def _new_name(name: str) -> str:
    stem = _clean(Path(name).stem)[:60] or "ref"
    suffix = _clean(Path(name).suffix) or ".png"
    return f"{uuid.uuid4().hex[:8]}_{stem}{suffix}"


@router.get("/options")
async def options(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """出片工作台可选参数 + ComfyUI 是否可用（Wan2.2 TI2V 5B，原生 24fps）。"""
    alive = await _comfyui_alive(COMFYUI_BASE_URL) if COMFYUI_BASE_URL else False
    return {
        "available": alive,
        "base_url": COMFYUI_BASE_URL,
        "model": "wan",
        "model_label": "Wan2.2 TI2V 5B",
        "default_size": f"{COMFYUI_VIDEO_WIDTH}x{COMFYUI_VIDEO_HEIGHT}",
        "default_length": COMFYUI_VIDEO_LENGTH,
        "default_fps": COMFYUI_VIDEO_FPS,
        "default_steps": COMFYUI_VIDEO_STEPS,
        "smooth_fps": COMFYUI_VIDEO_SMOOTH_FPS if COMFYUI_VIDEO_SMOOTH else 0,
        "presets": [
            {"key": "char_2x3", "label": "角色竖版 2:3 / 约5s", "size": "512x768", "duration_s": 5},
            {"key": "fast_480p", "label": "快速预览 480p / 约3s", "size": "832x480", "duration_s": 3},
            {"key": "std_480p", "label": "标准 480p / 约5s", "size": "832x480", "duration_s": 5},
            {"key": "wide_480p", "label": "宽屏 480p / 约3s", "size": "1280x704", "duration_s": 3},
        ],
    }


@router.post("/reference")
async def upload_reference(file: UploadFile = File(...),
                           ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="参考图不能超过 15MB")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = _new_name(file.filename or "ref.png")
    (_UPLOAD_DIR / fname).write_bytes(data)
    return {"file": fname}


@router.get("/files/{name}")
async def result_file(name: str, ctx: RequestContext = Depends(require(PERM_DOWNLOAD))):
    p = _RESULT_DIR / _clean(name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(p, media_type="video/mp4")


class GenIn(BaseModel):
    prompt: str = ""
    size: str = ""
    duration_s: float = 0
    fps: int = 0
    steps: int = 0
    reference: str = ""     # 首帧参考图文件名（/reference 上传返回）
    negative_prompt: str | None = None
    enhance: bool = True
    denoise: bool | None = None   # 时间域降噪，None=用默认配置
    # 接龙长视频：>0 时按「每段 segment_seconds 秒、逐段接上一段尾帧」出片，段数=segments。
    # 0 = 不接龙，走单段生成（duration_s 决定时长）。
    segments: int = 0
    segment_seconds: float = 0.0  # 每段秒数（默认 1s = 25 帧@24fps）
    job_id: str = ""              # 前端生成的进度任务号，供 GET /jobs/{id} 轮询处理过程


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """前端轮询出片进度。任务已被回收/未知时返回 stage='unknown'。"""
    job = progress.get(job_id)
    if job is not None and job.get("stage") == "sampling" and job.get("prompt_id"):
        st = await comfyui_queue_state(job["prompt_id"], COMFYUI_BASE_URL)
        qs = st.get("state") or ""
        progress.set_queue(job_id, qs, st.get("position") if qs == "queued" else None)
    return progress.snapshot(job_id)


@router.post("")
async def generate(body: GenIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """工作台出片与聊天工具、出图共用全局媒体队列（跨会话串行）。"""
    job_id = (body.job_id or "").strip()
    if job_id and progress.get(job_id) is None:
        progress.new_job(job_id, 1, est_total_s=_est_video_total_s(body))
        progress.set_stage(job_id, "queued", "已排队，等待空闲算力…")
    async with media_queue.media_slot("video", "video-gen"):
        try:
            return await _generate_impl(body, ctx)
        except Exception as e:  # noqa: BLE001 - 记录失败阶段后原样抛给上层
            progress.set_stage(job_id, "failed", "生成失败", detail=str(e))
            raise


def _est_video_total_s(body: GenIn) -> float:
    """按帧数/段数估个总耗时（Wan 5B 8G 约每 25 帧 60~90s，另加补帧/拼接）。
    仅用于 percent 估算。"""
    fps = max(8, int(body.fps or COMFYUI_VIDEO_FPS))
    frames = 0
    if body.segments and body.segments > 0:
        frames = int(body.segments) * max(17, int(round((body.segment_seconds or 1.0) * fps)))
    elif body.duration_s and body.duration_s > 0:
        frames = int(round(body.duration_s * fps))
    else:
        frames = COMFYUI_VIDEO_LENGTH
    per_frame = 3.0  # 秒/帧（粗估）
    return frames * per_frame + 30


async def _generate_impl(body: GenIn, ctx: RequestContext):
    job_id = (body.job_id or "").strip()
    if job_id and progress.get(job_id) is None:
        progress.new_job(job_id, 1, est_total_s=_est_video_total_s(body))
    if not COMFYUI_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置 ComfyUI 地址（.env COMFYUI_BASE_URL）")
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="提示词不能为空")
    progress.set_stage(job_id, "enhancing", "正在分析重组提示词…" if body.enhance else "准备参数…")

    # 1) LLM 提示词增强（失败回退原文）
    enhanced, enhance_note = body.prompt.strip(), ""
    if body.enhance:
        try:
            profile = await team_main_profile(ctx.team_id)
            ctx_llm = Ctx(role="main", run_type="videogen-enhance",
                          requirement=body.prompt.strip(), model=(profile.get("model") or ""),
                          model_profile=profile or {})
            llm = build_llm("main", ctx_llm)
            t0 = time.monotonic()
            resp = await llm.chat(
                [{"role": "user", "content": _ENHANCE_PROMPT + body.prompt.strip()}],
                tools=[], ctx=ctx_llm, on_delta=None, summarizer=None)
            text = (resp.text or "").strip()
            if text:
                enhanced = text[:1500]
                enhance_note = f"LLM 增强（{int((time.monotonic() - t0) * 1000)}ms）"
            else:
                enhance_note = "LLM 返回为空，已用原始提示词"
        except Exception as e:  # noqa: BLE001
            enhance_note = f"LLM 增强失败已回退原文：{e}"

    # 2) 首帧参考图
    ref_bytes = None
    if body.reference:
        rp = _UPLOAD_DIR / _clean(body.reference)
        if not rp.is_file():
            raise HTTPException(status_code=400, detail="参考图不存在或已过期，请重新上传")
        ref_bytes = rp.read_bytes()

    # 3) 确保 ComfyUI 在运行（未启动则自动拉起并等待就绪）
    progress.set_stage(job_id, "starting", "正在准备 ComfyUI 服务…")
    boot = await ensure_comfyui_running(COMFYUI_BASE_URL)
    if boot["error"]:
        raise HTTPException(status_code=502, detail=f"ComfyUI 不可用：{boot['error']}")
    boot_note = (f"ComfyUI 已自动启动（{boot['waited_ms'] / 1000:.1f}s）" if boot["started"] else "")

    w, h = _parse_size(body.size or f"{COMFYUI_VIDEO_WIDTH}x{COMFYUI_VIDEO_HEIGHT}")
    fps = max(8, min(int(body.fps or COMFYUI_VIDEO_FPS), 30))
    length = 0
    if body.duration_s and body.duration_s > 0:
        length = int(round(body.duration_s * fps))
    negative = COMFYUI_VIDEO_NEGATIVE_PROMPT if body.negative_prompt is None else body.negative_prompt
    steps_in = int(body.steps or COMFYUI_VIDEO_STEPS)
    denoise = COMFYUI_VIDEO_TEMPORAL_DENOISE if body.denoise is None else body.denoise
    # 真正下发的出片宽高：32 倍数；有参考图时贴合其长宽比（防拉伸/裁切变形）。
    w, h = fit_video_size(w, h, ref_bytes)

    t0 = time.monotonic()
    _target = COMFYUI_VIDEO_SMOOTH_FPS if COMFYUI_VIDEO_SMOOTH else 0
    _RESULT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"gen_{uuid.uuid4().hex[:10]}.mp4"

    if body.segments and body.segments > 0:
        # ---- 接龙长视频：逐段以上一段尾帧为首帧，串成连贯长片 ----
        seg_sec = body.segment_seconds if body.segment_seconds > 0 else 1.0
        seg_total = max(1, min(int(body.segments), 30))
        progress.set_stage(job_id, "sampling", f"接龙出片：第 1/{seg_total} 段…",
                           image_count=seg_total, image_index=0)
        try:
            data, cm = await generate_video_chain_bytes(
                enhanced, segments=int(body.segments), segment_seconds=seg_sec,
                reference=ref_bytes, target_fps=_target, denoise=denoise,
                negative_prompt=negative, width=w, height=h, fps=fps,
                steps=steps_in, cfg=COMFYUI_VIDEO_CFG,
                base_url=COMFYUI_BASE_URL, timeout_s=COMFYUI_VIDEO_TIMEOUT_S,
                on_segment=(lambda i, n: progress.set_stage(
                    job_id, "sampling", f"接龙出片：第 {i + 1}/{n} 段…",
                    image_count=n, image_index=i) if job_id else None))
        except ComfyUIError as e:
            raise HTTPException(status_code=502, detail=f"ComfyUI 接龙出片失败：{e}")
        progress.set_stage(job_id, "saving", "正在补帧并保存视频…", image_index=seg_total, image_count=seg_total)
        (_RESULT_DIR / fname).write_bytes(data)
        progress.set_stage(job_id, "done", "视频已生成，正在载入…", percent=100)
        _out_fps = COMFYUI_VIDEO_SMOOTH_FPS if cm.get("fps_raised") else fps
        return {
            "file": fname, "url": f"/api/video-gen/files/{fname}", "size": len(data),
            "enhanced_prompt": enhanced if body.enhance else "",
            "enhance_note": boot_note + enhance_note,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "model": "wan",
            "params": {"size": f"{w}x{h}", "fps": _out_fps, "src_fps": fps,
                       "smoothed": bool(cm.get("fps_raised")),
                       "denoised": bool(cm.get("denoised")),
                       "prompt": body.prompt, "model": "wan",
                       "reference": bool(ref_bytes), "chained": True,
                       "segments": cm.get("segments"),
                       "seg_seconds": round(cm.get("seg_seconds") or 0, 2),
                       "native_frames": cm.get("native_frames")},
        }

    # ---- 单段出片 ----
    progress.set_stage(job_id, "sampling", "已提交到 ComfyUI，开始出片…")
    try:
        data = await generate_video_bytes(
            enhanced, negative_prompt=negative, width=w, height=h, length=length,
            fps=fps, steps=steps_in, cfg=COMFYUI_VIDEO_CFG,
            base_url=COMFYUI_BASE_URL, timeout_s=COMFYUI_VIDEO_TIMEOUT_S,
            reference=ref_bytes,
            on_prompt_id=((lambda pid: progress.set_stage(
                job_id, "sampling", "ComfyUI 正在采样出片…",
                prompt_id=pid, queue_state="running")) if job_id else None))
    except ComfyUIError as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI 出片失败：{e}")

    # 后处理：Wan 原生 24fps → 补到 60fps；补帧前轻做时间域降噪（防漫剧割裂/闪烁）。
    progress.set_stage(job_id, "saving", "正在补帧并保存视频…")
    data, pp = await asyncio.to_thread(smooth_video_bytes, data, _target, fps, denoise)
    (_RESULT_DIR / fname).write_bytes(data)
    progress.set_stage(job_id, "done", "视频已生成，正在载入…", percent=100)
    _out_fps = COMFYUI_VIDEO_SMOOTH_FPS if pp.get("fps_raised") else fps
    return {
        "file": fname, "url": f"/api/video-gen/files/{fname}", "size": len(data),
        "enhanced_prompt": enhanced if body.enhance else "",
        "enhance_note": boot_note + enhance_note,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "model": "wan",
        "params": {"size": f"{w}x{h}", "fps": _out_fps, "src_fps": fps,
                   "smoothed": bool(pp.get("fps_raised")),
                   "denoised": bool(pp.get("denoised")),
                   "prompt": body.prompt, "model": "wan",
                   "reference": bool(ref_bytes)},
    }
