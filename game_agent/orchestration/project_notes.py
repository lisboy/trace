"""项目改动经验（工作区 md 文件）：把「值得后来人知道的经验和约定」按类别沉淀成 markdown。

为什么还要这一层（数据库里已经有一条 auto 项目记忆了）：
- 那条记忆是「当前状态」的一页纸摘要，长度封顶、每轮覆盖重写，实测把有价值的做法挤掉了
  （一次 20 张图标全量替换，记忆里只剩名册筛选的旧结论，「按什么规格替换」这条经验没了）；
- 用户要的是「后续再调整这个项目时，能知道有没有之前的改动经验」——那是分门别类的经验条目，
  允许积累、允许追新弃旧，而不是一段被反复改写的现状描述；
- 落成工作区里的 md，用户自己也能看、能改、能删。

规则：
- 不每轮都写。由 `judge()` 判断本轮是否值得沉淀，不值得就一行都不落；
- 分类固定（见 CATEGORIES），每类一个文件，条目按时间追加，超量时丢最旧的；
- 只写平台专属目录 `.trace/notes/`（平台文件，不参与智能体的改动审阅与回滚）。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from game_agent.logging_setup import get_logger

log = get_logger("orchestration.notes")

NOTES_DIR = ".trace/notes"

# category -> (文件名, 中文标题, 这类记什么)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "decision": ("decisions.md", "决策与取舍", "定了什么方向、为什么这么选、否掉了什么"),
    "pitfall": ("pitfalls.md", "踩坑与规避", "哪里会出错、为什么、下次怎么避免"),
    "convention": ("conventions.md", "项目约定", "命名/目录/规格/风格等必须延续的规矩"),
    "playbook": ("playbook.md", "可复用做法", "成套的操作流程：命令、端口、工具链、验收方式"),
    "codemap": ("codemap.md", "代码地图", "什么结构在哪个文件哪一段（区域描述 + 大致行号锚点）"),
}
_MAX_ENTRIES_PER_FILE = 40
# 注入上下文时的预算（token 估算），按文件从新到旧取，超预算就停
INJECT_BUDGET_TOKENS = 1500


def notes_root(workspace: Path | str) -> Path:
    return Path(workspace) / NOTES_DIR


def _title_key(title: str) -> str:
    """标题归一化，用于判重（去空格标点、小写、取哈希）。"""
    norm = re.sub(r"[\s\u3000]+", "", title).lower()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


def _entry_block(entry: dict, when: str, run_id: str) -> str:
    title = str(entry.get("title") or "").strip()[:80]
    detail = str(entry.get("detail") or "").strip()
    files = entry.get("files") or []
    lines = [f"### {when} · {title}"]
    for ln in detail.splitlines():
        ln = ln.strip()
        if ln:
            lines.append(f"- {ln}" if not ln.startswith("-") else ln)
    if files:
        lines.append("- 涉及：" + "、".join(str(f) for f in files[:8]))
    if run_id:
        lines.append(f"- run：{run_id[:8]}")
    return "\n".join(lines) + "\n"


def append(workspace: Path | str, entries: list[dict], run_id: str = "",
           when: datetime | None = None) -> list[str]:
    """按类别追加条目，返回写过的文件相对路径。重复标题直接跳过。"""
    ws = Path(workspace)
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y-%m-%d")
    touched: list[str] = []
    root = notes_root(ws)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("经验目录建不出来：%s", e)
        return []

    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        cat = str((e or {}).get("category") or "").strip()
        if cat not in CATEGORIES or not str(e.get("title") or "").strip():
            continue
        by_cat.setdefault(cat, []).append(e)

    for cat, items in by_cat.items():
        fname, cn, _desc = CATEGORIES[cat]
        p = root / fname
        old = ""
        if p.is_file():
            try:
                old = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
        existing = set(re.findall(r"<!--k:([0-9a-f]{12})-->", old))
        blocks = []
        for e in items:
            key = _title_key(str(e["title"]))
            if key in existing:
                continue
            existing.add(key)
            body = _entry_block(e, stamp, run_id)
            blocks.append(f"{body}\n<!--k:{key}-->")
        if not blocks:
            continue
        header = old if old.strip() else (
            f"# {cn}（项目改动经验）\n\n"
            f"> 由平台在每次 run 收尾时判断是否值得沉淀后追加，可以直接删条目；改标题即视为新条目。\n"
            f"> 这一类记：{_desc}。\n")
        text = header.rstrip("\n") + "\n\n" + "\n".join(blocks)
        text = _trim(text)
        try:
            p.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("经验写入失败 %s：%s", fname, e)
            continue
        touched.append(f"{NOTES_DIR}/{fname}")
    return touched


def _trim(text: str) -> str:
    """只保留最近 _MAX_ENTRIES_PER_FILE 条（文件头说明之后）。"""
    parts = re.split(r"(?=^### )", text, flags=re.M)
    head = [x for x in parts if not x.startswith("### ")]
    entries = [x for x in parts if x.startswith("### ")]
    if len(entries) <= _MAX_ENTRIES_PER_FILE:
        return text
    keep = entries[-_MAX_ENTRIES_PER_FILE:]
    dropped = len(entries) - len(keep)
    return "".join(head).rstrip("\n") + "\n\n" + f"<!-- 更早 {dropped} 条已归档删除 -->\n\n" \
        + "\n".join(keep)


def _chunks(text: str) -> tuple[str, list[str]]:
    """文件拆成（文件头, 条目块列表）。条目块以 `### ` 开头，末尾带自己的 `<!--k:-->` 标记。"""
    parts = re.split(r"(?=^### )", text, flags=re.M)
    head = "".join(x for x in parts if not x.startswith("### "))
    return head, [x for x in parts if x.startswith("### ")]


def _parse_entry(chunk: str) -> dict[str, Any]:
    lines = [ln for ln in chunk.splitlines() if ln.strip() and not ln.startswith("<!--")]
    head = lines[0] if lines else ""
    m = re.match(r"^###\s*(\d{4}-\d{2}-\d{2})\s*·\s*(.+)$", head)
    when, title = (m.group(1), m.group(2).strip()) if m else ("", head.lstrip("# ").strip())
    km = re.search(r"<!--k:([0-9a-f]{12})-->", chunk)
    detail: list[str] = []
    files: list[str] = []
    run_id = ""
    for ln in lines[1:]:
        t = ln.lstrip("- ").strip()
        if t.startswith("涉及："):
            files += [x.strip() for x in t[len("涉及："):].split("、") if x.strip()]
        elif t.startswith("run："):
            run_id = t[len("run："):].strip()
        elif t:
            detail.append(t)
    return {"when": when, "title": title, "detail": "\n".join(detail),
            "files": files, "run_id": run_id, "key": km.group(1) if km else _title_key(title)}


def list_entries(workspace: Path | str) -> list[dict[str, Any]]:
    """全部经验条目（给界面看）。按类别分组顺序返回，条目内新→旧。"""
    root = notes_root(Path(workspace))
    out: list[dict[str, Any]] = []
    for cat, (fname, cn, desc) in CATEGORIES.items():
        p = root / fname
        if not p.is_file():
            continue
        try:
            _, chunks = _chunks(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        items = [_parse_entry(c) for c in reversed(chunks)]
        for it in items:
            out.append({**it, "category": cat, "category_cn": cn, "category_desc": desc})
    return out


def delete_entry(workspace: Path | str, category: str, key: str) -> bool:
    """按 key 删一条（界面上的「删」按钮）。key 兼容标题哈希（手改过 md 没有标记时）。"""
    if category not in CATEGORIES:
        return False
    fname = CATEGORIES[category][0]
    p = notes_root(Path(workspace)) / fname
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    head, chunks = _chunks(text)
    keep, removed = [], False
    for c in chunks:
        parsed = _parse_entry(c)
        if not removed and (parsed["key"] == key or _title_key(parsed["title"]) == key):
            removed = True
            continue
        keep.append(c)
    if not removed:
        return False
    try:
        p.write_text(head.rstrip("\n") + ("\n\n" + "\n".join(keep) if keep else "\n"),
                     encoding="utf-8")
    except OSError as e:
        log.warning("经验删除失败 %s：%s", fname, e)
        return False
    return True


def index_titles(workspace: Path | str) -> list[str]:
    """已有条目的「类别 · 标题」清单，喂给判断模型防止重复沉淀。"""
    out: list[str] = []
    root = notes_root(Path(workspace))
    for cat, (fname, cn, _) in CATEGORIES.items():
        p = root / fname
        if not p.is_file():
            continue
        try:
            for m in re.finditer(r"^### (.+)$", p.read_text(encoding="utf-8", errors="replace"),
                                 flags=re.M):
                out.append(f"{cn} · {m.group(1).split(' · ', 1)[-1]}")
        except OSError:
            continue
    return out[:120]


def index_block(workspace: Path | str, budget_tokens: int = INJECT_BUDGET_TOKENS) -> str:
    """注入编排上下文的经验「索引」：每个分类一行 + 每条标题一行，正文不注入。

    为什么不inject正文：五类文件的正文全给要上千 token，而多数 run 只用得上其中一两类；
    给索引让 agent 自己按标题 read_file，需要哪类读哪类（.trace/notes 不在保护路径里，读得动）。
    """
    from game_agent.llm.context_window import estimate_tokens

    root = notes_root(Path(workspace))
    if not root.is_dir():
        return ""
    used = 0
    blocks: list[str] = []
    for cat, (fname, cn, desc) in CATEGORIES.items():
        p = root / fname
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        titles = [m.group(1).strip() for m in
                  re.finditer(r"^### (.+)$", text, flags=re.M)]
        if not titles:
            continue
        rows: list[str] = []
        for t in reversed(titles):          # 文件里最靠后的最新，索引里也新的在前
            cost = estimate_tokens(t) + 4
            if used + cost > budget_tokens:
                break
            rows.append(f"- {t}")
            used += cost
        if not rows:
            continue
        blocks.append(f"**{cn}** · {NOTES_DIR}/{fname}（{len(titles)} 条，{desc}）\n"
                      + "\n".join(rows))
    if not blocks:
        return ""
    return ("## 项目改动经验索引（只有标题，正文没注入）\n"
            "下面是历次 run 沉淀下来的条目标题。动手前先扫一眼：命中当前任务的，"
            "用 read_file 读那个文件（文件里最靠后的条目最新），照着已经定好的口径做，"
            "别把同一条坑再踩一遍、也别重新摸索已经记过的做法。\n\n"
            + "\n\n".join(blocks) + "\n\n")


def judge_prompt(requirement: str, summary: str, changed_files: list[str],
                 existing_titles: list[str]) -> str:
    """让模型判断本轮值不值得沉淀，值得就给出分类条目（JSON）。"""
    files = "、".join(changed_files[:20]) or "（未记录到文件改动）"
    titles = "\n".join(f"- {t}" for t in existing_titles[:60]) or "（还没有任何经验条目）"
    return (
        "你在为一个项目沉淀「改动经验」，供后续改这个项目的人/智能体参考。\n"
        "先判断这次调整是否值得记：值得 = 后来人需要延续的约定、会影响做法的决策、"
        "踩过的坑与规避办法、可复用的操作流程（命令/端口/规格/验收方式）、"
        "以及本次才摸清、此前没记过的结构位置；"
        "不值得 = 一次性的排版微调、纯文案改动、只改数值不改行为、代码里一眼就能看到的既定事实。\n"
        "输出严格 JSON，不要解释、不要代码围栏：\n"
        '{"worth": true|false, "reason": "一句话", "entries": '
        '[{"category": "decision|pitfall|convention|playbook|codemap", '
        '"title": "不超过 24 字，说清是什么事（不要写日期）", '
        '"detail": "1-3 句：结论 + 怎么做 / 为什么，用 - 开头分点", '
        '"files": ["相关路径"]}]}\n'
        "worth=false 时 entries 必须是空数组。一条经验一个条目，不要把三件事塞进一条。\n"
        "codemap（代码地图）单独说：如果本次 run 弄清楚了此前未记录的结构位置"
        "（例如「布局骨架在 style.css 约 200-244 行」「首页网格在 home-grid 一段」），"
        "就输出一条 codemap：title 写清是什么区域，detail 用 - 分点写"
        "「区域描述 + 文件路径 + 大致行号/选择器锚点」。行号允许漂移，写「约」就行——"
        "后来人是靠「哪个文件的哪一段」省掉全库 grep 的，靠的不是行号精确。\n\n"
        f"【本次需求】\n{requirement[:600]}\n\n"
        f"【本次结果】\n{summary[:2500]}\n\n"
        f"【改动到的文件】\n{files}\n\n"
        f"【已有经验条目（别重复沉淀同一件事；口径变了就写新条目并在 detail 里说明取代哪条）】\n{titles}"
    )


def parse_judge(raw: str) -> dict[str, Any]:
    """从模型回复里抠出 JSON；解析不了当作「不值得记」，绝不瞎写。"""
    import json

    text = (raw or "").strip()
    if not text:
        return {"worth": False, "reason": "空回复", "entries": []}
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S).strip()
    start = text.find("{")
    if start < 0:
        return {"worth": False, "reason": "没有 JSON", "entries": []}
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {"worth": False, "reason": "JSON 不完整", "entries": []}
    try:
        data = json.loads(text[start:end])
    except ValueError as e:
        return {"worth": False, "reason": f"JSON 解析失败：{e}", "entries": []}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    clean = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        cat = str(e.get("category") or "").strip()
        if cat not in CATEGORIES:
            continue
        clean.append({"category": cat, "title": str(e.get("title") or "").strip()[:80],
                      "detail": str(e.get("detail") or "").strip()[:900],
                      "files": [str(f) for f in (e.get("files") or []) if str(f)][:8]})
    return {"worth": bool(data.get("worth")) and bool(clean),
            "reason": str(data.get("reason") or "")[:200], "entries": clean}
