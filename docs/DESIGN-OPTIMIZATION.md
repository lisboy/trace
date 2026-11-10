# GameAgent 优化设计文档（思考链 · 功能 · 界面 · 性能 · 问题解决效率 · 记忆系统）

> 版本 **v0.2** · 2026-11-09 · 状态：**v0.1 候选清单中部分 P0 已落地，本文标出实施状态与验证方式**
>
> 变更说明（v0.1 → v0.2）：
> - §1.1 思考尾部优先截断：**已落地**（`runtime.py::_tail_keep_head`，L88；调用点 L1086/L1438）。
> - §1.3 前端长思考折叠：**已落地**（`RunTraceInline.tsx::ThinkRowBase`，超长思考摘要只显前 200 字，细节点开看全文）。
> - §4.2 页面隐藏暂停轮询：**已落地**（`Chat.tsx` 4s 消息兜底轮询、`ContextTrace.tsx` 轨迹轮询在 `document.hidden` 时跳过，回前台立即补一次）。
> - §5.1 死循环签名归一化：**已落地**（`runtime.py::_sig_arg` L45 / `_call_signature` L81）。
> - §5.2 工具结果尾部优先：**已落地**（`runtime.py::_tail_keep_head` L88 + `_tail_with_sources` L102；`runs.py::_tail_head` L822，续跑摘要取尾部）。
> - **新增 §10 子 agent 并行处理**：`spawn_agents` 工具 + `sub` 只读子角色已实现，本节说明能力、边界与验证方式。
>
> 与既有文档的关系：
> - `DESIGN.md`：产品与架构设计（v0.3 团队协作版）。
> - `DESIGN-RESUME-MEMORY.md`：中断恢复摘要 + 记忆系统加层（已落地）。
> - `DESIGN-DESKTOP.md`：桌面端 pywebview 路线。
> - 本文：在以上基础上做的**增量优化**盘点，不推翻既有决策，只补短板；已实施项以 ✅ 标注并给出行号。

---

## 0. 结论速览（TL;DR）

| 维度 | 核心结论 |
|---|---|
| 思考链 | ✅ 截断方向已改尾部优先、前端长思考已折叠；仍缺思考结构化检查点 `[CP]` 与子角色思考区分。 |
| 功能 | 核心链路已闭环；✅ 子 agent 并行已落地；仍缺项目删除、会话批量管理、变更审批流等团队化后半程能力。 |
| 界面应用性 | ✅ 长思考折叠、页面隐藏停轮询已落地；仍缺轨迹窗口渲染、键盘快捷键、移动端适配。 |
| 性能 | ✅ 页面隐藏停轮询已落地；结构扫描缓存、列表分页、索引复查仍未做。 |
| 问题解决效率 | ✅ 死循环签名归一化、工具结果尾部优先已落地；计划检查点持久化、探针按文件维度仍未做。 |
| 记忆系统 | 双层记忆已落地；攒批融合、LIKE 跨会话检索、冲突标注、env.json 限额仍未做。 |
| 子 agent 并行 | ✅ `spawn_agents` + `sub` 只读角色已实现并可注册调用（§10），上限 6 个并发。 |

**优先级划分（更新后）**：
- **P0（快赢，剩余）**：§3.5 RunChanges 按需拉取、§4.1 结构扫描缓存、§6.1 记忆融合攒批与衰减、§6.5 env.json 列表键限额。
- **P1（中期）**：§1.2 思考检查点、§2.1 项目删除、§2.2 会话批量管理、§3.1 轨迹窗口渲染、§4.4 索引复查、§5.3 计划检查点持久化、§5.4 探针按文件维度、§6.2 LIKE 跨会话检索。
- **P2（长期）**：§2.3 变更审批流、§2.5 暂停/恢复、§3.2 移动端、§6.3 embedding 检索、§6.4 记忆冲突检测。

---

## 1. 思考链（Agent 推理过程）

### 现状
- `runtime.py::_make_thinking_streamer`（约 L888）：LLM 增量攒批（0.4s / 160 字符）以 `agent_thinking_delta` 实时推送；整轮完整版以 `agent_thinking` 落库，前端刷新后以完整版为准。
- ✅ **尾部优先截断已落地**：`_tail_keep_head(text, limit, head=80)`（`runtime.py` L88）统一「前 80 字锚点 + …[中略]… + 尾部主体」，思考落库调用点 L1086（主体轮）与 L1438（收尾轮），参数 `limit=4000`。
- ✅ **续跑摘要已取尾部**：`runs.py::_tail_head(s, limit, head=80)`（L822），`_findings_brief`（L879）对工具结果与思考均按尾部优先截断。
- ✅ **前端长思考已折叠**：`RunTraceInline.tsx::ThinkRowBase`（L646）——思考行摘要只显示前 200 字（`THINK_COLLAPSE = 200`），点开 `<details>` 风格面板看全文（`max-h-72 overflow-y-auto`），长思考不再把整段文字铺在轨迹里。
- 子智能体（sub/artist/voice/tester）的 thinking 同样落库并走同一截断；`_findings_brief` 目前未按角色区分。

### 问题（剩余）
1. **思考无结构化检查点**：整段 reasoning 是自由文本，续跑/打回时模型要自己重新梳理「已完成/待办/已确认事实」，容易遗漏或重复。
2. **子角色思考未区分**：`_findings_brief` 取最近 3 条 `agent_thinking` 时不带 `agent_key` 过滤，续跑可能把子 agent 的思考当成主 agent 的结论。

### 优化方案（剩余）
1. **思考检查点提示（P1）**：在 agent 的 system prompt 里要求模型在每轮 reasoning 结尾以固定格式输出一行状态：
   `[CP] 步骤:i/n | 已确认:... | 下一步:... | 障碍:...`。落库与续跑时 `_findings_brief` 只抽取 `[CP]` 行即可得到高密度检查点，比截 300 字更准。注意：该行仅用于内部恢复，前端照常展示。
2. **子角色思考纳入续跑（P2）**：`_findings_brief` 增加 `agent_key` 过滤与标注（「子agent 的结论/主 agent 的结论」），让续跑能区分来源。

### 风险
- ✅ 截断方向改动已完成，只对新 run 生效；历史数据不受影响。
- `[CP]` 格式依赖模型服从性，需要容忍「没写 CP 行」的情况，不能强校验导致打回。

---

## 2. 功能

### 现状
- 核心链路已闭环：一句话需求 → 策划推进 → 开发改代码 → 美术/配音子智能体 → 测试按清单验证 → 变更报告。
- 已有：多会话并发（按会话串行、跨会话并行）、插话（前后端幂等）、继续（注入已读/已改文件 + 事实清单）、运行中切换模型/推理等级、运行中改运行选项、媒体全局单队列。
- ✅ **子 agent 并行处理已落地**：见 §10。
- 缺失或薄弱：项目只能**归档/恢复**、不能删除；会话无批量操作；「每 run 一条 git 分支 + 人工合并审批」（DESIGN.md D12）在 lite 单机模式下尚未成形；运行中只能取消、不能暂停/恢复；无失败自动重试。

### 优化方案（剩余）
1. **项目删除（P1）**：新增 `DELETE /api/projects/{id}`（owner/admin），语义先「软删除/归档 + 可恢复」，数据清理分两步：先隐藏，7 天后或用户二次确认后再物理清理（项目目录、会话、记忆、素材）。前端 `SessionList` 增加删除入口（两段确认）。
2. **会话批量管理（P1）**：批量归档/删除会话、按项目清空会话；新增 `POST /api/sessions/batch`（`{ids, action}`）。
3. **变更审查流（P2）**：run 结束后若改动落盘，生成 `ChangeReport`（已有）并停在「待审查」，用户可点开 `DiffView`（已有）确认后「接受/放弃」。默认不阻断。
4. **失败自动重试（P2）**：对「模型 5xx / 网络超时 / 供应商限流」这类瞬时故障在 run 内自动重试 1 次（带退避），确定性错误不重试直接暴露给模型。
5. **暂停/恢复（P2）**：`cancel_requested` 已有通道，可扩展为 `pause_requested`（轮次边界挂起，保留 messages 状态），恢复时续跑。

### 风险
- 项目删除涉及多表与磁盘目录，必须软删除先行、物理清理异步且幂等；误删不可逆，两段确认 + 日志。
- 变更审查流会改变「发需求 → 自动跑完」的用户预期，默认应**不阻断**。
- 自动重试要严格限定瞬时故障，否则会把确定性错误放大成多倍 token 浪费。

---

## 3. 界面应用性（可用性）

### 现状
- 已修过：深色壁纸弹层可读性、图片容器统一 `object-contain`、消息刷新 4s 轮询 + 内容指纹、默认头像回退、工具入参 JSON 序列化、轨迹去重。
- ✅ **长思考折叠**：`RunTraceInline.tsx` 思考行摘要 ≤200 字 + 展开看全文。
- ✅ **页面隐藏停轮询**：`Chat.tsx` 消息兜底轮询与 `ContextTrace.tsx` 轨迹轮询在 `document.hidden` 时跳过本轮，`visibilitychange` 回前台立即补一次。
- 已知未修：`RunChanges` 每条历史 run 默认挂载、`RunTraceInline` 长轨迹全量渲染；无键盘快捷键；移动端未专门适配。

### 优化方案（剩余）
1. **轨迹窗口渲染（P1）**：`RunTraceInline` 按 `buildRows` 生成的行做「只渲染最近 N 行 + 折叠更早事件」，历史长 run 只渲染最近 50 行，再考虑真虚拟滚动。
2. **移动端 / 窄屏适配（P2）**：侧栏在 <768px 改为抽屉；工具参数表/表格横向滚动；顶栏操作收进菜单。验收按 375px 宽截图。
3. **键盘快捷键与完成通知（P0–P1）**：`Ctrl/Cmd+Enter` 发送、`Ctrl/Cmd+K` 搜索会话、`Esc` 关闭弹层（P0 快赢）；run 结束浏览器 Notification + 提示音（P1）。
4. **空状态 / 骨架屏 / 无障碍（P1）**：无会话、无素材、无日志时的空状态引导；列表加载骨架屏；按钮 aria-label、弹层焦点陷阱。
5. **`RunChanges` 按需拉取（P0）**：收起的历史 run 不请求 `GET /runs/{id}/changes`，改为展开时才拉取。

### 风险
- 虚拟滚动改动 UI 核心组件，回归成本高；先用「最近 N 行 + 折叠」的轻量方案验证收益。
- 浏览器通知受权限策略限制，仅作为增强，不能替代页面内提示。
- 移动端适配不要引入新的布局体系，沿用 Tailwind 断点即可。

---

## 4. 性能

### 现状（已做，保持）
- `/messages` 附图不内联，`meta.image_urls` 指向文件 URL 懒加载，实测 1,012,230 → 39,821 字节。
- 缩略图按「原图 mtime+size」版本化，`Cache-Control: max-age=0, must-revalidate` + ETag，透明 PNG 走 PNG 缩略图。
- 执行态 4s 轮询 + 内容指纹挡重渲染；`RunTraceInline` 运行中 3s 补拉事件。
- ✅ **页面隐藏停轮询**：`Chat.tsx`、`ContextTrace.tsx` 在 `document.hidden` 时跳过轮询，回前台立即补一次，省流量与 CPU。
- 会话消息查询避开 MySQL 对巨型行 filesort。

### 优化方案（剩余）
1. **结构扫描缓存（P0）**：`_structure_block`（orchestrator.py L1642）每个 run 都实时递归扫描工作区。改为：按工作区根目录的「mtime + 目录列表指纹」缓存结果，变化才重扫；提供「重新扫描」入口。排除 `.trace`、`node_modules`、`web/dist` 等噪音目录。
2. **列表/轨迹分页（P1）**：`GET /runs`、`GET /events`、`GET /sessions/{id}/messages` 明确分页参数与游标，前端不再一次拉全量历史事件。
3. **索引复查（P1）**：复查 `Event(run_id, type)`、`Event(run_id, created_at)`、`Message(session_id, created_at)`、`Run(session_id)`、`Run(project_id, finished_at)` 等高频查询的索引。
4. **轮询退避（P2）**：无活动时 4s → 15s 指数退避，有 running run 恢复 4s；WS 正常时不轮询只做兜底心跳。

### 风险
- 结构扫描缓存若失效逻辑有误，会导致模型看不到新文件；必须用「目录指纹」而非仅根 mtime（子目录变化 mtime 不一定变）。
- 轮询退避可能让用户感觉「刷新变慢」，需保证 running 状态立即回到高频。

---

## 5. 思考过程优化（问题解决效率）

### 现状（已做，保持）
- 守卫体系已较完整：死循环签名检测、探针刹车、创建类任务 D 收敛、看图不落字守卫、参数 JSON 重试、空收尾重问、计划清单打回、续跑注入已读/已改文件 + 事实清单。
- ✅ **死循环签名归一化已落地**：`runtime.py::_sig_arg`（L45）剥离数字/时间戳/随机段，绝对路径取 basename；`_call_signature`（L81）生成「工具名 + 归一化参数」签名。
- ✅ **工具结果尾部优先已落地**：`_tail_keep_head`（L88）与 `_tail_with_sources`（L102）统一尾部优先截断，并保证 `[Sources]` 来源块完整保留；续跑摘要 `runs.py::_tail_head`（L822）同样取尾部。
- 工具描述与系统提示已教「互不依赖的查询一次问全」（并行读）。

### 优化方案（剩余）
1. **计划检查点持久化（P1）**：当前 `PlanChecklist` 是运行态展示，续跑靠启发式。把「清单 + 每项状态 + 已完成证据」在每次状态变更时写入 `run.plan_json`（已有字段），续跑直接按检查点恢复，不再依赖从轨迹猜。
2. **探针刹车按文件维度（P1）**：`explore_calls` 目前是「自上次改产品代码以来」的全局只读计数，agent 换文件排查时会被误伤。改为按「当前正在排查的文件」维度计数：同文件连续 N 次只读才刹车，换了文件重置。
3. **刹车文案给下一步菜单（P1）**：探针刹车/看图守卫的文案从「只警告」升级为「警告 + 三选一（继续排查 / 我来改 / 换方案）」。
4. **推理等级按任务复杂度自适应（P2）**：首轮由模型或启发式判定任务复杂度，给一次「建议档位」提示，用户确认后生效；只做建议、不做强制。

### 风险
- ✅ 签名归一化已上线，注意保留「工具名 + 归一化参数」并设置连续 N 次阈值，避免过度归一化误判。
- 检查点写入 `plan_json` 会增大该字段，注意与现有运行选项读写兼容。
- 自适应档位若自动降档，可能让用户觉得「变笨」，只做建议、不做强制。

---

## 6. 记忆系统

### 现状（已做，保持）
- 双层记忆：
  - **DB `ProjectMemory`**（`project_memory.py`）：结构化条目（title/body/tags/kind），模型多对话融合，上限 12 条，注入标题索引 + `memory_read` 按需取全文；manual 条目不可改写且注入全文。
  - **工作区 `.trace/notes/*.md`**（`project_notes.py`）：decision/pitfall/convention/playbook/codemap 五类，收尾由 `judge()` 判是否值得沉淀，注入标题索引（预算 1500 tokens）。
  - **环境画像 `.trace/env.json`**（`envfacts.py`）：命令失败 sniff shell/unavailable/git，`save_env_fact` 存入口事实，注入「环境事实与入口事实」。
- 已解决：融合空 ctx 回退失效端点、fallback 追加不丢记忆、`_memory_input_text` 在汇报为空时用落地动作兜底。

### 问题（剩余）
1. **无衰减/无失效**：12 条自动记忆只按更新时间排序、融合时「以新为准」，但旧条目仍在注入索引里占据预算；过时结论没有显式失效标记。
2. **无跨会话检索**：`memory_read` 只查 `ProjectMemory`，查不到历史会话/消息里说过的原话；LIKE 版仍未做。
3. **融合无攒批**：每次 run 成功都调一次 LLM 融合（90s 超时、额外 token），小调整任务也融合。
4. **双写不一致**：同一事实可能同时出现在 `ProjectMemory` 与 `.trace/notes`，没有查重提醒。
5. **notes 无限增长**：五类文件各 40 条上限、无热度概念，注入预算按从新到旧取，可能把老的高价值条目挤出预算。

### 优化方案（剩余）
1. **攒批融合 + 衰减（P0）**：`_remember` 改为触发式——满足其一才融合：①距上次融合 ≥ N 次成功 run（如 3 次）；②本次结果与已有记忆冲突；③用户手动点「AI 融合」。其余 run 只把 `final_summary` 暂存到「待融合队列」，攒批一起融。自动条目加 `stale`/置信度：连续 M 次融合都没被保留的条目自动降权。
2. **LIKE 版跨会话检索（P1）**：新增 `search_history` 能力——`project_memory.py` 增加 `search_messages(project_id, q)`：对 `Message.content` 做 `LIKE %q%`（MySQL 可用 FULLTEXT ngram；SQLite 先 LIKE + 会话/时间过滤，limit 5）。embedding 仍不做。
3. **记忆冲突提示（P2）**：`memory_read` 返回条目时附 `updated_at`；融合 prompt 增加「若已有条目与本次结果冲突，明确标注 `[冲突]` 而不是静默覆盖」。
4. **notes 命中计数（P2）**：`.trace/notes` 条目加 `hits`（运行期内存统计，不必持久化），注入索引按「命中热度 + 时间」排序。
5. **env.json 列表键限额（P0）**：`unavailable` 等列表键设置上限（如 20 条）与「最近命中」更新，避免无限增长挤掉标量键。

### 风险
- 攒批融合延迟了跨会话可见性：关键结论必须仍走 `.trace/notes` 立即沉淀（现状已满足），DB 记忆只承担「项目现状」职责，延迟 1–3 个 run 可接受。
- 衰减/降权若误伤仍成立的老约定，会引入错误；只降权不删除，注入文案保留「可能过期」标注。
- LIKE 检索对大表会慢：先 limit 会话数 + 时间范围 + 只查最近 N 个 run 的消息，避免全表扫。

---

## 7. 实施路线建议

| 阶段 | 内容 | 验收方式 |
|---|---|---|
| ✅ 已落地（本次 v0.2 前完成） | §1.1 截断尾部优先、§1.3 思考折叠、§4.2 页面隐藏停轮询、§5.1 签名归一化、§5.2 工具结果尾部优先、§10 子 agent 并行 | 前端构建通过；后端导入检查通过；临时端口截图；`spawn_agents` 注册与白名单检查 |
| 第 0 批（P0 快赢，剩余） | §3.5 RunChanges 按需拉取、§4.1 结构扫描缓存、§6.1 攒批融合、§6.5 env.json 限额 | 每项带单测/截图/字节数对比；跑既有回归 |
| 第 1 批（P1） | §1.2 检查点提示、§2.1 项目删除、§2.2 会话批量、§3.1 轨迹窗口渲染、§4.3 分页、§4.4 索引复查、§5.3 计划检查点持久化、§5.4 探针按文件维度、§6.2 LIKE 检索 | 端到端：多会话并发 + 长轨迹会话 + 项目删除恢复 + 续跑恢复 |
| 第 2 批（P2） | §2.3 变更审查流、§2.4 自动重试、§2.5 暂停/恢复、§3.2 移动端、§4.5 轮询退避、§5.6 档位自适应、§6.3 冲突提示、§6.4 notes 命中计数 | 按 DESIGN.md G 系列目标补测 + 真机窄屏截图 + 成本对比 |

---

## 8. 不做清单（明确排除，避免返工）
- **不做 embedding 语义检索**：在 LIKE 版与笔记命中计数验证收益前不引入（同 DESIGN-RESUME-MEMORY 结论）。
- **不做 SaaS 计费/订阅、K8s Operator、移动端原生 App、实时协同编辑**：与 DESIGN.md §1.2 一致。
- **不推翻双模式接口收敛原则**：所有优化不得在业务代码里出现 `if mode == "lite"`。
- **不把「审查流」设为默认强流程**：默认继续「自动跑完」，审查是可选增强。

---

## 9. 风险汇总

| 风险 | 影响 | 对策 |
|---|---|---|
| 思考截断改方向（已落地） | 续跑/展示回归 | 只对新 run 生效；`_findings_brief` 同步用 `_tail_head` |
| 结构扫描缓存失效 | 模型看不到新文件 | 目录指纹 + 手动重扫入口 + 排除噪音目录 |
| 记忆攒批延迟可见性 | 跨会话结论滞后 | 关键结论仍走 `.trace/notes` 即时沉淀；冲突触发即时融合 |
| 项目删除误删 | 不可逆 | 软删除两步走 + 二次确认 + 审计日志 |
| 轨迹窗口渲染 | 行高不定、媒体行错位 | 先做「最近 N 行 + 折叠」，再做虚拟滚动 |
| 自动重试放大成本 | 确定性错误被反复重试 | 只重试瞬时故障，分类白名单 |
| 自适应降档体验 | 用户觉得变笨 | 只建议不强制，保留用户显式档位 |
| 子 agent 并发放大 LLM 调用 | token 成本上升 | 上限 6 个、只读白名单、递归禁止（§10） |

---

## 10. 子 agent 并行处理（✅ 已落地）

### 能力
- **工具**：`spawn_agents`（`game_agent/agents/tools/spawn.py`），主 agent 把互不依赖的调研/核对拆成多个只读子 agent 并行执行，汇总结论返回。
- **入参**：`items: [{name, question, note?}]`，一次最多 `MAX_SUBAGENTS = 6` 个（`spawn.py` L17），单个问题上限 2000 字。
- **并发方式**：`asyncio.gather(*(_one(i, it) for i, it in enumerate(items)))`（`spawn.py` L54）——真并行发派，单个子 agent 失败不影响其他并行任务，失败项返回 `执行出错：...` 文本而不是整体崩掉。

### 子 agent 角色（`roles.py`）
- `sub` 白名单（L44–49）：只读工具 `read_file / list_dir / glob / grep / load_skill / search_history / memory_read / parse_document / web_search / web_fetch / view_image / view_images` + `report_to_user`。
- **只读不改**：没有写文件/跑命令/生成媒体工具，天然无写锁冲突，可安全并发。
- **递归禁止**：`sub` 白名单里没有 `spawn_agents`，派不出孙 agent。
- **系统提示词**（`roles.py` L242–248）：一次只核对一个问题、只读、结论具体可引用（路径/行号/数值/原文摘录）、输出 ≤400 字 Markdown、不知道就说不知道。

### 复用与记账
- 复用 `Orchestrator.run_agent`（`orchestrator.py` L730）：子 agent 走同一套 LLM 调用 / 取消看门狗 / task 记账，创建为 `kind=role="sub"` 的嵌套 task（`parent_task_id = 主 task`）。
- 前端轨迹按 `agent_key` 展示「子agent」标签（`ROLE_LABELS["sub"] = "子agent"`），思考行/工具行用 `RoleTag` 区分来源。
- 取消联动：`run_agent` 内部的 `_watch_cancel` 对嵌套 task 同样生效，用户点取消会连子 agent 一起掐掉。

### 边界与风险
- 子 agent 只读，**不能**让它写代码/跑命令/出图出音频——需要动手时主 agent 必须自己来。
- 上限 6 个并发，单次派发超出的 item 会被截断（`items[:MAX_SUBAGENTS]`）。
- 并发会放大 LLM 调用量：只应拆「互不依赖的核对/调研」，不要把串行依赖的任务拆开（子 agent 拿不到前一步结果）。
- 子 agent 结论是「返回文本」而非工具轨迹沉淀，关键结论由主 agent 复核后决定是否写进 `.trace/notes` 或 `ProjectMemory`。

### 验证方式
- 后端：`python -c "from game_agent.agents.tools.spawn import spawn_agents, MAX_SUBAGENTS; from game_agent.agents.roles import ROLE_TOOLS; print(MAX_SUBAGENTS, 'spawn_agents' in ROLE_TOOLS['main'], 'spawn_agents' in ROLE_TOOLS['sub'])"` 期望输出 `6 True False`。
- 功能：对话里让模型「并行核对 A/B/C 三个文件的某一点」，观察轨迹出现 3 条 `子agent` 行且并行完成、最终汇成一条结论。
- 成本：确认一次派发 ≤6 个子 agent、子 agent 只调用白名单内只读工具（轨迹里不应出现 `write_file`/`run_command`）。

---

## 11. 如何验证（本次已实施项 + 后续实施项通用）

### 通用原则
1. **不要直接重启生产/长驻服务**：验证用临时端口（如 9998）另起实例，验收完 `stop_service` 收掉，不动正在跑的任务。
2. 后端改动：先 `python -m compileall` 或单测，再临时端口起服务截图/接口验证。
3. 前端改动：`npm --prefix frontend run build` 通过后，由后端托管 `web/dist`，起临时端口截图验收。
4. 数据/行为类改动：给最小复现（临时端口 + demo 账号 + 一条具体需求），看轨迹/截图/接口返回。

### 本次 v0.2 的验证清单
| 项 | 验证动作 | 通过标准 |
|---|---|---|
| §1.1 思考尾部截断 | 跑一条会产生长思考的任务，查 `agent_thinking` 事件正文 | 超 4000 字时正文是「前 80 字 + …[中略]… + 尾部」 |
| §1.3 思考折叠 | 前端轨迹看长思考行 | 摘要 ≤200 字，点开才显示全文 |
| §4.2 页面隐藏停轮询 | 浏览器 Network 面板切到后台 | `hidden` 时无 `/messages`、`/runs/.../transcript` 请求；回前台立即补一次 |
| §5.1 签名归一化 | 连续用带时间戳参数的同一工具 | 签名一致，触发循环守卫 |
| §5.2 工具结果尾部优先 | 超长工具结果在轨迹里 | 显示「前 80 字 + …[中略]… + 尾部」，`[Sources]` 完整 |
| §10 子 agent 并行 | 对话派发 `spawn_agents` 核对多个文件 | 轨迹出现多条「子agent」行、并行完成、结论汇总 |
| §1.1/§5.1/§5.2 单测 | `pytest tests/test_tail_and_sig.py tests/test_spawn_agents.py -q` | 15 passed（截断尾部优先/签名归一化/并行派发/失败隔离/上限截断） |

### 后续实施项验证（按路线图）
- **P0 剩余项**：结构扫描缓存（改文件后目录指纹变化才重扫，字节数/耗时对比）、记忆攒批（连续 N 个成功 run 只融合一次，`test_resume_memory` 回归）、RunChanges 按需拉取（收起时 Network 无 `/changes` 请求）。
- **P1**：项目删除（软删→可恢复→二次确认物理删）、会话批量（多选操作）、轨迹窗口渲染（长 run 首屏只渲染最近 N 行）、计划检查点持久化（中断续跑清单完整恢复）、LIKE 检索（`search_history` 能搜到用户原话）。
- **P2**：审查流（改动清单 + 接受/回滚）、自动重试（瞬时故障重试 1 次、确定性错误不重试）、暂停/恢复、移动端 375px 截图。

### 回归基线
- 既有单测：`tests/` 下 `test_resume_memory`、`test_efficiency_guards`、`test_audio_*`、`test_context_digest`、`test_concurrency_and_sources` 等应保持通过（其中文档测试 6 个既有失败不算回归，见 pitfalls）。
- 新增单测：`tests/test_tail_and_sig.py`（10 条，`_tail_keep_head` 尾部优先 + `_sig_arg` 签名归一化）、`tests/test_spawn_agents.py`（5 条，`spawn_agents` 并行派发/失败隔离/上限截断）。
- 前端：`npm --prefix frontend run build` 零 TS 错误。
