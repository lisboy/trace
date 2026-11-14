"""界面直连语音工作台：不走 agent，用户自己出配音（对齐 imagegen.py 的形态）。

- GET  /status            语音服务状态（可达 / 模型已加载 / 音色预设列表 / 音色设计可用）
- POST /upload            上传参考音频（自己的音色素材）
- POST /                  合成：文本 + 音色（预设或上传的参考音频）+ 情绪
- GET  /files/{name}      取合成结果
- POST /design            音色设计：一句文字描述 → 造 1~5 条候选参考音频
- GET  /design/files/{n}  取候选音频（从 IndexTTS2 代理过来，浏览器只认本服务）
- POST /presets           把候选存成 IndexTTS2 预设，之后按名字复用

为什么文件要代理而不是把 127.0.0.1:7860 直接给浏览器：
语音服务只监听本机，而且它没有本项目的登录态；代理一层既解决可达性，
也把"谁能听"统一到本项目的 RBAC 上。
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from game_agent.api.deps import require
from game_agent.api.routers import progress
from game_agent.assets import gpu_arbiter, media_queue
from game_agent.assets import tts as tts_client
from game_agent.assets.tts import TTSError, ensure_tts_running
from game_agent.auth.rbac import PERM_DOWNLOAD, PERM_RUN_CREATE, PERM_VIEW
from game_agent.config import (
    DATA_DIR, TTS_AUTOSTART, TTS_BASE_URL, TTS_EMO_AUDIO, TTS_EMO_MODE, TTS_EMO_TEXT,
    TTS_EMO_VECTOR, TTS_EMO_WEIGHT, TTS_LANG, TTS_PRESET, TTS_REF_AUDIO, TTS_SPEED,
    TTS_TIMEOUT_S, TTS_VOICE_DESIGN)
from game_agent.core.context import RequestContext
from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm
from game_agent.llm.team_profile import team_main_profile

router = APIRouter(prefix="/api/voice-gen", tags=["voicegen"])

_VOICE_DIR = DATA_DIR / "objects" / "voicegen"
_UPLOAD_DIR = _VOICE_DIR / "uploads"
_RESULT_DIR = _VOICE_DIR / "generated"
_DESIGN_DIR = _VOICE_DIR / "design"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024      # 参考音频上限：10 秒干声远用不到 20MB
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".ogg")


def _safe(name: str) -> str:
    return _SAFE.sub("_", name)


def _resolve_text(text: str) -> str:
    """清洗待合成文本。

    为什么不是直接透传：界面会把 agent 的 Markdown 回复丢进来朗读，
    星号井号表格符号念出来是噪音；同时给个长度上限，防手滑粘一整篇。
    """
    t = text.strip()
    t = re.sub(r"```.*?```", " ", t, flags=re.S)          # 代码块不念
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)      # 链接只留文字
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)   # 标题井号
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)        # 列表符号
    t = re.sub(r"[*_~>|]+", "", t)                        # 强调/表格线
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:2000]


async def _presets_safe() -> tuple[list[str], str | None]:
    """列音色预设；服务没起来时返回空列表 + 原因，不抛异常（状态栏要能渲染）。"""
    try:
        return await tts_client.list_presets(), None
    except TTSError as e:
        return [], str(e)


def _audio_sniff_ok(data: bytes) -> bool:
    """常见音频容器魔数嗅探（与 IndexTTS2 server_helpers.looks_like_audio 同口径）。

    只拦「明显不是音频」的文件（改名的文本/图片），完整解码仍由服务端 librosa 做。
    """
    if not data or len(data) < 12:
        return False
    if data[:4] == b"RIFF":
        return data[8:12] == b"WAVE"
    if data[:4] in (b"OggS", b"fLaC"):
        return True
    if data.startswith(b"ID3"):
        return True
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    if data[4:8] == b"ftyp":
        return True
    return False


def _safe_upload_name(name: str) -> str:
    """上传落盘文件名：保留原后缀（服务端按后缀/魔数识别），主体做安全化。"""
    base = Path((name or "ref.wav").replace("\\", "/")).name
    stem = _safe(Path(base).stem)[:60] or "ref"
    suf = (Path(base).suffix.lower() or ".wav")
    if suf not in AUDIO_SUFFIXES:
        suf = ".wav"
    return f"{stem}{suf}"


_ENHANCE_PROMPT = """你是游戏配音的口播稿编辑。把下面这段文本改写成更适合 TTS 朗读的版本：
1. 只做「口语化 + 断句 + 停顿」处理，保留原意与信息，不要增删情节；
2. 把书面语、长难句拆成短句，用逗号/句号控制呼吸节奏；
3. 去掉 Markdown、括号注释、表情符号、URL 等念出来是噪音的内容；
4. 数字/缩写的读法如有歧义，用汉字写法标注（如 3D→三维、AI→人工智能）；
5. 只输出改写后的正文，不要解释、不要标题、不要引号包裹。
原文："""


@router.get("/status")
async def status(ctx: RequestContext = Depends(require(PERM_VIEW))):
    info = await tts_client.health()
    # 自愈：配置端口探不通时，找一下「外部已拉起、端口可能不同」的实例（用户手动起 / 旧实例），
    # 找到就把实际端口记下来再探一次——否则工作台永远显示「未连接」（对齐 ComfyUI 工作台）。
    if not info["reachable"]:
        ext = await tts_client._discover_external(probe=True)
        if ext:
            info = await tts_client.health()
    presets, perr = ([], None)
    if info["reachable"]:
        presets, perr = await _presets_safe()
    default = TTS_PRESET or (presets[0] if presets else "")
    return {
        **info,
        "presets": presets,
        "presets_error": perr,
        "default_preset": default,
        "has_ref_audio": bool(TTS_REF_AUDIO),
        "voice_design": bool(TTS_VOICE_DESIGN),
        "emo_mode": TTS_EMO_MODE,
        "emo_text": TTS_EMO_TEXT,
        "emo_weight": TTS_EMO_WEIGHT,
        "emo_vector": TTS_EMO_VECTOR,
        "has_emo_audio": bool(TTS_EMO_AUDIO),
        "default_speed": TTS_SPEED,
        "default_lang": TTS_LANG,
        "timeout_s": TTS_TIMEOUT_S,
        "autostart": TTS_AUTOSTART,
    }


@router.post("/start")
async def start_service(ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """手动启动本地语音服务（未运行才拉起，轮询等待就绪，最长 240s）。

    先把 GPU 从 ComfyUI 手里收回来再启动（与 /image-gen/comfyui/start 对称）：
    两个本地服务同时驻留就是显存 OOM，界面上点「启动语音」不能只管自己起没起来。
    """
    if not TTS_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置语音服务地址（.env TTS_BASE_URL）")
    info = await gpu_arbiter.prepare("voice")
    gpu_arbiter.release()
    boot = info.get("start") if isinstance(info.get("start"), dict) else {}
    # 界面上「启动失败」读的是顶层 error，别把它埋进 gpu 里；带上实际端口（可能已自动改绑）
    return {**boot, "base_url": tts_client.actual_base(),
            "gpu": info, "gpu_note": gpu_arbiter.note(info)}


@router.post("/stop")
async def stop_service(ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """手动停止本地语音服务：杀掉监听端口的进程及其子进程。"""
    if not TTS_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置语音服务地址（.env TTS_BASE_URL）")
    try:
        return await tts_client.stop_tts_service(tts_client.actual_base())
    except TTSError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"停止语音服务失败：{type(e).__name__}: {e}")


@router.post("/upload")
async def upload_reference(file: UploadFile = File(...),
                           ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="参考音频不能超过 20MB")
    name = (file.filename or "ref.wav").lower()
    if not name.endswith(AUDIO_SUFFIXES):
        raise HTTPException(status_code=400, detail="仅支持 wav / mp3 / m4a / flac / ogg")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:8]}_{_safe(Path(name).name)[:60]}"
    (_UPLOAD_DIR / fname).write_bytes(data)
    return {"file": fname, "size": len(data)}


@router.get("/files/{name}")
async def result_file(name: str, ctx: RequestContext = Depends(require(PERM_DOWNLOAD))):
    p = _RESULT_DIR / _safe(name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(p, media_type="audio/wav", filename=p.name)


class GenIn(BaseModel):
    text: str
    preset: str = ""          # IndexTTS2 预设名
    reference: str = ""       # /upload 返回的文件名（优先级高于 preset）
    # 情绪四模式：""=按可用素材自动/用 .env 默认；"0" 同参考；"1" 情感音频；
    # "2" 八维向量；"3" 文本描述（兼容旧的"2/3"语义）
    emo_mode: str = ""
    emo_text: str = ""
    emo_weight: float | None = None
    emo_vector: list[float] | None = None   # 8 维：高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静
    emo_audio: str = ""       # /upload 返回的情感参考音频文件名（emo_mode=1 时用）
    emo_auto: bool = False    # 未给任何情绪素材时，用台词本身做情感文本（Qwen 自动判情绪）
    speed: float | None = None  # 语速 0.5~2.0（>1 慢 <1 快；IndexTTS-2.5 生效）
    lang: str = "auto"        # 语言 auto/zh/en/ja/es/ar（2.5 生效）
    label: str = ""           # 文件名标签（例如角色名）
    enhance: bool = False     # 走 LLM 把文本润色成更适合朗读的口播稿
    job_id: str = ""          # 前端生成的进度任务号，供 GET /jobs/{id} 轮询处理过程


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """前端轮询配音进度。任务已被回收/未知时返回 stage='unknown'。"""
    return progress.snapshot(job_id)


@router.post("")
async def generate(body: GenIn, ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """工作台配音与聊天工具、出图共用全局媒体队列（跨会话串行）。"""
    job_id = (body.job_id or "").strip()
    if job_id and progress.get(job_id) is None:
        # 配音通常几秒到几十秒，先按 45s 估，进入合成后按文本长度再修正
        progress.new_job(job_id, 1, est_total_s=45.0)
        progress.set_stage(job_id, "queued", "已排队，等待空闲算力…")
    async with media_queue.media_slot("voice", "voice-gen"):
        try:
            return await _generate_impl(body, ctx)
        except Exception as e:  # noqa: BLE001 - 记录失败阶段后原样抛给上层
            progress.set_stage(job_id, "failed", "合成失败", detail=str(e))
            raise


async def _generate_impl(body: GenIn, ctx: RequestContext):
    job_id = (body.job_id or "").strip()
    if job_id and progress.get(job_id) is None:
        progress.new_job(job_id, 1, est_total_s=45.0)
    if not TTS_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置语音服务地址（.env TTS_BASE_URL）")
    text = _resolve_text(body.text)
    if not text:
        raise HTTPException(status_code=400, detail="文本为空")
    progress.set_stage(job_id, "enhancing", "正在润色口播稿…" if body.enhance else "准备文本…")

    # 0) LLM 润色（失败回退原文，绝不因此让合成失败）
    enhance_note = ""
    if body.enhance:
        try:
            profile = await team_main_profile(ctx.team_id)
            ctx_llm = Ctx(role="main", run_type="voicegen-polish", requirement=text,
                          model=(profile.get("model") or ""), model_profile=profile or {})
            llm = build_llm("main", ctx_llm)
            te = time.monotonic()
            resp = await llm.chat(
                [{"role": "user", "content": _ENHANCE_PROMPT + text}],
                tools=[], ctx=ctx_llm, on_delta=None, summarizer=None)
            out = _resolve_text(getattr(resp, "text", "") or "")
            if out:
                text = out[:2000]
                enhance_note = f"LLM 润色（{int((time.monotonic() - te) * 1000)}ms）"
            else:
                enhance_note = "LLM 返回为空，已用原文"
        except Exception as e:  # noqa: BLE001
            enhance_note = f"LLM 润色失败已回退原文：{e}"

    # 1) 语音服务未运行则自动拉起（后面取预设/合成都需要它）
    progress.set_stage(job_id, "starting", "正在准备语音服务…")
    boot = await ensure_tts_running(TTS_BASE_URL)
    if boot["error"]:
        raise HTTPException(status_code=502, detail=f"语音服务不可用：{boot['error']}")
    if boot["started"]:
        enhance_note = "；".join(x for x in (
            f"已自动启动语音服务（{boot['waited_ms'] / 1000:.1f}s）", enhance_note) if x)

    ref_bytes = None
    if body.reference:
        rp = _UPLOAD_DIR / _safe(body.reference)
        if not rp.is_file():
            raise HTTPException(status_code=400, detail="参考音频不存在或已过期，请重新上传")
        ref_bytes = rp.read_bytes()
    preset = "" if ref_bytes else (body.preset or TTS_PRESET)
    if not ref_bytes and not preset and TTS_REF_AUDIO:
        rp = Path(TTS_REF_AUDIO)
        if rp.is_file():
            ref_bytes = rp.read_bytes()
    if not ref_bytes and not preset:
        # 兜底：用服务端第一个预设。界面上"随手点一下朗读"不该因为没选音色就报错。
        presets, _ = await _presets_safe()
        preset = presets[0] if presets else ""
    if not ref_bytes and not preset:
        raise HTTPException(status_code=400,
                            detail="没有可用音色：请选一个预设、上传参考音频，或在 .env 配 TTS_PRESET")

    # 情感参考音频（mode=1）：来自本服务一次 /upload，读成字节随合成请求上传
    emo_bytes = None
    if body.emo_audio:
        ep = _UPLOAD_DIR / _safe(body.emo_audio)
        if not ep.is_file():
            raise HTTPException(status_code=400, detail="情感参考音频不存在或已过期，请重新上传")
        emo_bytes = ep.read_bytes()

    t0 = time.monotonic()
    # 合成耗时主要随文本长度增长；按 8 字/秒粗估一个总时长修正 percent（仅估算）
    progress.set_est_total(job_id, 15.0 + len(text) / 8.0)
    progress.set_stage(job_id, "sampling", "语音服务正在合成…")
    try:
        data, info = await tts_client.synthesize(
            text, reference=ref_bytes, preset=preset,
            emo_mode=(body.emo_mode or None), emo_text=(body.emo_text or None),
            emo_weight=body.emo_weight,
            emo_vector=body.emo_vector, emo_audio=emo_bytes,
            emo_auto=body.emo_auto,
            speed=body.speed, lang=(body.lang or "auto"))
    except TTSError as e:
        raise HTTPException(status_code=502, detail=str(e))

    progress.set_stage(job_id, "saving", "正在保存音频…")
    _RESULT_DIR.mkdir(parents=True, exist_ok=True)
    tag = _safe((body.label or "voice")[:20]) or "voice"
    fname = f"vc_{time.strftime('%Y%m%d_%H%M%S')}_{tag}_{uuid.uuid4().hex[:6]}.wav"
    (_RESULT_DIR / fname).write_bytes(data)
    progress.set_stage(job_id, "done", "音频已生成，正在载入…", percent=100)
    return {
        "file": fname,
        "url": f"/api/voice-gen/files/{quote(fname)}",
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "server_elapsed_s": info.get("elapsed"),
        "size": len(data),
        "preset": preset,
        "chars": len(text),
        "used_text": text,
        "enhance_note": enhance_note,
        "emo_mode": info.get("emo_mode"),   # 实际生效的情绪模式（前端如实显示）
    }


_DESIGN_PROMPT = """你是游戏配音导演。把下面的角色/音色描述改写成给 AI 音色克隆模型的「音色设计提示词」：
1. 只保留并展开**声音特征**：年龄段、性别、音色明暗/粗细、嗓音质感（沙哑/清亮/慵懒等）、
   语气习惯、语速、口音、情绪色彩；
2. 外貌、服装、身份剧情等设定只用来推断声音，不要写进结果；
3. 60~120 字，中文，一段话，直接描述「这是一个什么样的声音」；
4. 只输出改写后的提示词，不要解释、不要标题、不要引号包裹。
角色/音色描述："""


class DesignIn(BaseModel):
    desc: str
    say: str = "你好，我是这个声音，很高兴认识你。"
    n: int = 1
    enhance: bool = False  # LLM 把角色/音色描述润色成音色设计提示词（只提炼声音特征）


@router.post("/design")
async def design(body: DesignIn, ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    if not TTS_VOICE_DESIGN:
        raise HTTPException(status_code=400, detail="音色设计已关闭（.env GA_TTS_VOICE_DESIGN=false）")
    if not body.desc.strip():
        raise HTTPException(status_code=400, detail="音色描述不能空")
    # 0) LLM 润色设计提示词（失败回退原文，绝不因此让设计失败）
    desc = body.desc.strip()
    used_desc, enhance_note = desc, ""
    if body.enhance:
        try:
            profile = await team_main_profile(ctx.team_id)
            ctx_llm = Ctx(role="main", run_type="voicegen-design", requirement=desc,
                          model=(profile.get("model") or ""), model_profile=profile or {})
            llm = build_llm("main", ctx_llm)
            te = time.monotonic()
            resp = await llm.chat(
                [{"role": "user", "content": _DESIGN_PROMPT + desc}],
                tools=[], ctx=ctx_llm, on_delta=None, summarizer=None)
            out = (getattr(resp, "text", "") or "").strip()
            if out:
                used_desc = out[:500]
                enhance_note = f"已用润色后的设计提示词（{int((time.monotonic() - te) * 1000)}ms）"
            else:
                enhance_note = "LLM 返回为空，已用原文"
        except Exception as e:  # noqa: BLE001
            enhance_note = f"LLM 润色失败已回退原文：{e}"
    boot = await ensure_tts_running(TTS_BASE_URL)
    if boot["error"]:
        raise HTTPException(status_code=502, detail=f"语音服务不可用：{boot['error']}")
    try:
        j = await tts_client.design_voice(used_desc, body.say, body.n)
    except TTSError as e:
        raise HTTPException(status_code=502, detail=str(e))
    _DESIGN_DIR.mkdir(parents=True, exist_ok=True)
    # 候选音频落一份到本服务：IndexTTS2 侧只在生成目录里，页面刷新/换人也要能播
    files = []
    for f in j.get("files") or []:
        name = str(f.get("name") or "")
        if not name:
            continue
        try:
            data = await tts_client.design_audio(name)
        except TTSError:
            continue
        (_DESIGN_DIR / _safe(name)).write_bytes(data)
        files.append({
            "name": name,
            "url": f"/api/voice-gen/design/files/{quote(name)}",
            "seconds": f.get("seconds"),
            "elapsed": f.get("elapsed"),
        })
    if not files:
        raise HTTPException(status_code=502, detail="音色设计没有产出可用音频")
    return {"desc": body.desc, "used_desc": used_desc, "enhance_note": enhance_note,
            "say": body.say, "elapsed": j.get("elapsed"), "files": files}


@router.get("/design/files/{name}")
async def design_file(name: str, ctx: RequestContext = Depends(require(PERM_DOWNLOAD))):
    p = _DESIGN_DIR / _safe(name)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(p, media_type="audio/wav", filename=p.name)


class PresetIn(BaseModel):
    name: str
    file: str
    overwrite: bool = False


@router.post("/presets")
async def save_as_preset(body: PresetIn, ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="预设名不能为空")
    try:
        j = await tts_client.design_save_preset(body.name.strip(), body.file, overwrite=body.overwrite)
    except TTSError as e:
        # 撞名单独给 409，界面才好弹"覆盖吗"
        if "已存在" in str(e):
            return JSONResponse(status_code=409, content={"detail": str(e)})
        raise HTTPException(status_code=502, detail=str(e))
    return j


def _norm_preset_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", str(s or "").strip())
    s = re.sub(r"\s+", "_", s).strip("._")
    return s


def _pick_saved_name(req_name: str, presets: list[str]) -> str:
    """IndexTTS2 落盘时会把预设名做 safe_preset_name 清洗（空格→下划线等），
    回传「实际存下来的名字」，前端才能选中它。"""
    if req_name in presets:
        return req_name
    want = _norm_preset_name(req_name)
    for p in presets:
        if _norm_preset_name(p) == want:
            return p
    return req_name


@router.get("/presets")
async def list_voice_presets(ctx: RequestContext = Depends(require(PERM_VIEW))):
    presets, perr = await _presets_safe()
    return {"presets": presets, "error": perr}


@router.post("/presets/upload")
async def upload_as_preset(
    file: UploadFile = File(...),
    name: str = Form(...),
    emo_weight: float | None = Form(None),
    emo_text: str = Form(""),
    emo_vector: str = Form(""),     # "0.9,0,0,..." 8 维情感权重（存进预设，复用时带着）
    emo_audio: UploadFile | None = File(None),   # 情感参考音频（另一段带目标情绪的干声）
    overwrite: bool = Form(False),
    ctx: RequestContext = Depends(require(PERM_RUN_CREATE)),
):
    """上传一段现成音频 → 直接存成 IndexTTS2 音色预设（「上传即预设」）。

    与 /presets（存音色设计候选）的区别：那条路只能存 VoxCPM2 造出来的候选；
    这里直接把用户手里的干声（自己录的/别处拿的/之前生成的 wav）落成预设，
    之后工作台和对话里的 generate_audio(voice=预设名) 都能按名字复用同一个声音。
    """
    if not TTS_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置语音服务地址（.env TTS_BASE_URL）")
    pres = (name or "").strip()
    if not pres:
        raise HTTPException(status_code=400, detail="预设名不能为空")
    if len(pres) > 64:
        raise HTTPException(status_code=400, detail="预设名过长（上限 64 字）")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="音频文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="音频不能超过 20MB（参考音频 3~10 秒即可）")
    if not _audio_sniff_ok(data):
        raise HTTPException(status_code=400,
                            detail="不是可识别的音频文件（支持 wav / mp3 / m4a / flac / ogg）")
    # 先落一份到本服务：上传不白费（即便语音服务暂时起不来，用户也能稍后重试/试听）。
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:8]}_{_safe_upload_name(file.filename or 'ref.wav')}"
    (_UPLOAD_DIR / fname).write_bytes(data)

    emo_bytes = None
    emo_name = "emo.wav"
    if emo_audio is not None:
        emo_bytes = await emo_audio.read()
        if not emo_bytes:
            raise HTTPException(status_code=400, detail="情感参考音频为空")
        if len(emo_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="情感参考音频不能超过 20MB")
        if not _audio_sniff_ok(emo_bytes):
            raise HTTPException(status_code=400, detail="情感参考音频不是可识别的音频文件")
        emo_name = _safe_upload_name(emo_audio.filename or "emo.wav")

    boot = await ensure_tts_running(TTS_BASE_URL)
    if boot.get("error"):
        raise HTTPException(status_code=502, detail=f"语音服务不可用：{boot['error']}")
    try:
        j = await tts_client.create_preset(
            pres, data, filename=fname, overwrite=overwrite,
            emo_weight=emo_weight, emo_text=(emo_text or ""),
            emo_vector=(emo_vector or None), emo_audio=emo_bytes, emo_audio_name=emo_name)
    except TTSError as e:
        if "已存在" in str(e):
            return JSONResponse(status_code=409, content={"detail": str(e)})
        raise HTTPException(status_code=502, detail=str(e))
    presets = list(j.get("presets") or [])
    return {
        "ok": True,
        "name": _pick_saved_name(pres, presets),
        "presets": presets,
        "file": fname,
        "size": len(data),
        "started": bool(boot.get("started")),
    }


@router.get("/presets/{name}/audio")
async def preset_audio(name: str, ctx: RequestContext = Depends(require(PERM_DOWNLOAD))):
    """取某个预设的参考音频（试听：确认上传的那段存对了）。"""
    if not TTS_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置语音服务地址（.env TTS_BASE_URL）")
    try:
        data = await tts_client.preset_audio(name)
    except TTSError as e:
        raise HTTPException(status_code=404 if "不存在" in str(e) else 502, detail=str(e))
    return Response(content=data, media_type="audio/wav")
