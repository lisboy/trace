# 经验沉淀（.trace/notes）的废弃与瘦身方案

> **状态：已落地（2026-11-14 实施完成，638 passed / 前端构建通过）。**
> 实施结果见文末「七、落地记录」；下面二~六节保留原始设计与依据，便于回溯为什么这么改。
>
> 结论先行：改之前的机制里**没有任何「价值」概念**——淘汰只按时间丢最旧，注入只按顺序抢预算，
> 判重只看前 60 条标题。三类问题都在本机实测到了具体数字，不是理论推导。
> 本文给出可落地的改法（精确到行号 + 补丁），改完后：钉住的条目不淘汰、失效条目不注入、
> 五类公平分配预算、判重覆盖全量。

## 一、现状实测（本机 .trace/notes，130 条 / 68.8 KB）

| 文件 | 条目 | md 体积 | 全量标题 token | 已损坏的 key |
|---|---|---|---|---|
| decisions.md | 28 | 14.1 KB | 369 | — |
| pitfalls.md | 39 | 21.2 KB | 545 | 1 条（`gaautorun01`） |
| conventions.md | 22 | 10.8 KB | 291 | 2 条（无 key） |
| playbook.md | 14 | 6.6 KB | 177 | — |
| codemap.md | 27 | 16.1 KB | 330 | — |
| **合计** | **130** | **68.8 KB** | **1712** | **3 条** |

## 二、四个真实缺陷（都有实测证据）

### 缺陷 1：注入预算被「先到先得」吃光，**playbook 和 codemap 整类进不了上下文**

`index_block()`（`game_agent/orchestration/project_notes.py:231-273`）按 `CATEGORIES`
字典顺序逐个文件往里塞，预算 `INJECT_BUDGET_TOKENS = 1500`（第 30 行）用满就 `break`。
实测复现（预算 1500）：

```
decisions.md   共 28 条 注入 28 条
pitfalls.md    共 39 条 注入 39 条
conventions.md 共 22 条 注入 22 条
playbook.md    共 14 条 注入 14 条   ← 脚本按我预估的顺序算，实际真跑见下方「三·五」
codemap.md     共 27 条 注入  9 条
```

但**用现行代码真跑了一遍**（`project_notes.index_block(Path('.'))`），实际输出只有三行类别头：

```
**决策与取舍** · decisions.md（28 条…）
**踩坑与规避** · pitfalls.md（39 条…）
**项目约定** · conventions.md（22 条…）
总 token = 1502
```

也就是说 **playbook（14 条）和 codemap（27 条）这 41 条经验，一条都没注入**。
`CATEGORIES` 里这两类排最后（第 36 行），预算被前三类吃到 1502 后直接 `break`。
智能体看不到这些标题，就会重新全库 grep —— 正好废掉了当初加 codemap 的目的。

### 缺陷 2：判重上下文被双重截断，63 条完全隐形

- `index_titles()` 返回 `out[:120]`（第 228 行），而现有 130 条 → 尾部 10 条直接丢；
- `judge_prompt()` 又取 `existing_titles[:60]`（第 280 行）→ **只喂前 60 条**。

按 `CATEGORIES` 顺序，前 60 条 = decisions 28 条 + pitfalls 前 32 条。
也就是说 **conventions / playbook / codemap 三类共 63 条，判断模型一条都看不见**。

后果：模型不知道这三类里已有什么，每轮都可能重复沉淀同一件事；
`append()` 的标题哈希判重只在**措辞完全一致**时生效，标题改一个字就是新条目（第 187-189 行）。
现实例子：本机 `conventions.md` 里同时存在
「钉钉汇报正文上限 20000 且截断须显式标注」和「提醒正文上限20000且显式标注截断」，
`decisions.md` 里同时存在「收尾汇报按情况挑 3~5 块」和「收尾汇报按情况选块，不套固定六段」
—— 这就是判重失效留下的重复条目。

### 缺陷 3：淘汰只按时间，不看价值，40 条一满就无条件丢最旧

`_trim()`（第 124-134 行）：

```python
keep = entries[-_MAX_ENTRIES_PER_FILE:]   # 只保留最后 40 条
```

没有任何「这条还有用」的判断。旧的有价值条目只是因为排在前面，就成了第一个被挤掉的。
`pitfalls.md` 现在 **39 条，离 40 只差 1 条**——下一次再沉淀 2 条就开始丢 2026-10-30 那批坑。

### 缺陷 4：3 条条目 key 损坏，判重和删除都受影响

`append()` 用正则 `<!--k:([0-9a-f]{12})-->`（第 98 行）收集已有 key 做判重，
损坏的 key 收不进来 → 同标题再次沉淀会重复写入。实测：

- `pitfalls.md:281` → `<!--k:gaautorun01-->`，不是 12 位 hex，正则直接漏掉；
- `conventions.md:145`「推送记录可展开核对正文」、`conventions.md:150`「提醒正文上限20000…」
  两条**完全没有 key 标记**。

删除侧还能兜住（`_parse_entry` 第 179 行有 `key = _title_key(title)` 回退），
但判重侧就漏了。

## 三、方案：给条目加「生命周期」，淘汰分三级，注入改轮转

核心思路——**不再靠位置碰运气，让每条经验自己带状态**：

| 状态 | 标记 | 淘汰 | 注入索引 | 用途 |
|---|---|---|---|---|
| 钉住 keep | `<!--keep-->` | **永不淘汰** | 优先注入 | 长期有效的铁律（如「迁移先复制再删旧」） |
| 正常 active | 无 | 满了丢最旧 | 正常注入 | 大多数条目 |
| 失效 stale | `<!--stale-->` | **优先淘汰** | **不注入** | 口径变了、已过时、过程性残留 |

「失效」而不是「删除」的价值：**留痕**。用户看得见「这条作废了，因为 X」，
智能体不再被它误导，但历史仍在文件里（附一行 `- 失效：原因`）。

### 改点 1：`_parse_entry()` 解析状态（project_notes.py:162-186）

```python
    km = re.search(r"<!--k:([0-9a-f]{12})-->", chunk)
    keep = "<!--keep-->" in chunk
    stale = "<!--stale-->" in chunk
    ...
    return {"when": when, "title": title, "detail": "\n".join(detail),
            "files": files, "run_id": run_id, "key": km.group(1) if km else _title_key(title),
            "keep": keep, "stale": stale}
```

### 改点 2：`_trim()` 三级淘汰（替换 project_notes.py:124-134 全函数）

```python
def _trim(text: str) -> str:
    """保留最近 40 条，但按价值分三级：失效的最先丢、钉住的永不丢、其余丢最旧。"""
    parts = re.split(r"(?=^### )", text, flags=re.M)
    head = "".join(x for x in parts if not x.startswith("### "))
    entries = [x for x in parts if x.startswith("### ")]
    if len(entries) <= _MAX_ENTRIES_PER_FILE:
        return text
    parsed = [_parse_entry(c) for c in entries]
    keep_idx = [i for i, e in enumerate(parsed) if e["keep"] and not e["stale"]]
    stale_idx = [i for i, e in enumerate(parsed) if e["stale"]]
    normal_idx = [i for i, e in enumerate(parsed) if not e["keep"] and not e["stale"]]
    chosen = set(keep_idx)                       # 钉住的一律保留
    room = max(0, _MAX_ENTRIES_PER_FILE - len(chosen))
    if len(normal_idx) > room:
        chosen |= set(normal_idx[len(normal_idx) - room:]) if room else set()
    else:
        chosen |= set(normal_idx)
        room -= len(normal_idx)
        if room > 0:                             # 还有空位才让失效条目占（排最后）
            chosen |= set(stale_idx[len(stale_idx) - room:])
    dropped = len(entries) - len(chosen)
    kept = "\n".join(c for i, c in enumerate(entries) if i in chosen)
    note = (f"<!-- 更早 {dropped} 条已归档删除 -->\n\n" if dropped else "")
    return head.rstrip("\n") + "\n\n" + note + kept
```

保留原文件顺序输出（用 `enumerate` 按索引过滤），避免把条目顺序打乱导致「最旧」判错。

### 改点 3：`index_block()` 改轮转公平分配（替换 project_notes.py:239-267 的循环体）

不再「一类吃完再下一类」，改成**每类轮流加一条**，且 `stale` 不参与、`keep` 排前面：

```python
    from game_agent.llm.context_window import estimate_tokens

    root = notes_root(Path(workspace))
    if not root.is_dir():
        return ""
    buckets: list[dict] = []
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
        hidden = len(items) - len(alive)
        if not alive:
            continue
        newest = list(reversed(alive))                       # 文件里靠后的最新
        ordered = [e for e in newest if e["keep"]] + [e for e in newest if not e["keep"]]
        buckets.append({"cn": cn, "fname": fname, "desc": desc, "all": len(items),
                        "hidden": hidden, "ordered": ordered, "used": 0, "rows": []})
    if not buckets:
        return ""

    used = 0
    ptr = [0] * len(buckets)
    moved = True
    while moved:                                             # 轮转：每类轮流拿一条
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

    blocks = []
    for b in buckets:
        if not b["rows"]:
            continue
        extra = []
        if b["hidden"]:
            extra.append(f"另有 {b['hidden']} 条已失效")
        if len(b["ordered"]) > len(b["rows"]):
            extra.append(f"另有 {len(b['ordered']) - len(b['rows'])} 条未列出，可 read_file")
        tail = f"（共 {b['all']} 条" + ("；" + "；".join(extra) if extra else "") + "）"
        blocks.append(f"**{b['cn']}** · {NOTES_DIR}/{b['fname']}{tail}\n"
                      + "\n".join(b["rows"]))
```

这样 codemap 至少能拿到与其余四类相近的条数，不会再被饿死。

### 改点 4：判重吃全量（两处阈值）

- `index_titles()` 第 228 行 `return out[:120]` → `return out`（或抬到 400，
  130 条标题约 1.7k token，判断模型吃得下；真要省，就让 `judge_prompt` 按类别分组给）；
- `judge_prompt()` 第 280 行 `for t in existing_titles[:60]` → 去掉切片，按
  `CATEGORIES` 分组列出（分组比平铺更好判重）。

同时把 `judge_prompt` 的判重措辞补一句：
「标题相同或**意思相同**（同一件事换个说法）就别再写，用 stale 标旧条即可」——
现在的文案只说了「别重复沉淀同一件事」，没说允许标失效。

### 改点 5：`append()` 判重改用解析后的 key（project_notes.py:98）

```python
        existing = {_parse_entry(c)["key"] for c in _chunks(old)[1]} if old.strip() else set()
```

这样缺 key / key 损坏的条目也能按标题哈希参与判重（`_parse_entry` 有回退），
顺手兜住缺陷 4 的历史数据。

### 改点 6：新增「钉住 / 标失效」动作

`project_notes.py` 新增（放在 `delete_entry` 旁边，风格与它一致）：

```python
def set_flag(workspace: Path | str, category: str, key: str,
             *, keep: bool | None = None, stale: bool | None = None) -> bool:
    """给条目打/去标记：keep=钉住不淘汰，stale=已失效不注入。None 表示不动这个标记。"""
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
            if keep:
                body += "\n<!--keep-->"
            if stale:
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
```

接口侧（`game_agent/api/routers/memory.py`，紧跟 `delete_note`）：

```python
class NoteFlagIn(BaseModel):
    keep: bool | None = None
    stale: bool | None = None


@router.post("/projects/{project_id}/notes/{category}/{key}/flag")
async def flag_note(project_id: str, category: str, key: str, body: NoteFlagIn,
                    ctx: RequestContext = Depends(PERM_RUN_CREATE)):
    """钉住（不淘汰）/ 标失效（不注入）。失效是留痕，不是删除。"""
    p = await _check_project(project_id, ctx)
    if not project_notes.set_flag(PROJECTS_ROOT / p.repo_path, category, key,
                                  keep=body.keep, stale=body.stale):
        raise HTTPException(status_code=404, detail="经验条目不存在（或已被删除）")
    return {"ok": True}
```

前端三处（沿用现有 `ga-btn ga-btn-sm` 风格）：

1. `frontend/src/types.ts:305-315` `ProjectNote` 加 `keep: boolean; stale: boolean;`
2. `frontend/src/api.ts:383` 后面加
   `setNoteFlag: (projectId, category, key, body) => request(\`/projects/${projectId}/notes/${category}/${key}/flag\`, { method: "POST", body: JSON.stringify(body) })`
3. `frontend/src/pages/Config.tsx:837-846` 的「删除」按钮旁加「钉住 / 标失效」两个按钮，
   `stale` 的条目整卡降透明度（`opacity-60`）并显示「已失效」角标。

### 改点 7：瘦身——同主题合并

「瘦身」不是简单删，而是**合**。流程（可手动，也可后续让模型做）：

1. 把 N 条同主题旧条目合成 1 条新条目（标题略改 → 天然是新条目）；
2. 新条目 detail 里写「合并自：A / B / C」；
3. 旧的 N 条标 `stale`（留痕，不注入、优先淘汰）。

本机现成的重复对（可以直接按这个流程处理）：
- `decisions.md`「收尾汇报按情况挑 3~5 块」+「收尾汇报按情况选块，不套固定六段」
- `conventions.md`「钉钉汇报正文上限 20000 且截断须显式标注」+「提醒正文上限20000且显式标注截断」

## 三·五、方案已在真实数据上验过（不是纸上设计）

把上面 `_trim()` / `index_block()` 的新逻辑原样复刻成脚本，直接读本机 `.trace/notes` 的 130 条
真数据跑了一遍（`.trace/tmp/sim_lifecycle.py`，临时文件，收尾自动清）：

**① 注入分配（预算 1500）——五类都拿到，不再饿死**

| 类别 | 现行为 | 新逻辑 |
|---|---|---|
| 决策与取舍 | 28/28 条 | 18 条（另 10 条提示可 read_file） |
| 踩坑与规避 | 39/39 条 | 18 条 |
| 项目约定 | 22/22 条 | 17 条 |
| 可复用做法 | **0 条（整类没进上下文）** | **14/14 条（全进）** |
| 代码地图 | **0 条（整类没进上下文）** | 17 条 |

现行代码实测输出只有三行类别头（decisions/pitfalls/conventions），
**playbook 和 codemap 一个字都没注入**——比预想的「codemap 只进 9 条」更严重。
新逻辑下五类全部有份，短小的 playbook 还能整类进完。

**② 三级淘汰（conventions 灌到 52 条仿真）**

```
淘汰前 52 条 → 淘汰后 40 条
钉住的「同一 run 只挂一个轨迹宿主」还在吗：True      ← 钉住的不被挤掉
标失效的「消息底部不再挂独立素材区」还在吗：False   ← 失效优先淘汰
归档标记：<!-- 更早 12 条已归档删除 -->
```

三条核心承诺（钉住不淘汰 / 失效优先淘汰 / 五类公平预算）都实测成立了。

## 四、验收方式（改完自己跑，不用起服务）

`tests/test_resume_memory.py` 里紧接着 `test_codemap_category_roundtrip` 加：

```python
def test_trim_keeps_pinned_and_drops_stale_first(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    # 先写一条钉住的，再灌满 40 条把它挤出窗口
    project_notes.append(ws, [{"category": "convention", "title": "迁移先复制再删旧",
                               "detail": "- 铁律"}])
    key = project_notes.list_entries(ws)[0]["key"]
    assert project_notes.set_flag(ws, "convention", key, keep=True) is True
    for i in range(45):
        project_notes.append(ws, [{"category": "convention", "title": f"普通条目 {i}",
                                   "detail": "- x"}])
    titles = [e["title"] for e in project_notes.list_entries(ws)]
    assert "迁移先复制再删旧" in titles          # 钉住的没被挤掉
    assert len(titles) <= 40


def test_stale_entry_not_injected_and_dropped_first(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    project_notes.append(ws, [{"category": "pitfall", "title": "旧口径已过时", "detail": "- x"}])
    key = project_notes.list_entries(ws)[0]["key"]
    assert project_notes.set_flag(ws, "pitfall", key, stale=True) is True
    assert "旧口径已过时" not in project_notes.index_block(ws)
    assert project_notes.list_entries(ws)[0]["stale"] is True


def test_index_block_fair_budget_across_categories(tmp_path: Path) -> None:
    """五类都要拿到预算，不能前面的类把预算吃光（codemap 曾整类饿死）。"""
    ws = tmp_path / "ws"; ws.mkdir()
    for cat in ("decision", "pitfall", "convention", "playbook", "codemap"):
        for i in range(30):
            project_notes.append(ws, [{"category": cat, "title": f"{cat} 条目 {i}",
                                       "detail": "- x"}])
    block = project_notes.index_block(ws, budget_tokens=600)
    for cn in ("决策与取舍", "踩坑与规避", "项目约定", "可复用做法", "代码地图"):
        assert f"**{cn}**" in block, f"{cn} 没拿到任何预算"
```

## 五、回滚

- 代码：`project_notes.py` 四处（`_parse_entry` / `_trim` / `index_block` / `append` 判重行）
  + `memory.py` 一个路由 + 前端 3 处，都是局部替换，逐个 `edit_file` 反向改即回滚；
- 数据：`<!--keep-->` / `<!--stale-->` 是纯注释标记，**旧代码读它们只是当普通正文忽略**，
  不写回滚脚本也安全；真要清干净，`re.sub(r"\n*<!--(?:keep|stale)-->\s*", "\n", text)` 一把过。

## 六、建议实施顺序

1. **先修数据**（0 风险，5 分钟）：补 3 条缺失 key、修 `gaautorun01`；
2. **改点 1-3 + 6**（核心：生命周期 + 三级淘汰 + 公平预算 + 标记接口）；
3. **改点 4-5**（判重全量化）；
4. **改点 7**（用新机制处理现存重复对，pitfalls 39 条先标掉几条失效的腾坑）；
5. 前端按钮最后做（不影响后端正确性）。

## 七、落地记录（2026-11-14 实施）

按第六节顺序全部做完，未遗留 TODO。

### 代码改动

| 文件 | 改动 |
|---|---|
| `game_agent/orchestration/project_notes.py` | `_parse_entry` 解析 `keep`/`stale`；`_trim` 改三级淘汰（失效先丢 → 其余丢最旧 → 钉住永不丢，归档计数累加不叠行）；`index_block` 改轮转公平分配并扣除固定开销；`index_titles` 去掉 120 截断（改 `limit=400` 保险丝）；`judge_prompt` 按类别分组列全量 + 补「标失效/取代」措辞；`append` 判重改用 `_parse_entry` 的 key（兜住历史坏 key）；新增 `set_flag`、`_supersede_targets`、`_mark_superseded` |
| `game_agent/api/routers/memory.py` | 新增 `POST /projects/{id}/notes/{category}/{key}/flag`（`NoteFlagIn{keep,stale}`） |
| `frontend/src/types.ts` | `ProjectNote` 加 `keep` / `stale` |
| `frontend/src/api.ts` | 新增 `setProjectNoteFlag` |
| `frontend/src/pages/Config.tsx` | 每条经验加「钉住 / 标失效（恢复）/ 删除」三个按钮；失效卡片 `opacity-60` + 「已失效」角标；钉住显示「钉住」角标；顺带修正说明文案 `.agent/notes/` → `.trace/notes/` |

**额外做的一件（设计文档没写、但必要）**：`judge_prompt` 让模型在 detail 里写「取代：<旧条标题>」，
`append` 落盘后由 `_mark_superseded` 自动把被取代的旧条标 `stale`。否则「写新条标注取代」只停在
文字上——旧条照样注入、照样误导，等于没处理。匹配用「互相包含」容忍措辞微调，并跳过刚写入的新条防自我标记。

### 数据修复结果（本机 130 条）

```
修复损坏 key 3 条：pitfalls「工具类浅底…」、conventions「推送记录可展开核对正文」
                    「提醒正文上限20000且显式标注截断」  → 现在 key 不规范条目数 = 0
标失效 4 条（人工逐条核过正文，不按标题猜）：
  · conventions「任务提醒固定带来源与完整汇报」← 与「IM 提醒正文必须带来源 + 完整汇报」重复，且上限 3000 是旧口径
  · conventions「钉钉汇报正文上限 20000 且截断须显式标注」← 与信息更全的「提醒正文上限20000…」重复
  · pitfalls「amber底色被深色覆写成透明」← 与「工具类浅底在深色/壁纸下变成 12% 透明」同一件事
  · pitfalls「文档测试 6 个既有失败不要当成回归」← test_documents.py 现已 59 passed，事实不成立
钉住 7 条（数据安全铁律 / 硬约束 / 验收纪律）：
  迁移先复制、重启验证后再删旧 · 非 SQLite 连接串启动即报错 · 数据目录指针文件放用户主目录
  · 数据目录切换需重启才生效 · 子 agent 禁止递归派发 · SQLite 迁移不能直接拷贝 .db
  · 静音 WAV 被当成功交付
```

存活的 5 类条数：决策 28 / 踩坑 37 / 约定 20 / 可复用 14 / 代码地图 27。

### 实施中修掉的两个真问题（设计阶段没预见）

1. **公平预算会顶破总预算**：第一版实测注入 1776 token > 预算 1500（超 18%）。原因是类别标题行
   （含 desc、「共 N 条」）没计入逐条循环。改为**先扣固定开销**（外层说明 + 每个类别头的真实成本）
   再轮转分条，实测 1475 ≤ 1500，五类标题全部在列。
2. **`test_external_change_still_blocked` 是条已废弃的旧用例**：它断言 edit_file 的 stale 检查会拦，
   但 `native.py:621-629` 明确注释已刻意移除该检查（同轮并行 edit 会互相误报、`run_command` 改文件后
   必误报），安全闸改为「old_string 唯一命中」。改为 `test_external_change_is_still_safe_via_unique_match`，
   锁「外部改动后 old_string 唯一命中仍能精确改 / 不唯一必须拦下」，并防止有人把 stale 检查加回来。

### 验证

- `pytest tests/test_resume_memory.py -q` → **26 passed**（含新增 4 个：三级淘汰、失效不注入、五类公平预算、取代自动标失效）
- `pytest -q`（全量）→ **638 passed, 4 skipped**（skip 全是 Windows 无符号链接权限）
- `npm run build`（tsc -b && vite build）→ 通过
- 真实数据核对：钉住的 7 条排在各类别最前面并带「（钉住）」，4 条失效条目在 `index_block` 输出里零泄漏，
  `index_block` 实际 1475 token ≤ 1500

### 未验证 / 已知边界

- **真实 LLM 是否稳定按新 `judge_prompt` 输出「取代：xxx」** 没有验证——需要真跑几轮 run 才知道。
  即使模型不输出，也只是「旧条不会自动失效」，不影响其它机制（可手动在界面点「标失效」兜底）。
- 淘汰是**写入时**触发的（`append` 后 `_trim`），存量文件已超 40 条时不会自动瘦身，要等下一条写进来。
- 同类别里「标失效」不做跨类别去重：一条经验同时更像 pitfall 和 convention 时仍需人工判断。
