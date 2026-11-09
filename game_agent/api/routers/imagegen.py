"""界面直连生图工作台：不走 agent，用户在素材库页面手动出图。

- POST /reference      上传参考图（multipart），返回服务端文件名
- GET  /options        拉取 ComfyUI 可选模型 / LoRA 列表与 .env 默认值
- POST /loras          上传 LoRA 到 ComfyUI 的 models/loras（立即可在列表里选）
- GET/POST/PUT/DELETE /presets  出图参数预设（内置 + 团队自定义）
- POST /               生成：提示词（可选 LLM 增强）+ 参考图模式 + 工作流方式 + 模型 + LoRA
- GET  /files/{name}   生成结果文件
"""
from __future__ import annotations

import io
import json
import logging
import re
import struct
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import require
from game_agent.api.routers import progress
from game_agent.assets import gpu_arbiter, media_queue, presets as img_presets, views as img_views
from game_agent.assets.comfyui import (
    ComfyUIError, comfyui_queue_state, ensure_comfyui_running, fetch_model_options,
    generate_images_bytes, loras_dir, stop_comfyui_service)
from game_agent.assets.placeholder import _parse_size
from game_agent.assets.presets import BUILTIN_PRESETS
from game_agent.auth.rbac import PERM_AGENT_CONFIG, PERM_DOWNLOAD, PERM_VIEW
from game_agent.config import COMFYUI_BASE_URL, COMFYUI_NEGATIVE_PROMPT, COMFYUI_TIMEOUT_S, DATA_DIR
from game_agent.core.context import RequestContext
from game_agent.db.models import ImagePreset
from game_agent.db.session import SessionLocal
from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm
from game_agent.llm.team_profile import team_main_profile

router = APIRouter(prefix="/api/image-gen", tags=["imagegen"])
log = logging.getLogger("game_agent.imagegen")

_UPLOAD_DIR = DATA_DIR / "objects" / "imagegen" / "uploads"
_RESULT_DIR = DATA_DIR / "objects" / "imagegen" / "generated"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ENHANCE_PROMPT = """你是游戏美术的生图提示词专家。把用户输入改写成一段高质量生图提示词：
保留用户的所有关键意图（主体/场景/构图/风格/配色/用途），补全画面细节描述（光影、材质、
视角、背景、氛围），输出中文提示词一段话，不要解释、不要列表、不要前后缀。
用户输入："""

# 「帮填提示词」：用默认对话模型根据用户的一句话需求，生成正面 + 负面提示词。
# 全部用**中文**输出（用户要求提示词要中文）——Qwen-Image 等底模支持中文提示词，
# 中文也更方便用户直接看懂、微调。负面词按本次内容补充针对性排除项
# （如「机械」→锈迹/噪点；「Q版」→写实照片/三维渲染）。
_FILL_PROMPT = """你是资深游戏美术的生图提示词工程师。用户会用一句话描述想生成的画面，
你要据此写出可直接交给 Stable Diffusion / Qwen-Image 的提示词。只输出一个 JSON 对象，不要任何解释或代码块围栏。

JSON 格式：
{"positive": "正面提示词", "negative": "负面提示词", "note": "一句话说明思路"}

要求：
- 【全部用中文】撰写：positive 和 negative 都必须是中文，不要英文标签、不要英文单词堆砌。
- positive：用中文逗号分隔的短语，按「主体 → 数量/动作姿态 → 构图/镜头 → 风格 → 配色 →
  光影 → 材质 → 背景 → 画质」的顺序组织；主体在前，画质词在后（如：高清、杰作、极致细节）。
  例：「一位黑发冷艳女子，正面半身像，三分之四视角，国风厚涂，黑白配色，柔和侧光，
  丝绸质感，城市夜景虚化，高清，杰作」
- negative：用中文写出要排除的问题（中文逗号分隔），针对本次内容补充
  （例：人物→多余手指、畸形手、五官不对称；机械/硬表面→锈迹、噪点；Q版/卡通→写实照片、三维渲染；文字类→文字、水印）。
- 不要输出换行以外的东西；字符串内不要出现未转义的双引号。

用户需求：{user}
{base}"""


def _default_base_negative() -> str:
    """把服务默认负面词交给模型作参考——注意它本身是英文权重写法，要求模型理解后用中文表达同类项。"""
    return (f"\n服务默认负面词（下面是英文权重写法，请先理解其含义，再用**中文**写出同类排除项；"
            f"可精简、可补充与本次内容相关的项）：\n{COMFYUI_NEGATIVE_PROMPT}\n")


# ---- 出图后「AI 看图质检 + 自动修复」----
# 生成后把图连同提示词交给（视觉）主模型检查人体/结构异常；命中则补写负面词并重生成一次。
# 总开关由前端勾选控制（默认开），这里只负责「单张图」的检查与「按问题扩写负面词」。
_REVIEW_PROMPT = """你是游戏美术的出图质检员。这是用下面提示词生成的一张图，请检查是否存在明显的生成缺陷：
多余或缺失的手指（如六指）、多余或缺失的肢体（如三条腿、三只手臂）、手脚结构畸变、
五官错位/大小眼、身体比例严重失调、主体重复或肢体粘连等。
只输出一个 JSON 对象，不要解释、不要代码块围栏：
{"ok": true/false, "reason": "一句话说明判断依据（你在这张图上看到的关键部位，为什么通过或判定异常）", "issues": ["简短中文问题1", "问题2"], "fix": "针对这些问题要补进【负面提示词】的英文标签，逗号分隔"}
判定标准：只有明显、确实影响观感的缺陷才算不通过；风格化/Q版/卡通造型属正常，不要误判。
提示词：{prompt}
"""
_REVIEW_MAX_ATTEMPTS = 3  # 视觉模型偶发返回非 JSON / 空文本时重试，避免一次抖动就整批失败


def _coerce_review_result(raw: str) -> dict:
    """从质检模型回复里抠出 {ok, reason, issues, fix}。解析不出一律视为通过（不打扰正常出图）。"""
    txt = (raw or "").strip()
    if not txt:
        return {"ok": True, "reason": "", "issues": [], "fix": ""}
    fence = re.match(r"^```[a-zA-Z]*\s*([\s\S]*?)\s*```$", txt)
    if fence:
        txt = fence.group(1).strip()
    if not (txt.startswith("{") and txt.endswith("}")):
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            txt = txt[i : j + 1]
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            ok = obj.get("ok")
            if isinstance(ok, str):
                ok = ok.strip().lower() in ("true", "1", "yes", "是", "通过", "ok")
            issues = obj.get("issues") or []
            if not isinstance(issues, list):
                issues = [str(issues)]
            return {"ok": bool(ok) if ok is not None else True,
                    "reason": str(obj.get("reason") or "").strip()[:160],
                    "issues": [str(x).strip() for x in issues if str(x).strip()][:8],
                    "fix": str(obj.get("fix") or "").strip()}
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "reason": "", "issues": [], "fix": ""}  # 解析失败=放行


def _merge_negative(base: str, extra: str) -> str:
    """把修复标签并入负面词：按小写去重、顺序不变，避免同义项反复堆叠。"""
    base = (base or "").strip().strip(",")
    extra = (extra or "").strip().strip(",")
    if not extra:
        return base
    def _key(t: str) -> str:
        return re.sub(r"\s+", " ", t.strip().lower())
    seen = {_key(x) for x in base.split(",") if x.strip()}
    add = [x.strip() for x in extra.split(",") if x.strip() and _key(x) not in seen]
    if not add:
        return base
    return (base + ", " + ", ".join(add)) if base else ", ".join(add)


async def _review_one(llm, image_bytes: bytes, prompt_text: str) -> dict:
    """让视觉模型检查单张图。llm 为 None / 非视觉 / 出错 → 一律视为通过。"""
    if llm is None or not getattr(llm, "vision", False):
        return {"checked": False, "ok": True, "issues": [], "fix": ""}
    try:
        from game_agent.llm.context_window import encode_vision_image
        from PIL import Image as _PILImage
        im = _PILImage.open(io.BytesIO(image_bytes))
        data_url = encode_vision_image(im, max_edge=1024)
        msg = [{"role": "user", "content": [
            {"type": "text", "text": _REVIEW_PROMPT.replace("{prompt}", prompt_text or "(未记录提示词)")},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}]
        raw = ""
        for _ in range(_REVIEW_MAX_ATTEMPTS):
            resp = await llm.chat(msg, tools=[], ctx=None, on_delta=None, summarizer=None)
            raw = (resp.text or "").strip()
            if raw:
                break
        out = _coerce_review_result(raw)
        out["checked"] = True
        return out
    except Exception as e:  # noqa: BLE001 - 质检失败绝不挡出图
        log.warning("出图质检跳过：%s", e)
        return {"checked": False, "ok": True, "issues": [], "fix": "", "error": str(e)}


# ---- 多视角：每个视角「重新推理」出专属提示词 ----
# 用户要求：八方画像的每一张都应该按该视角重新构建提示词，而不是把总提示词原样塞给模型，
# 否则主提示词里的「三分之四视角 / 半身 / 特写」等词会让每张都画成同一种半身构图，
# 甚至被「多视角」措辞带成一张拼了很多姿势的联系表。这里在总提示词基础上，剥离镜头/景别词，
# 再让主模型重写成「单人、单视角、全身」的该视角提示词；失败则回退规则版 build_view_prompt。
_VIEW_REWRITE_PROMPT = """你是游戏美术的角色多视角提示词工程师。下面是一个角色的**总设定**，
以及要从某个特定视角出图时用的**相机方位**。请据此写一条**只针对这个视角**的生图提示词。

硬性要求：
1. 画面里**只画这一个人**（单人、单个完整身体），绝对不要出现多个人物、拼图、多视角、
   角色设计页、转盘式多角度——整张图只有一个角度。
2. 必须是**完整全身像**：头顶到脚完整入镜，绝不能只拍半身/胸像/局部特写，不能裁掉腿脚。
3. 只描述**这个视角**能看到的部分；相机方位以给定的为准。画面里绝不能出现相机、摄影机、
   摄影师或任何第二个人/旁观者（俯拍仰拍很容易脑补出这些东西，务必避免）。
4. 保持人物的外观（发型发色、五官、服装、配色、画风）与总设定一致。
5. 语言与总设定一致（总设定是中文就用中文，是英文就用英文），只输出提示词本身，
   不要解释、不要 JSON、不要前后缀。
6. 不要写「多视角」「参考图组」这类词。

总设定：{base}
相机方位：{camera}
构图：{framing}
{ref}"""


def _coerce_view_prompt(raw: str) -> str:
    """清洗视角重写结果：去掉代码围栏/引号/多余空白，过短或可疑（含多视角词）则弃用。"""
    t = (raw or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*([\s\S]*?)\s*```$", t)
    if fence:
        t = fence.group(1).strip()
    t = t.strip().strip('"').strip("“”").strip()
    if not t or len(t) < 12:
        return ""
    # 模型不听话把多视角/拼图词写回来时，弃用（回退规则版更安全）
    if any(w in t for w in ("多视角参考图组", "参考图组", "转盘", "角色设计页", "多角度拼")):
        return ""
    return t[:1500]


async def _rederive_view_prompt(base: str, view_key: str, has_reference: bool,
                                ctx: RequestContext) -> str:
    """返回该视角专属提示词：优先主模型重写，失败/不可用回退规则版。"""
    fallback = img_views.build_view_prompt(base, view_key, has_reference=has_reference)
    # 背面三个视角：模型重写常把「五官精致/肌肤白皙」这类脸部细节带进来，导致纯背面仍画出
    # 侧脸、并裁成半身（实测多次）。规则版 build_view_prompt 已剥离脸部/景别描述、英文相机置顶，
    # 实测能稳定出「纯背影、不露脸」，故背面直接用规则版，其余视角仍走模型重写。
    if view_key in img_views.BACK_KEYS:
        return fallback
    try:
        v = img_views.VIEWS_BY_KEY.get(view_key) or {}
        profile = await team_main_profile(ctx.team_id)
        ctx_llm = Ctx(role="main", run_type="imagegen-viewpoint", requirement=base,
                      model=(profile.get("model") or ""), model_profile=profile or {})
        llm = build_llm("main", ctx_llm)
        # 关键：总设定里常带「上半身 / 三分之四视角 / 半身像 / 居中构图」等**景别/镜头词**，
        # 若原样喂给重写模型，它会把这些词带进每个视角（实测背面被拉成半身、俯仰被裁成局部）。
        # 先剥离这些词，只保留「人物本身」的外观，再让模型按该视角重写。
        clean_base = img_views._strip_conflicts(base or "") or (base or "")
        prompt = (_VIEW_REWRITE_PROMPT
                  .replace("{base}", clean_base)
                  .replace("{camera}", v.get("camera", ""))
                  .replace("{framing}", v.get("framing", ""))
                  .replace("{ref}", "参考图提供的是同一人物，请保持人物一致。" if has_reference else ""))
        resp = await llm.chat([{"role": "user", "content": prompt}],
                              tools=[], ctx=ctx_llm, on_delta=None, summarizer=None)
        out = _coerce_view_prompt(resp.text or "")
        if out:
            # 底模对**纯中文**相机/景别指令不敏感（实测：即使中文写「完全看不到脸 / 高角度俯拍」
            # 仍画成正面半身美女）。必须把英文相机指令置顶，才能真正扭转朝向（背/俯/仰）。
            cam_en = v.get("camera_en", "")
            return f"{cam_en}，{img_views._FULLBODY_EN}。单张画面，只有一个角度、只画这一个人，仰拍/俯拍时不要出现相机或摄影师。{out}"
        return fallback
    except Exception as e:  # noqa: BLE001 - 重写失败不挡出图
        log.warning("视角提示词重写失败，回退规则版（%s）：%s", view_key, e)
        return fallback


class _IssueIn(BaseModel):
    prompt: str
    positive: str = ""       # 增强后的正面词（有就用它，否则用 prompt）
    negative: str = ""
    files: list[str] = []    # 本次生成的结果文件名（/files 接口用的 gen_xxx.png）


@router.post("/review-issues")
async def review_issues(body: _IssueIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """按提示词看图找缺陷（不重画，只出「问题描述 + 建议补进负面词的标签」）。
    给「生成前提示词检查」用：先审后画，发现再出图前就把问题堵住。"""
    from starlette.concurrency import run_in_threadpool
    profile = await team_main_profile(ctx.team_id)
    ctx_llm = Ctx(role="main", run_type="imagegen-review",
                  requirement=body.prompt.strip(), model=(profile.get("model") or ""),
                  model_profile=profile or {})
    llm = build_llm("main", ctx_llm)
    base = body.positive.strip() or body.prompt.strip()
    results = []
    for fn in body.files[:8]:
        p = _RESULT_DIR / _clean(fn)
        if not p.is_file():
            continue
        data = await run_in_threadpool(p.read_bytes)
        r = await _review_one(llm, data, base)
        r["file"] = fn
        results.append(r)
    return {"results": results,
            "issues": [i for r in results for i in r.get("issues", [])],
            "fix": _merge_negative("", ", ".join(r.get("fix", "") for r in results if r.get("fix"))),
            "model": getattr(llm, "model", "") or ""}


def _coerce_fill_result(raw: str) -> dict[str, str]:
    """从模型回复里抠出 {positive, negative, note}。模型偶尔带 ```json 围栏或额外文字，
    这里做一次宽松解析；解析不出就把整段当 positive（总比空着强）。"""
    txt = (raw or "").strip()
    if not txt:
        return {}
    # 去掉 ```json ... ``` 围栏
    fence = re.match(r"^```[a-zA-Z]*\s*([\s\S]*?)\s*```$", txt)
    if fence:
        txt = fence.group(1).strip()
    # 截取第一个 { 到最后一个 }
    if not (txt.startswith("{") and txt.endswith("}")):
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            txt = txt[i : j + 1]
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return {
                "positive": str(obj.get("positive") or "").strip(),
                "negative": str(obj.get("negative") or "").strip(),
                "note": str(obj.get("note") or "").strip(),
            }
    except Exception:  # noqa: BLE001
        pass
    # 退一步：尝试分别抠 positive / negative 两个字段的字面值
    def _grab(key: str) -> str:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', txt, re.S)
        if not m:
            return ""
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:  # noqa: BLE001
            return m.group(1)
    pos, neg = _grab("positive"), _grab("negative")
    if pos or neg:
        return {"positive": pos.strip(), "negative": neg.strip(), "note": ""}
    return {"positive": txt, "negative": "", "note": ""}


_LORA_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _u(v):  # type: ignore[no-untyped-def]
    """前端传字符串 UUID，ORM 比较前必须转回 UUID（见 ORM/raw Uuid 绑定坑）。"""
    try:
        return uuid.UUID(str(v))
    except Exception:  # noqa: BLE001
        return v


def _clean(name: str) -> str:
    """只清洗文件名（保留原样，不加前缀）——读取已有文件时用。"""
    return _SAFE_NAME.sub("_", name)


def _new_name(name: str) -> str:
    """清洗 + 加随机前缀生成新文件名——写入新文件时用。"""
    stem = _clean(Path(name).stem)[:60] or "ref"
    suffix = _clean(Path(name).suffix) or ".png"
    return f"{uuid.uuid4().hex[:8]}_{stem}{suffix}"


@router.get("/options")
async def options(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    data = await fetch_model_options(COMFYUI_BASE_URL)
    # 附带多视角目录与默认负面词，前端渲染视角选择器/负面框直接取用
    data["views"] = img_views.view_catalog()
    data["default_negative_prompt"] = COMFYUI_NEGATIVE_PROMPT
    return data


class FillIn(BaseModel):
    user_input: str                       # 用户的一句话画面需求 / 已有草稿
    current_positive: str = ""            # 当前正面词（有则在其基础上补全）
    current_negative: str = ""            # 当前负面词（空=用服务默认做参考）


@router.post("/fill-prompts")
async def fill_prompts(body: FillIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """用默认对话模型（main 角色）根据用户输入补全正面 / 负面提示词。

    工作台「根据我的描述帮填」按钮调用：不走出图、不占 GPU，只是一次文本补全。
    LLM 不可用时返回 502，前端保留原值不覆盖。
    """
    user = (body.user_input or "").strip()
    if not user:
        raise HTTPException(status_code=400, detail="请先输入想画的内容")
    extra = ""
    if body.current_positive.strip():
        extra += f"\n当前正面词草稿（请在其基础上补全优化）：\n{body.current_positive.strip()}\n"
    if body.current_negative.strip():
        extra += f"\n当前负面词草稿（请在其基础上补全优化）：\n{body.current_negative.strip()}\n"
    # 没有现成负面词时，把服务默认负面词交给模型作参考
    base = "" if body.current_negative.strip() else _default_base_negative()
    # 用 replace 而非 str.format —— 模板里有 JSON 示例的花括号，format 会误判占位符
    prompt = _FILL_PROMPT.replace("{user}", user).replace("{base}", base) + extra
    try:
        profile = await team_main_profile(ctx.team_id)
        ctx_llm = Ctx(role="main", run_type="imagegen-fill",
                      requirement=user, model=(profile.get("model") or ""),
                      model_profile=profile or {})
        llm = build_llm("main", ctx_llm)
        t0 = time.monotonic()
        resp = await llm.chat(
            [{"role": "user", "content": prompt}],
            tools=[], ctx=ctx_llm, on_delta=None, summarizer=None)
        raw = (resp.text or "").strip()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"调用对话模型失败：{e}") from e
    if not raw:
        raise HTTPException(status_code=502, detail="对话模型返回为空，请重试或手动填写")
    out = _coerce_fill_result(raw)
    if not out.get("positive"):
        raise HTTPException(status_code=502, detail="未能从模型回复中解析出正面提示词，请重试")
    model_name = getattr(llm, "model", "") or ""
    return {
        "positive": out.get("positive", ""),
        "negative": out.get("negative", ""),
        "note": out.get("note", ""),
        "model": model_name,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


@router.post("/comfyui/start")
async def comfyui_start(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """手动启动 ComfyUI 服务（未运行才拉起，轮询等待就绪，最长 180s）。
    会把 GPU 从语音服务手里收回来（先停 IndexTTS2，见 gpu_arbiter）。"""
    if not COMFYUI_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置 ComfyUI 地址（.env COMFYUI_BASE_URL）")
    info = await gpu_arbiter.prepare("image")
    gpu_arbiter.release()
    boot = info.get("start") if isinstance(info.get("start"), dict) else {}
    # 界面读顶层 error，别把启动失败埋进嵌套字段里；gpu 里另外给出腾卡过程
    return {**boot, "gpu": info, "gpu_note": gpu_arbiter.note(info)}


@router.post("/comfyui/stop")
async def comfyui_stop(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """手动停止 ComfyUI 服务：杀掉监听 API 端口的进程及其子进程。"""
    if not COMFYUI_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置 ComfyUI 地址（.env COMFYUI_BASE_URL）")
    res = await stop_comfyui_service(COMFYUI_BASE_URL)
    if not res.get("stopped"):
        return {"stopped": False, "detail": "ComfyUI 未在运行"}
    return {"stopped": True, "killed": res.get("killed") or []}


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
    return FileResponse(p, media_type="image/png")


# ---------------------------------------------------------------- LoRA 上传
def _safe_filename(name: str) -> str:
    """保留中文等 Unicode 字符，只去掉路径分隔符与非法字符（ComfyUI 能直接读）。"""
    base = Path(name or "").name
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return base[:120] or "lora.safetensors"


def _valid_safetensors_head(path: Path) -> bool:
    """粗校验：8 字节小端头长度 + 头 JSON 必须落在文件内。"""
    try:
        n = path.stat().st_size
        if n < 16:
            return False
        with path.open("rb") as f:
            (hlen,) = struct.unpack("<Q", f.read(8))
        return 0 < hlen <= n - 8
    except Exception:  # noqa: BLE001
        return False


@router.post("/loras")
async def upload_lora(file: UploadFile = File(...), subdir: str = Form("user"),
                      ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """把 .safetensors 放进 ComfyUI models/loras/<subdir>/，无需重启即可在列表里选。"""
    target_root = loras_dir()
    if target_root is None:
        raise HTTPException(
            status_code=400,
            detail="未找到 ComfyUI 的 models/loras 目录：请在 .env 设置 COMFYUI_LORAS_DIR，"
                   "或确保 COMFYUI_LAUNCH_CMD 指向的 main.py 所在目录下有 models/loras")
    fname = _safe_filename(file.filename or "lora.safetensors")
    if not fname.lower().endswith(".safetensors"):
        raise HTTPException(status_code=400, detail="仅支持 .safetensors 文件")
    sub = _clean(subdir or "user")[:40] or "user"
    folder = target_root / sub
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / fname
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"models/loras/{sub}/{fname} 已存在，"
                                                   "请改名后再传（不覆盖已有文件）")
    tmp = dest.with_name(dest.name + ".part")
    size = 0
    try:
        with tmp.open("wb") as f:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _LORA_MAX_BYTES:
                    raise HTTPException(status_code=400, detail="LoRA 文件不能超过 2GB")
                f.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="空文件")
        if not _valid_safetensors_head(tmp):
            raise HTTPException(status_code=400, detail="不是有效的 safetensors 文件（头部校验失败）")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"name": f"{sub}/{fname}", "size": size}


# ---------------------------------------------------------------- 预设 CRUD
class PresetIn(BaseModel):
    key: str = ""
    label: str
    params: dict = {}
    image: str = ""     # 本次生成结果的文件名；传了就把这张图存成预设参考图


class PresetPatch(BaseModel):
    label: str | None = None
    params: dict | None = None


class ProjectPresetIn(BaseModel):
    project_id: str
    preset_id: str = ""  # 空串 = 解除绑定


@router.get("/presets")
async def list_presets(ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        rows = (await s.execute(select(ImagePreset).where(
            ImagePreset.team_id == _u(ctx.team_id)).order_by(ImagePreset.created_at))).scalars().all()
    builtin = [img_presets.builtin_as_preset(p) for p in BUILTIN_PRESETS]
    custom = [{"id": str(r.id), "key": r.key, "label": r.label,
               "params": dict(r.params_json or {}), "builtin": False,
               "image_file": r.image_file or ""} for r in rows]
    return {"presets": builtin + custom}


@router.get("/presets/image/{name}")
async def preset_image(name: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """预设参考图（登录可看；文件名由服务端生成，已做目录穿越防护）。"""
    p = img_presets.preset_image_path(name)
    if p is None:
        raise HTTPException(status_code=404, detail="参考图不存在")
    return FileResponse(p, media_type="image/png")


@router.get("/project-preset")
async def get_project_preset(project_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    pid = await img_presets.get_project_preset_id(project_id)
    preset = await img_presets.get_preset(ctx.team_id, pid) if pid else None
    return {"project_id": project_id, "preset_id": pid, "preset": preset}


@router.put("/project-preset")
async def set_project_preset(body: ProjectPresetIn,
                             ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """项目绑定强制预设：agent 的 generate_image 生图时强制套用该预设。"""
    if body.preset_id:
        preset = await img_presets.get_preset(ctx.team_id, body.preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail="预设不存在")
        body.preset_id = preset["id"]
    await img_presets.set_project_preset(body.project_id, body.preset_id)
    return {"project_id": body.project_id, "preset_id": body.preset_id}


@router.post("/presets")
async def create_preset(body: PresetIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="预设名称不能为空")
    key = (body.key.strip() or f"p_{uuid.uuid4().hex[:8]}").lower()
    if any(p["key"] == key for p in BUILTIN_PRESETS):
        raise HTTPException(status_code=409, detail="与内置预设计划重名，请换一个名称")
    image_file = ""
    if body.image:
        src = _RESULT_DIR / _clean(body.image)
        if src.is_file():
            image_file = img_presets.save_preset_image(src.read_bytes())
    async with SessionLocal() as s:
        dup = (await s.execute(select(ImagePreset).where(
            ImagePreset.team_id == _u(ctx.team_id), ImagePreset.key == key))).scalars().first()
        if dup is not None:
            raise HTTPException(status_code=409, detail=f"预设「{key}」已存在")
        row = ImagePreset(team_id=_u(ctx.team_id), key=key, label=label,
                          params_json=dict(body.params or {}), image_file=image_file or None)
        s.add(row)
        await s.commit()
        return {"id": str(row.id), "key": row.key, "label": row.label,
                "params": row.params_json, "builtin": False, "image_file": row.image_file or ""}


async def _get_own_preset(s, preset_id: str, team_id: str) -> ImagePreset:
    row = await s.get(ImagePreset, _u(preset_id))
    if row is None or str(row.team_id) != str(team_id):
        raise HTTPException(status_code=404, detail="预设不存在")
    return row


@router.put("/presets/{preset_id}")
async def update_preset(preset_id: str, body: PresetPatch,
                        ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if preset_id.startswith("builtin:"):
        raise HTTPException(status_code=400, detail="内置预设不可修改，请先另存为自定义预设")
    async with SessionLocal() as s:
        row = await _get_own_preset(s, preset_id, ctx.team_id)
        if body.label is not None:
            if not body.label.strip():
                raise HTTPException(status_code=400, detail="预设名称不能为空")
            row.label = body.label.strip()
        if body.params is not None:
            row.params_json = dict(body.params)
        await s.commit()
        return {"id": str(row.id), "key": row.key, "label": row.label,
                "params": row.params_json, "builtin": False,
                "image_file": row.image_file or ""}


@router.delete("/presets/{preset_id}")
async def delete_preset(preset_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if preset_id.startswith("builtin:"):
        raise HTTPException(status_code=400, detail="内置预设不可删除")
    async with SessionLocal() as s:
        row = await _get_own_preset(s, preset_id, ctx.team_id)
        image_file = row.image_file or ""
        await s.delete(row)
        await s.commit()
    if image_file:
        img_presets.delete_preset_image(image_file)
    return {"ok": True}


class GenIn(BaseModel):
    prompt: str
    reference: str = ""            # /reference 返回的文件名，空=无参考图
    reference_mode: str = "style"  # style | img2img
    workflow_mode: str = "auto"    # auto | checkpoint | diffusion
    model: str = ""                # 手动指定模型文件名（可选）
    enhance: bool = True           # 走 LLM 分析重组提示词
    size: str = "1024x1024"
    count: int = 1                 # 一次生成张数（1-8，各自独立文件、独立下载）
    lora: str | None = None        # None=.env 默认 LoRA；""=不挂；具名=指定 LoRA
    lora_strength: float | None = None
    negative_prompt: str | None = None  # None=用 .env 默认；""=空负面词
    views: list[str] | None = None      # 多视角(八方画像)：视角 key 列表；空/无=单张。忽略 count。
    auto_fix: bool = False              # 出图后 AI 看图质检，发现手指/肢体等异常自动补负面词并重生成
    job_id: str = ""                    # 前端生成的进度任务号，供 GET /jobs/{id} 轮询处理过程


# ---------------------------------------------------------------------------
# 处理过程可视化：出图请求本身是阻塞的（一张 2K 图要 5 分钟），前端看不到中间状态，
# 只能干等。这里用「前端生成 job_id + 进程内任务表（progress 模块）」把处理阶段暴露出来：
#   queued → enhancing → starting(确保 ComfyUI 就绪) → preparing(解析画幅/参考图) →
#   sampling(逐张采样，含每张序号/排队位次) → review(AI 质检/自动修复) → saving → done/failed
# stage 与文案都是「真实状态」（来自代码执行到的位置 / ComfyUI /queue 里的 prompt_id），
# percent 是按经验耗时折算的**估算**值（前端会明确标注「估算」），不是真实采样步数——
# 因为 ComfyUI 只对本进程发起的 websocket 广播进度，外部 HTTP 客户端拿不到逐 step。
# ---------------------------------------------------------------------------
# 各阶段经验耗时（秒），仅用于 percent 估算；2K 大图实测约 330~340s。
_SAMPLING_BASE_S = 40.0   # 小图基础耗时
_SAMPLING_PER_MP_S = 70.0  # 每百万像素追加耗时（2K≈3.87MP → 40+271≈311s，接近实测）


def _est_total_s(w: int, h: int, count: int) -> float:
    mp = (w * h) / 1_000_000.0
    per_img = _SAMPLING_BASE_S + _SAMPLING_PER_MP_S * mp
    return per_img * max(1, count) + 20  # 预留增强/启动/质检/存盘


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """前端轮询出图进度。任务已被回收/未知时返回 done=False, stage='unknown'。"""
    job = progress.get(job_id)
    # 采样阶段实时查 ComfyUI 队列，把「正在跑 / 排队第几位」透出来
    if job is not None and job.get("stage") == "sampling" and job.get("prompt_id"):
        st = await comfyui_queue_state(job["prompt_id"], COMFYUI_BASE_URL)
        qs = st.get("state") or ""
        progress.set_queue(job_id, qs,
                           st.get("position") if qs == "queued" else None)
    return progress.snapshot(job_id)


@router.post("")
async def generate(body: GenIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """工作台出图与聊天工具、语音合成共用全局媒体队列（跨会话串行）。"""
    job_id = (body.job_id or "").strip()
    if job_id and progress.get(job_id) is None:
        progress.new_job(job_id, int(body.count or 1))
        progress.set_stage(job_id, "queued", "已排队，等待空闲算力…")
    async with media_queue.media_slot("image", "image-gen"):
        try:
            return await _generate_impl(body, ctx)
        except Exception as e:  # noqa: BLE001 - 记录失败阶段后原样抛给上层（前端照旧拿 detail）
            progress.set_stage(job_id, "failed", "生成失败", detail=str(e))
            raise


async def _generate_impl(body: GenIn, ctx: RequestContext):
    job_id = (body.job_id or "").strip()
    if job_id and progress.get(job_id) is None:
        progress.new_job(job_id, int(body.count or 1))
    if not COMFYUI_BASE_URL:
        raise HTTPException(status_code=400, detail="未配置 ComfyUI 地址（.env COMFYUI_BASE_URL）")
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="提示词不能为空")
    progress.set_stage(job_id, "enhancing", "正在分析重组提示词…" if body.enhance else "准备参数…")

    # 1) LLM 提示词增强（失败回退原文）。走团队 main 绑定档位（与聊天同一个模型），
    #    否则会回退到已失效的 .env 全局端点 → 403 SUBSCRIPTION_NOT_FOUND。
    enhanced, enhance_note = body.prompt.strip(), ""
    if body.enhance:
        try:
            profile = await team_main_profile(ctx.team_id)
            ctx_llm = Ctx(role="main", run_type="imagegen-enhance",
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

    # 2) 参考图
    ref_bytes = None
    if body.reference:
        rp = _UPLOAD_DIR / _clean(body.reference)
        if not rp.is_file():
            raise HTTPException(status_code=400, detail="参考图不存在或已过期，请重新上传")
        ref_bytes = rp.read_bytes()
    ref_mode = body.reference_mode if body.reference_mode in ("style", "img2img") else "style"
    if ref_bytes and ref_mode == "style" and body.workflow_mode == "checkpoint":
        enhance_note += "；checkpoint 方式不支持风格参考，已按图生图处理"

    # 3) 确保 ComfyUI 在运行（未启动则自动拉起并等待就绪）
    progress.set_stage(job_id, "starting", "正在准备 ComfyUI 服务…")
    boot = await ensure_comfyui_running(COMFYUI_BASE_URL)
    if boot["error"]:
        raise HTTPException(status_code=502, detail=f"ComfyUI 不可用：{boot['error']}")
    boot_note = (f"ComfyUI 已自动启动（{boot['waited_ms'] / 1000:.1f}s）" if boot["started"] else "")

    # 3.5) 解析画幅：以前 body.size 只在返回里回显、从没往下传，导致界面选 16:9/2:3
    #      实际一律按 generate_images_bytes 的默认 512x512 出图（DiT 文生图再被抬到 1024²）。
    #      这里真正落成 width/height，工作台的预设尺寸（1792x1024 / 1024x1536 等）才会生效。
    w, h = _parse_size(body.size)
    # 多视角（人物多角度参考图）：每张都是「单人全身」，必须是竖构图；横版/方形会把全身像
    # 裁成半身/胸像（实测横版 16:9 只拍到腰以上）。若用户给的画幅不够竖，强制改成等高竖版。
    _view_keys_probe = img_views.normalize_views(body.views)
    if _view_keys_probe and h < w * 1.15:
        if w > h:
            w, h = h, w                     # 横版 → 竖版（等比换向，不增像素，如 1920x1080→1080x1920）
        else:
            h = int(round(w * 4 / 3))       # 方版 → 3:4 竖版（如 1024x1024→1024x1365）
    if job_id:
        progress.set_est_total(job_id, _est_total_s(w, h, max(1, min(int(body.count or 1), 8))))

    # 4) 生成；style 风格参考依赖 Qwen-Image 官方三件套（TextEncodeQwenImage21），
    #    其它底模（如 anima-base）文本编码维度不兼容；img2img 对部分 DiT 底模同样可能
    #    latent 不适配。三级回退：style → img2img(0.5) → 纯文生图，保证尽量出图。
    t0 = time.monotonic()
    style_note = ""
    imgs: list[bytes] | None = None
    count = max(1, min(int(body.count or 1), 8))
    negative = COMFYUI_NEGATIVE_PROMPT if body.negative_prompt is None else body.negative_prompt
    # 多视角（八方画像）：给了 views 就按视角逐张出图（每张独立提示词），忽略 count。
    view_keys = img_views.normalize_views(body.views)
    if view_keys:
        # 每一个视角都「重新推理出一条只属于该视角的提示词」：在总提示词基础上，剥离所有
        # 镜头/景别词，只保留人物外观，再交给主模型重写为该视角的「单人、单视角、全身」提示词。
        # 模型不可用/超时 → 自动回退到规则版 build_view_prompt（同样已剥离冲突词、单人全身）。
        progress.set_stage(job_id, "preparing", f"正在为 {len(view_keys)} 个视角逐个构建提示词…")
        jobs = [(await _rederive_view_prompt(enhanced, k, bool(ref_bytes), ctx), k)
                for k in view_keys]
    else:
        jobs = [(enhanced, "")]

    # 质检机器提前就绪：改为「边生成边质检」——每出一张立刻检查，通过就即时显示，
    # 不再等整批生成完再统一质检（用户明确要求：多图时无需都等生成完才质检/显示）。
    review_note = ""
    review_issues: list[str] = []
    kept_indexes: list[int] = []
    checked = fixed = kept = 0
    llm_rv = None
    review_setup_err = ""
    if body.auto_fix:
        try:
            profile_rv = await team_main_profile(ctx.team_id)
            ctx_rv = Ctx(role="main", run_type="imagegen-review", requirement=enhanced,
                         model=(profile_rv.get("model") or ""), model_profile=profile_rv or {})
            llm_rv = build_llm("main", ctx_rv)
        except Exception as e:  # noqa: BLE001
            review_setup_err = str(e)
            log.warning("质检模型初始化失败，跳过看图质检：%s", e)

    async def _review_and_maybe_fix(i: int, data: bytes, job_prompt: str, reps: int,
                                    kw_now: dict, neg_i: str):
        """单张生成后立即质检（可选自动修复）；返回 (最终图字节, verdict, issues, ok, reason)。
        verdict ∈ pass / fixed / kept / skipped。neg_i 为该张实际使用的（含背面视角追加项）负面词。"""
        nonlocal checked, fixed, kept, review_note
        n = reps
        progress.set_stage(job_id, "review", f"AI 质检：正在检查第 {i + 1}/{n} 张…",
                           image_index=i, detail="")
        rv = await _review_one(llm_rv, data, job_prompt)
        if not rv.get("checked"):
            progress.set_stage(job_id, "review", f"AI 质检：第 {i + 1}/{n} 张已跳过（未开启视觉）",
                               image_index=i, detail="")
            return data, "skipped", [], True, ""
        checked += 1
        reason = (rv.get("reason") or "").strip()
        if rv.get("ok"):
            progress.set_stage(job_id, "review",
                               f"AI 质检：第 {i + 1}/{n} 张通过" + (f" · {reason}" if reason else ""),
                               image_index=i, detail="")
            return data, "pass", [], True, reason
        iss = rv.get("issues") or []
        iss_txt = "、".join(iss) or "结构缺陷"
        review_issues.extend(iss)
        new_neg = _merge_negative(neg_i, rv.get("fix") or "")
        if new_neg.strip() != (neg_i or "").strip():
            try:
                progress.set_stage(
                    job_id, "review",
                    f"AI 质检：第 {i + 1}/{n} 张发现「{iss_txt}」→ 补负面词重生成…"
                    + (f" · {reason}" if reason else ""),
                    image_index=i, prompt_id="", queue_state="", detail="")
                got2 = await generate_images_bytes(
                    job_prompt, negative_prompt=new_neg,
                    base_url=COMFYUI_BASE_URL, timeout_s=COMFYUI_TIMEOUT_S,
                    width=w, height=h,
                    workflow_mode=body.workflow_mode, model=body.model,
                    lora=body.lora, lora_strength=body.lora_strength,
                    on_prompt_id=((lambda pid, _i=i: progress.set_stage(
                        job_id, "review", f"AI 质检：第 {_i + 1}/{n} 张修复重生成中…",
                        image_index=_i, prompt_id=pid, queue_state="running")) if job_id else None),
                    **kw_now)
                if got2:
                    fixed += 1
                    review_note += f"；第{i + 1}张异常（{iss_txt}）→ 已修复提示词并重生成"
                    progress.set_stage(job_id, "review", f"AI 质检：第 {i + 1}/{n} 张已修复重生成",
                                       image_index=i, prompt_id="", queue_state="", detail="")
                    return got2[0], "fixed", iss, False, reason
            except ComfyUIError as e:
                log.warning("自动修复重生成失败（保留原图）：%s", e)
        kept += 1
        kept_indexes.append(i)
        review_note += f"；第{i + 1}张异常（{iss_txt}）"
        progress.set_stage(job_id, "review",
                           f"AI 质检：第 {i + 1}/{n} 张异常（{iss_txt}）· 未自动修复，保留原图",
                           image_index=i, prompt_id="", queue_state="", detail="")
        return data, "kept", iss, False, reason

    attempts = []
    if ref_bytes:
        attempts = [
            (dict(reference=ref_bytes, reference_mode=ref_mode), ""),
            (dict(reference=ref_bytes, reference_mode="img2img", reference_denoise=0.5),
             "；该模型不支持所选参考模式，已用低重绘度（0.5）图生图近似"),
            (dict(), "；参考图在该模型上不可用，已按纯文生图生成"),
        ]
    else:
        attempts = [(dict(), "")]
    labels: list[str] = []
    file_entries: list[tuple[str, int]] = []
    total = 0
    _RESULT_DIR.mkdir(parents=True, exist_ok=True)
    ok_run = False
    for kw, note in attempts:
        try:
            # 串行多次出图，而不是 latent batch：显存占用与单张一致
            # （qwen_image int8 已占 ~6.9G，8G 卡上 batch>1 极易 OOM）。
            # 每次调用都会重新随机 seed，所以每张构图/配色不同，且各自独立文件。
            # 每张「生成→质检→（必要时）修复→存盘→推送预览」全程串行，通过一张显示一张。
            labels = []
            file_entries, total = [], 0
            checked = fixed = kept = 0
            kept_indexes = []
            review_note = ""
            progress.clear_previews(job_id)  # 丢弃上一轮回退失败时可能已推送的图
            reps = len(jobs) if view_keys else count
            progress.set_stage(job_id, "sampling", f"已提交到 ComfyUI，开始采样（共 {reps} 张）…",
                       image_count=reps, sample_started_ms=int(time.time() * 1000))
            for i in range(reps):
                job_prompt, vk = jobs[i] if view_keys else jobs[0]
                # 背面视角追加「排除脸/正面」负面词（否则底模仍画正脸）
                neg_i = img_views.build_view_negative(negative, vk) if view_keys else negative
                label_i = f"正在生成第 {i + 1}/{reps} 张…"

                def _on_pid(pid: str, _i=i, _lbl=label_i) -> None:
                    progress.set_stage(job_id, "sampling", _lbl, image_index=_i,
                               prompt_id=pid, queue_state="running")

                progress.set_stage(job_id, "sampling", label_i, image_index=i, prompt_id="", queue_state="")
                got = await generate_images_bytes(
                    job_prompt, negative_prompt=neg_i,
                    base_url=COMFYUI_BASE_URL, timeout_s=COMFYUI_TIMEOUT_S,
                    width=w, height=h,
                    workflow_mode=body.workflow_mode, model=body.model,
                    lora=body.lora, lora_strength=body.lora_strength,
                    on_prompt_id=(_on_pid if job_id else None), **kw)
                if not got:
                    continue
                data = got[0]
                verdict, issues, ok, reason = "", [], True, ""
                if body.auto_fix and llm_rv is not None:
                    data, verdict, issues, ok, reason = await _review_and_maybe_fix(
                        i, data, job_prompt, reps, kw, neg_i)
                # 存盘 + 立即推送预览（通过一张显示一张）
                fname = f"gen_{uuid.uuid4().hex[:10]}.png"
                (_RESULT_DIR / fname).write_bytes(data)
                fsz = len(data)
                file_entries.append((fname, fsz))
                total += fsz
                lbl_view = img_views.view_label(vk)
                labels.append(lbl_view)
                progress.add_preview(job_id, {
                    "index": i, "total": reps, "file": fname,
                    "url": f"/api/image-gen/files/{fname}", "size": fsz,
                    "view": lbl_view, "verdict": verdict,
                    "issues": issues, "ok": ok,
                })
            if not file_entries:
                raise ComfyUIError("未生成任何图片")
            style_note = note
            ok_run = True
            break
        except ComfyUIError as e:
            # 本轮回退失败：清掉本轮已写文件与预览，换下一套 kw 重试
            for fn, _sz in file_entries:
                (_RESULT_DIR / fn).unlink(missing_ok=True)
            file_entries, labels = [], []
            total = 0
            last_err = e
    if not ok_run:
        raise HTTPException(status_code=502, detail=f"ComfyUI 生成失败：{last_err}")

    # 质检小结（边生成边质检后统一汇总，写入 enhance_note 供结果区展示）
    if body.auto_fix:
        if review_setup_err:
            review_note += f"；AI 质检异常已跳过：{review_setup_err}"
        elif checked == 0:
            review_note += "；看图质检未执行（当前主模型未开启视觉）"
        elif fixed or kept:
            review_note = f"；AI 质检：{fixed} 张已修复重生成，{kept} 张未自动修复" + review_note
        else:
            review_note = f"；AI 质检通过（{checked} 张无异常）"

    progress.set_stage(job_id, "saving", "正在保存生成的图片…",
                       image_count=len(file_entries), image_index=len(file_entries))
    files = [{"file": n, "url": f"/api/image-gen/files/{n}", "size": sz,
              "view": (labels[i] if i < len(labels) else "")}
             for i, (n, sz) in enumerate(file_entries)]
    if view_keys:
        style_note += f"；多视角 {len(files)} 张（{'、'.join(labels)}）"
    elif count > 1:
        style_note += f"；一次生成 {len(files)} 张"
    progress.set_stage(job_id, "done", f"已生成 {len(files)} 张，正在载入…",
               image_count=len(files), image_index=len(files), percent=100, detail="")
    return {
        "file": files[0]["file"], "url": files[0]["url"],  # 兼容旧前端
        "files": files, "count": len(files),
        "enhanced_prompt": enhanced if body.enhance else "",
        "enhance_note": boot_note + enhance_note + style_note + review_note,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "size": total,
        # 质检结果：failed_indexes = 自动修复后仍不合格（或无法自动修复）的张序（0 起）
        "review": {"enabled": bool(body.auto_fix), "issues": review_issues,
                   "failed_indexes": kept_indexes} if body.auto_fix else None,
        # 本次实际生效的参数：前端「把这批图存成预设」直接用它，避免 UI 状态漂移
        "params": {
            "workflow_mode": body.workflow_mode, "model": body.model,
            "lora": body.lora, "lora_strength": body.lora_strength,
            "negative_prompt": negative, "size": body.size,
            "count": len(files), "enhance": body.enhance, "prompt": body.prompt,
            "views": view_keys,
        },
    }
