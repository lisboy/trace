"""媒体直连工具：generate_image（美术模型）/ generate_audio（配音模型）。

v3 直连架构：没有美术/配音子角色，也不再有派发。会话模型需要出图/出音频时
直接调用这里的工具，工具内部负责：拉起/切换本地服务（GPU 互斥）、串行生成、
进度事件、失败回退占位——拿到文件路径就接着干活。

批量写法（items / count>1）：进程内**串行**逐个生成——本地 ComfyUI 单卡单队列、
IndexTTS2 同理，并发只会堆排队叠显存峰值；串行还能让界面实时看到「第几条/占位与否」。
"""
from __future__ import annotations

import json

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.assets import media_queue
from game_agent.agents.tools.specialized import (
    _generate_image_with_meta, _generate_video_with_meta, _tts_with_meta,
    generate_music, generate_sfx,
)
from game_agent.config import COMFYUI_BASE_URL
from game_agent.logging_setup import get_logger

log = get_logger("agents.media")


async def report_to_user(args: dict, t: ToolContext) -> str:
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    report = args.get("report", "")
    # 子agent 的「汇报」其实是交给主agent 的调研结论：单独存到 task 维度。
    # 以前和主agent 共用 set_final_summary(run_id)，子agent 一汇报就把用户看到的
    # 最终总结顶成子任务的片段（run 54fbf901 实测：主agent 还没收尾，final_summary
    # 已经是某个子agent 的结论）。
    if getattr(t, "role", "") == "sub" and getattr(t, "task_id", ""):
        await t.orchestrator.set_sub_summary(str(t.task_id), report)
        return "已汇报给主agent。"
    await t.orchestrator.set_final_summary(t.run_ctx.run_id, report)
    return "已汇报给用户。"


# 一次调用最多出多少张（防止模型一口气报 200 张把 run 拖死）
MAX_ART_JOBS = 24
# 批量时透传给每张图的公共字段
_ART_BASE_KEYS = ("size", "category", "style", "reference", "reference_mode",
                  "reference_denoise", "transparent", "note", "negative_prompt", "views")


def _art_prompt(job: dict) -> str:
    parts = [str(job.get(k)).strip() for k in ("subject", "scene", "style", "note")
             if str(job.get(k) or "").strip()]
    return "，".join(parts)


def one_line(s: str, n: int = 120) -> str:
    line = " ".join(str(s or "").split())
    return line if len(line) <= n else line[:n] + "…"


def _art_jobs(args: dict) -> list[dict]:
    """把一次调用展开成「要出的图」清单（顺序即出图顺序，串行执行）。

    支持三种写法（可混用，按优先级）：
      1. items: [{slug, prompt 或 subject/scene/style, size, ...}, ...] —— 一次出多张；
      2. count: N —— 同一套参数出 N 张（slug 自动加 -1/-2 后缀）；
      3. 都不写 —— 就一张。
    """
    base = {k: args.get(k) for k in _ART_BASE_KEYS if args.get(k) is not None}
    jobs: list[dict] = []
    items = args.get("items")
    if isinstance(items, list):
        for it in items[:MAX_ART_JOBS]:
            if not isinstance(it, dict):
                continue
            job = dict(base)
            job.update({k: v for k, v in it.items() if v is not None})
            # 允许每项自带 prompt（直连写法）或 subject/scene/style 组合（旧派发写法）
            if _art_prompt(job) or str(job.get("prompt") or "").strip() or job.get("slug"):
                jobs.append(job)
    if not jobs:
        try:
            n = int(args.get("count") or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, MAX_ART_JOBS))
        slug0 = str(args.get("slug") or "asset")
        for i in range(n):
            job = dict(base)
            job["subject"] = args.get("subject")
            job["scene"] = args.get("scene")
            job["slug"] = f"{slug0}-{i + 1}" if n > 1 else slug0
            jobs.append(job)
    # slug 去重：同一批里重名的，后面自动加序号，避免互相覆盖同一个 _v1.png
    seen: dict[str, int] = {}
    for job in jobs:
        slug = str(job.get("slug") or "asset")
        if slug in seen:
            seen[slug] += 1
            job["slug"] = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 1
    # 多视角（八方画像）：把每个 job 的 views 展开成逐视角的子 job，
    # 提示词按视角改写、slug 加视角后缀，保证同一人物同画风、只变观察方向。
    from game_agent.assets import views as img_views
    expanded: list[dict] = []
    for job in jobs:
        vkeys = img_views.normalize_views(job.get("views"))
        if not vkeys:
            expanded.append(job)
            continue
        base_prompt = _art_prompt(job) or str(job.get("prompt") or "").strip()
        has_ref = bool(job.get("reference"))
        for vk in vkeys:
            sub = dict(job)
            sub.pop("views", None)
            sub.pop("count", None)
            sub["slug"] = f"{job.get('slug') or 'asset'}-{vk}"
            sub["prompt"] = img_views.augment_prompt(base_prompt, vk, has_reference=has_ref)
            sub["view"] = vk
            sub.pop("subject", None)  # 已并入 prompt
            sub.pop("scene", None)
            sub.pop("style", None)
            expanded.append(sub)
    jobs = expanded
    return jobs


async def _warm_image_backend(t: ToolContext) -> str:
    """整批出图前**只**把生图后端拉起一次（本地 ComfyUI），返回一行说明。

    为什么单独做：以前每张图各调一次 ensure，虽然幂等，但在冷启动那一次会等模型/服务
    就绪；把它提到循环外面，整批只承担一次启动开销，后面每张都是纯出图。
    """
    if t.orchestrator is None:
        return ""
    try:
        prof = await t.orchestrator.resolve_media_profile(str(t.run_ctx.team_id), "image")
    except Exception:  # noqa: BLE001
        prof = {}
    provider = (prof.get("provider") or "").lower()
    if provider == "openai_image":
        return ""  # 远程图像档位，不需要本地服务
    if provider and provider != "comfyui":
        return ""
    try:
        from game_agent.assets.comfyui import ensure_comfyui_running
        boot = await ensure_comfyui_running(prof.get("base_url") or "")
    except Exception as e:  # noqa: BLE001 - 起不来就让单张调用自己去报错
        log.warning("整批出图预热：生图服务检查失败 %s: %s", type(e).__name__, e)
        return f"（本地生图服务检查失败：{type(e).__name__}: {e}）"
    if boot.get("error"):
        log.warning("整批出图预热失败：%s", boot["error"])
        return f"（本地生图服务未就绪：{boot['error']}）"
    if boot.get("started"):
        note = f"（已启动本地生图服务，耗时 {boot.get('waited_ms', 0)}ms）"
        log.info("整批出图预热：%s", note)
        return note
    log.info("整批出图预热：生图服务已在运行，直接开始")
    return ""


@media_queue.serialized("image")
async def _image_batch(jobs: list[dict], t: ToolContext) -> str:
    """串行出图：服务只起一次，然后一张一张出，出完一张立刻出下一张。

    为什么串行而不是并行：本地 ComfyUI 是单卡单队列，一次只吃一张，并发只会把请求
    堆在服务端排队、把显存峰值叠起来（首图加载模型时最容易 OOM），表现就是超时/卡死。
    串行还有个好处：每张出完就落一个 artifact 事件，界面上能看见进度在动。

    进度上报：每张图前后各发一个 art_progress 事件（done/total/当前 slug/占位标记），
    前端 RunTraceInline 据此画进度条。
    """
    total = len(jobs)
    done: list[dict] = []
    failed: list[dict] = []
    real_count = 0
    placeholder_count = 0

    async def _progress(**kw) -> None:
        if t.orchestrator is None:
            return
        # 单条的「这条是否占位」不能占用 placeholder 这个键：payload 里的 placeholder
        # 是整批累计数，被 bool 覆盖后前端「占位 N」会在每条 item_done 时闪回 0
        if "placeholder" in kw:
            kw["item_placeholder"] = kw.pop("placeholder")
        try:
            await t.orchestrator._emit(t.run_ctx, "art_progress", {  # noqa: SLF001
                "media": "image",
                "total": total, "real": real_count, "placeholder": placeholder_count,
                "failed": len(failed), **kw})
        except Exception:  # noqa: BLE001 - 进度事件失败不影响出图
            log.exception("art_progress 事件发送失败")

    # 先发一条 start：冷启动预热（拉起 ComfyUI）可能要等 1-3 分钟，
    # 此前这整段时间一个事件都不发，用户看到的就是"生成开始后毫无动静"
    await _progress(done=0, current="", phase="start", note="正在检查/启动生图服务…")
    warm = await _warm_image_backend(t)
    # 冷启动可能耗掉几分钟（拉起 ComfyUI、加载模型），这段没有「出图」进展；
    # 服务就绪算一次进展，让看门狗从「第一张开始」重新计时——否则冷启动 + 首张慢
    # 相加可能越过硬超时，把本来正常的批量误杀。
    t.progress_seq += 1
    if warm and t.orchestrator is not None:
        try:
            await t.orchestrator._emit(t.run_ctx, "status", {  # noqa: SLF001
                "status": "running", "note": f"准备串行出图 {total} 张{warm}"})
        except Exception:  # noqa: BLE001
            pass
    await _progress(done=0, current="", phase="start", note=warm or "生图服务就绪，开始出图")
    warm_failed = bool(warm)  # 预热带回了警告/错误说明
    aborted_note = ""
    for i, job in enumerate(jobs, 1):
        slug = str(job.get("slug") or f"asset-{i}")
        prompt = str(job.get("prompt") or "").strip() or _art_prompt(job)
        await _progress(done=i - 1, current=slug, phase="generating")
        if not prompt:
            failed.append({"slug": slug, "error": "缺 prompt（或 subject/scene/style），无法拼提示词"})
            await _progress(done=i - 1, current=slug, phase="item_done", placeholder=False, error="缺提示词参数")
            continue
        try:
            text, meta = await _generate_image_with_meta({
                "prompt": prompt,
                "slug": slug,
                "category": job.get("category") or "illustration",
                "size": job.get("size") or "1024x1024",
                "transparent": bool(job.get("transparent", False)),
                "reference": job.get("reference") or "",
                "reference_mode": job.get("reference_mode") or "style",
                "reference_denoise": job.get("reference_denoise"),
                "negative_prompt": job.get("negative_prompt"),
                "view": job.get("view") or "",
            }, t)
            done.append({"slug": slug, "index": i, "result": text})
            if meta.get("placeholder"):
                placeholder_count += 1
            else:
                real_count += 1
            t.progress_seq += 1  # 真出了一张（含占位）：给硬超时看门狗「有进展」信号
            await _progress(done=i, current=slug, phase="item_done",
                            placeholder=bool(meta.get("placeholder")),
                            error=meta.get("error") or "")
        except Exception as e:  # noqa: BLE001 - 单张失败不拖垮整批
            failed.append({"slug": slug, "index": i,
                           "error": f"{type(e).__name__}: {e}"})
            t.progress_seq += 1  # 这一张有了确定结局（失败）也算推进，别让整批被总时长误杀
            await _progress(done=i, current=slug, phase="item_done",
                            placeholder=False, error=f"{type(e).__name__}: {e}")
        # 生图服务明显不可用（预热失败 + 连续 2 张都没出真实图）→ 早停整批，
        # 不要把剩余十几张全部烧成占位图再让用户等十分钟（实测 20 张 × 4s 占位 + 预热 3 分钟）
        if warm_failed and i >= 2 and real_count == 0 and i < total:
            aborted_note = (f"；生图服务不可用（{one_line(warm, 120)}），"
                            f"已中止剩余 {total - i} 张 —— 修复生图服务后重新生成即可")
            log.error("整批出图早停：%s", aborted_note)
            break
    head = (f"批量出图完成：真实 {real_count} 张、占位 {placeholder_count} 张、"
            f"失败 {len(failed)} 张（共 {total} 张）")
    if aborted_note:
        head += aborted_note
    elif placeholder_count and not real_count:
        head += " —— 全部回退了占位图，看上方失败原因（通常是生图服务未就绪）"
    log.info("批量出图结束：%s", head)
    await _progress(done=total, current="", phase="batch_done")
    return json.dumps({"request": {"count": total}, "serial": True, "summary": head,
                       "real": real_count, "placeholder": placeholder_count,
                       "succeeded": done, "failed": failed}, ensure_ascii=False)


@media_queue.serialized("image")
async def generate_image(args: dict, t: ToolContext) -> str:
    """直接生成图片（走设置里绑定的美术模型/本地 ComfyUI）。

    - 批量（items / count > 1）：串行逐张生成，一张一张落盘并推进度事件；
    - 单张：直接生成本地真实图片文件，返回文件相对路径。
    """
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    jobs = _art_jobs(args)

    if len(jobs) > 1:
        return await _image_batch(jobs, t)

    single = dict(args)
    single.pop("items", None)
    single.pop("count", None)
    if jobs:
        for k, v in jobs[0].items():
            if v is not None:
                single[k] = v
    prompt = str(single.get("prompt") or "").strip() or _art_prompt(single)
    if not prompt:
        raise ToolError("generate_image 缺 prompt（或 subject/scene/style 组合）")
    text, _meta = await _generate_image_with_meta({
        "prompt": prompt,
        "slug": single.get("slug") or "asset",
        "category": single.get("category") or "illustration",
        "size": single.get("size") or "1024x1024",
        "transparent": bool(single.get("transparent", False)),
        "reference": single.get("reference") or "",
        "reference_mode": single.get("reference_mode") or "style",
        "reference_denoise": single.get("reference_denoise"),
        "negative_prompt": single.get("negative_prompt"),
    }, t)
    return text


# ---------------- 视频直连工具 generate_video ----------------
# 出片比出图慢得多（单条 2–5 分钟），批量上限更小，避免一口气排一小时的队。
MAX_VIDEO_JOBS = 6
_VIDEO_BASE_KEYS = ("size", "category", "reference", "fps", "duration_s", "frames",
                    "segments", "segment_seconds", "note")


async def _warm_video_backend(t: ToolContext) -> str:
    """整批出片前把 ComfyUI 拉起一次（与出图预热同一套规则）。视频也用 ComfyUI。"""
    if not COMFYUI_BASE_URL:
        return "（未配置 ComfyUI 地址）"
    try:
        from game_agent.assets.comfyui import actual_base, ensure_comfyui_running
        boot = await ensure_comfyui_running(actual_base())
    except Exception as e:  # noqa: BLE001
        log.warning("整批出片预热：检查失败 %s: %s", type(e).__name__, e)
        return f"（本地出片服务检查失败：{type(e).__name__}: {e}）"
    if boot.get("error"):
        log.warning("整批出片预热失败：%s", boot["error"])
        return f"（本地出片服务未就绪：{boot['error']}）"
    if boot.get("started"):
        return f"（已启动本地出片服务，耗时 {boot.get('waited_ms', 0)}ms）"
    return ""


def _video_jobs(args: dict) -> list[dict]:
    """把一次调用展开成「要出的视频」清单（顺序即出片顺序，串行）。写法与 _art_jobs 对齐。"""
    base = {k: args.get(k) for k in _VIDEO_BASE_KEYS if args.get(k) is not None}
    jobs: list[dict] = []
    items = args.get("items")
    if isinstance(items, list):
        for it in items[:MAX_VIDEO_JOBS]:
            if not isinstance(it, dict):
                continue
            job = dict(base)
            job.update({k: v for k, v in it.items() if v is not None})
            if str(job.get("prompt") or "").strip() or job.get("slug"):
                jobs.append(job)
    if not jobs:
        try:
            n = int(args.get("count") or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, MAX_VIDEO_JOBS))
        slug0 = str(args.get("slug") or "clip")
        for i in range(n):
            job = dict(base)
            job["prompt"] = args.get("prompt")
            job["slug"] = f"{slug0}-{i + 1}" if n > 1 else slug0
            jobs.append(job)
    seen: dict[str, int] = {}
    for job in jobs:
        slug = str(job.get("slug") or "clip")
        if slug in seen:
            seen[slug] += 1
            job["slug"] = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 1
    return jobs


@media_queue.serialized("video")
async def _video_batch(jobs: list[dict], t: ToolContext) -> str:
    """串行出片：ComfyUI 只起一次，一条一条出。进度事件 media=video，前端画进度条。"""
    total = len(jobs)
    done: list[dict] = []
    failed: list[dict] = []
    real_count = 0
    placeholder_count = 0

    async def _progress(**kw) -> None:
        if t.orchestrator is None:
            return
        if "placeholder" in kw:
            kw["item_placeholder"] = kw.pop("placeholder")
        try:
            await t.orchestrator._emit(t.run_ctx, "art_progress", {  # noqa: SLF001
                "media": "video", "total": total, "real": real_count,
                "placeholder": placeholder_count, "failed": len(failed), **kw})
        except Exception:  # noqa: BLE001
            log.exception("art_progress(video) 事件发送失败")

    await _progress(done=0, current="", phase="start", note="正在检查/启动出片服务…")
    warm = await _warm_video_backend(t)
    t.progress_seq += 1
    await _progress(done=0, current="", phase="start", note=warm or "出片服务就绪，开始生成")
    warm_failed = bool(warm)
    aborted_note = ""
    for i, job in enumerate(jobs, 1):
        slug = str(job.get("slug") or f"clip-{i}")
        prompt = str(job.get("prompt") or "").strip()
        await _progress(done=i - 1, current=slug, phase="generating")
        if not prompt:
            failed.append({"slug": slug, "error": "缺 prompt（视频描述）"})
            await _progress(done=i - 1, current=slug, phase="item_done",
                            placeholder=False, error="缺 prompt 参数")
            continue
        try:
            text, meta = await _generate_video_with_meta({
                "prompt": prompt, "slug": slug,
                "category": job.get("category") or "cutscene",
                "size": job.get("size") or "",
                "reference": job.get("reference") or "",
                "fps": job.get("fps"), "duration_s": job.get("duration_s"),
                "frames": job.get("frames"),
                "segments": job.get("segments"), "segment_seconds": job.get("segment_seconds"),
            }, t)
            done.append({"slug": slug, "index": i, "result": text})
            if meta.get("placeholder"):
                placeholder_count += 1
            else:
                real_count += 1
            t.progress_seq += 1
            await _progress(done=i, current=slug, phase="item_done",
                            placeholder=bool(meta.get("placeholder")),
                            error=meta.get("error") or "")
        except Exception as e:  # noqa: BLE001 - 单条失败不拖垮整批
            failed.append({"slug": slug, "index": i, "error": f"{type(e).__name__}: {e}"})
            t.progress_seq += 1
            await _progress(done=i, current=slug, phase="item_done",
                            placeholder=False, error=f"{type(e).__name__}: {e}")
        if warm_failed and i >= 2 and real_count == 0 and i < total:
            aborted_note = (f"；出片服务不可用（{one_line(warm, 120)}），"
                            f"已中止剩余 {total - i} 条 —— 修复出片服务后重新生成即可")
            log.error("整批出片早停：%s", aborted_note)
            break
    head = (f"批量出片完成：真实 {real_count} 条、占位 {placeholder_count} 条、"
            f"失败 {len(failed)} 条（共 {total} 条）")
    if aborted_note:
        head += aborted_note
    elif placeholder_count and not real_count:
        head += " —— 全部回退了占位视频，看上方失败原因（通常是出片服务未就绪）"
    log.info("批量出片结束：%s", head)
    await _progress(done=total, current="", phase="batch_done")
    return json.dumps({"request": {"count": total}, "serial": True, "summary": head,
                       "real": real_count, "placeholder": placeholder_count,
                       "succeeded": done, "failed": failed}, ensure_ascii=False)


@media_queue.serialized("video")
async def generate_video(args: dict, t: ToolContext) -> str:
    """直接生成真实视频（用户要动态素材/过场动画/镜头片段时调用）。

    - 走本地 ComfyUI（Wan2.2 TI2V 5B）；传 reference 走图生视频（首帧），否则文生视频；
    - 批量（items / count > 1）：串行逐条生成，一条一条落盘并推进度事件；
    - 单条：直接生成本地真实 mp4 文件，返回文件相对路径。
    """
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    jobs = _video_jobs(args)
    if len(jobs) > 1:
        return await _video_batch(jobs, t)
    single = dict(args)
    single.pop("items", None)
    single.pop("count", None)
    if jobs:
        for k, v in jobs[0].items():
            if v is not None:
                single[k] = v
    prompt = str(single.get("prompt") or "").strip()
    if not prompt:
        raise ToolError("generate_video 缺 prompt（视频描述）")
    text, _meta = await _generate_video_with_meta({
        "prompt": prompt,
        "slug": single.get("slug") or "clip",
        "category": single.get("category") or "cutscene",
        "size": single.get("size") or "",
        "reference": single.get("reference") or "",
        "fps": single.get("fps"), "duration_s": single.get("duration_s"),
        "frames": single.get("frames"),
        "segments": single.get("segments"), "segment_seconds": single.get("segment_seconds"),
    }, t)
    return text


# 一次调用最多出多少条音频（与出图同一道闸，防模型一口气报 200 条把 run 拖死）
MAX_AUDIO_JOBS = 24
# 批量时透传给每条音频的公共字段
_AUDIO_BASE_KEYS = ("voice", "kind", "note", "slug", "out", "design_desc", "design_say",
                    "preset_name", "design_n", "prompt", "text", "duration_s",
                    "emo_mode", "emo_text", "emo_weight", "emo_vector", "emo_audio",
                    "speed", "lang")


def _audio_jobs(args: dict) -> list[dict]:
    """把一次配音调用展开成「要合成的音频」清单（顺序即合成顺序，串行执行）。

    写法与 _art_jobs 对齐：
      1. items: [{slug, text, voice, kind, note}, ...] —— 一次出多条；
      2. count: N —— 同一套参数出 N 条（slug 自动加 -1/-2 后缀）；
      3. 都不写 —— 就一条。
    """
    base = {k: args.get(k) for k in _AUDIO_BASE_KEYS if args.get(k) is not None}
    base.setdefault("kind", "voice")
    jobs: list[dict] = []
    items = args.get("items")
    if isinstance(items, list):
        for it in items[:MAX_AUDIO_JOBS]:
            if not isinstance(it, dict):
                continue
            job = dict(base)
            job.update({k: v for k, v in it.items() if v is not None})
            if str(job.get("text") or "").strip() or job.get("slug"):
                jobs.append(job)
    if not jobs:
        try:
            n = int(args.get("count") or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, MAX_AUDIO_JOBS))
        slug0 = str(args.get("slug") or "voice_line")
        for i in range(n):
            job = dict(base)
            job["text"] = args.get("text")
            job["slug"] = f"{slug0}-{i + 1}" if n > 1 else slug0
            jobs.append(job)
    # slug 去重：同一批重名会互相覆盖同一个 wav
    seen: dict[str, int] = {}
    for job in jobs:
        slug = str(job.get("slug") or "voice_line")
        if slug in seen:
            seen[slug] += 1
            job["slug"] = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 1
    # 所有 voice 类条目都没有台词 → 这批注定全失败，别让服务白跑一趟；
    # 但 sfx / music 不需要 text（以前这里用 all(...) 空迭代器恒真，导致
    # 「generate_audio(kind=sfx, prompt=...)」被误判成「缺 text」直接拒绝）。
    voice_jobs = [j for j in jobs if str(j.get("kind") or "voice") in ("", "voice", "配音")]
    if voice_jobs and all(str(j.get("text") or "").strip() == "" for j in voice_jobs):
        return []
    return jobs


async def _warm_audio_backend(t: ToolContext) -> str:
    """整批配音前把本地语音服务拉起一次（与生图预热同一套规则），返回一行说明。

    提到循环外面：冷启动要等模型加载（可能 1-3 分钟），整批只承担一次开销。
    """
    if t.orchestrator is None:
        return ""
    try:
        prof = await t.orchestrator.resolve_media_profile(str(t.run_ctx.team_id), "audio")
    except Exception:  # noqa: BLE001
        prof = {}
    provider = (prof.get("provider") or "").lower()
    if provider in ("", "openai_tts"):
        return ""  # 没档位（走占位）或远程档位：都不需要本地服务
    try:
        from game_agent.assets import tts as local_tts
        boot = await local_tts.ensure_tts_running(prof.get("base_url") or "")
    except Exception as e:  # noqa: BLE001 - 起不来就让单条调用自己去报错
        log.warning("整批配音预热：语音服务检查失败 %s: %s", type(e).__name__, e)
        return f"（本地语音服务检查失败：{type(e).__name__}: {e}）"
    if boot.get("error"):
        log.warning("整批配音预热失败：%s", boot["error"])
        return f"（本地语音服务未就绪：{boot['error']}）"
    if boot.get("started"):
        return f"（已启动本地语音服务，耗时 {boot.get('waited_ms', 0)}ms）"
    return ""


@media_queue.serialized("voice")
async def _audio_one(job: dict, t: ToolContext) -> tuple[str, dict]:
    """按 kind 路由一条音频的真实生成，带回 {placeholder, rel_path, error}。"""
    kind = str(job.get("kind") or "voice").strip().lower()
    if kind in ("sfx", "音效"):
        # 音效用内置合成器出真实可听波形（provider=synth）；只有显式占位或合成失败
        # 才回退静音，且 meta.placeholder 会如实上报，进度条不会把静音算成「真实」。
        text = await generate_sfx(job, t)
        placeholder = "占位" in text
        return text, {"placeholder": placeholder, "rel_path": "", "error": text if placeholder else ""}
    if kind in ("music", "bgm", "音乐"):
        text = await generate_music(job, t)
        placeholder = "占位" in text
        return text, {"placeholder": placeholder, "rel_path": "", "error": text if placeholder else ""}
    return await _tts_with_meta(job, t)


async def _design_preset_if_needed(jobs: list[dict], t: ToolContext) -> tuple[str, list[dict]]:
    """调用里带 design_desc 时：先用文字描述造音色 → 存成预设 → 这批都用它合成。

    用户说「不要之前的预设、重新做一个妩媚的声线」时，平台需要「用文字造新音色」
    的能力；把造音色收进 generate_audio 链，模型就没有绕行去自己起服务的理由了。
    返回 (说明, 成功造出的预设列表)。
    """
    from game_agent.config import TTS_VOICE_DESIGN
    targets = [j for j in jobs if str(j.get("design_desc") or "").strip()]
    if not targets:
        return "", []
    if not TTS_VOICE_DESIGN:
        raise ToolError("音色设计已关闭（.env GA_TTS_VOICE_DESIGN=false），只能使用已有预设")
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    prof = await t.orchestrator.resolve_media_profile(str(t.run_ctx.team_id), "audio")
    base = (prof.get("base_url") or "") if prof else ""
    from game_agent.assets import tts as local_tts
    boot = await local_tts.ensure_tts_running(base)
    if boot.get("error"):
        raise ToolError(f"语音服务不可用：{boot['error']}")
    made: list[dict] = []
    designed: dict[str, str] = {}  # 预设名 -> 已生成的候选文件名（同名只造一次）
    for job in targets:
        desc = str(job.get("design_desc") or "").strip()
        name = str(job.get("preset_name") or job.get("voice") or "").strip()
        if not name:
            raise ToolError("带 design_desc 时必须给 preset_name（新音色要存成预设名）")
        # 同一预设名整批只造一次音色：批量配音时 design_desc 常写在顶层，会被
        # _audio_jobs 广播到每一条台词；若不按名去重，N 条台词就要造 N 次音色
        # （每次几十秒~几分钟），整批看起来就是在「一直生成、卡住」，根本轮不到
        # 后面真正的台词合成（实测 2026-10-31：24 条台词连造 20+ 次音色）。
        if name in designed:
            job["voice"] = name          # 复用已造好的预设
            continue
        say = str(job.get("design_say") or job.get("text") or "你好，我是这个声音。").strip()
        try:
            n = max(1, min(5, int(job.get("design_n") or 1)))
        except (TypeError, ValueError):
            n = 1
        j = await local_tts.design_voice(desc, say, n, base_url=base)
        files = [f for f in (j.get("files") or []) if f.get("name")]
        if not files:
            raise ToolError(f"音色设计没有产出候选音频（预设 {name}）")
        pick = files[0]
        await local_tts.design_save_preset(name, str(pick["name"]), base_url=base, overwrite=True)
        designed[name] = str(pick["name"])
        job["voice"] = name
        made.append({"preset": name, "desc": desc, "file": pick["name"],
                     "seconds": pick.get("seconds")})
        log.info("音色设计完成并存入预设：%s（候选 %s）", name, pick["name"])
    # 整批里其它把该预设名当 voice/preset_name 的条目也统一对齐，确保本批都用新音色
    for j in jobs:
        nm = str(j.get("preset_name") or "").strip()
        if nm and nm in designed and not str(j.get("voice") or "").strip():
            j["voice"] = nm
    note = "；".join(f"新音色「{m['preset']}」已按描述「{m['desc'][:40]}」生成并保存" for m in made)
    return note, made


@media_queue.serialized("voice")
async def _audio_batch(jobs: list[dict], t: ToolContext) -> str:
    """串行合成音频：语音服务只起一次，一条一条出，出完一条立刻下一条。

    进度上报与批量出图共用 art_progress 事件（media=audio 区分），前端据此画
    「配音出音 N/M」进度条 + 真实/占位/失败计数。
    """
    total = len(jobs)
    done: list[dict] = []
    failed: list[dict] = []
    real_count = 0
    placeholder_count = 0
    placeholder_reasons: list[str] = []

    async def _progress(**kw) -> None:
        if t.orchestrator is None:
            return
        if "placeholder" in kw:  # 同批量出图：别用单条标记覆盖整批累计数
            kw["item_placeholder"] = kw.pop("placeholder")
        try:
            await t.orchestrator._emit(t.run_ctx, "art_progress", {  # noqa: SLF001
                "media": "audio",
                "total": total, "real": real_count, "placeholder": placeholder_count,
                "failed": len(failed), **kw})
        except Exception:  # noqa: BLE001 - 进度事件失败不影响合成
            log.exception("art_progress(audio) 事件发送失败")

    await _progress(done=0, current="", phase="start", note="正在检查/启动语音服务…")
    design_note = ""
    designs: list[dict] = []
    if any(str(j.get("design_desc") or "").strip() for j in jobs):
        await _progress(done=0, current="", phase="design", note="正在按文字描述生成新音色…")
        design_note, designs = await _design_preset_if_needed(jobs, t)
    warm = await _warm_audio_backend(t)
    # 语音服务冷启动也可能耗时；就绪算一次进展，让看门狗从「第一句开始」重新计时。
    t.progress_seq += 1
    await _progress(done=0, current="", phase="start", note=warm or "语音服务就绪，开始合成")
    warm_failed = bool(warm)
    aborted_note = ""
    for i, job in enumerate(jobs, 1):
        slug = str(job.get("slug") or f"line-{i}")
        kind = str(job.get("kind") or "voice").strip().lower()
        await _progress(done=i - 1, current=slug, phase="generating")
        if not str(job.get("text") or "").strip() and kind in ("", "voice"):
            failed.append({"slug": slug, "error": "缺 text（要念的台词）"})
            await _progress(done=i - 1, current=slug, phase="item_done",
                            placeholder=False, error="缺 text 参数")
            continue
        try:
            text, meta = await _audio_one(job, t)
            done.append({"slug": slug, "index": i, "result": text})
            if meta.get("placeholder"):
                placeholder_count += 1
                if meta.get("error"):
                    placeholder_reasons.append(str(meta["error"]))
            else:
                real_count += 1
            t.progress_seq += 1  # 真出了一条（含占位）：给硬超时看门狗「有进展」信号
            await _progress(done=i, current=slug, phase="item_done",
                            placeholder=bool(meta.get("placeholder")),
                            error=meta.get("error") or "")
        except Exception as e:  # noqa: BLE001 - 单条失败不拖垮整批
            failed.append({"slug": slug, "index": i,
                           "error": f"{type(e).__name__}: {e}"})
            t.progress_seq += 1  # 这一条有确定结局也算推进
            await _progress(done=i, current=slug, phase="item_done",
                            placeholder=False, error=f"{type(e).__name__}: {e}")
        # 语音服务明显不可用（预热失败 + 连续 2 条都没出真实音频）→ 早停整批，
        # 不要把剩余几十条全烧成静音占位再让用户等十分钟
        if warm_failed and i >= 2 and real_count == 0 and i < total:
            aborted_note = (f"；语音服务不可用（{one_line(warm, 120)}），"
                            f"已中止剩余 {total - i} 条 —— 修复语音服务后重新生成即可")
            log.error("整批配音早停：%s", aborted_note)
            break
    head = (f"批量配音完成：真实 {real_count} 条、占位 {placeholder_count} 条、"
            f"失败 {len(failed)} 条（共 {total} 条）")
    if design_note:
        head = f"{design_note}。{head}"
    if aborted_note:
        head += aborted_note
    elif placeholder_count and not real_count:
        reason = placeholder_reasons[0] if placeholder_reasons else "语音服务未就绪"
        head += f" —— 全部回退了占位音频，原因：{reason}"
    log.info("批量配音结束：%s", head)
    await _progress(done=total, current="", phase="batch_done")
    return json.dumps({"request": {"count": total}, "serial": True, "summary": head,
                       "real": real_count, "placeholder": placeholder_count,
                       "succeeded": done, "failed": failed,
                       "designed_presets": designs}, ensure_ascii=False)


@media_queue.serialized("voice")
async def generate_audio(args: dict, t: ToolContext) -> str:
    """直接生成音频（走设置里绑定的配音模型/本地 IndexTTS2）。

    - kind: voice（台词配音，默认）| sfx（音效）| music（BGM）；
    - 批量（items / count > 1）：串行逐条生成并推进度事件；
    - 带 design_desc 时先按文字描述造新音色并存预设，再合成（用户说「不要之前的预设 / 换声线」）。
    - out 参数可指定落盘路径（如 web/audio/cv/<id>/<slot>.mp3，自动转 24k 单声道 mp3）。
    """
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    jobs = _audio_jobs(args)
    if not jobs:
        raise ToolError("generate_audio 缺 text（要念的台词）；音效/BGM 请传 kind=sfx|music + prompt")

    if len(jobs) > 1:
        return await _audio_batch(jobs, t)

    job = jobs[0]
    single = dict(args)
    single.pop("items", None)
    single.pop("count", None)
    for k, v in job.items():
        if v is not None:
            single[k] = v
    await _design_preset_if_needed([single], t)
    text, _meta = await _audio_one(single, t)
    return text
