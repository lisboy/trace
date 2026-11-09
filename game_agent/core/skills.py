"""技能系统（渐进式披露）：SKILL.md 技能包的发现 / 解析 / 解析加载。

三种来源（load_skill 按此优先级同名去重）：
- project : 项目工作区 .trace/skills/<name>.md 或 <name>/SKILL.md
- global  : 服务端全局目录 DATA_DIR/skills/（所有项目共享）
- root    : 用户在技能面板添加的任意文件夹（Settings KV skill_roots）

渐进式披露：上下文里只注入「名称 + 描述」清单；正文由模型按需调 load_skill 加载。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from game_agent.config import DATA_DIR

GLOBAL_SKILLS_DIR = DATA_DIR / "skills"
SETTINGS_KEY_ROOTS = "skill_roots"
MAX_SKILL_BODY = 12000

SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SKILL_SCOPE_LABELS = {"project": "项目", "global": "全局", "root": "自定义目录"}
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)


def _parse_meta(text: str, fallback_name: str) -> tuple[str, str]:
    """从 SKILL.md 提取 (name, description)：frontmatter 优先，缺省回退首行标题/首段。"""
    name, desc = "", ""
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        body = text[m.end():]
        for ln in m.group(1).splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            k, v = k.strip().lower(), v.strip().strip('"').strip("'")
            if k == "name" and not name:
                name = v[:64]
            elif k == "description" and not desc:
                desc = v[:200]
    if not name:
        for ln in body.splitlines():
            if ln.strip().startswith("#"):
                name = ln.lstrip("#").strip()[:64]
                break
    if not desc:
        for ln in body.splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith("---"):
                continue
            desc = s[:200]
            break
    return (name or fallback_name), desc


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _scan_dir(root: Path, scope: str) -> list[dict]:
    """扫一个技能根目录：支持 <name>.md 与 <name>/SKILL.md 两种形态。"""
    out: list[dict] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for child in children:
        try:
            if child.is_dir():
                sp = child / "SKILL.md"
                if not sp.is_file():
                    continue
                title, desc = _parse_meta(_read(sp), child.name)
                out.append({"name": child.name, "title": title, "description": desc,
                            "scope": scope, "path": str(sp), "dir": str(child)})
            elif child.suffix.lower() == ".md" and SKILL_NAME_RE.match(child.stem):
                title, desc = _parse_meta(_read(child), child.stem)
                out.append({"name": child.stem, "title": title, "description": desc,
                            "scope": scope, "path": str(child), "dir": str(child.parent)})
        except Exception:  # noqa: BLE001 - 单个技能坏了不拖垮整张清单
            continue
    return out


async def get_extra_roots() -> list[str]:
    from game_agent.db.models import Settings
    from game_agent.db.session import SessionLocal
    async with SessionLocal() as s:
        row = await s.get(Settings, SETTINGS_KEY_ROOTS)
        if row is None:
            return []
        return [str(p) for p in ((row.value_json or {}).get("paths") or [])]


async def set_extra_roots(paths: list[str]) -> list[str]:
    import datetime as _dt
    from game_agent.db.models import Settings
    from game_agent.db.session import SessionLocal
    clean: list[str] = []
    for p in paths:
        p = str(p).strip()
        if p and p not in clean:
            clean.append(p)
    async with SessionLocal() as s:
        row = await s.get(Settings, SETTINGS_KEY_ROOTS)
        if row is None:
            row = Settings(key=SETTINGS_KEY_ROOTS, value_json={})
            s.add(row)
        row.value_json = {"paths": clean}
        row.updated_at = _dt.datetime.now(_dt.timezone.utc)
        await s.commit()
    return clean


def roots_for(workspace: str | Path | None, extra_roots: list[str] | None = None) -> list[tuple[str, Path]]:
    """解析顺序：project → global → extra roots（同名高优先级生效）。"""
    out: list[tuple[str, Path]] = []
    if workspace:
        out.append(("project", Path(workspace) / ".trace" / "skills"))
    out.append(("global", GLOBAL_SKILLS_DIR))
    for p in (extra_roots or []):
        out.append(("root", Path(p)))
    return out


async def list_skills(workspace: str | Path | None) -> dict:
    """面板 / 注入共用的技能清单。返回 {skills, roots, global_dir}。"""
    extra = await get_extra_roots()
    skills: list[dict] = []
    seen: set[str] = set()
    for scope, root in roots_for(workspace, extra):
        for meta in _scan_dir(root, scope):
            if meta["name"] in seen:  # 同名：高优先级来源生效
                continue
            seen.add(meta["name"])
            skills.append(meta)
    roots = [{"scope": sc, "path": str(rt), "exists": rt.is_dir()}
             for sc, rt in roots_for(workspace, extra)]
    return {"skills": skills, "roots": roots, "global_dir": str(GLOBAL_SKILLS_DIR)}


def _resolve_file(name: str, workspace: str | Path | None,
                  extra_roots: list[str] | None) -> Path | None:
    if not SKILL_NAME_RE.match(name):
        return None
    for _scope, root in roots_for(workspace, extra_roots):
        for cand in (root / f"{name}.md", root / name / "SKILL.md"):
            try:
                if cand.is_file():
                    return cand
            except OSError:
                continue
    return None


async def skill_body(name: str, workspace: str | Path | None) -> str:
    """load_skill 正文：多根解析 + 截断。找不到抛 FileNotFoundError。"""
    extra = await get_extra_roots()
    path = _resolve_file(name, workspace, extra)
    if path is None:
        raise FileNotFoundError(name)
    text = _read(path)
    if len(text) > MAX_SKILL_BODY:
        text = text[:MAX_SKILL_BODY] + f"\n...[截断，共 {len(text)} 字符]"
    return text


async def resolve_attached(names: list[str], workspace: str | Path | None) -> list[dict]:
    """把会话挂载的技能名列表解析成清单（跳过不存在的，不阻塞 run）。"""
    pool = (await list_skills(workspace))["skills"]
    by_name = {sk["name"]: sk for sk in pool}
    out = []
    for n in names:
        sk = by_name.get(str(n))
        if sk:
            out.append(sk)
    return out


def save_global_skill(name: str, description: str, content: str) -> Path:
    """创建/覆盖一个全局技能（<name>/SKILL.md，frontmatter 带描述）。"""
    if not SKILL_NAME_RE.match(name):
        raise ValueError("技能名只允许字母/数字/下划线/中划线（1-64 字符）")
    d = GLOBAL_SKILLS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    desc = description.strip().replace("\n", " ")[:200]
    fm = f"---\nname: {name}\ndescription: {desc}\n---\n\n" if desc else ""
    path = d / "SKILL.md"
    path.write_text(fm + content, encoding="utf-8")
    return path


def delete_global_skill(name: str) -> None:
    if not SKILL_NAME_RE.match(name):
        raise ValueError("技能名非法")
    d = GLOBAL_SKILLS_DIR / name
    f = GLOBAL_SKILLS_DIR / f"{name}.md"
    if d.is_dir():
        shutil.rmtree(d)
    elif f.is_file():
        f.unlink()
    else:
        raise FileNotFoundError(name)


# ---------- 内置技能播种（excel / word / pdf 解析） ----------

_BUILTIN_SKILLS_VERSION = "2"

_BUILTIN_SKILLS: dict[str, tuple[str, str]] = {
    "excel": (
        "解析用户上传的 Excel/CSV 表格附件（xlsx→TSV，可选工作表/行区间）；"
        "涉及表格数据读取、核对、汇总、按表生成代码或配置时先读本技能",
        """# Excel / CSV 表格解析

## 什么时候用
- 用户消息末尾的「已存入工作区」清单里出现 .xlsx / .csv 附件
- 需求涉及：读取/核对/汇总表格数据、按表格内容批量生成代码或配置、把结果写回成新表

## 怎么读
- .xlsx → parse_document 工具：{"path": "<附件路径>"}，可选参数
  - sheet：第几个工作表（1 基，默认第 1 个；所选文本开头列出全部工作表名）
  - max_rows：选取多少行（默认 200，上限 2000）
  - start_row：选区起始行（1 基，默认 1）
  - offset：所选文本的 1 基 Unicode 字符位置（按码点，不是字节，默认 1），包含格式标签、Tab 和换行，不含分页头尾
  - limit：返回多少字符（默认/上限 6000）；即使单个单元格或一行很长，也可按字符继续读
- 按末尾提示设置 offset，保持其他参数不变，先读完所选行的全部文本；再按提示设置 start_row 并重置 offset=1。更换 sheet/max_rows 也重置 offset=1
- .csv / .txt → 直接 read_file（纯文本，别用 parse_document）

返回格式：工作表信息之后，每条记录以 `[第 N 行]` 开头，所有列按原顺序用 Tab 分隔。

## 限制与坑
- 公式单元格读到的是「上次在 Excel 里保存时的缓存值」；程序生成的 xlsx 若没保存过缓存，公式格会是空——缺数据时先说明，别瞎编数值
- 只省略每行末尾的空白列，不限制列数；Tab/换行会转为空格/⏎
- 汇总/统计不要靠反复翻页肉眼看：数据量大时写个临时 python 脚本（openpyxl 已装在环境里）算完落成文件再读结果
""",
    ),
    "word": (
        "解析用户上传的 Word 文档附件（docx→段落+表格文本）；"
        "涉及 docx 需求文档、策划案、规格说明的读取时先读本技能",
        """# Word 文档解析

## 什么时候用
- 用户消息末尾的「已存入工作区」清单里出现 .docx 附件（需求文档/策划案/接口说明等）

## 怎么读
parse_document 工具：{"path": "<附件路径>"}
返回：正文段落与表格交错，保持原文顺序；表格以 `[表格 k：R 行 × C 列]` 开头、Tab 分隔的行。
使用 offset/limit 按字符分页：offset 是所选文本的 1 基 Unicode 字符位置（按码点，不是字节，默认 1），包含格式标签、Tab 和换行，不含分页头尾；limit 默认/上限 6000。
按末尾提示设置 offset，保持其他参数不变，直到提示文本已读完；长段落和表格都可继续读取，不必要求用户拆分。

## 限制与坑
- 旧版 .doc 不支持解析，回复用户请其另存为 .docx 再上传
- 文档里的图片不会出现在解析结果中；图片内容很重要时明确告诉用户提取不了
""",
    ),
    "pdf": (
        "解析用户上传的 PDF 附件（按页提取文本层）；"
        "涉及 pdf 附件读取时先读本技能（扫描件/图片型 PDF 提不出文本）",
        """# PDF 解析

## 什么时候用
- 用户消息末尾的「已存入工作区」清单里出现 .pdf 附件

## 怎么读
parse_document 工具：{"path": "<附件路径>"}，默认只提取第 1 页；page=N 只提取第 N 页（1 基）。
返回：总页数和 `--- 第 k 页 ---` 页标记。使用 offset/limit 读完该页：offset 是所选文本的 1 基 Unicode 字符位置（按码点，不是字节，默认 1），包含格式标签、Tab 和换行，不含分页头尾；limit 默认/上限 6000。
单页再长也按末尾提示设置 offset，保持其他参数不变；该页读完后再按提示设置 page，并重置 offset=1。

## 限制与坑
- 「没有可提取的文本」可能是空白页或扫描件/图片页，不代表整份 PDF 都是扫描件。扫描件需要 OCR；本工具不做 OCR，不要猜内容
- 「文本提取失败」是解析错误，不是扫描件判定；有密码且无法解密时会明确报错
- 提取出的文本可能没有排版（多栏/表格会串行）：涉及表格数据时和用户确认关键字段
""",
    ),
    "imagegen": (
        "游戏美术生图与预设（generate_image 工具 / 素材库「AI 生图」工作台）；"
        "涉及出图、卡面/立绘/头像/图标、项目画风预设时先读本技能",
        """# 生图与预设

## 工具入口
- 所有出图走 generate_image（唯一入口）。多张图用一次调用 + items / count，别反复单张调用。
- 参数：prompt（主体+场景+构图+风格+光照）、size、category、transparent、
  reference（工作区相对路径）+ reference_mode（style 画风参考 / img2img 重绘）。
- 不要自己写脚本调 ComfyUI HTTP API、不要手动起服务；工具内部自动拉起并串行出图。

## 项目强制预设
- 素材库 → AI 生图面板里可以给当前项目绑定「项目强制预设」。
- 绑定后，generate_image 会强制使用预设的尺寸 / LoRA / 强度 / 生成方式 / 负面词，
  你传的 size 不生效；工具回报里会写「项目预设「X」」。汇报时如实说明，不要谎报尺寸画风。

## 工作台（人用，agent 不操作）
- 选预设 = 一键套用参数，每个预设旁有参考样图；仍可继续改尺寸 / LoRA / 强度 / 张数。
- 上传 LoRA：写进 ComfyUI 的 models/loras/user/，不重启即可在列表里选。
- 满意后点「满意，存为预设」：把本次实际参数和这张效果图一起存成新预设。
- 实测参考（2026-10-29，qwen_image_2.1_int8）：gptimage2 LoRA 0.6~0.8 稳（厚涂质感），
  0.9 起容易出四指；油画 LoRA 笔触最重但手部易崩、色彩发灰。
""",
    ),
}


def seed_builtin_skills() -> list[str]:
    """补齐内置技能，保留自定义内容；成功标记后尊重删除，升级版本时才补回。"""
    marker = GLOBAL_SKILLS_DIR / ".builtin-seeded"
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == _BUILTIN_SKILLS_VERSION:
                return []
        except OSError:
            pass
    created: list[str] = []
    failed = False
    for name, (desc, body) in _BUILTIN_SKILLS.items():
        path = GLOBAL_SKILLS_DIR / name / "SKILL.md"
        if path.is_file() or (GLOBAL_SKILLS_DIR / f"{name}.md").is_file():
            continue
        pending = path.with_suffix(".tmp")  # 部分写入不能被误认成已安装技能。
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}", encoding="utf-8")
            pending.replace(path)
            created.append(name)
        except (OSError, ValueError):
            failed = True
        finally:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
    if failed:
        return created
    try:
        GLOBAL_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(_BUILTIN_SKILLS_VERSION, encoding="utf-8")
    except OSError:
        pass
    return created
