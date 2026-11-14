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
        # 用解析后的 key 判重（含「缺 key / key 写坏」的旧条目走标题哈希回退），
        # 否则手改过 md、key 格式不规范的条目判重会漏，同一件事被反复沉淀。
        existing = ({_parse_entry(c)["key"] for c in _chunks(old)[1]}
                    if old.strip() else set())
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
        # judge_prompt 让模型在 detail 里写「取代：<旧条标题>」，这里兑现它：
        # 把被取代的旧条标为失效（不删、留痕），否则旧口径会一直注入误导后续 run。
        for e in items:
            for target in _supersede_targets(str(e.get("detail") or "")):
                _mark_superseded(ws, cat, target)
    return touched


def _supersede_targets(detail: str) -> list[str]:
    """从 detail 里抠出「取代：xxx」的旧条标题（支持全角/半角冒号）。"""
    out: list[str] = []
    for m in re.finditer(r"取代[:：]\s*([^\n；;]+)", detail or ""):
        t = m.group(1).strip().strip("「」『』\"'《》〈〉").strip()
        if t:
            out.append(t)
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"[\s\u3000]+", "", t or "").lower()


def _mark_superseded(workspace: Path | str, category: str, target_title: str) -> bool:
    """把同类别里标题匹配「被取代」的旧条标为失效。匹配用互相包含，容忍措辞微调。"""
    if category not in CATEGORIES:
        return False
    p = notes_root(Path(workspace)) / CATEGORIES[category][0]
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _, chunks = _chunks(text)
    want = _norm_title(target_title)
    if not want:
        return False
    # 跳过最后一条（刚写入的新条目本身），避免自我标记
    for c in chunks[:-1]:
        parsed = _parse_entry(c)
        have = _norm_title(parsed["title"])
        if not have or parsed["stale"] or parsed["keep"]:
            continue
        if want == have or want in have or have in want:
            return set_flag(workspace, category, parsed["key"], stale=True)
    return False


def _trim(text: str) -> str:
    """保留最近 _MAX_ENTRIES_PER_FILE 条，按价值分三级淘汰。

    为什么不再「纯丢最旧」：旧条不一定没用（「迁移先复制再删旧」这种铁律写一次就管很久），
    而新条也不一定有用（早期的过程性流水账）。只按位置淘汰等于把最该留的和最该丢的一起丢。

    三级：
    - 标 `<!--stale-->` 的（已失效）：**最先丢**，腾坑优先于一切；
    - 标 `<!--keep-->` 的（钉住）：**永不淘汰**，除非钉住数本身就超过上限；
    - 其余：维持原行为，满了丢最旧。
    """
    parts = re.split(r"(?=^### )", text, flags=re.M)
    head = "".join(x for x in parts if not x.startswith("### "))
    entries = [x for x in parts if x.startswith("### ")]
    if len(entries) <= _MAX_ENTRIES_PER_FILE:
        return text
    parsed = [_parse_entry(c) for c in entries]
    pinned = [i for i, e in enumerate(parsed) if e["keep"] and not e["stale"]]
    stale = [i for i, e in enumerate(parsed) if e["stale"]]
    normal = [i for i, e in enumerate(parsed) if not e["keep"] and not e["stale"]]
    # 钉住的优先占坑（极端情况钉了超过上限，只留最早的 _MAX 条，避免文件无限增长）
    chosen = set(pinned[:_MAX_ENTRIES_PER_FILE])
    room = _MAX_ENTRIES_PER_FILE - len(chosen)
    if room > 0 and normal:
        take = min(room, len(normal))
        chosen |= set(normal[len(normal) - take:])      # 正常条目丢最旧、留最新
        room -= take
    if room > 0 and stale:
        take = min(room, len(stale))                    # 还有空位才轮到失效条目（排最后）
        chosen |= set(stale[len(stale) - take:])
    dropped = len(entries) - len(chosen)
    kept = "\n".join(c for i, c in enumerate(entries) if i in chosen)

    # 归档标记累计次数，别每次 trim 都往上叠一行
    prev = sum(int(x) for x in re.findall(r"<!-- 更早 (\d+) 条已归档删除 -->", head))
    head = re.sub(r"\n*<!-- 更早 \d+ 条已归档删除 -->\s*", "\n", head)
    total = dropped + prev
    note = f"<!-- 更早 {total} 条已归档删除 -->\n\n" if total else ""
    return head.rstrip("\n") + "\n\n" + note + kept


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
            "files": files, "run_id": run_id, "key": km.group(1) if km else _title_key(title),
            # 生命周期标记：keep=钉住不淘汰，stale=已失效（不注入、优先淘汰）
            "keep": "<!--keep-->" in chunk, "stale": "<!--stale-->" in chunk}


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


def set_flag(workspace: Path | str, category: str, key: str,
             *, keep: bool | None = None, stale: bool | None = None) -> bool:
    """给条目打/去标记：keep=钉住（永不淘汰），stale=已失效（不注入、优先淘汰）。

    None 表示不动这个标记（只想改一个就不传另一个）。为什么用标记而不是直接删：
    失效要留痕——用户看得见「这条作废了」，智能体不再被它误导，历史也还在文件里。
    """
    if category not in CATEGORIES:
        return False
    p = notes_root(Path(workspace)) / CATEGORIES[category][0]
    if not p.is_file():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    head, chunks = _chunks(text)
    hit = False
    out: list[str] = []
    for c in chunks:
        parsed = _parse_entry(c)
        if not hit and (parsed["key"] == key or _title_key(parsed["title"]) == key):
            hit = True
            body = re.sub(r"\n*<!--(?:keep|stale)-->\s*", "\n", c).rstrip()
            new_keep = parsed["keep"] if keep is None else keep
            new_stale = parsed["stale"] if stale is None else stale
            if new_keep:
                body += "\n<!--keep-->"
            if new_stale:
                body += "\n<!--stale-->"
            c = body + "\n"
        out.append(c)
    if not hit:
        return False
    try:
        p.write_text(head.rstrip("\n") + "\n\n" + "\n".join(out), encoding="utf-8")
    except OSError as e:
        log.warning("经验标记失败 %s：%s", CATEGORIES[category][0], e)
        return False
    return True


def index_titles(workspace: Path | str, limit: int = 400) -> list[str]:
    """已有条目的「类别 · 标题」清单，喂给判断模型防止重复沉淀。

    为什么不再截 120 条：判重靠的是「模型看得见已有条目」，截断等于让后半部分文件隐形。
    本机 130 条约 1.7k token，判断模型吃得下；limit 只是防文件失控增长的保险丝。
    """
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
    return out[:limit]


def index_block(workspace: Path | str, budget_tokens: int = INJECT_BUDGET_TOKENS) -> str:
    """注入编排上下文的经验「索引」：每个分类一行 + 每条标题一行，正文不注入。

    为什么不inject正文：五类文件的正文全给要上千 token，而多数 run 只用得上其中一两类；
    给索引让 agent 自己按标题 read_file，需要哪类读哪类（.trace/notes 不在保护路径里，读得动）。

    为什么按「轮转」分配预算而不是一类吃完再下一类：CATEGORIES 里排最后的 playbook/codemap
    曾被前三类把 1500 token 预算吃光后整类饿死，一条标题都进不了上下文——智能体看不到，
    就会重新全库 grep，正好废掉 codemap 的意义。现在每类轮流拿一条，五类都有份。
    已失效（stale）的条目不注入，钉住（keep）的排该类最前面。
    """
    from game_agent.llm.context_window import estimate_tokens

    root = notes_root(Path(workspace))
    if not root.is_dir():
        return ""
    buckets: list[dict[str, Any]] = []
    for cat, (fname, cn, desc) in CATEGORIES.items():
        p = root / fname
        if not p.is_file():
            continue
        try:
            _, chunks = _chunks(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        items = [_parse_entry(c) for c in chunks]
        alive = [e for e in items if not e["stale"]]
        if not alive:
            continue
        newest = list(reversed(alive))                  # 文件里最靠后的最新，索引里也新的在前
        ordered = [e for e in newest if e["keep"]] + [e for e in newest if not e["keep"]]
        buckets.append({"cn": cn, "fname": fname, "desc": desc, "all": len(items),
                        "hidden": len(items) - len(alive), "ordered": ordered, "rows": []})
    if not buckets:
        return ""

    # 固定开销先扣掉：外层说明 + 每个类别头（含 desc 与「共 N 条」），否则五类各塞到
    # 「刚好不超」时会一起把总量顶出预算（实测曾超 18%）。
    preamble = ("## 项目改动经验索引（只有标题，正文没注入）\n")
    fixed = estimate_tokens(preamble) + 60
    for b in buckets:
        fixed += estimate_tokens(f"**{b['cn']}** · {NOTES_DIR}/{b['fname']}"
                                 f"（共 {b['all']} 条；另有 N 条未列出，可 read_file；{b['desc']}）") + 2
    used = fixed
    ptr = [0] * len(buckets)
    moved = True
    while moved:                                        # 轮转：每类轮流拿一条
        moved = False
        for i, b in enumerate(buckets):
            if ptr[i] >= len(b["ordered"]):
                continue
            t = b["ordered"][ptr[i]]
            stamp = f'{t["when"]} · ' if t["when"] else ""
            line = f'- {stamp}{t["title"]}' + ("（钉住）" if t["keep"] else "")
            cost = estimate_tokens(line) + 2
            if used + cost > budget_tokens:
                continue
            b["rows"].append(line)
            ptr[i] += 1
            used += cost
            moved = True

    blocks: list[str] = []
    for b in buckets:
        if not b["rows"]:
            continue
        extra: list[str] = []
        if b["hidden"]:
            extra.append(f"另有 {b['hidden']} 条已失效")
        rest = len(b["ordered"]) - len(b["rows"])
        if rest > 0:
            extra.append(f"另有 {rest} 条未列出，可 read_file")
        tail = f"（共 {b['all']} 条" + ("；" + "；".join(extra) if extra else "") + f"；{b['desc']}）"
        blocks.append(f"**{b['cn']}** · {NOTES_DIR}/{b['fname']}{tail}\n"
                      + "\n".join(str(r) for r in b["rows"]))
    if not blocks:
        return ""
    return (preamble
            + "下面是历次 run 沉淀下来的条目标题。动手前先扫一眼：命中当前任务的，"
            "用 read_file 读那个文件（文件里最靠后的条目最新），照着已经定好的口径做，"
            "别把同一条坑再踩一遍、也别重新摸索已经记过的做法。"
            "标「已失效」的条目已经过时，不要照它做。\n\n"
            + "\n\n".join(blocks) + "\n\n")


def judge_prompt(requirement: str, summary: str, changed_files: list[str],
                 existing_titles: list[str]) -> str:
    """让模型判断本轮值不值得沉淀，值得就给出分类条目（JSON）。"""
    files = "、".join(changed_files[:20]) or "（未记录到文件改动）"
    # 按类别分组列全量：平铺 + 截断会让后半部分的类别（conventions/playbook/codemap）
    # 对判断模型完全隐形，结果就是同一件事换句话反复沉淀。
    grouped: dict[str, list[str]] = {}
    for t in existing_titles:
        cn, _, title = t.partition(" · ")
        grouped.setdefault(cn or "其他", []).append(title or t)
    if grouped:
        lines = []
        for cn, titles in grouped.items():
            lines.append(f"【{cn}】" + "；".join(titles))
        titles = "\n".join(lines)
    else:
        titles = "（还没有任何经验条目）"
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
         "判重要严：标题相同、或**意思相同**（同一件事换个说法、把上限数字写全/省略）都算重复，"
         "别再写第二遍；确实口径变了（旧条已作废），就写新条目，并在 detail 里首发一句"
         "「取代：<旧条标题>」——平台据此把旧条标记为失效（不删除、留痕）。\n"
         "只写「现在仍然成立」的结论；已经被后续改动推翻、或只属于那次过程的选择，不要写进 entries。\n"
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
