"""美术 / 配音 / 测试 专属工具（设计 §4.2 / §4.3 / §7.8）。

无真实供应商时走占位模式：同路径同名，配 key 后一键重画代码零改动。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
from pathlib import Path

import httpx

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.assets import gpu_arbiter
from game_agent.assets import media_queue
from game_agent.assets.comfyui import (
    ComfyUIError, ensure_comfyui_running, generate_image_bytes as comfy_generate,
    generate_video_bytes as comfy_generate_video, smooth_video_bytes, fit_video_size)
from game_agent.assets.placeholder import _parse_size, generate_placeholder_image, generate_silent_wav
from game_agent.assets import synth as audio_synth
from game_agent.config import (COMFYUI_BASE_URL, COMFYUI_NEGATIVE_PROMPT, COMFYUI_TIMEOUT_S,
                               COMFYUI_WORKFLOW_MODE, COMFYUI_VIDEO_CFG, COMFYUI_VIDEO_FPS,
                               COMFYUI_VIDEO_HEIGHT, COMFYUI_VIDEO_LENGTH,
                               COMFYUI_VIDEO_NEGATIVE_PROMPT,
                               COMFYUI_VIDEO_SMOOTH, COMFYUI_VIDEO_SMOOTH_FPS,
                               COMFYUI_VIDEO_STEPS, COMFYUI_VIDEO_TIMEOUT_S,
                               COMFYUI_VIDEO_TEMPORAL_DENOISE, COMFYUI_VIDEO_WIDTH,
                               PLACEHOLDER_ENABLED)
from game_agent.logging_setup import get_logger

log = get_logger("agents.media")


def _safe_slug(value: object, default: str) -> str:
    """把 agent 给的 slug 收成"单个文件名"，防止写出目标目录。

    为什么必须收：实测（2026-10-27 顾霜心 CV 测试）配音 agent 为了让产物落到
    `web/audio/`，把 slug 传成 `../web/audio/gu-shuangxin-phases`、`web/audio/x`，
    结果文件被写到 `assets/audio/voice/../web/...` 与 `assets/audio/voice/web/...`——
    路径逃出了工具约定的落盘目录，还制造了一堆没人引用的垃圾文件。
    """
    s = str(value or "").strip().replace("\\", "/")
    s = s.split("/")[-1]              # 只取最后一段，丢掉 ../ 与任何子目录
    s = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", s).strip(".-")
    return s or default


async def _media_profile(t: ToolContext, kind: str) -> dict:
    """解析角色绑定的媒体档位（image→artist / audio→voice）；编排器不可用或未绑定时返回 {}。"""
    if t.orchestrator is None:
        return {}
    try:
        return await t.orchestrator.resolve_media_profile(str(t.run_ctx.team_id), kind)
    except Exception:  # noqa: BLE001
        return {}


def _auth_headers(prof: dict) -> dict:
    """远端媒体档位（可能是挂在 Cloudflare 后面的中转站）的请求头：
    带上浏览器 UA，否则 WAF 会把 python-httpx 判成机器人回 403 + 人机验证页；
    本地 ComfyUI / IndexTTS 带这个头也无害。"""
    from game_agent.config import LLM_USER_AGENT
    headers = {"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else {}
    if prof.get("api_key"):
        headers["Authorization"] = f"Bearer {prof['api_key']}"
    return headers


async def _openai_image_generate(prompt: str, size: str, prof: dict) -> bytes:
    """OpenAI 兼容图像生成（POST /images/generations，b64_json 返回）。"""
    base = (prof.get("base_url") or "").rstrip("/")
    if not base:
        raise ToolError("图像档位未配置 Base URL")
    body = {"model": prof.get("model") or "", "prompt": prompt, "size": size,
            "n": 1, "response_format": "b64_json"}
    headers = _auth_headers(prof)
    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
        r = await client.post(f"{base}/images/generations", json=body, headers=headers)
        if r.status_code != 200:
            raise ToolError(f"图像接口 HTTP {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or [{}]
        b64 = data[0].get("b64_json") or ""
        url = data[0].get("url") or ""
        if b64:
            return base64.b64decode(b64)
        if url:
            rr = await client.get(url)
            rr.raise_for_status()
            return rr.content
        raise ToolError("图像接口未返回图片数据")


def _artifact(t: ToolContext, **kw):  # type: ignore[no-untyped-def]
    return t.run_ctx.repo.create_artifact(
        team_id=t.run_ctx.team_id, project_id=t.run_ctx.project_id,
        run_id=t.run_ctx.run_id, task_id=t.task_id, **kw,
    )


def _emit(t: ToolContext, kind: str, payload: dict) -> None:
    """发一个运行事件；事件总线缺失/异常绝不影响真实生成。"""
    import asyncio
    try:
        bus = getattr(t.run_ctx, "bus", None)
        if bus is None:
            return
        asyncio.get_running_loop().create_task(bus.emit(_env(t, kind, payload)))
    except Exception:  # noqa: BLE001 - 事件只是体验层，不能挡产物
        pass


def _env(t: ToolContext, kind: str, payload: dict):  # type: ignore[no-untyped-def]
    from game_agent.core.abstractions import EventEnvelope
    return EventEnvelope(
        team_id=t.run_ctx.team_id, project_id=t.run_ctx.project_id,
        run_id=t.run_ctx.run_id, task_id=t.task_id, type=kind, payload=payload,
    )


@media_queue.serialized("image")
async def _generate_image_with_meta(args: dict, t: ToolContext) -> tuple[str, dict]:
    """generate_image 的实现体，附带结构化结果（placeholder/provider/rel_path/error），
    供批量出图统计「真实 / 占位 / 失败」并推进度事件——
    以前批量汇总把回退占位图也算成"成功 20/20 张"，用户看到全占位还以为出图成功了。"""
    meta: dict = {"placeholder": False, "provider": "", "rel_path": "", "error": "", "preset": ""}
    slug = _safe_slug(args.get("slug"), "asset")
    category = args.get("category") or "ui"
    size = args.get("size") or "512x256"
    transparent = bool(args.get("transparent", False))
    prompt = args.get("prompt", "")
    rel = f"assets/art/{category}/{slug}_v1.png"
    meta["rel_path"] = rel
    abs_path = t.workspace / rel

    # 项目绑定了强制预设：工具生图必须套用它的尺寸/LoRA/强度/工作流/负面词
    # （工作台手动出图不受影响，留给调参实验）。
    forced = None
    fp: dict = {}
    if t.run_ctx.project_id:
        try:
            from game_agent.assets import presets as img_presets
            forced = await img_presets.get_project_preset(t.run_ctx.project_id, t.run_ctx.team_id)
            fp = (forced or {}).get("params") or {}
        except Exception:  # noqa: BLE001 - 预设读不到不该挡住出图
            log.exception("读取项目强制预设失败（project=%s）", t.run_ctx.project_id)
    if forced:
        # 强制的是画风（LoRA/强度/工作流/负面词）；尺寸按素材用途走——头像 1:1、
        # 卡面 16:9 各不相同，一律套预设尺寸会把头像画成带白边的横图。
        # 只有工具没给尺寸时才用预设里的兜底尺寸。
        if not args.get("size"):
            size = str(fp.get("size") or size)
        meta["preset"] = forced["id"]
        log.info("generate_image 强制套用项目预设「%s」：size=%s lora=%s@%s",
                 forced.get("label"), size, fp.get("lora"), fp.get("lora_strength"))
    preset_desc = f"，项目预设「{forced['label']}」" if forced else ""

    w, h = _parse_size(size)
    log.info("generate_image 开始：slug=%s %dx%d cat=%s prompt=%r",
             slug, w, h, category, prompt[:80])
    # 负面词优先级：工具显式传入 > 项目强制预设 > .env 默认
    neg_override = args.get("negative_prompt")
    if neg_override is not None:
        negative_prompt = str(neg_override)
    elif fp.get("negative_prompt") is not None:
        negative_prompt = str(fp["negative_prompt"])
    else:
        negative_prompt = COMFYUI_NEGATIVE_PROMPT
    # 背面视角（背后/右后/左后）：追加「排除脸/正面/看镜头」负面词，否则底模即便正向写
    # 「看不到脸」仍会画出正脸（实测）。只对多视角逐张出图的背面子任务生效。
    _vkey = str(args.get("view") or "").strip()
    if _vkey:
        from game_agent.assets import views as _img_views
        negative_prompt = _img_views.build_view_negative(negative_prompt, _vkey)

    # 参考图：路径可相对工作区或绝对；读不到则退回纯文生图
    # reference_mode: style=只取画风内容全新（默认）；img2img=以参考图为底图重绘
    ref_bytes = None
    ref_desc = ""
    ref_mode = str(args.get("reference_mode") or "style").strip().lower()
    if ref_mode not in ("style", "img2img"):
        ref_mode = "style"
    # 重绘度由模型按「这次要改多少」决定，不写死：模型看需求自己传 reference_denoise。
    #   0.3~0.45 微调（改色/小修饰，保留构图与细节）
    #   0.5~0.65 中等改造（换背景/换姿态，保留脸与主体）
    #   0.75~0.9 大改（只借参考图起手，主体可明显变化）
    # 不传则用 .env 的 COMFYUI_IMG2IMG_DENOISE（0.42）兜底。
    try:
        ref_denoise = float(args.get("reference_denoise")) if args.get("reference_denoise") is not None else None
    except (TypeError, ValueError):
        ref_denoise = None
    if ref_denoise is not None:
        ref_denoise = max(0.0, min(1.0, ref_denoise))
    ref_path = str(args.get("reference") or "").strip()
    if ref_path:
        rp = Path(ref_path)
        if not rp.is_absolute():
            rp = (t.workspace / rp).resolve()
        if rp.is_file():
            try:
                ref_bytes = rp.read_bytes()
                _dn = ref_denoise if ref_denoise is not None else "默认"
                ref_desc = f"，参考图：{ref_path}（{'风格参考' if ref_mode == 'style' else f'img2img 重绘 denoise={_dn}'}）"
            except Exception:  # noqa: BLE001
                ref_bytes = None
        else:
            ref_desc = f"，参考图 {ref_path} 不存在已忽略"

    # 生成链路：美术角色绑定的图像档位 > 环境变量 COMFYUI_BASE_URL > 占位
    prof = await _media_profile(t, "image")
    provider = (prof.get("provider") or "").lower()
    fallback_note = ""
    if provider == "openai_image" and prof:
        try:
            data = await _openai_image_generate(prompt, size, prof)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(data)
            await _artifact(t, asset_kind="image", slug=slug, rel_path=rel,
                            provider="openai_image", is_placeholder=False,
                            params_json=args, size=len(data))
            _emit(t, "artifact", {"asset_kind": "image", "slug": slug, "rel_path": rel,
                                  "placeholder": False})
            meta.update(provider="openai_image")
            log.info("generate_image 成功（openai_image）：slug=%s -> %s（%d bytes）", slug, rel, len(data))
            return (f"已通过图像档位「{prof.get('model') or provider}」生成真实图片：{rel} {w}x{h}{preset_desc}，"
                    f"prompt：{prompt[:150]}"), meta
        except (ToolError, ComfyUIError, Exception) as e:  # noqa: BLE001
            fallback_note = f"图像档位生成失败，已回退：{e}\n"
            log.warning("generate_image 图像档位失败（slug=%s）：%s: %s", slug, type(e).__name__, e)
            if not PLACEHOLDER_ENABLED and not COMFYUI_BASE_URL:
                raise ToolError(f"图像档位生成失败且无回退可用：{e}")
    elif (provider == "comfyui" and prof) or COMFYUI_BASE_URL:
        # 未运行则自动拉起 ComfyUI（失败不阻塞，继续尝试生成/回退）
        comfy_base = (prof.get("base_url") or "").rstrip("/") or COMFYUI_BASE_URL
        gpu_arbiter.mark_used("image", t.run_ctx.session_id)
        boot = await ensure_comfyui_running(comfy_base)
        if boot["error"]:
            fallback_note = f"ComfyUI 自动启动失败：{boot['error']}\n"
            log.warning("generate_image ComfyUI 启动失败（slug=%s）：%s", slug, boot["error"])
        data, style_fallback, last_err = None, "", ""
        # 三级回退：所选参考模式 → 低重绘度图生图 → 纯文生图（参考图在部分 DiT 底模上不适配）
        first: dict = dict(reference=ref_bytes, reference_mode=ref_mode)
        if ref_mode != "style" and ref_denoise is not None:
            first["reference_denoise"] = ref_denoise  # 模型按需求给的重绘度
        attempts = [(first, "")] if ref_bytes else [(dict(), "")]
        if ref_bytes:
            attempts += [
                (dict(reference=ref_bytes, reference_mode="img2img", reference_denoise=0.5),
                 "（该模型不支持所选参考模式，已用低重绘度图生图近似）"),
                (dict(), "（参考图在该模型上不可用，已按纯文生图生成）"),
            ]
        for i, (kw, note) in enumerate(attempts, 1):
            try:
                data = await comfy_generate(
                    prompt,
                    negative_prompt=negative_prompt,
                    width=w, height=h,
                    base_url=comfy_base,
                    workflow_mode=str(fp.get("workflow_mode") or COMFYUI_WORKFLOW_MODE),
                    model=str(fp.get("model") or ""),
                    lora=(fp["lora"] if "lora" in fp else None),
                    lora_strength=fp.get("lora_strength"),
                    timeout_s=COMFYUI_TIMEOUT_S, **kw)
                style_fallback = note
                break
            except ComfyUIError as e:
                # 真实失败原因必须带出去：以前这里静默吞掉，占位图返回里只有一句
                # "ComfyUI 生成失败"，根因（连接拒绝/超时/工作流被拒）全靠猜
                last_err = str(e)
                data = None
                log.warning("generate_image ComfyUI 第 %d/%d 次尝试失败（slug=%s）：%s",
                            i, len(attempts), slug, last_err)
                if "超时" in last_err:
                    # 超时是负载/显存问题，换参考模式重试照样超时（2026-10-29：
                    # 一张图三次各超 300s，素材条十分钟不涨，用户以为在卡死）。
                    # 超时任务已被撤销，直接收工报错，不要再用另外两种模式各烧一个超时。
                    break
        if data:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(data)
            await _artifact(t, asset_kind="image", slug=slug, rel_path=rel, provider="comfyui",
                            is_placeholder=False, params_json=args, size=len(data))
            _emit(t, "artifact", {"asset_kind": "image", "slug": slug, "rel_path": rel,
                                  "placeholder": False})
            meta.update(provider="comfyui")
            alpha = "（注意：当前 ComfyUI 输出不带透明通道，透明需求需在自定义工作流中处理）" if transparent else ""
            return f"已通过 ComfyUI 生成真实图片：{rel} {w}x{h}{preset_desc}，prompt：{prompt[:150]}{ref_desc}{style_fallback}{alpha}", meta
        else:
            reason = last_err or (fallback_note.strip() or "原因未知")
            fallback_note = f"ComfyUI 生成失败（{reason}），已回退占位图\n"
            meta["error"] = reason
            log.warning("generate_image 全部尝试失败（slug=%s）：%s", slug, reason)
    if not PLACEHOLDER_ENABLED:
        raise ToolError("ComfyUI 生成失败且占位模式已关闭" if fallback_note
                        else "未配置图像供应商且占位模式已关闭")
    info = generate_placeholder_image(str(abs_path), slug, size, transparent,
                                      prompt=prompt)
    await _artifact(
        t, asset_kind="image", slug=slug, rel_path=rel, provider="placeholder",
        is_placeholder=True, params_json=args, size=info.get("width", 0) * info.get("height", 0),
    )
    await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
    _emit(t, "artifact", {"asset_kind": "image", "slug": slug, "rel_path": rel, "placeholder": True})
    meta.update(placeholder=True, provider="placeholder")
    return (f"{fallback_note}已生成占位图：{rel} {size} "
            f"{'透明' if transparent else ''}。配置 ComfyUI/供应商后可在素材库重画，文件名不变。"), meta


async def generate_image(args: dict, t: ToolContext) -> str:
    text, _meta = await _generate_image_with_meta(args, t)
    return text


async def edit_image(args: dict, t: ToolContext) -> str:
    return "edit_image 在占位模式下不可用，请使用 generate_image 重画。"


def _placeholder_mp4(abs_path: Path, width: int, height: int,
                     fps: int = 16, seconds: float = 2.0) -> bool:
    """出片失败时用 ffmpeg 合成一段占位视频（深灰底 + ASCII 提示），保证交付路径存在。
    需要系统 ffmpeg（配音转 mp3 已在用）；失败返回 False，由上层如实报错。"""
    try:
        dur = max(1.0, float(seconds))
        vf = ("drawtext=text='VIDEO PLACEHOLDER':fontcolor=white:fontsize=h/12:"
              "x=(w-text_w)/2:y=(h-text_h)/2")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        cp = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
            f"color=c=0x2b2f3a:s={width}x{height}:r={fps}:d={dur}",
            "-vf", vf, "-pix_fmt", "yuv420p", "-c:v", "libx264", str(abs_path)],
            capture_output=True, timeout=120)
        return cp.returncode == 0 and abs_path.is_file() and abs_path.stat().st_size > 0
    except Exception:  # noqa: BLE001 - 占位失败不致命，上层会如实说明
        return False


@media_queue.serialized("video")
async def _generate_video_with_meta(args: dict, t: ToolContext) -> tuple[str, dict]:
    """generate_video 实现体：走本地 ComfyUI（Wan2.2 TI2V 5B）出真实 mp4。
    返回 (给模型看的文本, 结构化 meta{placeholder,provider,rel_path,error})。
    参考图提供时走 i2v（首帧），否则 t2v。失败回退占位视频（ffmpeg）。"""
    meta: dict = {"placeholder": False, "provider": "", "rel_path": "", "error": ""}
    slug = _safe_slug(args.get("slug"), "clip")
    category = args.get("category") or "cutscene"
    prompt = str(args.get("prompt") or "").strip()
    # 尺寸：显式 size（如 832x480）优先，否则用默认（832x480）。
    size = str(args.get("size") or f"{COMFYUI_VIDEO_WIDTH}x{COMFYUI_VIDEO_HEIGHT}")
    w, h = _parse_size(size)
    # 时长：给 duration_s 按帧率换算帧数；否则用默认帧数。帧数会再被 4n+1 约束收敛。
    try:
        fps = int(args.get("fps") or COMFYUI_VIDEO_FPS)
    except (TypeError, ValueError):
        fps = COMFYUI_VIDEO_FPS
    fps = max(8, min(fps, 30))
    try:
        length = int(args.get("frames") or args.get("length") or 0)
    except (TypeError, ValueError):
        length = 0
    if not length:
        try:
            dur = float(args.get("duration_s") or 0)
        except (TypeError, ValueError):
            dur = 0
        length = int(round(dur * fps)) if dur > 0 else COMFYUI_VIDEO_LENGTH
    rel = f"assets/video/{category}/{slug}_v1.mp4"
    meta["rel_path"] = rel
    abs_path = t.workspace / rel

    # 参考图（首帧）：路径可相对工作区或绝对
    ref_bytes = None
    ref_desc = ""
    ref_path = str(args.get("reference") or "").strip()
    if ref_path:
        rp = Path(ref_path)
        if not rp.is_absolute():
            rp = (t.workspace / rp).resolve()
        if rp.is_file():
            try:
                ref_bytes = rp.read_bytes()
                ref_desc = f"，首帧参考图：{ref_path}"
            except Exception:  # noqa: BLE001
                ref_bytes = None
        else:
            ref_desc = f"，参考图 {ref_path} 不存在已忽略（走文生视频）"

    # 真正下发的出片宽高：32 倍数；有参考图时贴合其长宽比（防拉伸/裁切变形）。
    w, h = fit_video_size(w, h, ref_bytes)

    log.info("generate_video 开始：slug=%s %dx%d 帧数=%d fps=%d 参考图=%s prompt=%r",
             slug, w, h, length, fps, bool(ref_bytes), prompt[:80])

    # 视频只走本地 ComfyUI（Wan2.2 TI2V 5B，无远程视频档位）。未配 ComfyUI 直接报/占位。
    last_err = ""
    if COMFYUI_BASE_URL and prompt:
        comfy_base = COMFYUI_BASE_URL
        gpu_arbiter.mark_used("image", t.run_ctx.session_id)  # 视频与出图共用 ComfyUI，同样记 image
        try:
            boot = await ensure_comfyui_running(comfy_base)
            if boot.get("error"):
                last_err = f"ComfyUI 自动启动失败：{boot['error']}"
                log.warning("generate_video ComfyUI 启动失败（slug=%s）：%s", slug, boot["error"])
            else:
                _denoise = COMFYUI_VIDEO_TEMPORAL_DENOISE
                _target = COMFYUI_VIDEO_SMOOTH_FPS if COMFYUI_VIDEO_SMOOTH else 0
                try:
                    _segs = int(args.get("segments") or 0)
                except (TypeError, ValueError):
                    _segs = 0
                if _segs > 0:
                    # 接龙长视频：每段 1s，逐段以上一段尾帧为首帧续写，拼成连贯长片。
                    from game_agent.assets.comfyui import generate_video_chain_bytes
                    data, _cm = await generate_video_chain_bytes(
                        prompt, segments=_segs,
                        segment_seconds=float(args.get("segment_seconds") or 1.0),
                        reference=ref_bytes, target_fps=_target, denoise=_denoise,
                        negative_prompt=COMFYUI_VIDEO_NEGATIVE_PROMPT,
                        width=w, height=h, fps=fps, steps=COMFYUI_VIDEO_STEPS,
                        cfg=COMFYUI_VIDEO_CFG, base_url=comfy_base,
                        timeout_s=COMFYUI_VIDEO_TIMEOUT_S)
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_bytes(data)
                    await _artifact(t, asset_kind="video", slug=slug, rel_path=rel,
                                    provider="comfyui", is_placeholder=False,
                                    params_json=args, size=len(data))
                    _emit(t, "artifact", {"asset_kind": "video", "slug": slug,
                                          "rel_path": rel, "placeholder": False})
                    meta.update(provider="comfyui")
                    nf = _cm.get("native_frames") or 0
                    dur = nf / max(1, fps)
                    pp_desc = []
                    if _cm.get("fps_raised"):
                        pp_desc.append(f"已补帧到 {COMFYUI_VIDEO_SMOOTH_FPS}fps")
                    if _cm.get("denoised"):
                        pp_desc.append("已做时间域降噪")
                    pp_note = ("，" + "、".join(pp_desc)) if pp_desc else ""
                    return (f"已通过 ComfyUI（Wan2.2 TI2V 5B）接龙生成真实长视频：{rel} "
                            f"{w}x{h} 共{_cm.get('segments')}段×{_cm.get('seg_seconds'):.1f}s 约{dur:.1f}s"
                            f"{ref_desc}{pp_note}，prompt：{prompt[:150]}"), meta
                data = await comfy_generate_video(
                    prompt,
                    negative_prompt=COMFYUI_VIDEO_NEGATIVE_PROMPT,
                    width=w, height=h, length=length, fps=fps,
                    steps=COMFYUI_VIDEO_STEPS, cfg=COMFYUI_VIDEO_CFG,
                    base_url=comfy_base,
                    timeout_s=COMFYUI_VIDEO_TIMEOUT_S, reference=ref_bytes)
                # 后处理：Wan 原生 24fps → 补到 60fps；补帧前轻做时间域降噪（防漫剧割裂/闪烁）。
                data, _pp = await asyncio.to_thread(
                    smooth_video_bytes, data, _target, fps, _denoise)
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_bytes(data)
                await _artifact(t, asset_kind="video", slug=slug, rel_path=rel,
                                provider="comfyui", is_placeholder=False,
                                params_json=args, size=len(data))
                _emit(t, "artifact", {"asset_kind": "video", "slug": slug,
                                      "rel_path": rel, "placeholder": False})
                meta.update(provider="comfyui")
                dur = (length - 1) / max(1, fps)
                pp_desc = []
                if _pp.get("fps_raised"):
                    pp_desc.append(f"已补帧到 {COMFYUI_VIDEO_SMOOTH_FPS}fps")
                if _pp.get("denoised"):
                    pp_desc.append("已做时间域降噪")
                pp_note = ("，" + "、".join(pp_desc)) if pp_desc else ""
                return (f"已通过 ComfyUI（Wan2.2 TI2V 5B）生成真实视频：{rel} "
                        f"{w}x{h} 原{fps}fps 约{dur:.1f}s{ref_desc}{pp_note}，"
                        f"prompt：{prompt[:150]}"), meta
        except ComfyUIError as e:
            last_err = str(e)
            log.warning("generate_video ComfyUI 失败（slug=%s）：%s", slug, last_err)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            log.exception("generate_video 异常（slug=%s）", slug)
    elif not prompt:
        last_err = "缺 prompt（视频描述）"
    else:
        last_err = "未配置 ComfyUI 地址（.env COMFYUI_BASE_URL），无法生成视频"

    # 回退占位视频
    if not PLACEHOLDER_ENABLED:
        raise ToolError(f"视频生成失败且占位已关闭：{last_err}")
    if _placeholder_mp4(abs_path, w, h, fps=fps, seconds=max(1.0, (length - 1) / max(1, fps))):
        await _artifact(t, asset_kind="video", slug=slug, rel_path=rel, provider="placeholder",
                        is_placeholder=True, params_json=args,
                        size=abs_path.stat().st_size)
        await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
        _emit(t, "artifact", {"asset_kind": "video", "slug": slug, "rel_path": rel, "placeholder": True})
        meta.update(placeholder=True, provider="placeholder", error=last_err)
        return (f"视频生成失败（{last_err}），已回退占位视频：{rel}。"
                f"修复 ComfyUI 后可重新生成。"), meta
    meta["error"] = last_err
    raise ToolError(f"视频生成失败，且无法生成占位视频（ffmpeg 不可用）：{last_err}")


async def generate_video(args: dict, t: ToolContext) -> str:
    text, _meta = await _generate_video_with_meta(args, t)
    return text


async def save_character_sheet(args: dict, t: ToolContext) -> str:
    slug = _safe_slug(args.get("slug"), "character")
    rel = f".trace/characters/{slug}.json"
    path = t.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"已保存角色设定：{rel}"


async def _openai_tts_generate(text: str, prof: dict, voice: str = "") -> tuple[bytes, str]:
    """OpenAI 兼容语音合成（POST /audio/speech）。返回 (bytes, fmt)。"""
    base = (prof.get("base_url") or "").rstrip("/")
    if not base:
        raise ToolError("音频档位未配置 Base URL")
    fmt = (prof.get("params") or {}).get("format") or "wav"
    body = {"model": prof.get("model") or "", "input": text,
            "voice": voice or (prof.get("params") or {}).get("voice") or "alloy",
            "response_format": fmt}
    headers = _auth_headers(prof)
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        r = await client.post(f"{base}/audio/speech", json=body, headers=headers)
        if r.status_code != 200:
            raise ToolError(f"语音接口 HTTP {r.status_code}: {r.text[:200]}")
        return r.content, fmt


async def _indextts_generate(text: str, prof: dict, args: dict,
                             workspace: Path | None = None,
                             on_done: "Callable[[str], None] | None" = None) -> tuple[bytes, str]:
    """本机 IndexTTS2 零样本克隆。音色来自档位的「预设名」或「参考音频路径」。

    跟 openai_tts 的区别：那个只能选固定内置音色（alloy 之类），这个能克隆
    任意自定音色 —— 这是本项目配音环节真正需要的（角色一人一嗓）。

    on_done：拿到服务端 done（含成品 wav 落盘路径）就回调一次，供上层在随后
    SSE 中断/读超时时把已生成的成品捡回来（「生成完了但没取到」的兜底）。
    """
    from game_agent.assets import tts as local_tts

    params = prof.get("params") or {}
    base = prof.get("base_url") or params.get("base_url") or ""
    # 音色优先级：条目自带的 preset / voice > 档位默认。voice 必须透传成预设名，
    # 否则不管传什么都是档位里写死的那个音色（项目里实测记过这条坑）
    preset = str(args.get("preset") or args.get("voice")
                 or params.get("preset") or "").strip()
    ref_path = str(args.get("reference") or params.get("reference_audio") or "").strip()
    ref_bytes = None
    if ref_path:
        # 相对路径按项目工作区解析（角色参考音频通常就放在工程里）
        p = Path(ref_path)
        if not p.is_absolute() and workspace is not None:
            p = workspace / ref_path
        if p.is_file():
            ref_bytes = p.read_bytes()
    if not ref_bytes and not preset:
        raise ToolError("音频档位未配置音色：provider=indextts2 需要 preset 或 reference_audio")
    # 情绪：条目入参 > 档位 params > .env 默认（synthesize 里兜底）。
    # 三种素材都可选：情感参考音频 emo_audio / 八维向量 emo_vector / 文本 emo_text。
    emo_mode = str(args.get("emo_mode") or params.get("emo_mode") or "") or None
    emo_text = str(args.get("emo_text") or params.get("emo_text") or "")
    emo_vector = args.get("emo_vector") or params.get("emo_vector") or None
    emo_audio_path = str(args.get("emo_audio") or params.get("emo_audio")
                         or params.get("emo_audio_path") or "").strip()
    emo_audio_bytes = None
    if emo_audio_path:
        ep = Path(emo_audio_path)
        if not ep.is_absolute() and workspace is not None:
            ep = workspace / emo_audio_path
        try:
            if ep.is_file():
                emo_audio_bytes = ep.read_bytes()
            else:
                log.warning("情感参考音频不存在：%s（忽略，按无情感音频处理）", ep)
        except OSError as e:  # noqa: BLE001 - 读不到不致命，退回流式模式
            log.warning("读取情感参考音频失败：%s: %s", ep, e)
    # 与 tts.synthesize 一致：给了哪种情绪素材就按哪种走（mode/素材解耦）。
    # 条目给 emo_text 即使档位默认是 0 也要生效，否则模型按台词给的情绪会被吞掉。
    if emo_audio_bytes:
        emo_mode = "1"
    elif emo_vector:
        emo_mode = "2"
    elif emo_text.strip():
        emo_mode = "3"
    elif emo_mode == "3":
        emo_mode = "0"
    weight = params.get("emo_weight") or 0.65
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        weight = 0.65
    # 派发链路此前从不自动拉起语音服务：服务没起就直接合成 → 连接拒绝 → 回退静音占位，
    # 用户只会看到"配音是空的"。这里跟生图一样先 ensure（内部有启动状态校验，不会重复拉起）。
    try:
        boot = await local_tts.ensure_tts_running(base)
        if boot.get("error"):
            log.warning("indextts 服务未就绪：%s", boot["error"])
            raise local_tts.TTSError(boot["error"])
        if boot.get("started"):
            log.info("本地语音服务已由本次调用拉起（等待 %dms）", boot.get("waited_ms", 0))
    except local_tts.TTSError:
        raise
    except Exception as e:  # noqa: BLE001 - ensure 自身异常不拦合成，让 synthesize 去报真实错误
        log.warning("indextts 启动检查失败：%s: %s", type(e).__name__, e)
    log.info("indextts 合成开始：preset=%r ref=%s 字数=%d", preset, bool(ref_bytes), len(text))
    try:
        data, _info = await local_tts.synthesize(
            text, reference=ref_bytes, preset=preset, base_url=base,
            emo_mode=emo_mode,
            emo_text=emo_text,
            emo_weight=weight,
            emo_vector=emo_vector,
            emo_audio=emo_audio_bytes,
            on_done=(lambda d: on_done(str(d.get("wav") or ""))) if on_done else None,
        )
    except local_tts.TTSError as e:
        log.warning("indextts 合成失败：%s", e)
        raise ToolError(str(e))
    log.info("indextts 合成完成：%d bytes", len(data))
    return data, "wav"


@media_queue.serialized("voice")
async def _tts_with_meta(args: dict, t: ToolContext) -> tuple[str, dict]:
    """tts 的实现体，附带结构化结果（placeholder/provider/rel_path/error），
    供批量派发统计「真实 / 占位 / 失败」并推进度事件。"""
    meta: dict = {"placeholder": False, "provider": "", "rel_path": "", "error": ""}
    slug = _safe_slug(args.get("slug"), "voice_line")
    text = args.get("text") or ""
    # 配音项目通常要给「web 站点实际读取的路径」出音频（如 web/audio/cv/<id>/<slot>.mp3），
    # 默认落 assets/audio/voice/ 只是暂存。带 out 时按目标路径写，并按扩展名转码。
    out_rel = _safe_out_path(t.workspace, str(args.get("out") or ""))
    prof = await _media_profile(t, "audio")
    provider = (prof.get("provider") or "").lower()
    meta["provider"] = provider
    fallback_note = ""
    label = provider
    wav_holder: dict = {"abs": ""}  # 服务端成品路径（超时/断开时用于把已落盘的成品捡回来）

    if text.strip() and provider in ("openai_tts", "indextts2", "indextts", "local_tts"):
        try:
            if provider == "openai_tts":
                data, fmt = await _openai_tts_generate(text, prof, args.get("voice") or "")
                label = prof.get("model") or "tts"
            else:
                gpu_arbiter.mark_used("voice", t.run_ctx.session_id)
                data, fmt = await _indextts_generate(
                    text, prof, args, t.workspace,
                    on_done=lambda d: wav_holder.__setitem__("abs", d))
                # 回报里写实际用的音色（条目 voice 优先），不能永远显示档位默认
                _params = prof.get("params") or {}
                _eff = str(args.get("preset") or args.get("voice")
                           or _params.get("preset") or "").strip()
                label = f"IndexTTS2{'·' + _eff if _eff else ''}"
            # 空声音防线：TTS 服务对异常参考音频/空结果可能返回全零 wav。
            # 不检查就会把「没声音」当成功交付（用户实测就是这样听到静音的）。
            if fmt.lower() == "wav":
                probe = audio_synth.analyze_wav(data)
                if probe.get("silent"):
                    raise ToolError(
                        f"合成结果是无声音的静音波形（{probe.get('duration_s')}s，峰值低于阈值），"
                        "已拒绝写入；请检查音色预设的参考音频是否有效，或换一个音色重试")
                meta["duration_s"] = probe.get("duration_s")
                meta["peak"] = probe.get("peak")
            rel = _out_file_rel(out_rel, slug, fmt)
            if rel:
                if rel.lower().endswith(".mp3") and fmt.lower() != "mp3":
                    data = _wav_to_mp3(data) if fmt.lower() == "wav" else data
                rel_ext = Path(rel).suffix.lower().lstrip(".")
                if rel_ext and rel_ext != fmt.lower() and not (rel_ext == "mp3" and fmt.lower() == "wav"):
                    rel = f"{rel}.{fmt}"
            else:
                rel = f"assets/audio/voice/{slug}.{fmt}"
            abs_path = t.workspace / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(data)
            meta["rel_path"] = rel
            await _artifact(t, asset_kind="voice", slug=slug, rel_path=rel,
                            provider=provider, is_placeholder=False, params_json=args,
                            size=len(data))
            _emit(t, "artifact", {"asset_kind": "voice", "slug": slug, "rel_path": rel,
                                  "placeholder": False})
            return f"已通过音频档位「{label}」生成真实配音：{rel}", meta
        except Exception as e:  # noqa: BLE001
            # 「生成完了但没取到」兜底：服务端其实已经把成品 wav 落盘了（SSE 中断/
            # 读超时/下载失败时很常见）。只要成品路径已知就直接拿它，别丢掉已经
            # 成功生成的真实配音、也别白白回退成静音占位。
            recovered = _recover_local_wav(wav_holder.get("abs", ""))
            if recovered is not None:
                probe = audio_synth.analyze_wav(recovered)
                if not probe.get("silent"):
                    rel = _out_file_rel(out_rel, slug, "wav")
                    if not rel:
                        rel = f"assets/audio/voice/{slug}.wav"
                    try:
                        if rel.lower().endswith(".mp3"):
                            recovered = _wav_to_mp3(recovered)
                            if not rel.lower().endswith(".mp3"):
                                rel = f"{rel}.mp3"
                        abs_path = t.workspace / rel
                        abs_path.parent.mkdir(parents=True, exist_ok=True)
                        abs_path.write_bytes(recovered)
                        meta.update({"rel_path": rel, "duration_s": probe.get("duration_s"),
                                     "peak": probe.get("peak")})
                        await _artifact(t, asset_kind="voice", slug=slug, rel_path=rel,
                                        provider=provider, is_placeholder=False,
                                        params_json=args, size=len(recovered))
                        _emit(t, "artifact", {"asset_kind": "voice", "slug": slug,
                                              "rel_path": rel, "placeholder": False})
                        log.warning("配音「%s」客户端报错（%s: %s），但已从服务端落盘成品取回并交付：%s",
                                    slug, type(e).__name__, e, rel)
                        return (f"已通过音频档位「{label}」生成真实配音"
                                f"（客户端超时/断开，但成品已落盘并取回）：{rel}", meta)
                    except Exception as e2:  # noqa: BLE001
                        log.warning("取回服务端成品失败（slug=%s）：%s: %s", slug, type(e2).__name__, e2)
            if not PLACEHOLDER_ENABLED:
                raise ToolError(f"音频档位生成失败且占位模式已关闭：{e}")
            log.warning("tts 生成失败（slug=%s）：%s: %s", slug, type(e).__name__, e)
            meta["error"] = f"{type(e).__name__}: {e}"
            fallback_note = f"音频档位生成失败，已回退占位：{e}\n"
    if not PLACEHOLDER_ENABLED:
        raise ToolError("未配置语音供应商且占位模式已关闭")
    rel = "assets/audio/voice/{}.wav".format(slug)
    abs_path = t.workspace / rel
    generate_silent_wav(str(abs_path), text)
    await _artifact(t, asset_kind="voice", slug=slug, rel_path=rel, provider="placeholder",
                   is_placeholder=True, params_json=args)
    await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
    _emit(t, "artifact", {"asset_kind": "voice", "slug": slug, "rel_path": rel, "placeholder": True})
    meta.update({"placeholder": True, "rel_path": rel,
                 "error": meta["error"] or "未配置可用的音频档位"})
    return f"{fallback_note}已生成占位音频（静音 wav，同长度）：{rel}", meta


async def tts(args: dict, t: ToolContext) -> str:
    text, _meta = await _tts_with_meta(args, t)
    return text


async def list_voice_presets(args: dict, t: ToolContext) -> str:
    """列出语音服务里已存在的音色预设名（给 generate_audio 的 voice 参数用）。

    为什么做成工具：模型想查「有哪些音色」此前只能 dir 语音服务安装目录
    （还差点被配音路由误杀）——查预设是出配音前的正当侦察，就该是正路工具。
    """
    prof = await _media_profile(t, "audio")
    provider = ((prof or {}).get("provider") or "").lower()
    if provider == "openai_tts":
        return ("在线 TTS 用固定内置音色，voice 直接传：alloy / echo / fable / onyx / "
                "nova / shimmer 之一。")
    if not provider:
        return ("未配置音频档位（团队设置 → 模型档位 kind=audio）：generate_audio 会走"
                "占位模式 ⚠️——先告知用户配置配音模型，不要闷头生成。")
    from game_agent.assets import tts as local_tts
    base = (prof or {}).get("base_url") or ""
    try:
        boot = await local_tts.ensure_tts_running(base)
        if boot.get("error"):
            return (f"语音服务未就绪：{boot['error']}。generate_audio 调用时仍会自动"
                    "重试拉起，不用你管服务。")
        presets = await local_tts.list_presets(base)
    except Exception as e:  # noqa: BLE001 - 查预设失败不致命，给模型指路即可
        return f"获取音色预设失败：{type(e).__name__}: {e}"
    if not presets:
        return ("服务端当前没有已保存的音色预设。两种做法：① generate_audio 不传 "
                "voice，用服务默认音色；② 传 design_desc=音色描述 + preset_name=新"
                "预设名，先造音色再合成（成功后自动存为预设，下次可直接按名复用）。")
    return ("可用音色预设（generate_audio 的 voice 直接传名字）：\n- "
            + "\n- ".join(presets))


@media_queue.serialized("voice")
async def generate_sfx(args: dict, t: ToolContext) -> str:
    """生成音效：默认合成真实可听音频（不再无条件写静音占位）。

    旧实现无论 prompt 写什么都是 generate_silent_wav() 全零静音——用户听到的
    就是「音效没声音」。现在默认用 game_agent.assets.synth.synthesize_sfx
    按关键词/哈希合成真实波形；只有显式 placeholder=true（或合成失败且允许占位）才回退静音。
    """
    slug = _safe_slug(args.get("slug"), "sfx")
    out_rel = _safe_out_path(t.workspace, str(args.get("out") or ""))
    rel = out_rel or f"assets/audio/sfx/{slug}.wav"
    abs_path = t.workspace / rel
    prompt = str(args.get("prompt") or args.get("text") or args.get("note") or slug)
    if args.get("placeholder"):
        if not PLACEHOLDER_ENABLED:
            raise ToolError("占位模式已关闭")
        generate_silent_wav(str(abs_path), "")
        await _artifact(t, asset_kind="sfx", slug=slug, rel_path=rel, provider="placeholder",
                        is_placeholder=True, params_json=args)
        await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
        return f"已生成占位音效（静音）：{rel}"
    try:
        data, info = audio_synth.synthesize_sfx(prompt, slug)
    except Exception as e:  # noqa: BLE001
        if not PLACEHOLDER_ENABLED:
            raise ToolError(f"音效合成失败：{e}")
        log.warning("generate_sfx 合成失败（slug=%s）：%s: %s", slug, type(e).__name__, e)
        generate_silent_wav(str(abs_path), "")
        await _artifact(t, asset_kind="sfx", slug=slug, rel_path=rel, provider="placeholder",
                        is_placeholder=True, params_json=args)
        await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
        return f"音效合成失败，已回退占位静音：{rel}（{e}）"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(data)
    await _artifact(t, asset_kind="sfx", slug=slug, rel_path=rel, provider="synth",
                    is_placeholder=False, params_json=args, size=len(data))
    _emit(t, "artifact", {"asset_kind": "sfx", "slug": slug, "rel_path": rel, "placeholder": False})
    return f"已合成真实音效（{info['profile']}，{info['duration_s']}s）：{rel}"


@media_queue.serialized("voice")
async def generate_music(args: dict, t: ToolContext) -> str:
    """生成 BGM：用内置合成器产出真实可听的循环片段（不再是 30s 静音）。"""
    slug = _safe_slug(args.get("slug"), "bgm")
    out_rel = _safe_out_path(t.workspace, str(args.get("out") or ""))
    rel = out_rel or f"assets/audio/bgm/{slug}.wav"
    abs_path = t.workspace / rel
    prompt = str(args.get("prompt") or args.get("text") or args.get("note") or slug)
    try:
        duration = float(args.get("duration_s") or 30)
    except (TypeError, ValueError):
        duration = 30.0
    if args.get("placeholder"):
        if not PLACEHOLDER_ENABLED:
            raise ToolError("占位模式已关闭")
        generate_silent_wav(str(abs_path), "", duration_s=duration)
        await _artifact(t, asset_kind="music", slug=slug, rel_path=rel, provider="placeholder",
                        is_placeholder=True, params_json=args)
        await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
        return f"已生成占位 BGM（静音）：{rel}"
    try:
        data, info = audio_synth.synthesize_music(prompt, duration, slug)
    except Exception as e:  # noqa: BLE001
        if not PLACEHOLDER_ENABLED:
            raise ToolError(f"BGM 合成失败：{e}")
        log.warning("generate_music 合成失败（slug=%s）：%s: %s", slug, type(e).__name__, e)
        generate_silent_wav(str(abs_path), "", duration_s=duration)
        await _artifact(t, asset_kind="music", slug=slug, rel_path=rel, provider="placeholder",
                        is_placeholder=True, params_json=args)
        await t.run_ctx.repo.update_run(t.run_ctx.run_id, has_placeholder=True)
        return f"BGM 合成失败，已回退占位静音：{rel}（{e}）"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(data)
    await _artifact(t, asset_kind="music", slug=slug, rel_path=rel, provider="synth",
                    is_placeholder=False, params_json=args, size=len(data))
    _emit(t, "artifact", {"asset_kind": "music", "slug": slug, "rel_path": rel, "placeholder": False})
    return (f"已合成真实 BGM（{info['bpm']} BPM，{info['duration_s']}s，"
            f"{'小调' if info['minor'] else '大调'}）：{rel}")


def _safe_out_path(workspace: Path, out: str) -> str:
    """把 tts 的 out 参数收成工作区内的相对路径（拒绝绝对路径与 ../ 逃逸）。

    目录式 out（以 `/` 结尾，如 `web/audio/cv/me/`）会保留结尾的 `/` 原样返回，
    由调用方按 slug 拼出具体文件名——**不能**直接把它当文件名写，否则整批条目会
    落到同一个无扩展名文件上互相覆盖（实测 2026-10-31：24 条台词全写进
    `web/audio/cv/me`，最后只剩一条，用户只觉得「生成完却取不到」）。
    """
    raw = str(out or "").strip().replace("\\", "/")
    if not raw:
        return ""
    is_dir = raw.endswith("/")
    p = Path(raw)
    if p.is_absolute():
        raise ToolError("out 必须是工作区内的相对路径（如 web/audio/cv/ur-yx-01/enter.mp3）")
    resolved = (workspace / p).resolve()
    ws = workspace.resolve()
    if not resolved.is_relative_to(ws):
        raise ToolError("out 路径越界（不允许 ../ 或工作区外路径）")
    rel = str(resolved.relative_to(ws)).replace("\\", "/")
    if rel in (".git", ".venv") or rel.startswith(".git/") or rel.startswith(".venv/"):
        raise ToolError("out 不能指向受保护目录")
    return rel + "/" if is_dir else rel


def _out_file_rel(out_rel: str, slug: str, default_ext: str) -> str:
    """把 out 收成「具体文件」相对路径：目录式 out 按 slug 拼到目录下，否则原样。"""
    if not out_rel:
        return ""
    if out_rel.endswith("/"):
        return f"{out_rel}{slug}.{default_ext}"
    return out_rel


def _recover_local_wav(server_wav_path: str) -> bytes | None:
    """客户端报错（读超时/断开/下载失败）时，从服务端已落盘的成品 wav 捡回字节。

    `server_wav_path` 是服务端 done 事件里给的相对/绝对路径（如
    `D:\\AI\\IndexTTS2.0_v3\\outputs\\xxx.wav` 或 `/api/v1/audio/xxx.wav`）。
    只读本地文件，不发网络请求；文件不存在/过小则返回 None。
    """
    p = str(server_wav_path or "").strip()
    if not p:
        return None
    cand = Path(p)
    if not cand.is_absolute():
        # 相对路径可能是 /api/v1/audio/<name>，取最后一段拼 TTS_HOME/outputs
        try:
            from game_agent.config import TTS_HOME
            cand = Path(TTS_HOME) / "outputs" / Path(p).name
        except Exception:  # noqa: BLE001
            return None
    try:
        if cand.is_file() and cand.stat().st_size > 1024:
            return cand.read_bytes()
    except OSError:
        return None
    return None


def _wav_to_mp3(data: bytes, *, sample_rate: int = 24000, bitrate: str = "64k") -> bytes:
    """wav → 24k 单声道 64k mp3（项目前端按 24k 起播，别用 44.1k/48k）。"""
    import tempfile
    src = dst = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
            src = f.name
        dst = src[:-4] + ".mp3"
        cp = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-ar", str(sample_rate), "-ac", "1", "-b:a", bitrate, dst],
            capture_output=True, timeout=120)
        if cp.returncode != 0 or not Path(dst).is_file():
            raise ToolError(f"ffmpeg 转 mp3 失败：{cp.stderr.decode('utf-8', 'replace')[:300]}")
        return Path(dst).read_bytes()
    except FileNotFoundError:
        raise ToolError("找不到 ffmpeg（转 mp3 需要它；或把 out 写成 .wav）")
    finally:
        for p in (src, dst):
            try:
                if p:
                    Path(p).unlink(missing_ok=True)
            except OSError:
                pass


async def upsert_voice_profile(args: dict, t: ToolContext) -> str:
    return f"已记录音色档案：{args.get('slug') or 'voice'}"


async def design_voice(args: dict, t: ToolContext) -> str:
    """配音角色用：按文字描述造一个新音色，并保存成 IndexTTS2 预设。

    为什么放在配音角色手里：用户说「不要之前的预设、要一个妩媚的声线」时，
    造音色本来就是配音的活。以前平台没有这个工具，组长只能自己起 8860 服务、
    跑 python 管线（2026-10-28 实测就是这么绕出去的）。
    """
    from game_agent.assets import tts as local_tts
    desc = str(args.get("desc") or "").strip()
    name = str(args.get("name") or "").strip()
    say = str(args.get("say") or "你好，我是这个声音。").strip()
    if not desc:
        raise ToolError("缺少 desc（音色文字描述）")
    if not name:
        raise ToolError("缺少 name（要保存的预设名）")
    try:
        n = max(1, min(5, int(args.get("n") or 1)))
    except (TypeError, ValueError):
        n = 1
    prof = await _media_profile(t, "audio")
    base = (prof.get("base_url") or "") if prof else ""
    gpu_arbiter.mark_used("voice", t.run_ctx.session_id)
    boot = await local_tts.ensure_tts_running(base)
    if boot.get("error"):
        raise ToolError(f"语音服务不可用：{boot['error']}")
    j = await local_tts.design_voice(desc, say, n, base_url=base)
    files = [f for f in (j.get("files") or []) if f.get("name")]
    if not files:
        raise ToolError("音色设计没有产出候选音频")
    pick = files[0]
    await local_tts.design_save_preset(name, str(pick["name"]), base_url=base, overwrite=True)
    return (f"已按描述「{desc[:60]}」生成新音色并保存为预设「{name}」"
            f"（试音候选 {pick['name']}，{pick.get('seconds') or '?'}s）。"
            f"下一步用 tts(text=..., voice=\"{name}\") 合成台词。")
