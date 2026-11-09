"""会话权限作用域 + 指导项（团队级默认设置 + 每会话覆盖）。

- 权限作用域（permission scope）：
  · ``workspace`` 仅工作区（默认）：读写/解析工具只能碰当前项目工作区内。
  · ``full`` 全部权限：允许访问工作区以外的本机路径（系统关键目录仍受保护）。
  默认值存在 Settings 表（助手行为设置里可调），每个会话可单独覆盖。

- 指导项（guidance items）：
  用户可增删的「给模型看的经验条目」，每次 run 组装进系统提示，用来纠正
  那些反复踩坑的常识性操作（如：jar 包里的源码要先解压再看）。内置若干默认项，
  用户可停用或补充。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from game_agent.db.models import Settings
from game_agent.db.session import SessionLocal

SCOPE_WORKSPACE = "workspace"
SCOPE_FULL = "full"
SCOPES = (SCOPE_WORKSPACE, SCOPE_FULL)
DEFAULT_SCOPE = SCOPE_WORKSPACE

# 设置存 Settings 表的一行（团队无关；本产品按「用户自己的助手」使用，一套设置即可）
POLICY_KEY = "assistant_policy"


def normalize_scope(raw: object, default: str = DEFAULT_SCOPE) -> str:
    v = str(raw or "").strip().lower()
    return v if v in SCOPES else default


# 内置指导项（用户可逐条停用/编辑，也可新增自己的）。文本直接进系统提示，写清楚「怎么做」。
DEFAULT_GUIDANCE: list[dict] = [
    {"id": "archive-extract",
     "title": "压缩包先解压再读",
     "enabled": True,
     "text": "遇到 .zip / .jar / .7z / .tar / .gz 等压缩包，里面若是源码或资源，"
             "先解压到工作区临时目录再 read_file / grep；不要直接按文本读压缩包（只会是乱码）。"},
    {"id": "jar-decompile",
     "title": ".jar 是 zip，.class 要反编译",
     "enabled": True,
     "text": ".jar 本质是 zip，可解压查看包结构、MANIFEST、资源与配置。若源码只有 .class，"
             "解压后按需用反编译工具（如 javap -c、CFR、procyon）查看逻辑，不要靠文件名猜实现。"},
    {"id": "binary-no-read",
     "title": "二进制产物不按文本读",
     "enabled": True,
     "text": ".dll / .so / .exe / .pyc / .pdb / .pak / .bundle / AssetBundle 等是二进制，"
             "read_file 读不出内容。先找同名源码、符号表或文档；确实要看内容时用专门的解包/反编译手段。"},
    {"id": "engine-meta",
     "title": "引擎生成文件不要手改",
     "enabled": True,
     "text": "Unity 的 .meta / .asset、Godot 的 .import、UE 的 .uasset 等引擎自动生成文件不要手改，"
             "改对应源资产/配置，让引擎重新生成，否则引用（GUID/路径）会断。"},
    {"id": "locate-then-read",
     "title": "先定位再精读",
     "enabled": True,
     "text": "不确定功能在哪时，先用 glob 找文件、grep 找内容定位，再 read_file 按行精读；"
             "不要对整份大文件反复整读，也不要靠记忆猜路径。"},
]


def _clean_guidance(raw: object) -> list[dict]:
    """把前端传来的指导项归一化；非法条目丢弃，id 缺失时补一个。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        title = str(it.get("title") or "").strip() or text[:16]
        out.append({
            "id": str(it.get("id") or uuid.uuid4().hex[:8]),
            "title": title[:60],
            "text": text[:800],
            "enabled": bool(it.get("enabled", True)),
        })
        if len(out) >= 60:
            break
    return out


async def load_policy() -> dict:
    """读取设置：{permission_scope, guidance}。缺省用内置默认。"""
    async with SessionLocal() as s:
        row = await s.get(Settings, POLICY_KEY)
        val = (row.value_json or {}) if row else {}
    scope = normalize_scope(val.get("permission_scope"))
    # guidance 未存过 → 用内置默认；存过（哪怕是空列表）就以存值为准
    if "guidance" in val and isinstance(val.get("guidance"), list):
        guidance = _clean_guidance(val.get("guidance"))
    else:
        guidance = [dict(x) for x in DEFAULT_GUIDANCE]
    return {"permission_scope": scope, "guidance": guidance}


async def save_policy(permission_scope: object, guidance: object) -> dict:
    payload = {
        "permission_scope": normalize_scope(permission_scope),
        "guidance": _clean_guidance(guidance),
    }
    async with SessionLocal() as s:
        row = await s.get(Settings, POLICY_KEY)
        if row is None:
            row = Settings(key=POLICY_KEY, value_json=payload)
            s.add(row)
        else:
            row.value_json = payload
        await s.commit()
    return payload


def enabled_guidance(guidance: list[dict]) -> list[dict]:
    return [g for g in (guidance or []) if g.get("enabled", True) and g.get("text")]


def guidance_block(guidance: list[dict]) -> str:
    """把启用的指导项拼成系统提示里的一段。没有就返回空串。"""
    items = enabled_guidance(guidance)
    if not items:
        return ""
    lines = [f"- {g['text']}" for g in items]
    return ("## 经验指导（用户维护的操作要点，遇到相关场景务必遵守）\n" + "\n".join(lines))


def permission_hint(scope: str) -> str:
    """把权限作用域写进本轮提示，让模型知道自己能碰哪里。

    这条放在系统提示**最前面**（紧跟指导项），并明确写出工具被拒时的正确反应：
    实测模型撞到「路径越界」后，会改去 shell（type/cat）绕开工具守卫——守卫是硬拦，
    绕过去既读不到该读的东西、又白白烧掉一整轮。所以要明说：被拒就停手、如实告知。
    """
    if normalize_scope(scope) == SCOPE_FULL:
        return ("【本轮权限：全部权限】可以使用工作区以外的本机路径（例如项目目录外的脚本、配置、"
                "依赖目录）。系统关键目录（盘符根、Windows/Program Files 等）仍受保护。"
                "改动工作区外的文件同样会留快照，请谨慎并说明改了什么。")
    return ("【本轮权限：仅工作区】read_file / write_file / edit_file / delete_file / list_dir / "
            "glob / grep / view_image 等文件类工具只能访问当前项目工作区内，**工作区外一律拒绝**。\n"
            "要点：\n"
            "1. 先用 list_dir / glob 在工作区内确认文件是否已存在；需要工作区外的内容时，"
            "直接说明「这个文件在工作区外，需要把会话权限切到『全部权限』」，然后停手，"
            "不要自己动手。\n"
            "2. 工具返回「路径越界」时：**立即停止对该路径的一切尝试**，如实把这条错误告诉用户，"
            "并提示可切换权限；不要改用 run_command（type / cat / Get-Content 等）去绕过工具守卫"
            "——守卫拦的是路径而不是命令，绕过去属于违规，且会浪费整轮时间。\n"
            "3. 需要读工作区外的文件时，优先请用户把它放进工作区，或把会话权限切成「全部权限」。")
