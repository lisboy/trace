"""技能系统（渐进式披露）：SKILL.md 技能包的发现 / 解析 / 解析加载。

三种来源（load_skill 按此优先级同名去重）：
- project : 项目工作区 .trace/skills/<name>.md 或 <name>/SKILL.md
- global  : 服务端全局目录 DATA_DIR/skills/（所有项目共享）
- root    : 用户在技能面板添加的任意文件夹（Settings KV skill_roots）

渐进式披露：上下文里只注入「名称 + 描述」清单；正文由模型按需调 load_skill 加载。
"""
from __future__ import annotations

import json
import hashlib
import re
import shutil
from pathlib import Path

from game_agent.config import DATA_DIR

GLOBAL_SKILLS_DIR = DATA_DIR / "skills"
SETTINGS_KEY_ROOTS = "skill_roots"
MAX_SKILL_BODY = 12000
_BUILTIN_HASHES_FILE = ".builtin-hashes.json"   # 记录每个内置技能「上次写入的正文哈希」

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

_BUILTIN_SKILLS_VERSION = "3"

# 历史版本内置正文的规范化哈希：用于判断「文件还是我们当初写的原文」，
# 当 .builtin-hashes.json 缺失（老版本升级上来）时也能安全刷新旧正文。
_PREVIOUS_BUILTIN_HASHES: dict[str, tuple[str, ...]] = {
    # v2（2025 版）正文
    "excel": ("e98d38913be621042b26bd2e8e4f09174df87eb7cc74a9ec6b5912fe113c72d7",),
    "word": ("1975a31dff5c31017b98684d7a30cc3e756cf1b278b2beb2dec713623b202e5e",),
    "pdf": ("07d16379948a492864828e8dbc72d47e95c032d955d93e74296fe449d83a8f54",),
    "imagegen": ("dc133efb5e09d13be29559a677bd254f7b712f66d027b9a7ea143146c62f7f75",),
}

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
        "解析用户上传的 Word 文档附件（docx 正文+表格+图片+内嵌表格/图表+文本框/批注）；"
        "涉及 docx 需求文档、策划案、规格说明的读取时先读本技能",
        """# Word 文档解析（正文 + 图片 + 内嵌表格）

## 什么时候用
- 用户消息末尾的「已存入工作区」清单里出现 .docx 附件（需求文档/策划案/接口说明/验收单等）
- 用户说「按这份文档做」而文档是 docx：必须先把文档读全，再动手改代码

## 核心原则：文档里的信息不只在文字里
策划案/需求文档常把关键信息放在**截图、UML 图、数值表、批注**里：
- 数值（血量/伤害/掉落率/时长）常在被截图的 Excel 里，正文只写「见下表」
- UI 布局、关卡流程、角色动作要求常是**图片**，正文写「如上图所示」
- 评审意见常在**批注**里，正文看不到
所以只读文字就动手 = 漏掉一多半需求。**解析结果里凡是出现图片/内嵌表格/图表/批注标记，都要按下面的方法真的看一遍再下结论。**

## 第一步：读文本（段落 + 正文表格）
parse_document 工具：{"path": "<附件路径>"}
- 返回按原文顺序交错的正文段落与表格；正文表格以 `[表格 k：R 行 × C 列]` 开头，行内 Tab 分隔
- 分页用 offset/limit：offset 是所选文本的 1 基 Unicode 字符位置（按码点，不是字节，默认 1），含格式标签/Tab/换行，不含分页头尾；limit 默认与上限都是 6000
- 按末尾提示设置 offset、其余参数不变，一直读到「本段文本已读完」；长文档不要要求用户拆分

## 第二步：看图（图片不进文本，要自己调工具看）
解析结果里图片以标记出现，形如：
`[图片 3（1280×720，PNG） → .trace/docx-assets/需求文档-a1b2c3d4/image3.png]`
（在表格里的会带位置：`[｛表格 1 第 2 行第 3 列｝图片 5（…） → …]`）

看图的规矩：
- 一次最多 9 张：用 **view_images**（paths 传多个路径）合成一张预览图一起看；超过 9 张分批
- 只有需要抠细节（数值、坐标、颜色、按钮文案）时才用 **view_image** 单看那一张
- 路径就是文档里给的相对工作区路径，**不要**去 base64、不要猜图片内容
- 看完必须把「从图里读到的信息」写成文字：需求摘要/实现清单里明确写「图 3：血条宽度 120px，左上角显示等级」，否则下一轮就等于没看过
- 图的说明文字（alt/descr）会跟在标记后面，可作定位线索，但**不能替代看图**

## 第三步：内嵌表格与图表（已自动展开，直接用）
- 内嵌 Excel：以 `[内嵌表格开始]`…`[内嵌表格结束]` 包住，里面 `[内嵌表格 k/n：工作表名，共约 R 行]` + Tab 分隔的数据行，**就是文档里的原始数值，直接采信**
  - 默认每表只展开前 200 行；看到「[本表只展开前 200 行]」说明被截断
  - 需要完整数据/公式/更多 sheet 时：让用户把该 Excel 单独上传一份，再用 excel 技能按 sheet/行区间精确读，不要靠猜
- 图表：以 `[图表 1：柱状图，标题「通关率分布」；「通关率」：一关=0.8、二关=0.55、三关=0.3]` 出现，数值来自文档里缓存的图表数据，可直接引用；没有缓存值时会写「（没有缓存数值）」
- 其它内嵌对象（内嵌 Word/PDF/Visio 等）：只会给出 `[内嵌文件：xxx（类型）无法在正文里展开]`，需要用户另存为独立文件后再 parse_document

## 其它会被提取的内容
- 文本框：`[文本框 1：血条宽度=120]`（画在图形里的说明文字，正文段落里没有）
- 批注：`[批注 1（策划A）：这里要求补充数值]`——评审意见与追加要求，优先级等同正文
- 页眉页脚：`[页眉]` / `[页脚]` 后跟内容（常是项目名/版本/公司模板字段）

## 汇总口径（读完 deliver 什么）
向用户/自己汇报解析结果时，按这四块写，缺哪块明确说「文档未提供」：
1. 功能与流程（文字 + 图片里看出的界面/流程）
2. 数值与配置（正文表格 + 内嵌表格 + 图表缓存值，标注来源是「正文表 2」还是「内嵌表格 敌人配置」）
3. 待确认项（批注里的追加要求、图片里读不清的地方、被截断的内嵌表）
4. 文档没写清、需要用户拍板的口径

## 限制与坑
- 旧版 .doc 不支持解析，回复用户请其另存为 .docx 再上传
- 单份文档最多导出 60 张图，超出会写「[还有 N 张图片超出单次导出上限]」：先按正文顺序看关键图，剩下的向用户确认是否需要
- EMF/WMF 等矢量图会尝试转 PNG；转不了时标记里写「此格式无法直接查看」，如实告诉用户而不是编内容
- 图片导出目录是 `.trace/docx-assets/<文档名>-<hash>/`，同一份文档重复解析会覆盖同名文件，不会堆垃圾
- 内嵌表格里的公式单元格：读的是保存时的缓存值，可能是空——缺数据时先说明，别瞎编数值
- 解析失败/打不开会直接报错（损坏或加密），不要假设文档内容
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


def _skill_file_text(name: str, desc: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def _normalize(text: str) -> str:
    """统一换行与首尾空白，跨平台比哈希（Windows 的 write_text 会写 \r\n）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _text_hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _load_seeded_hashes() -> dict[str, str]:
    try:
        raw = json.loads((GLOBAL_SKILLS_DIR / _BUILTIN_HASHES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _write_seeded_hashes(hashes: dict[str, str]) -> bool:
    try:
        GLOBAL_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        (GLOBAL_SKILLS_DIR / _BUILTIN_HASHES_FILE).write_text(
            json.dumps(hashes, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        return True
    except OSError:
        return False


def _install_skill(path: Path, text: str) -> bool:
    """原子写入：先写 .tmp 再替换，半截文件不会被当成已安装技能。"""
    pending = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(text, encoding="utf-8")
        pending.replace(path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def seed_builtin_skills() -> list[str]:
    """补齐并升级内置技能。

    升级口径：文件内容仍是「我们当初写入的原文」时随版本覆盖成新正文；
    用户改动过的技能一律保留（原文哈希对不上就不动），与删除后尊重不回填一致。
    """
    marker = GLOBAL_SKILLS_DIR / ".builtin-seeded"
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == _BUILTIN_SKILLS_VERSION:
                return []
        except OSError:
            pass
    recorded = _load_seeded_hashes()
    hashes: dict[str, str] = dict(recorded)
    created: list[str] = []
    failed = False
    for name, (desc, body) in _BUILTIN_SKILLS.items():
        text = _skill_file_text(name, desc, body)
        want = _text_hash(text)
        hashes[name] = want
        path = GLOBAL_SKILLS_DIR / name / "SKILL.md"
        flat = GLOBAL_SKILLS_DIR / f"{name}.md"
        existing = path if path.is_file() else (flat if flat.is_file() else None)
        if existing is None:
            if _install_skill(path, text):
                created.append(name)
            else:
                failed = True
            continue
        try:
            current = _text_hash(existing.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            failed = True
            continue
        if current == want:
            continue                        # 已是最新
        known = {recorded.get(name)} | set(_PREVIOUS_BUILTIN_HASHES.get(name, ()))
        known.discard(None)
        if current not in known:
            continue                        # 用户改过 → 保留自定义内容
        if _install_skill(existing, text):
            created.append(name)
        else:
            failed = True
    if failed:
        return created
    if not _write_seeded_hashes(hashes):
        return created
    try:
        GLOBAL_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(_BUILTIN_SKILLS_VERSION, encoding="utf-8")
    except OSError:
        pass
    return created
