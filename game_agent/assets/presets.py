"""生图预设：内置配方、预设参考图、项目级强制预设的存取。

预设 = 一套出图参数（工作流方式 / 尺寸 / LoRA / 强度 / 负面词）+ 一张参考图。
项目可以绑定一个预设：agent 的 generate_image 工具生图时**强制套用**该预设
（用户要求，2026-10-29），工作台手动生图不受影响（留给实验调参）。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from game_agent.config import DATA_DIR
from game_agent.db.models import ImagePreset, Settings
from game_agent.db.session import SessionLocal

# 实测（2026-10-29，qwen_image_2.1_int8）：gptimage2 LoRA 0.6~0.8 厚涂质感与手部都稳，
# 0.9 开始出四指；discolysium 油画笔触最重但手部易崩、色彩发灰，只做大胆实验项。
LORA_GPTIMAGE2 = "qwen21_style/4cf5d2_@gptimage2.safetensors"
LORA_OIL = "qwen21_style/bd9b39_discolysium.safetensors"

PRESET_IMG_DIR = DATA_DIR / "objects" / "imagegen" / "presets"

# 内置预设：image 为 PRESET_IMG_DIR 下的样图文件名（缺失时界面显示占位）
BUILTIN_PRESETS: list[dict[str, Any]] = [
    {"key": "guofeng_houtu", "label": "国风厚涂卡面 16:9", "image": "guofeng_houtu.png",
     "params": {"workflow_mode": "diffusion", "model": "", "size": "1920x1080",
                "lora": LORA_GPTIMAGE2, "lora_strength": 0.7, "enhance": True, "count": 1}},
    {"key": "portrait_2_3", "label": "竖版角色立绘 2:3", "image": "portrait_2_3.png",
     "params": {"workflow_mode": "diffusion", "model": "", "size": "1024x1536",
                "lora": LORA_GPTIMAGE2, "lora_strength": 0.6, "enhance": True, "count": 1}},
    {"key": "avatar_square", "label": "头像 / 图标 1:1", "image": "avatar_square.png",
     "params": {"workflow_mode": "diffusion", "model": "", "size": "1024x1024",
                "lora": LORA_GPTIMAGE2, "lora_strength": 0.5, "enhance": True, "count": 1}},
    {"key": "oil_bold", "label": "油画笔触（大胆实验）", "image": "oil_bold.png",
     "params": {"workflow_mode": "diffusion", "model": "", "size": "1792x1024",
                "lora": LORA_OIL, "lora_strength": 0.8, "enhance": True, "count": 1}},
    {"key": "no_lora", "label": "纯净底模（不挂 LoRA）", "image": "no_lora.png",
     "params": {"workflow_mode": "diffusion", "model": "", "size": "1024x1024",
                "lora": "", "lora_strength": 0.0, "enhance": True, "count": 1}},
    # 2K 大图（约 3.87MP）：8G 显存实测约 5 分钟，走分块解码；比 1920x1080 更细腻，
    # 适合卡面/宣传图等需要高分辨率的成品资产。超 4MP（如 4K）会自动两段式出图。
    {"key": "card_2k", "label": "高分辨率卡面 2K（2360×1640）", "image": "card_2k.png",
     "params": {"workflow_mode": "diffusion", "model": "", "size": "2360x1640",
                "lora": LORA_GPTIMAGE2, "lora_strength": 0.7, "enhance": True, "count": 1}},
]


def _u(v):  # type: ignore[no-untyped-def]
    try:
        return uuid.UUID(str(v))
    except Exception:  # noqa: BLE001
        return v


def preset_image_path(name: str) -> Path | None:
    """预设参考图路径；只取文件名，杜绝目录穿越。"""
    base = Path(str(name or "")).name
    if not base:
        return None
    p = PRESET_IMG_DIR / base
    return p if p.is_file() else None


def save_preset_image(data: bytes) -> str:
    """把一张图存成预设参考图，返回文件名（供 params/DB 引用）。"""
    PRESET_IMG_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"p_{uuid.uuid4().hex[:12]}.png"
    (PRESET_IMG_DIR / fname).write_bytes(data)
    return fname


def delete_preset_image(name: str) -> None:
    p = preset_image_path(name)
    if p is not None:
        try:
            p.unlink()
        except OSError:
            pass


def builtin_as_preset(p: dict[str, Any]) -> dict[str, Any]:
    image = p.get("image") or ""
    return {"id": f"builtin:{p['key']}", "key": p["key"], "label": p["label"],
            "params": dict(p["params"]), "builtin": True,
            "image_file": image if image and (PRESET_IMG_DIR / image).is_file() else ""}


async def get_preset(team_id: str, preset_id: str) -> dict[str, Any] | None:
    """按 id 取预设（builtin:<key> 或自定义 UUID），不存在/越权返回 None。"""
    pid = str(preset_id or "").strip()
    if not pid:
        return None
    key = pid.split(":", 1)[1] if pid.startswith("builtin:") else pid
    for p in BUILTIN_PRESETS:
        if p["key"] == key:
            return builtin_as_preset(p)
    async with SessionLocal() as s:
        row = await s.get(ImagePreset, _u(pid))
        if row is None or str(row.team_id) != str(team_id):
            return None
        return {"id": str(row.id), "key": row.key, "label": row.label,
                "params": dict(row.params_json or {}), "builtin": False,
                "image_file": row.image_file or ""}


def _project_key(project_id: str) -> str:
    return f"image_preset:{project_id}"


async def get_project_preset_id(project_id: str) -> str:
    if not project_id:
        return ""
    async with SessionLocal() as s:
        row = await s.get(Settings, _project_key(project_id))
    if row is None:
        return ""
    return str((row.value_json or {}).get("preset_id") or "")


async def set_project_preset(project_id: str, preset_id: str) -> None:
    """project_id 绑一个预设；preset_id 空串 = 解除绑定。"""
    key = _project_key(project_id)
    async with SessionLocal() as s:
        row = await s.get(Settings, key)
        value = {"preset_id": preset_id or ""}
        if row is None:
            s.add(Settings(key=key, value_json=value))
        else:
            row.value_json = value
        await s.commit()


async def get_project_preset(project_id: str, team_id: str) -> dict[str, Any] | None:
    pid = await get_project_preset_id(project_id)
    if not pid:
        return None
    return await get_preset(team_id, pid)
