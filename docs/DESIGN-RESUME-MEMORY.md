# 设计文档：中断恢复摘要 + 快赢修复 + 记忆系统加层

> 背景：2026-10-28 会话「首页为啥还有滚动条」两个 run 共 31 分钟，其中约 19 分钟是
> 可避免浪费（中断重做 ~8min、登录受挫 ~6min、edit 自伤 ~3min、环境试探 ~2min）。
> 根因在 Run 1 第 7 分钟已定位，但被服务重启打断后全部丢失重来。
> 本文档给出四组改动的函数级设计。实施前请以当前代码为准核对行号。

---

## 改动一：edit_file / write_file 的 stale 自愈（快赢，平台缺陷）

### 现状
- `agents/tools/__init__.py:46-48`：`ToolContext.read_set / read_stamp: dict[str, tuple[float,int]]`，run 级生命周期。
- `agents/tools/native.py:283,308`：edit/write 前校验 `read_stamp[key]` 与磁盘 stat 是否一致，
  不一致即报「文件在你读过之后已经被改过」。
- 问题：同一 run 内 **agent 自己上一次 edit 成功落盘后**，read_stamp 还是旧值，
  下一次 edit 必然撞 stale。本次会话连续触发 3 次，每轮浪费 re-read + 重试 2-3 轮工具循环。

### 改法
在 `native.py` 中 edit_file / write_file **成功保存之后**（写盘分支 return 前），用落盘后的最新 stat
刷新 read_stamp，使「自己刚写的」等价于「自己刚读的」：

```python
# 伪代码，加在保存成功、返回成功消息之前
try:
    st = path.stat()
    t.read_stamp[key] = (st.st_mtime, st.st_size)
    t.read_set.add(key)          # 写过即视为已知内容
except OSError:
    pass
```

注意：跨 run 的外部修改（如用户手工改文件）仍然会被拦截——read_stamp 是 run 级的，行为不变。

### 验证
单测：read → edit → edit 连续两次，第二次不再报 stale；run 外修改文件后 edit 仍报 stale。

---

## 改动二：视觉回灌瘦身（快赢，参数调整）

### 现状
- `agents/tools/service.py:263-277` `_to_data_url(png, max_width=1280)`：已有 PIL 缩图 + JPEG q82，
  原图留在磁盘。轨迹里「图片已附（1418KB）」显示的是磁盘 PNG 大小，回灌已被压缩。
- 剩余浪费：1280 宽对「判断布局是否溢出」仍偏大；同一验证循环里反复 view_image 相似截图；
  agent 倾向于看图而不是先看 eval 数字。

### 改法（三处小改）
1. `_to_data_url` 默认 `max_width: 1024`，quality 82→78（UI 截图实测可读性足够）。
2. `view_image`（`service.py:311` 起）返回文案追加一行节流提示：
   「本图与上一张查看间隔 < N 次工具调用且路径相同 → 提示优先用 eval 数字指标」。
   实现：`ToolContext` 加 `last_view: dict[str, int] = field(default_factory=dict)`（key=路径，value=工具调用计数）。
3. `game_agent/agents/prompts` 下工具使用说明（playbook 类提示词）补一句：
   「布局类验证先 eval 拿数字（scrollH/clientH/rect），截图只用于最终视觉确认」。

### 验证
对比改造前后同一验证循环的回灌字节数（预期单图 base64 从 ~300KB 级降到 ~180KB 级）。

---

## 改动三：中断恢复摘要（`continue_run` 注入「已确认事实」）

### 现状
- `api/routers/runs.py:504-640` `continue_run`：brief_parts 已含 原文/图片/附件/错误/计划/已改文件/
  素材/结论/最后 8 个动作（只列工具名+参数摘要），**不含任何 tool_result 内容**——
  Run 1 的测量结果（scrollH 1029 > 911、根因结论）因此全部丢失。

### 改法
`runs.py` 新增 `_findings_brief(session: Session, sf) -> str`，在 continue_run 的 brief_parts 中、
「上次最后的动作」之前插入：

```python
async def _findings_brief(sf, run_id) -> str:
    """从被中断 run 的轨迹里启发式抽取关键事实，无需 LLM、无额外延迟。"""
    # 1) 取该 run 最后 ~40 条 type="tool_result" 事件（Event 表，倒序）
    #    过滤：空结果、<40 字符的跳过；每条截前 200 字，加时间戳与工具名前缀
    # 2) 取最后 ~3 条 type="agent_thinking" 事件，每条截前 300 字
    #    （agent_thinking 落库，内含模型自己的阶段性结论，如「根因是 hero-stage 固定高度」）
    # 3) 顺序 = 时间正序；总长超过 ~1200 字时仅保留最后 15 条 tool_result + 3 条 thinking
    # 4) 返回分节文本：
    #    「上次已确认的事实与测量（从中断处往前）」「上次的阶段性推理」
```

- 组装处：`continue_run` 内
  `acts = await _last_actions(...)` 附近调用，`brief_parts.append(_findings_brief(...))`。
- 事件查询复用 `runs.py` 现有按 run_id 拉 Event 的模式（与 `_last_actions` 同款查询），
  不新增表、不新增列。
- 不做 LLM 压缩（中断恢复要快）；启发式截断已足够，agent 自会甄别。

### 风险与边界
- run 特别长时 tool_result 巨多 → 已用「只取最后 40 条 + 总长截断」兜底。
- thinking 里可能混有错误结论 → 文案标注「阶段性推理（未必正确，需复核）」。

### 验证
造一个含 eval 输出与 agent_thinking 的 run，强制 failed 后调 continue，检查注入块内容与顺序。

---

## 改动四：记忆系统加层（不动现有四文件机制）

### 现状
- `orchestration/project_notes.py`：四类固定文件（conventions/decisions/pitfalls/playbook），
  已有 `append / list_entries / index_titles() / read_block(budget_tokens) / judge_prompt`。
- 收尾沉淀：`orchestrator.py:465` `_remember_if_dirty` 只在「非成功终态但已落地改动」时补记，
  **无法挽回中断 run 的过程性事实**；notes 的写入走 `_harvest_notes`（run 收尾判定）。
- 注入侧：`orchestrator.py:1134 _load_context`（run 开始组装上下文）。
  本次会话中 agent 仍需自己 read 4 个 notes 全文、自己重新踩环境坑。

### 新增 L0：环境画像 `.agent/env.json`（平台自动维护，agent 不可见文件格式）
数据结构（上限 40 键，超过按时间丢最旧）：

```json
{
  "shell": "cmd",
  "git": false,
  "powershell_wrapping": "cmd /c 或 powershell -NoProfile -Command",
  "service_start": "start_service 工具，端口 5174",
  "test_account": {"login": "xxx", "note": "见 playbook"},
  "unavailable": ["Get-NetTCPConnection(直接跑)", "Get-ChildItem(直接跑)"]
}
```

- **写入**：`native.py` `run_command` 的错误分支里加启发式捕获（纯函数 `_sniff_env_facts(cmd, stderr, exit)`）：
  - 匹配「不是内部或外部命令」→ 记 shell=cmd + 该命令进 unavailable
  - git 命令 exit=128 / "not a git repository" → git=false
  - 写入去重：已有同键不覆盖、unavailable 用集合追加。
- **读取/注入**：`_load_context`（或系统提示词组装处，实施时确认具体函数）追加一块
  「环境事实（来自 env.json，直接相信，不要再试探）」，逐键一行。
- `env.json` 进 `ensure_ignored`（project_notes.py:68 已有 .agent 忽略机制，确认覆盖）。

### 新增 L1：代码地图 `.agent/notes/codemap.md`（第 5 类）
- `project_notes.py:31` `CATEGORIES` 增加一类：
  `"codemap": ("codemap.md", "代码地图", "什么结构在哪个文件哪一段（含行号锚点）")`。
  现有 append/index/read_block/trim 机制自动获得该类，无需新逻辑。
- **写入**：`judge_prompt`（project_notes.py:302）的判定说明里加一类引导：
  「若本次 run 摸清了此前未记录的结构位置（如：布局骨架在 style.css 200-244、home-grid 在 394），
  输出 codemap 条目」。行号允许漂移，条目写「区域描述 + 大致锚点」。
- **注入**：run 开始时只注入 `index_titles()` 生成的**标题索引**（每条一行），
  不注入正文；agent 需要时按标题 read。这同时解决 L2 问题（见下）。

### 优化 L2：notes 由「agent 自己全文读」改为「索引注入 + 按需读」
- `_load_context` 组装时调用现成的 `project_notes.index_titles(workspace)`，
  把四类+codemap 的条目标题（每条一行，含分类名）注入系统提示词，
  并注明「需要某条详情时用 read_file 读对应文件」。
- 不改变 `_harvest_notes` 收尾判定流程。

### 风险与边界
- CATEGORIES 加类会影响 `list_entries/delete_entry/read_block` 的遍历——均为按 dict 遍历，天然兼容；
  需跑一遍现有 notes 相关测试确认无硬编码「4 类」假设。
- env.json 键值来自错误信息启发式，允许脏值；文案「直接相信」仅限已记录键。
- index_titles 注入量：现有四文件条目数 × 每条一行，超预算时按 `read_block` 同款 budget 截断。

### 验证
- 单测：CATEGORIES 第 5 类的 append/index/read/trim/delete。
- 手工：在 kpgame 工作区跑一个 run，确认 env.json 捕获 cmd/git 事实、
  下一个 run 的系统提示词含「环境事实」块与 notes 索引。

---

## 实施顺序建议

1. 改动一（edit 自愈）——最小、独立、立竿见影。
2. 改动三（中断恢复摘要）——本次最大浪费的直接止血。
3. 改动四（L0 → L2 → L1，按此顺序）——L0 一天內见效，L1 依赖收尾判定文案。
4. 改动二（视觉瘦身）——参数级，随手带上。

## 不在本期范围（记录备查）
- 登录态固化（测试账号写入 .agent + playbook 记录登录方法）——可并入改动四的 env.json 键，建议实施时顺带。
- search_history 跨会话检索、embedding 检索——等 L0/L1 验证收益后再评估。

---

## 实施状态（2026-10-29 补）

已落地，全部有回归测试钉住（`tests/test_resume_memory.py`、`tests/test_efficiency_guards.py`）：
- 改动一 stale 自愈：`_restamp_after_write`（write/edit 落盘后刷新 run 内指纹）。
- 改动二 视觉瘦身：`_to_data_url` 1024/q78 + `view_image`/`screenshot_page` 同图重看节流提示。
- 改动三 中断恢复摘要：`runs.py::_findings_brief` 在续跑上下文里带回「已量到的事实 + 阶段性推理」。
- 改动四 记忆加层：L0 `.agent/env.json`（`envfacts.py`，命令失败自动 sniff shell/不可用命令/git）、
  L1 `codemap` 第 5 类、L2 notes 改标题索引注入。

上面「不在本期范围」的第一条已顺带做完（**改动七：入口事实固化**）：
- 新工具 `save_env_fact(key, value, force)` —— 测试账号、登录入口、页面路径这类
  只有试过才知道的事实，一次确认就写进 `.agent/env.json`；默认不覆盖已有值，`force=true` 才订正。
- `start_service` 就绪后自动记 `service_start` / `service_url`（起得来的命令是最强证据）。
- 注入块标题改为「环境事实与入口事实」，正文明确要求：已有入口事实就直接照用，
  不要换端口重试、不要翻代码找账号；本轮新试通的用 `save_env_fact` 记一条。

另外从同一次会话的量出来的两处补充（也属于「少烧无效调用」）：
- **改动五**：疑问式故障描述（「首页为啥还有滚动条」= 疑问词 + 问题还在）现在同样触发
  「必须动手」守卫与「只排查不动手」刹车；用户明说「只分析/别改代码」时两道守卫都放行。
- **改动六**：`read_file`/`grep`/`glob`/`list_dir` 的工具描述里教模型「互不依赖的一次问全」
  （同一批调用由 runtime 并行执行），系统提示词里也加了同一条。挂在工具描述是因为
  系统提示会被上下文压缩挤掉。

第二条（跨会话检索 / embedding）**仍未做**，理由不变：先让 L0/L1 与入口事实在真实 run 里
证明自己。真要跨会话查旧话，先加 LIKE 版（约 30 行），embedding 需要 MySQL FULLTEXT ngram
与 sqlite fts5/trigram 两套实现，收益未证实前不值得。
