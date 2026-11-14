# GameAgent 设计方案（团队协作型多角色游戏开发智能体）

> 版本 **v0.3.1** · 2026-10-25 · 状态：**设计定稿（第四轮决策已并入），待编码**
>
> 一句话：**一个可多人在线协作的服务端系统——团队成员共享同一个游戏项目工作区，每个需求由「项目组长」自己读代码、改代码、起服务截图验收，只在出图/出音频时派给「美术」「配音」，最终以「可审查的 git 分支 + 素材 + 变更清单」交付。**

> **v0.3 是一次架构级转向**：从 v0.2 的「单机单人、单进程、SQLite」改为「**团队多用户、API 与执行分离、PostgreSQL + Redis**」。
> 被推翻的旧决策见 §0.2；保住的核心资产见 §0.3。

---

## 0. 已确认决策

### 0.1 决策表

| # | 议题 | 决定 | 影响章节 |
|---|---|---|---|
| D1 | 试点项目技术栈 | **Python**（探测/构建/测试全按 Python） | §7.10 |
| D2 | 编码执行方式 | **先原生模式**（fs+shell），CLI 后置 | §7.3 |
| D3 | 素材供应商 | **只做配置口，不预置 key**，零 key 走占位素材 | §7.8 / §7.9 |
| D4 | 前端 | React 18 + TS + Vite + Tailwind | §11 |
| D5 | 实施节奏 | 先定稿设计，编码待启动 | §16 |
| D6 | 数据目录/端口 | 默认 `~/.trace`、`127.0.0.1:8765`（lite 模式） | §13 |
| D7 | Python 环境 | **独立 venv**，不碰 conda base | §15.2 |
| D8 | 交付形态 | 可打包运行 + 一键打包 + 启停脚本 + 部署文档 | §15 |
| **D9** | **产品形态** | **团队协作型**：多用户共享同一项目工作区，服务端部署 | §2 / §3 / §6 |
| **D10** | **运行模式** | **双模式**：`lite`（单机 SQLite，1–3 人）+ `server`（Postgres+Redis+Docker，团队生产） | §2.1 |
| **D11** | **数据库** | **PostgreSQL 15+**（生产）/ SQLite（lite），Alembic 管理迁移 | §9 / §15.6 |
| **D12** | **并发策略** | **项目级读写锁 + run 串行排队** + **每 run 一条 git 分支** | §6 |
| **D13** | **部署目标** | **双轨并行**：Linux + Docker（server，主力）+ Windows 单机包（lite，同期交付） | §15 |
| **D14** | **团队规模** | **3–10 人**（单机 compose 足够，不引入 K8s） | §2.5 / §15.2 |
| **D15** | **代码托管** | **本地 git**（无远端为主），合并走本地 merge + 内置评审 | §5.3 / §15.7 |
| **D16** | **示例项目** | **要**：`examples/cardgame/` 最小 Python 卡牌游戏，M4–M6 验收基准 | §16 |
| **D17** | **单点登录** | **要 OIDC**（通用 OIDC + Entra/Google/飞书/钉钉预置），M1 落地，与密码登录共存 | §3.4.1 |

### 0.2 被 v0.3 推翻的 v0.2 决策（明确记录，避免误读旧文档）

| 旧决策（v0.2） | 为什么必须推翻 |
|---|---|
| 「不做账号体系（单机单人）」 | D9：多人共享工作区，必须有身份、权限、归属 |
| 「单进程 asyncio，不用多进程」 | 多人并发 → API 需多副本、执行需 worker 池；通信走 Redis 而非内存 |
| 「SQLite（WAL），路径 `~/.trace`」 | 仅保留为 lite 模式；生产用 PostgreSQL |
| 「不做云端多租户」 | 改为「团队内多租户（Team 隔离）」，不做 SaaS 计费 |
| 「工作区直接在本地目录改」 | 团队环境直接改主干 = 事故；改为**每 run 独立 git 分支 + 人工合并审批** |
| 「事件流内存扇出」 | 多副本 API 无法共享内存 → Redis Streams/PubSub + DB 补拉 |

### 0.3 保住的核心资产（v0.3 继续沿用）

五个 AI 角色的职责与 prompt 骨架、**agent-as-a-tool 协同模型**、工单/功能点清单（Work Order / Checklist）、能力清单注入、Art Bible + Character Sheet、测试回环、变更报告、**占位素材模式**、**驱动注册表 + schema 驱动表单**、原生开发模式的硬约束（未读不许写 / 唯一命中替换 / 交差前自检）。

---

## 目录

0. [已确认决策](#0-已确认决策)
1. [目标与非目标](#1-目标与非目标)
2. [系统形态与服务拓扑](#2-系统形态与服务拓扑)
3. [多租户与权限模型](#3-多租户与权限模型)
4. [智能体角色](#4-智能体角色)
5. [一次任务的全链路](#5-一次任务的全链路)
6. [并发与工作区一致性](#6-并发与工作区一致性) ← **团队化的核心难点**
7. [核心机制](#7-核心机制)
8. [实时协作与事件总线](#8-实时协作与事件总线)
9. [数据模型](#9-数据模型)
10. [后端 API](#10-后端-api)
11. [前端界面](#11-前端界面)
12. [目录结构](#12-目录结构)
13. [配置与环境变量](#13-配置与环境变量)
14. [安全](#14-安全)
15. [打包、部署与运维](#15-打包部署与运维)
16. [里程碑与验收](#16-里程碑与验收)
17. [风险与对策](#17-风险与对策)
18. [已确认事项与遗留问题](#18-已确认事项与遗留问题)

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 | 验收方式 |
|---|---|---|
| G1 | 服务端部署，团队成员浏览器访问 | `docker compose up -d` → `https://ga.内网域名` 可登录 |
| G2 | **多人共享同一项目工作区**，互不覆盖 | 两人同时发需求 → 一个执行、一个排队；各产出独立分支 |
| G3 | 5 个 AI 角色各自绑定不同大模型 | 团队管理员配置，成员直接复用 |
| G4 | 用户选项目 → 发一句话 → 策划经理自主推进 | 对话窗口看到完整工作过程流 |
| G5 | 开发角色真改代码（原生模式为主，CLI 为增强） | 产出 git 分支 + 改动明细 |
| G6 | 缺图 → 唤起美术（提示词+参考图+画风）→ 回填继续 | 时间线里嵌套 `request_art` |
| G7 | 缺音频 → 开发先自判 → 不行才唤起配音 | 能力清单驱动 |
| G8 | 测试按功能点清单逐条验证，通过才收工 | checklist 形式 pass/fail + 证据 |
| G9 | 策划经理汇报：完成情况 + 改动文件 + 改了什么 | 结构化 `ChangeReport` |
| G10 | 零供应商 key 也能跑通全链路（占位素材） | 标「占位」，配 key 后一键重画，代码零改动 |
| G11 | 能打包分发、一键启停、升级回滚 | 脚本 + `docs/DEPLOY.md` |
| **G12** | **权限隔离**：成员不能改配置/停别人的任务/看别的团队 | RBAC 矩阵全覆盖测试 |
| **G13** | **可审计**：谁在什么时候改了什么、花了多少 | `audit_logs` + `usage_ledger` + 用量看板 |
| **G14** | **实时协作**：多人看同一条执行流、评论、审批 | presence + 评论 + 合并审批 |

### 1.2 非目标（v0.3 明确不做）

- **不做 SaaS 计费/订阅**：只做用量统计与配额限制，不接支付。
- 不做 K8s Operator / 自动弹性伸缩（给 HPA 提示，不做 Operator）。
- 不做移动端原生 App（响应式 Web 即可）。
- 不做「多团队之间共享模型档位」的复杂授权（团队级隔离，够用）。
- 不自研模型、不预置任何供应商 key、不内置免费额度代理。
- 不做实时协同编辑（同一文件的多人同时手改）——代码编辑仍由 AI 串行进行。
- 不支持非 git 项目的团队模式（lite 模式可以）。

### 1.3 两个容易混淆的「角色」

| 概念 | 数量 | 说明 | 章节 |
|---|---|---|---|
| **人类角色**（RBAC） | 4 种：`owner` / `admin` / `developer` / `viewer` | 团队成员在团队中的权限等级 | §3.2 |
| **智能体角色**（Agent） | 5 个：`planner` / `developer` / `artist` / `voice` / `tester` | 由 LLM 驱动、各绑一套模型配置的 AI 工作者 | §4 |

后文「角色」若无前缀，指智能体角色；人类用「成员」。

---

## 2. 系统形态与服务拓扑

### 2.1 两种运行模式（同一套代码）

| | `lite` 模式 | `server` 模式 |
|---|---|---|
| 适用 | 1–3 人试用、演示、离线笔记本 | 团队生产（3–50 人） |
| 部署 | 单个 zip，`start.cmd` / `start.sh` | Docker Compose（或 K8s） |
| 进程 | 1 个（API + 执行同进程） | API ×N + worker ×M + 调度器 |
| 数据库 | SQLite（WAL） | PostgreSQL 15+ |
| 事件总线 | 进程内 asyncio EventBus | Redis Streams + PubSub |
| 队列 | 进程内 asyncio 队列 | PostgreSQL `FOR UPDATE SKIP LOCKED` |
| 锁 | 进程内 `asyncio.Lock` | Redis 锁 + DB 记账 + fencing token |
| 文件存储 | 本地磁盘 | 本地/共享卷 + S3/MinIO（可选） |
| 多用户 | 有账号，但不建议多人同时用 | 完整支持 |
| 需要 Docker | ❌ | ✅ |
| 上线速度 | 5 分钟 | 30 分钟 |

> **设计约束**：所有跨模式差异收敛在 5 个接口后面——`Repository`、`EventBus`、`Queue`、`LockManager`、`ObjectStore`。业务代码（agent/tool/orchestration）**只依赖接口**，不出现 `if mode == "lite"`。这是双模式能长期维护的唯一前提。

### 2.2 server 模式拓扑

```
                          ┌─────────────────────┐
  浏览器 / CLI ──HTTPS──► │  Caddy / Nginx      │  TLS 终止 + 反代 + 静态缓存
                          │  (gzip, rate limit) │
                          └──────────┬──────────┘
                                     │
                ┌────────────────────┼────────────────────┐
                │                    │                    │
        ┌───────▼──────┐    ┌────────▼───────┐   ┌────────▼───────┐
        │ api-1        │    │ api-2          │   │ api-N          │  ← 无状态，可水平扩
        │ FastAPI      │    │ FastAPI        │   │ FastAPI        │
        │ + WebSocket  │    │ + WebSocket    │   │ + WebSocket    │
        └───┬──────┬───┘    └───────┬────────┘   └───────┬────────┘
            │      │                │                    │
            │      └────────┬───────┴────────────────────┘
            │               │
    ┌───────▼────────┐  ┌───▼──────────────────┐  ┌──────────────┐
    │ PostgreSQL 15  │  │ Redis 7              │  │ MinIO / S3   │
    │ 主数据 + 队列   │  │ 锁 / PubSub /限流/在线│  │ 素材归档      │
    │ + 审计 + 用量   │  │ Streams              │  │              │
    └───────┬────────┘  └───┬──────────────────┘  └──────▲───────┘
            │               │                            │
            │   ┌───────────┴────────────┐               │
            │   │                        │               │
    ┌───────▼───▼────┐      ┌────────────▼────────┐      │
    │ worker-1       │      │ worker-M            │──────┘
    │ Orchestrator   │      │ Orchestrator        │
    │ 5×AgentRuntime │      │ 5×AgentRuntime      │
    └───────┬────────┘      └──────────┬──────────┘
            │                          │
            └──────────┬───────────────┘
                       │ 共享挂载（NFS / CephFS / RWX PVC）
              ┌────────▼─────────┐
              │  PROJECTS_ROOT   │  ← ★ 所有 worker 必须看到同一份工作区
              │ /data/projects/  │
              └──────────────────┘
```

**关键约束（部署时必须满足，否则会静默出错）**：所有 worker 必须挂载**同一份** `PROJECTS_ROOT`。若 worker 分布在不同机器，必须是共享文件系统（NFS v4 / CephFS / EFS / Azure Files / RWX PVC）。否则 run 在 worker-1 改的代码，worker-2 看不到 → 分支状态错乱。§15.4 有展开说明与替代方案（worker 亲和）。

### 2.3 为什么 API 与执行必须分离

v0.2 的「单进程 + agent-as-tool」在单机是对的：嵌套调用同步、事件直连前端。多人场景下它有三处硬伤：

1. **API 要水平扩，执行不能跟着扩**：WebSocket 连接是长连接、内存吃满；而 agent run 是 CPU/IO 长任务，两者扩缩容节奏完全相反。挤在一个进程里，一个人跑 30 分钟的 run 会拖慢所有人刷页面。
2. **进程崩溃会带走正在跑的 run**：API 重启（发版、OOM）会让全部在途 run 陪葬。分离后 run 在 worker，worker 重启可从 DB 恢复（状态机 + 幂等）。
3. **内存事件总线不能跨副本**：多人要看同一条流，必须有一个共享的事件通道（Redis）。

但 **agent-as-tool 保留**：它在 worker 进程内部依然是同步嵌套调用。只是 run 的入口从「HTTP 请求」变成「队列任务」。这个组合是：**跨进程用队列，进程内用同步嵌套**，两层各司其职。

### 2.4 技术选型

| 组件 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn（`--workers` 或 `gunicorn -k uvicorn.workers.UvicornWorker`） | asyncio 原生、OpenAPI |
| ORM/驱动 | SQLAlchemy 2.0 async + asyncpg（server）/ aiosqlite（lite） | 同套模型两种方言 |
| 迁移 | Alembic | 团队协作必须可重复迁移 |
| 队列 | PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` | run 少而长，避免为队列再引入 Celery；事务内可见、UI 直接读队列 |
| 缓存/锁/广播 | Redis 7（Streams + PubSub + SET NX PX + INCR） | 一个组件同时满足四件事 |
| 对象存储 | MinIO（自托管）/ S3 兼容 | 素材归档、跨机房下载、过期清理 |
| 反向代理 | Caddy 2（自动 TLS）或 Nginx | Caddy 配置更短，内网自签也方便 |
| 前端 | React 18 + TS + Vite + Tailwind + Zustand + TanStack Query | 与 v0.2 一致 |
| 可观测 | structlog JSON 日志 + Prometheus `/metrics` + 可选 Sentry | |
| 密码哈希 | Argon2id（`argon2-cffi`） | |
| 加密 | AES-GCM 或 Fernet，主密钥来自 env / KMS | |

### 2.5 规模假设与资源配置（按 D14：3–10 人定标）

| 维度 | 定标值 | 说明与上限 |
|---|---|---|
| 团队人数 | **3–10** | 上限 15；再往上建议拆团队或换 K8s |
| 团队数 | 1（单实例单团队） | 多团队用独立实例，不做 SaaS |
| 同时在线 | ≤15 | 单 API 副本足够，预留 2 副本 |
| 同时提交需求 | 2–4（峰值 6） | **同项目串行**，所以队列深度通常 1–3 |
| 同时在跑的 run | 1–2（受项目锁约束） | 多项目时 ≤4 |
| 单项目仓库 | <1 GB，文件 <5 万 | 超出需 sparse checkout（v2） |
| 单 run 时长 | 5–25 分钟 | 超 45 分钟告警，超 60 强制中止 |

**建议服务器规格**（单机 Docker Compose 跑全套）：

| 档位 | 配置 | 可支撑 |
|---|---|---|
| 最低 | 2C / 4G / 40G SSD | 3 人、单项目、lite 或 server 最小配置 |
| **推荐** | **4C / 8G / 100G SSD** | **3–10 人、2–3 个项目、日常够用** |
| 充裕 | 8C / 16G / 200G SSD | 10–15 人、5+ 项目、事件保留更久 |

容器配额建议：`postgres` 2G 内存上限、`redis` 512M、`api` 1G、`worker` 2G（原生模式主要吃 LLM 网络等待，不重 CPU；真正吃内存的是 git 操作与大文件读取）。

**3–10 人下不引入的东西**（明确写下来，防止过度设计）：K8s、消息中间件（Kafka/RabbitMQ）、独立搜索（ES）、服务网格、PgBouncer（连接数够用）。这些等到 >15 人再评估。

---

## 3. 多租户与权限模型

### 3.1 租户层级

```
Team（团队 = 租户，数据隔离边界）
 └─ Project（项目 = 一个 git 仓库 + 一份工作区目录）
     ├─ Session（会话 = 一段连续对话上下文，项目内共享可见）
     │   └─ Message（用户发言 / 策划经理回复）
     ├─ Run（一次执行：用户发一句话 = 一个 run）
     │   ├─ Task（任务树，含嵌套的 art / test 子任务）
     │   ├─ Event（事件流，实时推给所有在看的人）
     │   ├─ Artifact（素材）
     │   ├─ Comment（评论，可 @ 人）
     │   └─ Approval（合并审批 / 超预算审批）
     └─ ProjectLock（工作区写锁，全局唯一）
```

**隔离原则**：所有业务表必须带 `team_id`；所有查询必须从认证上下文取 `team_id` 强制过滤，**不接受前端传 team_id 作为授权依据**。SQL 层可再加 PostgreSQL RLS 作为第二道闸（v2 硬化项）。

### 3.2 人类角色（4 种）

| 角色 | 定位 | 关键权限 |
|---|---|---|
| `owner` | 团队创建者 | 全部权限 + 删除团队 + 转移所有权 + 改成员角色 |
| `admin` | 技术负责人 | 配置模型档位/智能体 prompt/供应商/预算、管理项目、取消任何 run、审批合并 |
| `developer` | 普通成员 | 发起 run、停止**自己的** run、评论、下载产物、查看全部执行流 |
| `viewer` | 只读成员（策划/制作/老板） | 看会话、看执行流、看产物、下载、评论；不能发起 run、不能改配置 |

> 项目级可覆盖：`project_members(project_id, user_id, role)` 允许某人在某项目降级/升级（例如外包只在某一项目做 viewer）。

### 3.3 权限矩阵

| 操作 | owner | admin | developer | viewer |
|---|:--:|:--:|:--:|:--:|
| 查看会话 / 事件流 / 产物 | ✅ | ✅ | ✅ | ✅ |
| 下载产物文件 | ✅ | ✅ | ✅ | ✅ |
| 发起 run | ✅ | ✅ | ✅ | ❌ |
| 停止自己的 run | ✅ | ✅ | ✅ | — |
| 停止他人的 run | ✅ | ✅ | ❌ | ❌ |
| 评论 / @提及 | ✅ | ✅ | ✅ | ✅ |
| 审批合并到主干 | ✅ | ✅ | ❌ | ❌ |
| 修改智能体 prompt / 绑模型 | ✅ | ✅ | ❌ | ❌ |
| 配置模型档位 / 供应商 / key | ✅ | ✅ | ❌ | ❌ |
| 新建 / 归档项目 | ✅ | ✅ | ❌ | ❌ |
| 改预算 / 配额 | ✅ | ✅ | ❌ | ❌ |
| 邀请成员 / 改角色 / 移除 | ✅ | ✅ | ❌ | ❌ |
| 删除团队 | ✅ | ❌ | ❌ | ❌ |
| 查看用量与审计 | ✅ | ✅ | ⚠️仅自己 | ⚠️仅自己 |

实现：FastAPI 依赖注入 `require(perm)`，**每个路由显式声明所需权限**；不在前端隐藏按钮上做安全（前端只做体验，后端才是闸门）。

### 3.4 认证

| 方式 | 用途 | 细节 |
|---|---|---|
| 邮箱 + 密码 | Web 登录 | Argon2id；密码策略 ≥10 位；连续 5 次失败锁 15 分钟 |
| 会话 Cookie | Web 后续请求 | `HttpOnly; Secure; SameSite=Lax`；服务端 `sessions_auth` 表存 hash，可即时撤销 |
| CSRF Token | 写操作 | 双提交 Cookie 模式；`SameSite=Lax` 之外的第二道 |
| Personal Access Token | CLI / CI / 脚本 | `ga_<team>_<random>`，只存 hash，带 scopes 与过期时间 |
| OIDC（**D17：要做**） | 企业 SSO | 通用 OIDC discovery（Keycloak/Entra/飞书/钉钉/Google），与密码登录共存 |

**首启引导**：启动时若 `users` 为空 → 控制台打印一次性引导 token，访问 `/setup` 创建第一个 owner。避免出厂默认密码。

**认证上下文**：`RequestContext(user_id, team_id, role, project_ids, request_id)` 贯穿日志与审计。

> **现状（个人版已改）**：本文档早期按「多用户 + 密码/OIDC 登录」设计。个人版已**整体移除登录与账号体系**
> （没有注册、密码、PAT、引导 token、OIDC）：身份固定取本机默认用户（`game_agent/auth/local.py`，
> 首启幂等创建「一个用户 + 一个工作区」，老库复用最早的账号），`deps.get_current_context` 不看任何凭证、
> role 恒为 owner。`users / teams / team_members` 表保留，只作为会话/项目/审计的内部外键分区。
> 下文 OIDC 与相关配置项**仅作历史记录**，代码里已不存在。

#### 3.4.1 OIDC 接入（D17，M1 落地）—— 已移除，仅存档

**目标**：让企业/团队用自己的身份系统登录，同时保留密码登录作为兜底（内网服务、OIDC 配错时还能进）。

**模式开关**：`GA_AUTH_MODE=password | oidc | both`（默认 `both`）。lite 模式默认 `password`，但改配置可开 OIDC。

**支持范围**（先通用、再预置）：

| 类型 | 实现方式 |
|---|---|
| 通用 OIDC | 只填 `issuer` + `client_id` + `client_secret` + `scopes`，用 discovery（`/.well-known/openid-configuration`）自动拉 endpoints 与 JWKS |
| Keycloak / Authentik | 走通用 OIDC，无特殊代码 |
| Microsoft Entra ID (Azure AD) | 预置模板（tenant id 填进 issuer） |
| Google Workspace | 预置模板 |
| 飞书 / 钉钉 / 企业微信 | 预置模板（注意：飞书/钉钉是 OAuth2 而非严格 OIDC，需要 `userinfo` 映射适配层） |

**流程（授权码 + PKCE）**：

```
GET /api/auth/oidc/{provider}/start
  → 生成 state + code_verifier（Redis 存 10 分钟，绑定浏览器会话）
  → 302 到 IdP 授权页（scope: openid profile email）
GET /api/auth/oidc/{provider}/callback?code=&state=
  → 校验 state（一次性，防 CSRF）
  → 用 code + verifier 换 token（客户端密钥或 PKCE）
  → 校验 id_token：签名（JWKS 缓存 24h）+ iss + aud + exp + nonce
  → 取 sub / email / name / 可选 groups
  → 绑定或创建用户（见下）→ 建会话 → Set-Cookie → 302 首页
```

**账号绑定与自动入组**：

| 情形 | 处理 |
|---|---|
| `oidc_sub` 已存在 | 直接登录 |
| 邮箱匹配已有本地用户，且 `oidc_sub` 为空 | **绑定**到该用户（避免同一人两个账号） |
| 邮箱不匹配任何用户 | 新建用户，`password_hash = NULL`（只能走 OIDC 登录） |
| 邮箱域不在允许清单 | 拒绝，提示「你的组织未获授权」 |
| 新用户入哪个团队 | 由 `OIDC_AUTO_JOIN_TEAM` 指定；默认角色 `developer`（可配为 `viewer` 先观察） |
| 带 `groups` 声明 | 可选映射：`OIDC_GROUP_ROLE_MAP={"ga-admins":"admin","ga-devs":"developer"}`；每次登录按最新 groups 同步角色 |

**配置项**：

```dotenv
GA_AUTH_MODE=both
OIDC_ALLOWED_EMAIL_DOMAINS=yourcompany.com          # 空=允许任意域（谨慎）
OIDC_AUTO_JOIN_TEAM=<team-slug>                     # 首次登录自动加入的团队
OIDC_DEFAULT_ROLE=developer
OIDC_GROUP_ROLE_MAP={"ga-admins":"admin","ga-devs":"developer","ga-viewers":"viewer"}
OIDC_AUTO_CREATE_USER=true
# 具体 provider 存在 DB（teams 或 global settings），便于 UI 里增删而不重启
```

**安全要点**：

- `state` 一次性 + 绑定原会话；`nonce` 校验；PKCE（`S256`）。
- JWKS 缓存 24h，密钥轮换时按 `kid` 未命中强制刷新一次。
- **不做**「按邮箱前缀猜成员」这类隐式授权。
- OIDC 用户改密码走 IdP，系统内不提供改密码入口（UI 上隐藏）。
- 登录成功/失败都写审计（`auth.login` / `auth.login_failed`，记 provider）。
- 管理员可禁用某人（`users.is_active=false`）——即使 IdP 里还存在，也进不来。

**UI**：登录页在密码框上方显示配置了的企业登录按钮（「用 飞书 登录」），未配置则不显示。设置页里 admin 可增删 OIDC provider 并点「测试发现」（拉一次 discovery 验证 issuer 可达）。

### 3.5 审计

`audit_logs` 记录以下动作（不可修改、只追加）：

```
team.create / team.update / team.member_add / team.member_role_change / team.member_remove
project.create / project.archive / project.config_update / project.lock_force_release
agent.prompt_update / agent.model_bind
profile.create / profile.update / profile.delete / profile.test
provider.create / provider.update / provider.delete / provider.test
run.create / run.cancel / run.force_cancel
approval.decide
secret.update        ← 只记「谁改了哪个字段」，绝不记值
auth.login / auth.login_failed / auth.logout / auth.token_create / auth.token_revoke
```

每条含 `actor_id / ip / user_agent / target / detail_json / created_at`。UI 里 admin 可查、可按人/动作/时间筛。

---

## 4. 智能体角色

### 4.1 总表

> **v2 极简架构（当前实现）**：只剩 3 个角色。原来的 `developer` / `tester` 子角色，以及
> 工单（`write_work_order`）、`dispatch_development`、`dispatch_testing`、`request_art`、
> `request_audio` 这些中间层全部移除——实测一个需求要经过「拆工单→派开发→派测试→回传」
> 四次 LLM 往返，大量轮次消耗在过程推导而不是产物上。现在由项目组长一个人从头干到尾，
> 只在「出图 / 出音频」两件事上派给美术与配音。

| Key | 名称 | 定位 | 绑定模型（建议） | 关键工具 |
|---|---|---|---|---|
| `planner` | 项目组长 | 唯一面向用户、也是唯一执行者：查文件、写代码、编译、起服务、截图验收、维护待办清单、汇报；只在出图形/出音频时派发 | 强推理 + 会写代码（deepseek-v3 / claude sonnet / gpt-5 级） | `read/list/glob/grep` `write_file` `edit_file` `delete_file` `run_command` `start_service` `service_logs` `stop_service` `screenshot_page` `dispatch_art` `dispatch_audio` `update_plan` `ask_user` `report_to_user` |
| `artist` | 美术 | 只出图：读画风设定，生成/改图，回报文件路径，不做别的 | 多模态（gemini-3-pro / gpt-5 / seedream） | `read/list/glob` `generate_image` `edit_image` `save_character_sheet` |
| `voice` | 配音 | 只出音频：TTS / 音效 / BGM，回报文件路径，不做别的 | 便宜快速（qwen-flash / gpt-4o-mini） | `read/list/glob` `tts` `generate_sfx` `generate_music` `upsert_voice_profile` |

> 3 个角色都是**同一个 AgentRuntime 类的实例**，差异来自：system prompt、工具白名单、模型档位、参数。
> **配置粒度为团队级**（admin 改，全团队生效），成员不能各自改 prompt——否则同一个项目的行为会因人而异，出问题无法复现。

### 4.1.1 「改完自己验」闭环

组长不再是纯派发者，所以自测能力必须做进工具层（否则它只能「声称改好了」）：

```
write_file/edit_file → run_command（编译、pytest）→ start_service（后台起、等端口）
→ screenshot_page（截图作为图片回灌进模型，它必须真的看图）→ 不对就改 → stop_service
```

`screenshot_page` 返回的截图会以 `image_url` 形式追加成一条 user 消息送回模型，
模型看到的是真实渲染结果，而不是文件内容——这是「巡游验证」不靠嘴说、靠图看的关键。


### 4.2 各角色 prompt 要点

> 默认 prompt 按 Python 项目写。构建/测试命令段由系统按 `projects.commands_json` 动态替换。
>
> ⚠️ **以代码为准**：当前默认提示词在 `game_agent/agents/roles.py`（三角色 v2 版）。
> 下面 4.2 里「开发 / 测试」以及带工单的那部分 planner 要点是 v1 历史版本，仅作背景参考。

**策划经理**

- 你是本项目唯一对用户负责的角色。用户说一句模糊需求，你要把它变成**可开发的工单**。
- 先做三件事：①读工程结构判断现状；②判断需求类型（新玩法 / 加内容 / 改数值 / 修 bug）；③判断是否要拆成多个工单串行做。
- 工单必须含：`goal`、`spec`（数据结构/接口/界面/边界）、`checklist`（可验证功能点，含 id/描述/验证方法/预期）、`art_direction`、`asset_plan`。
- `checklist` 是测试的唯一执行依据，必须**可客观验证**。
- 开发完必须走测试；不过就把具体失败项丢回开发，最多 3 轮，仍不过如实告知用户。
- 绝不自己写代码，绝不编造「已完成」。
- 汇报用固定结构（§7.7）。

**开发**

- 产出概括：**在工作区里产生能跑的代码 + 正确引用素材**。
- Python 改动以行级编辑为主：先 `read_file`，再 `edit_file` 精确替换，改完自检。不要整文件重写。
- 需要视觉素材：立刻 `request_art`（主体/构图/风格/尺寸/透明 + 参考图路径 + 工单画风），不要自己画也不要留 TODO。
- 需要音频：**先判断**——短音效/简单念白能自己生成就自己生成；**角色配音、带情绪台词、BGM 必须转交配音**。
- 素材返回后负责**接进工程**（正确路径与引用）。
- 禁止：修改 `.venv/`、装全局包、改 `.agent/` 之外的系统目录。
- **注意**：你的所有改动都在一条独立分支上，不会影响主干；但也不要提交、不要 push，提交由系统收尾统一做。

**美术**

- 目标是**风格一致的可用素材**，不是好看的图。
- 生成前先读画风圣经与角色设定，拼进最终提示词；同角色同场景保持设定一致。
- 有参考图走参考图模式，不要重画。
- 输出：`category_slug_v{n}.png`，落工作区 `assets/` 对应子目录；透明需求必须真透明（本地后处理）。
- 参数（prompt/seed/provider/size/参考图）全部记进 Artifact，保证可复现。
- 无可用供应商时产出占位素材并标注原因，不要把占位说成成品。
- 生成失败如实说原因（内容策略/额度/尺寸不支持）。

**配音**

- 先查角色音色档案，有就复用；没有就新建并告知创建了什么音色。
- 台词按「角色 → 情绪 → 语速 → 停顿」拆参数，不要整段念。
- 输出统一 wav/mp3，落 `assets/audio/{voice|sfx|bgm}/`。
- 生成不了就产出占位音频并标注，绝不静默什么都不生成。

**测试**

- 只做一件事：**把 checklist 逐条跑出结论**。
- 每条必须 `pass/fail/blocked` + **证据**（命令输出/文件内容/截图路径），禁止「看起来没问题」。
- Python 默认手段：`compileall` → `pytest` → 导入冒烟 → 必要时在 `.agent/tmp/` 写临时用例（**不污染工程 tests/**）。
- GUI 类设 `SDL_VIDEODRIVER=dummy` 无头冒烟；无法无头验证的判 `blocked` 并写「需人工目视」。
- 允许跑构建/测试/lint，不允许改业务代码。
- 发现问题定位到「哪个功能点、什么现象、复现步骤、可能涉及哪个文件」。

### 4.3 智能体的工具权限矩阵

| 工具 | planner | developer | artist | voice | tester |
|---|:--:|:--:|:--:|:--:|:--:|
| `read_file` / `list_dir` / `glob` / `grep` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `write_file` / `edit_file` | ❌ | ✅ | ⚠️仅 `assets/` `.agent/` | ⚠️仅 `assets/` `.agent/` | ⚠️仅 `.agent/tmp/` |
| `run_command` | ❌ | ✅ 白名单 | ❌ | ❌ | ✅ 白名单 |
| `run_coder_cli` | ❌ | ✅（M9，未装则不出现） | ❌ | ❌ | ❌ |
| `generate_image` / `edit_image` | ❌ | ❌ | ✅ | ❌ | ❌ |
| `tts` / `generate_sfx` / `generate_music` | ❌ | ⚠️仅能力自检 | ❌ | ✅ | ❌ |
| `request_art` / `request_audio` | ❌ | ✅ | ❌ | ❌ | ❌ |
| `dispatch_*` / `report_to_user` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `update_plan`（计划机制） | ✅ | ❌ | ❌ | ❌ | ❌ |
| `ask_user`（不确定就问） | ✅ | ❌ | ❌ | ❌ | ❌ |

> ⚠️ 均为工具实现层硬校验，不靠 prompt 约束。

### 4.4 计划机制（待办清单）与执行观测

**待办清单**：组长按提问复杂度决定是否列计划——简单问题不列；≥3 步 / 多文件 / 多角色协作的需求，
开工前用 `update_plan` 列出 3–8 项待办（每项是「可独立验证的具体动作」），此后每完成一步就整体覆盖
一次清单把该顶置 `completed`。

- 存储：`runs.plan_json`（DB 为权威源），结构 `[{id, content, status, note}]`，
  `status ∈ pending | in_progress | completed`；单一写者（组长），整体覆盖语义。
- 实时：`update_plan` 落库后广播 `plan_update` 事件（含 `progress`），前端 `PlanChecklist`
  据此渲染 ✅/🔄/⬜ 与完成度进度条；刷新/重连后回落 `GET /api/runs/{id}/plan`。
- 与工单的区别：工单 `checklist` 是给测试的验收依据；待办清单是给用户看的执行进度。

**取消**：`POST /api/runs/{id}/cancel` 只置 `cancel_requested`；执行侧有 1s 轮询的**取消看门狗**，
置位即 `task.cancel()` 掐断正在进行的 LLM 流（此前只在工具轮次之间检查，实测一次取消要等几分钟
才生效）。取消属「任务级取消」，`execute_run` 落 `cancelled` + 会话内系统提示后正常返回；只有
worker 自身被 cancel（服务关闭）才继续向上抛，避免取消几次把 worker 池打空。

**用量观测**：`llm_calls` 每次往返即时落库（`prompt_tokens` / `completion_tokens` / `cached_tokens`
/ `latency_ms` / `ttft_ms`）。`cached_tokens` 由适配层归一（DeepSeek `prompt_cache_hit_tokens`、
OpenAI `prompt_tokens_details.cached_tokens`）。会话级聚合 `GET /api/sessions/{id}/usage` 返回
总消耗 token（输入+输出）、缓存命中率、生成速度 tok/s、调用次数——落库持久，重进会话仍可见。

**首 token 耗时（TTFT）**：用户真正的体感是「点了发送后干等多久才出第一个字」，它和「总共跑
多久」是两件事。适配层在流式循环里记「第一个内容增量（正文 delta 或思考 delta）到达的时刻
− 请求发出时刻」→ `LLMResponse.ttft_ms` → 落 `llm_calls.ttft_ms`。非流式端点（`stream` 不支持、
降级到 `_parse_completion`）拿不到首字时刻，记 0，接口返回 0 + `ttft_calls` 计数，前端显示
「未测到」而不是假报 0 秒。顶栏「首字」显示最近一轮实测值，悬浮里另给本会话中位数——用中位数
而不是平均，是因为偶发一次排队/限流重试就能把平均拉爆，「常态等多久」只能靠中位数表达。

**缓存命中明细（点开看）**：顶栏「缓存命中」可点击，弹层按「总量 = 未缓存输入 + 缓存读取 +
输出」逐行列账（头部给总 token）。只给百分比看不出分母是 6.9M 还是 69k，也就判断不了这个会话
贵不贵；把加法摆开就能自己核对。弹层还带「最近一轮」与「按角色」两个小表：最近一轮回答
「刚才那次为什么这么慢/这么贵」，按角色回答「哪个角色在烧钱」（子 agent 的缓存几乎打不中，
命中率会把主对话的高命中摊薄，按角色拆开才不会误判）。端点没上报缓存字段时，百分比与
「缓存读取」都显示「未上报」而不是 0%——「不知道」和「真没命中」是两件事。
按角色合计只累加「输入量在窗口 1.5 倍以内」的可信行：中转偶尔把整轮累计量塞进单条 usage，
那种行加进来会让按角色表比会话总量还大（与上下文占用条同一套排除口径）。

### 4.5 不确定就问（ask_user）与提问续跑

需求里有**无法自行拍板的取舍**（口径、范围、风格、优先级）时，组长必须问用户，禁止自行决定。

- 工具：`ask_user(question, options[2–6], allow_text)`；每项 `{label, detail?}`，实现层校验
  选项数量与 `default_index` 范围。调用后**立即中止本轮**（抛 `AskUserStop`，
  在 tool 注册层的通用异常处理之前单独 `except` 掉，否则会被当成「工具报错」吞掉）。
- **不阻塞 worker**：本轮 run 直接落 `blocked`（`task_done` + `run_done` 带 question），
  释放项目写锁与 worker。问题落 `run_questions` 表（options JSON、answer_json、status）。
- 作答：`POST /api/questions/{id}/answer`（`option_index` 或 `text`）→ 写一条
  `【回答】…` 的 user 消息进会话历史，并**新建一个 queued run** 续跑——
  模型看到的是完整会话（含自己当时的提问与用户的回答），无需恢复执行栈。
- 前端：输入框上方 `AskQuestion` 面板（等待回答时展开单选 + 自由输入，作答后折叠为
  「提问 · N/M 已回答」留痕）；`blocked` 状态显示为「等待回答」，不计入「会话正在运行」
  的转圈（`queued` / `claimed` / `running` 才算）。
- 提示词落库：角色 prompt 的权威源是 DB `agents.system_prompt`（`get_agent_config` 读取），
  改 `game_agent/agents/roles.py` 后必须走 `db/migrate.py` 的迁移把新段落写进已有行，
  否则线上不生效。

---

## 5. 一次任务的全链路

### 5.1 场景：两个成员同时提需求

```
时间轴 ──────────────────────────────────────────────────────────────────►

[10:00] alice(developer) 在项目 myCardGame 发需求：
        「给主界面加一个卡牌详情弹窗，显示卡面/名字/技能/稀有度，颜色区分」
        → POST /projects/{p}/runs → run#42 queued（created_by=alice）
        → 请求项目写锁 → 成功 → running

[10:03] bob(developer) 在同一项目发需求：
        「卡牌列表增加按稀有度排序」
        → run#43 queued
        → 请求项目写锁 → 失败（alice 持有）
        → 状态保持 queued，UI 显示「排队中：前面有 1 个任务（run#42，已运行 3 分钟）」
        → bob 能实时看到 run#42 的执行流（viewer 级观察权），但改不了

[10:04] alice 的 run#42：
        ├─ git switch -c agent/run-42 main        （校验工作区 clean）
        ├─ planner: 读工程 → write_work_order → dispatch_development
        ├─ developer: request_art → artist 出 4 张稀有度边框（无 key → 占位）
        ├─            edit_file × 5 → run_command compileall → pytest
        ├─ planner: dispatch_testing → tester：3 pass / 1 fail（点遮罩不关闭）
        ├─ planner: dispatch_development(fix_focus=[C3]) → 修好
        ├─ planner: dispatch_testing → pass
        └─ planner: report_to_user（含占位素材提示）
        → 收尾：git commit（作者 GameAgent，trailer 记 run#42/alice）
        → 释放写锁 → run#42 done

[10:12] run#43 自动出队拿到锁 → 开始执行（看到的是 main，因为 run#42 还没合并）

[10:15] carol(admin) 打开 run#42 详情 → 看 diff → 评论「epic 和 legendary 颜色太近」
        → alice 在分支上追加一轮 fix（rerun on branch，复用同分支）→ carol 点「合并到 main」
        → 审批通过 → git merge --no-ff agent/run-42
        → run#43 的分支已基于旧 main → 提示「主干已变，建议 rebase 后重跑」（标记，不强制）
```

**三个关键设计点在这段场景里体现**：

1. **串行不是缺陷**：两个 AI 同时改同一份代码，冲突处理成本远高于等待 10 分钟。
2. **排队可见**：bob 不是被"忽略"，而是明确看到队列位置与预计等待。
3. **产出是分支不是主干**：任何 AI 改动都要人看过再合并——这是团队场景的底线。

### 5.2 任务树

```
run#42  「加卡牌详情弹窗」            created_by=alice  branch=agent/run-42
├─ task#1  plan      (planner)     ✅  12s
├─ task#2  implement (developer)   ✅  3m12s  12 files
│  └─ task#3  art    (artist)      ✅  12s    4 images（占位）
├─ task#5  test      (tester)      ❌  1 fail
├─ task#6  fix       (developer)   ✅  40s
└─ task#7  test      (tester)      ✅  pass
```

### 5.3 分支与提交策略（团队底线）

| 阶段 | 动作 |
|---|---|
| run 入队后、开工前 | ①`git fetch`（有远端）②校验工作区 **clean**（dirty → 拒绝，提示提交或 stash；admin 可 `force=true`）③`git switch -c agent/run-<id> <base_branch>` |
| run 执行中 | 所有编辑发生在该分支；开发角色**不许** `git commit/push`（工具层拦） |
| run 结束 | `git add -A && git commit -m "<工单标题> (run #42)"`，commit 作者固定为 `GameAgent <agent@local>`，`Co-authored-by: alice <alice@team>` + `GameAgent-Run: 42` trailer |
| 可选推送 | **本地 git 为主（D15）**：`git_remote` 为空时全部操作在本地完成；若填了远端，额外 `git push origin agent/run-<id>` 供外部 PR 流程使用 |
| 合并（本地） | admin/owner 点「合并到 main」→ 审批通过 → `git switch main` → `git merge --no-ff agent/run-<id>` → 成功则 `git branch -d`；**冲突则中止 merge（`git merge --abort`）** 并把冲突文件清单推给 UI，让人处理 |
| 合并前校验 | 合并前必须检查：①目标分支未前进（`main == base_commit`），否则提示「主干已变，建议 rebase 重跑」；②工作区 clean；③审批已通过 |
| 回滚 | 未合并 → `git branch -D`；已合并 → 生成 `git revert` 提交（保留历史，不 rewrite） |
| 保留期 | 分支默认保留 30 天（可配），到期清理；被判 rejected 的分支保留 7 天供参考 |
| 备份 | 本地 git 无远端 = 唯一副本，必须每日 `git bundle` + 目录快照（§15.7，风险 R16） |

**为什么强制 git**：没有 diff 就没有审查，没有分支就没有回滚，团队协作里这两件事不可妥协。因此 **server 模式要求项目必须是 git 仓库**（lite 模式可放宽）。

#### 5.3.1 本地 git 的代价：评审必须自己做

D15 选了本地 git（无远端），意味着**没有 GitLab/GitHub 的 Merge Request 界面**。如果只做一个「合并」按钮，评审环节会退化成「看一眼就点通过」——那 AI 产出的代码质量就没人真正把关了。

所以评审体验必须在产品里自建（M8 交付）：

| 能力 | 说明 |
|---|---|
| 文件树 + 双栏 diff | `main...agent/run-x` 的可折叠 diff，按文件切换，语法高亮 |
| 行内评论 | 点某一行开评论线程（存 `comments.target_type='diff_line'`，带 `path + line + side`），可 resolved |
| 变更概览 | 文件数、增删行、新增素材、测试报告并排 |
| 检查项卡片 | 测试 pass 率、占位素材数、未提交残留、分支是否基于最新主干 |
| 合并门槛 | 默认要求：测试 verdict=pass、无冲突、审批通过；admin 可强制覆盖（写审计） |
| 合并后 | 展示 merge commit，提供「回滚」按钮，通知发起人 |

**一句话原则**：本地 git 省掉的是远端依赖，不是评审责任。评审做得越好，AI 才越敢放开手脚改。



---

## 6. 并发与工作区一致性

> 这是「多人共用一个工作区」最核心的设计。做错了表现为：代码被覆盖、分支错乱、素材被别人的 run 删掉、两次 run 互相 checkout。

### 6.1 为什么必须加锁

工作区是**全局可变状态**：`git checkout` 改的是整个目录，`edit_file` 改的是共享文件，构建产物会互相污染。两个 run 并发进入 = 一定出事。所以原则是：

> **同一项目的「写」严格串行；「读」可并发。**

### 6.2 读写锁模型

| run 阶段 | 锁需求 | 说明 |
|---|---|---|
| `plan`（planner 读工程） | 共享读锁 | 多人可同时规划，不影响 |
| `implement` / `fix`（developer 写代码） | **排他写锁** | 独占项目工作区 |
| `test`（跑构建/测试） | **排他写锁** | 需要稳定快照；别人同时改会让测试结果失真 |
| `art` / `voice`（嵌套在 developer 内） | 继承父锁 | 已持有写锁，无需额外申请 |
| 纯查看（前端读） | 无锁 | 只看不写 |

实现为「读写锁」：写锁排他，读锁可共享且与写锁互斥。v1 简化为**按 run 粒度申请一次写锁**（因为 plan→implement→test 是一条链），plan 阶段短暂持有读锁，进入 implement 时升级为写锁。

#### 6.2.1 关键优化：区分「只分析」与「要改代码」

3–10 人的场景里，大量需求其实是**问问题**：「这个项目的战斗逻辑在哪」「现在卡牌数据是怎么存的」「如果加一个稀有度字段会牵动哪些文件」。这类 run 如果也排队等写锁，团队体感会非常差（为了问句话等 20 分钟）。

所以 run 分两类：

| 类型 | 判定方式 | 锁 | 会不会排队 |
|---|---|---|---|
| `analyze`（只读 run） | 用户勾选「只分析不改代码」，**或** planner 首轮判定需求为纯问答/调研 | 仅**共享读锁** | 只在别人持有写锁时等待；多个 analyze 可**同时**跑 |
| `modify`（改动 run） | 其余情况（新增功能/改 bug/改数值） | **排他写锁** | 按队列串行 |

规则：

- `analyze` run 的工具白名单**去掉所有写工具**（`write_file` / `edit_file` / `run_command` 里会改状态的部分），从根上保证它不可能动代码——不是靠 planner 自觉。
- `analyze` 不创建分支、不产生 commit、不产出 diff。
- 若 planner 发现「这个问题其实要改代码」，**不能自行升级**，而是回话：「这里需要改动，要不要我提一个改动任务？」由用户点确认后新建 `modify` run（避免暗中插队）。
- UI 上「只分析」是一个显眼的复选框，默认跟随上次选择。

> 这条优化的收益很直接：团队里 2–3 个人可以同时问项目问题，只有真正要改代码时才排队。


### 6.3 锁实现

```
acquire(project_id, run_id, mode=exclusive, ttl=120s)
  ├─ Redis: SET lock:project:{id} <fencing_token> NX PX 120000
  ├─ DB:   INSERT INTO project_locks(project_id, run_id, fencing_token,
                                     holder_worker, acquired_at, expires_at)
  │        ON CONFLICT 更新（记录可见性，供 UI 显示"谁在占用"）
  └─ 心跳: worker 每 30s 续期（PEXPIRE）；run 结束或异常必释放（finally + 进程退出钩子）

release(project_id, run_id, fencing_token)
  └─ 仅当 fencing_token 匹配才删（防僵尸 worker 释放了别人的锁）
```

**Fencing token**：单调递增的整数，锁每次转移 +1。所有写操作（文件写入、git 操作）前校验「我持有的 token 是否仍是当前值」；worker 因网络分区假死后恢复，token 已变 → 立即自杀退出，绝不继续写。这是防「双写脑裂」的关键。

**TTL 与长任务**：run 可能跑 40 分钟，靠心跳续期而不是长 TTL；心跳失败 3 次（90s）视为失联 → 锁自动过期，run 标记 `orphaned` 等待恢复或人工处理。

**锁等待上限**：默认 30 分钟，超时 → run 状态 `blocked`，通知发起人「前面任务太久」，可选择继续等或取消。

### 6.4 队列与可见性

- 队列表就是 `runs` 表（`status='queued'`），worker 用
  `SELECT ... WHERE status='queued' AND … ORDER BY priority DESC, created_at FOR UPDATE SKIP LOCKED LIMIT 1`
  领取，事务内立刻改 `status='claimed'`。天然幂等、天然多 worker 安全，不需要额外队列组件。
- 优先级：admin 可把某 run 提到 high（插队），默认 FIFO。
- UI 显示：队列位置、前面任务、预计等待（用最近 20 个 run 的中位时长估）。
- 取消排队中的 run：直接置 `cancelled`；正在运行的 run：发取消信号（worker 轮询 `runs.cancel_requested`）。

### 6.5 幂等与防重复烧钱

| 风险 | 对策 |
|---|---|
| worker 崩溃后重新领到同一 run 已做过的子任务 | `tasks` 状态机 + `attempt`；已 `done` 的子任务直接跳过（结果在 `result_json`） |
| 消息重复投递导致两次调 LLM | 每次 LLM 调用有 `call_id`，结果落 `llm_calls` 表，重放时命中即复用 |
| 素材重复生成烧钱 | `artifacts` 唯一键 `(run_id, task_id, slug, attempt)`，已存在且非占位则直接返回 |
| 用户连点「发送」 | 前端禁用 + 后端 `Idempotency-Key` 头，10 分钟内同 key 返回同一 run |
| 合并按钮连点 | 审批表状态机，`pending` → `approved` 只允许一次迁移 |

### 6.6 共享文件系统要求

| 部署形态 | 工作区放哪 | 说明 |
|---|---|---|
| 单机 compose（推荐起步） | 宿主机目录 bind mount 到 api+worker | 最简单，所有人同一台机器 |
| 多机 worker | NFS v4 / CephFS / EFS / Azure Files，挂到同一路径 | **必须**，否则分支状态错乱 |
| K8s | RWX PVC（NFS/CephFS/EFS CSI） | RWO PVC 多副本 worker 会出事 |
| 备选：worker 亲和 | 项目绑定到固定 worker（`projects.pinned_worker_group`） | 适合大仓/低带宽；v2 |

**git 并发注意**：同一仓库被多个进程并发 `git` 操作会撞 `index.lock`。因为写锁已保证同一时刻只有一个 run 动工作区，冲突概率低；但 `git fetch`（只读）仍可能并发 → 用项目级 `git_fetch` 短锁（TTL 30s）串行化。

---

## 7. 核心机制

### 7.1 AgentRuntime：唯一执行引擎

```python
class AgentRuntime:
    async def run(self, ctx: RunContext) -> AgentResult:
        messages = self.build_messages(ctx)        # system + 能力清单 + 历史摘要
        for round in range(self.cfg.max_tool_rounds):
            await ctx.fencing.assert_valid()        # ★ 每次 LLM 往返前校验锁 token
            resp = await self.llm.chat(messages, tools=self.tools.specs(), stream=True)
            if not resp.tool_calls:
                return AgentResult(text=resp.text, usage=resp.usage)
            for call in resp.tool_calls:
                await ctx.bus.emit("tool_call", ...)     # 推给所有在看的人
                result = await self.tools.execute(call, ctx)
                messages.append(tool_message(call.id, truncate(result)))
        raise MaxRoundsExceeded(self.cfg.key)
```

要点：

- 每次工具调用推事件（多人可见）。
- 工具结果截断（默认 8k 字符），文件类走摘要+展开。
- 上下文超限：system + 最近 N 轮 + 结构化摘要。
- **每次 LLM 往返前校验 fencing token**：锁丢了立刻停，杜绝双写。
- 取消：`asyncio.CancelledError` 冒泡；`finally` 杀子进程、写取消事件、释放锁。
- 工具可见性动态化：没装 CLI → 不出现 `run_coder_cli`；没配供应商 → `generate_image` 仍出现（走占位），描述里注明。

### 7.2 agent-as-tool：角色协同契约

协同仍是**工具调用同步返回**（worker 进程内）。控制流清晰、错误可追踪、嵌套深度受限。

```python
@tool(name="request_art", description="需要图片素材时调用：出什么/什么风格/多大/要不要透明/参考图")
async def request_art(args, ctx) -> str:
    artist = ctx.registry.get("artist")
    child = await ctx.orchestrator.spawn(parent=ctx.task, agent=artist, kind="art", payload=args)
    return child.result.render_for_parent()      # 自然语言 + 结构化路径
```

返回给开发的形态：

```
美术已完成 4 张图（风格：厚涂赛璐璐，冷色调描边）。
⚠️ 当前未配置图像供应商，以下为占位图（已按最终路径命名，配上 key 后可原地替换，代码无需改动）：
- assets/art/ui/rarity_frame_common_v1.png   256x256 透明   占位
- assets/art/ui/rarity_frame_rare_v1.png     256x256 透明   占位
- assets/art/ui/rarity_frame_epic_v1.png     256x256 透明   占位
- assets/art/ui/rarity_frame_legend_v1.png   256x256 透明   占位
注意：epic 与 legendary 主色偏近，建议 UI 里再叠一层发光区分。
```

`request_audio` / `dispatch_testing` / `dispatch_development` 同一套机制。嵌套深度上限 4（防爆栈）。

### 7.3 代码执行层

#### 7.3.1 原生开发模式（第一版主线）

开发角色工具：`read_file` / `write_file` / `edit_file` / `glob` / `grep` / `list_dir` / `run_command`。

**为什么 Python 项目原生模式够用**：Python 改动原子是行级；`grep + read_file` 定位足够；`pytest` 反馈环路最短；少一层 CLI 不确定性（版本/权限/MCP/resume）。

**三件硬约束**：

| 约束 | 实现 |
|---|---|
| 未读过不许写 | 写/改前检查该文件在本 run 内被 `read_file` 过，否则拒绝并提示"先读" |
| 精确替换 | `edit_file` 要求 `old_string` 唯一命中；0 或多命中都报错，让模型重读重试 |
| 交差前必自检 | 结束时工具层触发 `compileall`（或 `pytest`），失败则回灌错误再修（≤3 次） |

**硬限制**：单文件写入 ≤2MB；单次替换 ≤200 行；只许动工作区内路径（resolve + 前缀校验）；默认禁写 `.venv/` `__pycache__/` `.git/` `.agent/events/`；`run_command` 白名单（§13）；单 run 写文件 >30 时推 `budget_warn`。

#### 7.3.2 Windows/Linux 子进程要点

| 问题 | 对策 |
|---|---|
| 中文乱码 | env 设 `PYTHONIOENCODING=utf-8` `PYTHONUTF8=1`；读取 `errors="replace"` |
| 杀不干净 | Windows 建进程组，`CTRL_BREAK` → `taskkill /T /F`；Linux `SIGTERM` → `SIGKILL` 进程组 |
| 输出卡缓冲 | `readline()` 循环；Python 加 `-u` |
| 长任务无输出 | 心跳事件每 15s（`cmd_heartbeat`） |
| 路径带空格 | argv 数组传参 |
| 超时 | `tool_timeout_s=300` + 无输出超时 `cmd_idle_timeout_s=120` |
| 装包污染 | `pip install --no-input`；未检测到 venv 直接拒 |

#### 7.3.3 编码 CLI 接入（M9，后置）

`run_coder_cli` 在 M4 就注册进 Registry，但 `probe()` 全返回不可用 → **工具不出现在列表里**（不是调用报错）。装了 claude code / opencode 后自动出现。

CLI 中途要图的三种通道（按优先级降级）：

| 通道 | 机制 | 前提 |
|---|---|---|
| A · MCP | 自带 stdio MCP server 暴露 `request_art` 等，`--mcp-config` 注入，MCP handler HTTP 打回 `/api/bridge/tools/*` | 支持 MCP |
| B · HTTP 桥 | 注入 `GA_BRIDGE_TOKEN`，指令里让 CLI `curl` 桥接接口 | 能发 HTTP |
| C · 标记协议 | stdout 出现 `<<<GA_REQUEST>>>{json}` → 截获 → 中断 → 跑美术 → `--resume` 续接 | 支持 resume |

**桥接鉴权在团队模式下更严格**：token 绑定 `(run_id, worker_id)`，仅回环地址或 worker 内网 IP 可用，run 结束立即失效。

### 7.4 能力清单（Capability Manifest）

每次 Agent 启动，把**真实可用能力**注入 system prompt 末尾。这是「开发自行判断能不能生成音频」的依据。

```markdown
## 你当前可用的能力（系统实时探测，以此为准）

### 代码执行
- 原生模式（fs + shell）:              ✅ 当前可用（主线）
- claude code / opencode / codex:      ❌ 均未安装

### 图片生成
- 未配置任何图像供应商 → 占位模式      ⚠️ 会生成占位图并标记，不是成品

### 音频
- edge-tts（免费，无需 key）:          ✅ 可生成简单念白
- 本地 CC0 音效库:                     ✅ 706 条可检索
- 音乐供应商:                          ❌ 未配置 → 必须转交配音角色

### 项目环境
- 项目: myCardGame · 分支: agent/run-42 · 基线: main@a1b2c3d
- 类型: Python · 解释器: /data/venvs/mycardgame/bin/python
- 构建: python -m compileall -q .
- 测试: python -m pytest -q
```

判断规则写在清单下：

> 音效、简单念白且 TTS 可用 → 自己生成；
> 角色配音 / 带情绪台词 / 音乐 → 必须 `request_audio`；
> 图像一律走 `request_art`，不自己画、不留 TODO；
> 标 ❌ 或 ⚠️ 的如实转交/上报，不硬试，也不声称已完成。

### 7.5 素材一致性：Art Bible + Character Sheet

`.agent/art_bible.md`（项目内文件，随 git 走，团队共享同一份画风）：

```markdown
# 画风圣经
## 全局风格
- 风格关键词: 厚涂赛璐璐, 日系, 冷色调, 硬边描边 2px, 高对比
- 光影: 顶侧光, 柔和阴影
- 配色: 主色 #2B3A67 / 强调 #F2A65A / 危险 #D64550
- 禁止: 写实照片感, 3D 渲染感, 文字水印, 多余手指
## 品类规范
- ui: 透明背景, 2px 描边, 圆角 8px, 无投影
- portrait: 半身, 3/4 侧面, 纯色背景便于抠图
## 负面提示词
lowres, blurry, watermark, text, extra fingers, deformed hands, jpeg artifacts
```

`.agent/characters/<slug>.json`：

```json
{
  "slug": "knight_lyra",
  "name": "莱拉",
  "role": "骑士团长",
  "prompt_fragment": "silver-haired female knight, 25yo, blue eyes, scar on left cheek, ornate silver plate armor with blue cape",
  "palette": ["#C8D3E0", "#2B3A67", "#F2A65A"],
  "references": ["assets/art/characters/lyra_ref_sheet_v1.png"],
  "canonical_seed": 78120,
  "voice_profile": "voice_knight_lyra",
  "notes": "左脸疤痕必须画在角色左侧（观众右侧）"
}
```

提示词由代码按固定顺序拼：`全局风格 + 角色片段 + 本次主体 + 品类规范 + 尺寸/透明/数量 + 负面词`。
所有参数落 `artifacts` 表，UI 一键「同参重画」。

> **团队价值**：`art_bible.md` 和 `characters/` 在仓库里，所以**任何成员发起的 run 都产出同一画风**——这是共享工作区最直接的好处。

### 7.6 测试回环

```
dispatch_development → DevReport → dispatch_testing → TestReport
      ▲                                    │
      │                        pass ───────┴──► report_to_user（→ 合并审批）
      └──── fail（带失败项）◄──────────────┘  最多 3 轮
                                             │
                                  超 3 轮 ───► 升级给用户（附完整报告）
```

`TestReport` 每条必须有 `evidence`，无证据判 `blocked`（不是 `pass`）：

```json
{
  "verdict": "fail",
  "summary": "4 项功能点，2 通过，1 失败，1 阻塞",
  "items": [
    {"id":"C1","desc":"点击卡牌弹出详情","status":"pass",
     "evidence":"test_card_detail.py::test_open_dialog PASSED"},
    {"id":"C3","desc":"点击弹窗外部关闭","status":"fail",
     "evidence":"test_card_detail.py::test_click_outside FAILED\nAssertionError: dialog still visible",
     "suspected_files":["game/ui/card_detail.py"],
     "repro":"打开主界面 → 点任意卡牌 → 点击遮罩空白处"},
    {"id":"C4","desc":"稀有度颜色区分","status":"blocked",
     "evidence":"ImportError: cannot import name 'RARITY_COLORS'"}
  ],
  "commands":[
    {"cmd":"python -m compileall -q .","exit":0},
    {"cmd":"python -m pytest -q .agent/tmp","exit":1,"tail":"1 failed, 2 passed"}
  ]
}
```

### 7.7 变更报告 & 最终汇报

**开工前快照**：`git rev-parse HEAD`、`git status --porcelain`（要求为空）、分支名、文件 sha1 索引（非 git 兜底，仅 lite）。

**结束后**：`git diff --stat main...HEAD` + `git diff main...HEAD`（逐文件，超长截断）+ 新增/删除文件；非 git 用索引差集 + `difflib`。

策划经理渲染成固定六段（目标 / 改动 / 原因与说明 / 风险点与影响 / 验证证据 / 回滚与后续）。
**收尾总结必须写清「为什么这么改、改完意味着什么、可能出什么问题」**，不能只罗列改了哪些文件：

> **中途进度不套这套格式**：活跑得久时，主 agent 可以随时用 `report_to_user` 发一两句口语化进度
> （「现在在改 X，前面 Y 已经跑通了」），由模型自己决定说不说——界面上标成「进度」并注明
> 还有几项没做完，**不是交付**。六段格式只用于全部做完后的最后一次汇报。

```markdown
## ✅ 已完成：卡牌详情弹窗

**① 目标（为什么做）**：主界面点卡牌弹出详情，展示卡面、名字、技能、稀有度颜色。

**② 代码改动（5 个文件，+218 / -12）from `agent/run-42` → `main`**
| 文件 | 变化 | 说明 |
|---|---|---|
| `game/ui/card_detail.py` | +142 | 新增弹窗组件，含点击遮罩关闭 |
| `game/ui/card_grid.py` | +38 -6 | 卡片点击绑定 on_select |
| `game/ui/rarity.py` | +24 | 稀有度 → 颜色/名称映射 |
| `game/data/card.py` | +9 -6 | Card dataclass 加 rarity |
| `game/ui/theme.py` | +5 | 4 个稀有度颜色常量 |

**③ 原因与说明**：只为「展示」加组件，不改数据流；稀有度颜色集中在 theme.py，避免每处硬编码。
取舍：暂不做动画（简单优先），后续要动效再接。

**④ 风险点与影响**
- 点击遮罩关闭在移动端可能与滑动冲突，目前只在桌面验证过。
- `Card.rarity` 新增字段，旧存档（无该字段）走默认值 `common`，已加空值保护。

**⑤ 验证证据：3/4 通过，1 项需人工目视**
| 功能点 | 结果 |
|---|---|
| C1 点击卡牌弹出详情 | ✅ |
| C2 展示卡面/名字/技能/稀有度 | ✅ |
| C3 点击外部关闭 | ✅（第 2 轮修复） |
| C4 稀有度颜色区分 | ⚠️ 未验证：需 GUI 目视 |

**⑥ 回滚与后续**：回滚 = 还原上表 5 个文件（或 revert 本次 run 快照）；无后续遗留项。

**结论**：现在可用；桌面端已验，移动端点击关闭需你确认一次。
```

### 7.8 占位素材模式（零 key 也能跑通）

**问题**：不预置任何供应商 key。若不处理，链路会在 `generate_image` 断掉。

**设计**：素材层永远有输出，只是标记质量等级。

| 类型 | 占位实现 | 产物 |
|---|---|---|
| `image` | PIL 绘制：按 size 出图，底色取 art_bible 主色，居中写 slug + 尺寸 + `PLACEHOLDER`；透明需求出真 RGBA + 半透明描边 | 同路径 png，`is_placeholder=1` |
| `tts`/`voice` | 同长度静音 wav（字数×0.25s，最短 0.8s）+ 同名 `.txt` 放台词 | `*.wav` + `*.wav.txt` |
| `sfx` | 200ms 静音 wav | 同上 |
| `music` | 30s 静音 wav | 同上 |

**四条硬约束**：

1. **同路径同名** —— 占位与真图路径完全一致，配好 key 后重画只覆盖内容，**代码零改动**。
2. **显式标记** —— `artifacts.is_placeholder=1`；前端橙色角标「占位」；素材库可筛。
3. **必须通报** —— 首次触发推 `placeholder_warn`，工单记 `asset_quality`，汇报单列一段。
4. **重画不重跑** —— `POST /artifacts/{id}/regenerate` 用存好的 prompt/seed/size/refs 走真供应商；批量接口一次补齐整个 run。

**开关**：`PLACEHOLDER_ENABLED=true`。设 `false` 时无供应商直接失败上报（"宁可不做也不出假素材"）。

### 7.9 供应商配置口：驱动注册表（schema 驱动表单）

**前端不写死任何供应商表单，后端注册表说了算。** 新增供应商 = 加一个文件 + 一个 `@register_driver`，前端零改动。

```python
@register_driver(
    key="gemini_image", kind="image", label="Google Gemini（图像）",
    docs_url="https://ai.google.dev/",
    fields=[
        Field("api_key", "API Key", type="secret", required=True),
        Field("base_url", "Base URL", type="text", default="https://generativelanguage.googleapis.com"),
        Field("model", "模型", type="select", default="gemini-2.5-flash-image",
              options=["gemini-2.5-flash-image", "imagen-4.0-generate-001"]),
        Field("max_images", "单次上限", type="int", default=4, min=1, max=8),
    ],
    capabilities=Capabilities(references="multi", transparent="post_process",
                              sizes=["1024x1024", "1536x1024"]),
)
class GeminiImageDriver(ImageDriver):
    async def generate(self, req) -> list[ImageResult]: ...
    async def test(self) -> ProbeResult: ...
```

- `GET /providers/drivers` 返回 `[{key,kind,label,docs_url,fields[],capabilities}]`
- 前端 `ProviderForm` 遍历 `fields` 渲染（secret 掩码 / select 下拉 / int 数字框）
- `probe()` 结果进能力清单
- **配置是团队级**：admin 配一次，全员可用；成员只能看「已配置/未配置」，看不到 key 明文

首发驱动：image = `gemini_image` `openai_images` `seedream` `dashscope` `comfyui` `sd_webui` `custom_http`；tts = `edge_tts`（免费）`openai_tts` `minimax_tts` `azure_tts`；sfx = `local_library`（免费）`elevenlabs_sfx`；music = `minimax_music` `local_library`。

> `edge_tts` 与 `local_library` 不需要 key → 音频侧能先出真声音。

### 7.10 Python 项目适配

**探测**（建项目时自动跑，可改）：`pyproject.toml` → python + 入口；`requirements.txt`/`Pipfile`/`setup.py` → python；`.venv/`/`venv/` → 记录解释器；`main.py`/`app.py`/`game.py` → 运行入口。

**默认命令**：

| 键 | 默认 | 说明 |
|---|---|---|
| `build` | `python -m compileall -q .` | 语法校验，无副作用 |
| `test` | `python -m pytest -q` | 无 tests/ 退 `unittest discover` |
| `lint` | `ruff check .` | 未安装则标 skipped |
| `run` | `python main.py` | 仅冒烟 |

**测试角色 Python 手段**：静态（compileall + ruff）→ 导入冒烟（`python -c "import x"`）→ 行为（`.agent/tmp/` 下写 pytest，不污染 tests/）→ GUI 无头（`SDL_VIDEODRIVER=dummy`）→ 证据（stdout 尾 + 用例名 + 退出码）。

---

## 8. 实时协作与事件总线

### 8.1 事件流与扇出

```
worker ──emit──► ① DB events 表（权威、可补拉）
                 └► ② Redis Stream events:run:{id}（maxlen 5000）
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
           api-1                 api-2                  api-N
        (订阅 channel)        (订阅 channel)         (订阅 channel)
              │                     │                     │
              ▼                     ▼                     ▼
        WS 客户端组            WS 客户端组             WS 客户端组
```

- **权威源是 DB**，Redis 只是加速通道。客户端断线重连带 `last_seq` → 从 DB 补拉 `events WHERE run_id=? AND seq>?`，然后接上实时流。保证不丢不重。
- WS 订阅粒度：`project:{id}`（项目内所有人看同一条流，符合团队协作）。
- 背压：单 run 事件超过 3000 条/分钟时合并 `message_delta`（流式 token 本身合并成 200ms 一批）。

### 8.2 presence（在线状态）

`SETEX presence:project:{id}:{user_id} 60 <json>`，前端每 25s 心跳。UI 显示「3 人在看」。不加锁语义，纯展示。

### 8.3 评论 / @提及 / 通知

- `comments(target_type=run|task|artifact|work_order, target_id, body, mentions[])`
- `@name` 解析成 `user_id` → 生成 `notifications` 行 + WebSocket `notification` 事件 + 可选 webhook/邮件
- 评论可标记 `resolved`（类似 review thread）
- 通知渠道：站内（默认）、Webhook（钉钉/飞书/Slack/企微，HMAC 签名）、SMTP 邮件

### 8.4 审批

| 类型 | 触发 | 审批人 |
|---|---|---|
| `merge` | 点「合并到 main」 | admin / owner |
| `budget` | 单 run 预估超出团队配额 | admin / owner |
| `force_unlock` | 强制释放某项目的僵尸锁 | admin / owner |
| `danger_command` | 命中高危命令白名单 | admin（可配为自动拒） |

审批记录入 `approvals`，状态机 `pending → approved | rejected | expired`；审批动作本身写审计。

---

## 9. 数据模型

### 9.1 表清单（PostgreSQL 15+；lite 用同模型 SQLite 方言）

```
身份:    users, auth_sessions, api_tokens
租户:    teams, team_members
项目:    projects, project_members, project_locks
对话:    sessions, messages
执行:    runs, tasks, work_orders
产出:    artifacts, approvals, comments
遥测:    events, audit_logs, usage_ledger, llm_calls
配置:    model_profiles, asset_providers, agents, voice_profiles, webhooks, notifications
系统:    alembic_version, settings
```

### 9.2 关键 DDL

```sql
-- ============ 身份 ============
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         citext NOT NULL UNIQUE,
  display_name  text NOT NULL,
  password_hash text,                      -- OIDC 用户可为空
  oidc_sub      text,
  is_active     boolean NOT NULL DEFAULT true,
  is_superuser  boolean NOT NULL DEFAULT false,   -- 仅在无团队归属时救急用
  failed_logins int NOT NULL DEFAULT 0,
  locked_until  timestamptz,
  last_login_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auth_sessions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  text NOT NULL UNIQUE,
  ip          inet,
  user_agent  text,
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON auth_sessions(user_id, expires_at);

CREATE TABLE api_tokens (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        text NOT NULL,
  token_hash  text NOT NULL UNIQUE,        -- 只存 hash
  scopes      text[] NOT NULL DEFAULT '{}',-- run:create, run:read, artifact:read ...
  expires_at  timestamptz,
  last_used_at timestamptz,
  revoked_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ============ 租户 ============
CREATE TABLE teams (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  slug          text NOT NULL UNIQUE,
  budget_json   jsonb NOT NULL DEFAULT '{}',   -- {monthly_tokens, monthly_images, ...}
  settings_json jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE team_members (
  team_id   uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role      text NOT NULL CHECK (role IN ('owner','admin','developer','viewer')),
  invited_by uuid REFERENCES users(id),
  joined_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (team_id, user_id)
);
CREATE INDEX ON team_members(user_id);

-- ============ 项目 ============
CREATE TABLE projects (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id        uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  name           text NOT NULL,
  slug           text NOT NULL,
  repo_path      text NOT NULL,            -- PROJECTS_ROOT 下的相对路径
  git_remote     text,
  base_branch    text NOT NULL DEFAULT 'main',
  project_type   text NOT NULL DEFAULT 'python',
  python_bin     text,
  entry_point    text,
  commands_json  jsonb NOT NULL DEFAULT '{}',   -- {build,test,lint,run,package?}
  budget_json    jsonb NOT NULL DEFAULT '{}',
  branch_ttl_days int NOT NULL DEFAULT 30,
  archived_at    timestamptz,
  created_by     uuid REFERENCES users(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (team_id, slug)
);
CREATE INDEX ON projects(team_id, archived_at);

CREATE TABLE project_members (           -- 可选的角色覆盖
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('admin','developer','viewer')),
  PRIMARY KEY (project_id, user_id)
);

-- ============ 锁（DB 记账；Redis 是主锁） ============
CREATE TABLE project_locks (
  project_id    uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  run_id        uuid,
  mode          text NOT NULL,             -- exclusive | shared
  fencing_token bigint NOT NULL,           -- ★ 单调递增，防僵尸 worker
  holder_worker text NOT NULL,
  acquired_at   timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  heartbeat_at  timestamptz NOT NULL DEFAULT now()
);

-- ============ 对话 ============
CREATE TABLE sessions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id     uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title       text,
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  last_active timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON sessions(project_id, last_active DESC);

CREATE TABLE messages (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id      uuid,
  author_type text NOT NULL,               -- user | agent | system
  author_id   uuid REFERENCES users(id),   -- author_type=user 时
  agent_key   text,                        -- author_type=agent 时
  content     text NOT NULL,
  meta_json   jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON messages(session_id, created_at);

-- ============ 执行 ============
CREATE TABLE runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id          uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id       uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  triggered_by     uuid NOT NULL REFERENCES users(id),
  trigger_msg_id   uuid REFERENCES messages(id),
  status           text NOT NULL DEFAULT 'queued',
                   -- queued|claimed|running|blocked|orphaned|succeeded|failed|cancelled
  priority         int NOT NULL DEFAULT 0,
  branch           text,
  base_branch      text,
  base_commit      text,
  worker_id        text,
  lock_fencing     bigint,                 -- 持有锁时的 token，用于收尾校验
  snapshot_json    jsonb,
  change_report_json jsonb,
  final_summary    text,
  usage_json       jsonb NOT NULL DEFAULT '{}',
  has_placeholder  boolean NOT NULL DEFAULT false,
  cancel_requested boolean NOT NULL DEFAULT false,
  idempotency_key  text,
  error            text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  claimed_at       timestamptz,
  started_at       timestamptz,
  ended_at         timestamptz
);
CREATE INDEX idx_runs_queue ON runs(status, priority DESC, created_at)
  WHERE status IN ('queued','claimed');
CREATE INDEX ON runs(project_id, created_at DESC);
CREATE INDEX ON runs(triggered_by, created_at DESC);
CREATE UNIQUE INDEX ON runs(project_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE TABLE tasks (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id         uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  parent_id      uuid REFERENCES tasks(id) ON DELETE CASCADE,
  kind           text NOT NULL,            -- plan|implement|art|audio|test|fix|cmd
  title          text,
  status         text NOT NULL DEFAULT 'pending',
  agent_key      text,
  payload_json   jsonb,
  result_json    jsonb,
  attempt        int NOT NULL DEFAULT 1,
  error          text,
  started_at     timestamptz,
  ended_at       timestamptz,
  sort_order     int NOT NULL DEFAULT 0
);
CREATE INDEX ON tasks(run_id, sort_order);

CREATE TABLE work_orders (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  goal            text NOT NULL,
  spec_md         text NOT NULL,
  checklist_json  jsonb NOT NULL,
  art_direction   text,
  asset_plan_json jsonb NOT NULL DEFAULT '[]',
  asset_quality   text NOT NULL DEFAULT 'unknown',
  status          text NOT NULL DEFAULT 'draft',
  revision        int NOT NULL DEFAULT 1,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- ============ 产出 ============
CREATE TABLE artifacts (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id        uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  project_id     uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id         uuid REFERENCES runs(id) ON DELETE SET NULL,
  task_id        uuid REFERENCES tasks(id) ON DELETE SET NULL,
  kind           text NOT NULL,            -- image|voice|sfx|music|video|file
  repo_path      text,                     -- 工作区内相对路径（在仓里的文件）
  storage        text NOT NULL DEFAULT 'local',  -- local | s3
  object_key     text,                     -- s3 key
  sha256         text,
  bytes          bigint,
  prompt         text,
  negative       text,
  provider_id    uuid,
  seed           bigint,
  size           text,
  refs_json      jsonb NOT NULL DEFAULT '[]',
  is_placeholder boolean NOT NULL DEFAULT false,
  meta_json      jsonb NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON artifacts(run_id, is_placeholder);
CREATE INDEX ON artifacts(project_id, created_at DESC);
CREATE UNIQUE INDEX ON artifacts(run_id, task_id, (meta_json->>'slug'), attempt)
  WHERE meta_json ? 'slug';

CREATE TABLE comments (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id     uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  target_type text NOT NULL,               -- run|task|artifact|work_order
  target_id   uuid NOT NULL,
  author_id   uuid NOT NULL REFERENCES users(id),
  body        text NOT NULL,
  mentions    uuid[] NOT NULL DEFAULT '{}',
  resolved_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON comments(target_type, target_id, created_at);

CREATE TABLE approvals (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id      uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  run_id       uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  kind         text NOT NULL,              -- merge|budget|force_unlock|danger_command
  payload_json jsonb NOT NULL DEFAULT '{}',
  requested_by uuid NOT NULL REFERENCES users(id),
  status       text NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired
  decided_by   uuid REFERENCES users(id),
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  decided_at   timestamptz
);
CREATE INDEX ON approvals(team_id, status, created_at DESC);

-- ============ 遥测 ============
CREATE TABLE events (
  id         bigserial PRIMARY KEY,
  team_id    uuid NOT NULL,
  run_id     uuid NOT NULL,
  task_id    uuid,
  parent_task_id uuid,
  seq        bigint NOT NULL,
  kind       text NOT NULL,
  agent_key  text,
  actor_id   uuid,                          -- 触发的人（若有）
  payload_json jsonb,
  ts         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON events(run_id, seq);
-- 建议按月分区：PARTITION BY RANGE (ts)

CREATE TABLE llm_calls (
  id          bigserial PRIMARY KEY,
  run_id      uuid NOT NULL,
  task_id     uuid,
  agent_key   text NOT NULL,
  provider    text NOT NULL,
  model       text NOT NULL,
  request_hash text NOT NULL,               -- 幂等：重放命中即复用
  prompt_tokens int,
  completion_tokens int,
  cost_micros bigint,
  latency_ms  int,
  status      text NOT NULL,
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON llm_calls(run_id);
CREATE INDEX ON llm_calls(run_id, request_hash);

CREATE TABLE usage_ledger (
  id          bigserial PRIMARY KEY,
  team_id     uuid NOT NULL,
  project_id  uuid,
  run_id      uuid,
  user_id     uuid,
  kind        text NOT NULL,               -- tokens|image|tts_chars|music_sec|cli_min
  provider    text,
  quantity    numeric(18,4) NOT NULL,
  cost_micros bigint NOT NULL DEFAULT 0,
  meta_json   jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON usage_ledger(team_id, created_at);
CREATE INDEX ON usage_ledger(run_id);

CREATE TABLE audit_logs (
  id          bigserial PRIMARY KEY,
  team_id     uuid,
  actor_id    uuid,
  action      text NOT NULL,
  target_type text,
  target_id   text,
  detail_json jsonb NOT NULL DEFAULT '{}',
  ip          inet,
  user_agent  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_logs(team_id, created_at DESC);
CREATE INDEX ON audit_logs(actor_id, created_at DESC);

-- ============ 配置（团队级） ============
CREATE TABLE model_profiles (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id      uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  name         text NOT NULL,
  provider     text NOT NULL,              -- openai_compat|anthropic|google|ollama
  base_url     text,
  api_key_enc  bytea,                      -- AES-GCM/Fernet 加密
  model        text NOT NULL,
  temperature  real DEFAULT 0.7,
  max_tokens   int DEFAULT 8192,
  extra_json   jsonb NOT NULL DEFAULT '{}',
  supports_tools boolean DEFAULT true,
  supports_vision boolean DEFAULT false,
  verified_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (team_id, name)
);

CREATE TABLE asset_providers (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id     uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  kind        text NOT NULL,               -- image|tts|sfx|music
  driver      text NOT NULL,               -- 注册表 key
  name        text NOT NULL,
  config_json jsonb NOT NULL DEFAULT '{}', -- 按 driver fields schema 存（key 加密字段）
  enabled     boolean NOT NULL DEFAULT true,
  priority    int NOT NULL DEFAULT 100,
  verified_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agents (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id           uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  key               text NOT NULL,          -- planner|developer|artist|voice|tester
  name              text NOT NULL,
  system_prompt     text NOT NULL,
  model_profile_id  uuid REFERENCES model_profiles(id),
  fallback_profile_id uuid REFERENCES model_profiles(id),
  temperature       real,
  max_tool_rounds   int NOT NULL DEFAULT 20,
  timeout_s         int NOT NULL DEFAULT 1800,
  tools_json        jsonb NOT NULL,
  params_json       jsonb NOT NULL DEFAULT '{}',
  enabled           boolean NOT NULL DEFAULT true,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (team_id, key)
);

CREATE TABLE voice_profiles (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id      uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  character    text NOT NULL,
  provider_id  uuid REFERENCES asset_providers(id),
  voice_id     text,
  params_json  jsonb NOT NULL DEFAULT '{}',
  sample_path  text,
  notes        text,
  UNIQUE (project_id, character)
);

CREATE TABLE webhooks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id     uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  url         text NOT NULL,
  secret_enc  bytea,
  events      text[] NOT NULL,             -- run.succeeded, run.failed, approval.pending
  enabled     boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notifications (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  team_id    uuid NOT NULL,
  kind       text NOT NULL,                -- mention|run_done|run_failed|approval
  title      text NOT NULL,
  body       text,
  link       text,
  read_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON notifications(user_id, read_at, created_at DESC);
```

### 9.3 索引与容量策略

- `events` 表增长最快（一次 run 数千行）→ 按月分区 + 保留 90 天（可配），归档到对象存储。
- `llm_calls` / `usage_ledger` 保留 2 年（成本审计）。
- 每张表都有 `team_id`（`events`/`usage_ledger` 冗余存以便免 join 过滤）。
- 连接池：SQLAlchemy `pool_size=10, max_overflow=20`，`pool_pre_ping=True`；多副本时按副本数 × 池大小估算总连接，必要时上 PgBouncer。

---

## 10. 后端 API

### 10.1 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/setup/status` | 是否已完成首启引导 |
| POST | `/api/setup` | 一次性 token 创建首个 owner + 团队 |
| POST | `/api/auth/login` | 邮箱密码 → Set-Cookie |
| POST | `/api/auth/logout` | 撤销会话 |
| GET | `/api/auth/me` | 当前用户 + 团队列表 + 各团队角色 + 权限集 |
| POST | `/api/auth/refresh` | 续期 |
| GET | `/api/auth/oidc/{provider}` / callback | OIDC（可选） |
| GET/POST/DELETE | `/api/tokens` | 个人访问令牌（CLI 用） |

### 10.2 团队与成员

| 方法 | 路径 | 权限 |
|---|---|---|
| GET/POST | `/api/teams` | 登录用户 / — |
| PATCH/DELETE | `/api/teams/{id}` | owner |
| GET/POST | `/api/teams/{id}/members` | admin |
| PATCH/DELETE | `/api/teams/{id}/members/{uid}` | owner（admin 不能改 admin） |
| GET | `/api/teams/{id}/usage?from=&to=` | admin；developer/viewer 只读自己 |
| GET | `/api/teams/{id}/audit?...` | admin |

### 10.3 项目

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/projects` | 列表 / 新建（校验 repo_path、git、探测项目类型与命令） |
| PATCH/DELETE | `/api/projects/{id}` | 改名/改命令/改预算 / 归档 |
| POST | `/api/projects/{id}/detect` | 重新探测 |
| GET | `/api/projects/{id}/lock` | 当前锁持有者（谁在占用、剩余时间、排队位置） |
| POST | `/api/projects/{id}/lock/force-release` | admin，写审计 + 审批 |
| GET | `/api/projects/{id}/branches` | agent 分支列表（含是否可合并、是否基于旧主干） |
| POST | `/api/projects/{id}/merge` | 发起合并审批 |
| GET | `/api/projects/{id}/artifacts?...` | 素材列表（可筛占位） |

### 10.4 会话 / 执行

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/projects/{id}/sessions` | 项目内会话（全员可见） |
| GET | `/api/sessions/{id}/messages` | 历史 |
| POST | `/api/sessions/{id}/runs` | **发起 run**（`Idempotency-Key` 支持）；返回 run + 队列位置 |
| GET | `/api/runs?project_id=&status=&mine=` | run 列表（分页） |
| GET | `/api/runs/{id}` | 详情（状态/分支/用量/汇总） |
| GET | `/api/runs/{id}/tasks` | 任务树 |
| GET | `/api/runs/{id}/events?after_seq=` | 事件补拉 |
| POST | `/api/runs/{id}/cancel` | 自己的 run；他人需 admin |
| POST | `/api/runs/{id}/retry` | 失败重试（复用分支或重建） |
| GET | `/api/runs/{id}/diff` | 变更报告 |
| GET | `/api/runs/{id}/test-report` | 测试报告 |
| GET | `/api/runs/{id}/queue-position` | 排队位置 + 预计等待 |

### 10.5 配置（团队级）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET/POST | `/api/model-profiles`；PATCH/DELETE `/api/model-profiles/{id}` | admin |
| POST | `/api/model-profiles/{id}/test` | admin |
| GET | `/api/providers/drivers` | 登录用户（只读 schema） |
| GET/POST/PATCH/DELETE | `/api/asset-providers` | admin |
| POST | `/api/asset-providers/{id}/test` | admin |
| GET/PATCH | `/api/agents`、`/api/agents/{key}` | admin |
| POST | `/api/agents/{key}/reset-prompt` | admin |
| GET/PATCH | `/api/settings` | admin |

### 10.6 协作

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/comments?target_type=&target_id=` | 评论 |
| PATCH | `/api/comments/{id}` | 编辑/标记 resolved（作者或 admin） |
| GET | `/api/approvals?status=pending` | 待我审批 |
| POST | `/api/approvals/{id}/decide` | admin/owner |
| GET/PATCH | `/api/notifications` | 通知列表 / 标记已读 |

### 10.7 WebSocket

`wss://host/ws/projects/{project_id}`（连接时校验成员身份）

客户端 → 服务端：

```json
{"type":"subscribe","project_id":"...","last_seq":{"run_42":120}}
{"type":"chat","session_id":"...","content":"给主界面加卡牌详情弹窗","idempotency_key":"..."}
{"type":"cancel","run_id":"..."}
{"type":"comment","target_type":"run","target_id":"...","body":"@carol 看下这里"}
{"type":"presence_ping"}
```

服务端 → 客户端：

```json
{"type":"event","project_id":"...","run_id":"...","task_id":"...",
 "parent_task_id":"...","agent":"artist","seq":137,"ts":"...",
 "kind":"tool_call",
 "payload":{"name":"generate_image","args":{...},"display":"美术正在生成：稀有度边框（史诗）"}}
```

`kind` 全集：

```
run_queued, run_start, run_end, run_cancelled, run_failed, lock_acquired, lock_released, queue_position
agent_start, agent_end
thought, message_delta, message_end
tool_call, tool_result, tool_error
cmd_output, cmd_heartbeat
task_status
artifact, placeholder_warn
diff_report, test_report, work_order
budget_warn, approval_requested, approval_decided
comment_created, notification
presence_update
error
```

未知 `kind` → 前端渲染成通用折叠块（前向兼容）。

### 10.8 运维接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | liveness（进程活着） |
| GET | `/readyz` | readiness（DB + Redis 连通 + 迁移已到最新） |
| GET | `/metrics` | Prometheus 指标（需内网/令牌） |
| GET | `/api/admin/version` | 版本 + git sha + 构建时间 + 迁移版本 |
| POST | `/api/admin/shutdown` | 仅 lite / 单机；server 模式禁用 |
| GET | `/api/admin/logs?tail=200` | 尾部日志（诊断包） |

---

## 11. 前端界面

### 11.1 信息架构

```
顶部：Team ▾ | Project ▾ | 在线头像堆 | 队列徽章(2) | 通知 🔔 | 用户菜单
左侧：会话列表（项目内共享，含发起人头像） | 我参与的 run
主区：对话流 + 执行过程流（同一时间轴，AI 步骤默认折叠）
右栏：任务树 / Diff / 测试报告 / 素材 / 评论 / 审批
```

### 11.2 页面清单

| 页面 | 关键交互 | 权限感知 |
|---|---|---|
| **登录 / 首启引导** | 密码登录；OIDC 按钮；`/setup` 建团队 | — |
| **对话页**（主） | 选项目 → 发消息 → 流式；「▸ 4 个内部步骤」折叠；工具调用卡片；素材内联缩略图；**排队时显示队列位置与预计等待** | viewer 输入框禁用并提示「你是只读成员」 |
| **Run 详情** | 任务树 + 事件流 + Diff + 测试报告 + 素材 + 评论；「合并到 main」按钮 | 合并按钮仅 admin/owner 可见 |
| **项目页** | 仓库状态、分支列表、锁占用、命令配置、预算、成员 | 配置区 admin 可编辑，其他人只读 |
| **团队设置** | 成员与角色、邀请、团队预算、审计日志、用量看板 | owner/admin |
| **配置** | 模型档位 / 智能体 / 供应商（schema 驱动表单）/ 占位模式开关 | admin only |
| **素材库** | 按项目/run/类型/占位筛选；「批量重画占位」 | developer+ |
| **审批中心** | 待我审批的合并/预算申请 | admin/owner |
| **我的** | 我的 run、我的用量、访问令牌 | 全部 |

### 11.3 多人协作的 UI 细节

- **在线状态**：看同一 run 的人显示头像堆；鼠标悬停「carol 正在查看」。
- **谁点了发送**：每条用户消息带头像+名字；AI 回复标注「由 planner（deepseek-v3）生成」。
- **排队必须可见**：bob 的 run 在 queued 时，页面显著展示「前面 1 个任务（run#42，已 3 分钟）」+ 取消按钮，而不是转圈。
- **锁被占用时**：显示持有者与剩余 TTL；admin 看到「强制释放」。
- **分支提示**：run 详情顶部常驻「分支 `agent/run-42` · 基线 `main@a1b2c3d`」；主干有更新时提示「主干已前进 2 个提交，建议 rebase」。
- **占位必须一眼可辨**：橙色角标 + 素材库筛选 + 汇报单列一段。
- **权限要解释**：viewer 看不到的按钮不是隐藏，而是禁用并给 tooltip（「需要 admin 权限」）。
- **错误可读**：`401` → 「模型档位 X 的 key 无效」，带跳转按钮。

---

## 12. 目录结构

```
carGameAgent/
├─ pyproject.toml
├─ README.md
├─ .env.example                     # server 模式
├─ docker-compose.yml               # api/worker/postgres/redis/minio/caddy
├─ Dockerfile                       # 多阶段：node build → python runtime
├─ alembic.ini
├─ docs/
│  ├─ DESIGN.md                     # 本文件
│  ├─ PROTOCOL.md                   # 事件 schema / 桥接协议
│  └─ DEPLOY.md                     # 部署运维手册（M10 产出）
├─ deploy/
│  ├─ caddy/Caddyfile
│  ├─ postgres/init.sql
│  ├─ k8s/                          # 可选：Deployment/Service/Ingress/HPA/RWX PVC
│  ├─ windows/                      # lite 与 on-prem：package.cmd / start.cmd / stop.cmd
│  └─ unix/                         # package.sh / start.sh / stop.sh / backup.sh / restore.sh
├─ examples/
│  └─ cardgame/                      # ★ 示例项目（D16）：最小 Python 卡牌游戏
│     ├─ main.py                     #   入口：卡牌列表 + 主界面（纯终端/或 tkinter）
│     ├─ game/cards.py               #   卡牌数据模型（name/atk/hp/cost）
│     ├─ game/ui/                    #   界面层（卡牌网格渲染）
│     ├─ assets/art/                 #   预留素材目录（含 art_bible.md 示例）
│     ├─ tests/test_cards.py         #   已有单测（测试角色的基准）
│     └─ README.md                   #   说明「这是给 GameAgent 练手的示例项目」
├─ scripts/                         # 开发期脚本
│  ├─ dev.cmd  / dev.sh
│  ├─ gen_secret.py                 # 生成 GA_SECRET_KEY
│  └─ seed_demo.py                  # 造演示数据（团队/项目/会话）
├─ backend/
│  └─ game_agent/
│     ├─ __init__.py                # __version__
│     ├─ __main__.py                # lite 入口：python -m game_agent
│     ├─ main_api.py                # server 入口：API 进程
│     ├─ main_worker.py             # server 入口：worker 进程
│     ├─ config.py                  # pydantic-settings（env 驱动）
│     ├─ db/
│     │  ├─ engine.py               # async engine（pg / sqlite）
│     │  ├─ models.py               # SQLModel 表
│     │  ├─ repositories.py         # ★ Repository 接口 + 两个实现
│     │  └─ migrations/             # alembic versions
│     ├─ auth/
│     │  ├─ passwords.py  sessions.py  tokens.py  oidc.py
│     │  └─ rbac.py                 # ★ 权限矩阵与 require()
│     ├─ tenancy/
│     │  ├─ context.py              # RequestContext（team/project 注入）
│     │  └─ guards.py               # 团队/项目归属校验
│     ├─ infra/
│     │  ├─ lock.py                 # ★ LockManager（redis | asyncio）
│     │  ├─ bus.py                  # ★ EventBus（redis streams | in-proc）
│     │  ├─ queue.py                # ★ Queue（pg skip locked | in-proc）
│     │  ├─ storage.py              # ★ ObjectStore（s3 | local）
│     │  └─ cache.py
│     ├─ api/
│     │  ├─ deps.py  auth.py  teams.py  projects.py  sessions.py
│     │  ├─ runs.py  artifacts.py  comments.py  approvals.py
│     │  ├─ config_routes.py  usage.py  audit.py  admin.py  ws.py
│     ├─ llm/  base.py openai_compat.py anthropic.py google.py ollama.py router.py
│     ├─ agents/ runtime.py registry.py context.py prompts/*.md
│     ├─ tools/  base.py fs.py shell.py handoff.py image.py audio.py
│     │          workorder.py report.py git.py ask.py coder_cli.py
│     ├─ cli_adapters/           # M9
│     ├─ providers/ registry.py base.py placeholder.py image/*.py audio/*.py
│     ├─ orchestration/
│     │  ├─ orchestrator.py      # spawn / 状态机 / 取消
│     │  ├─ worker.py            # 领取队列、加锁、执行、收尾、释放
│     │  ├─ git_ops.py           # ★ 分支创建/提交/合并/回滚
│     │  ├─ diff.py  workorder.py  detect.py  capabilities.py  budget.py
│     │  ├─ notify.py            # webhook / 邮件
│     │  └─ cleanup.py           # 过期分支/事件归档
│     └─ web/                    # 前端构建产物（打包时生成）
├─ frontend/
│  └─ src/
│     ├─ pages/  Login Chat RunDetail Project TeamSettings Config Assets Approvals Me
│     ├─ components/  ...（同 v0.2 + PresenceStack ReviewThread ApprovalCard QueueBanner）
│     ├─ stores/  auth teams projects runs notifications
│     └─ lib/  ws.ts（断线重连 + last_seq 补拉）
└─ tests/
   ├─ test_rbac.py               # ★ 权限矩阵全覆盖
   ├─ test_lock.py               # ★ 锁：并发、TTL、fencing、僵尸 worker
   ├─ test_queue.py              # ★ SKIP LOCKED 不被重复领取
   ├─ test_git_ops.py            # ★ 分支创建/提交/合并/回滚
   ├─ test_tenancy.py            # ★ 跨团队越权访问必须 404
   ├─ test_idempotency.py        # 重复请求不重复烧钱
   ├─ test_runtime.py  test_handoff.py  test_tools_fs.py
   ├─ test_placeholder.py  test_registry.py  test_detect.py  test_diff.py
   ├─ test_ws_fanout.py          # 多副本事件扇出 + 补拉
   └─ test_api_smoke.py
```

---

## 13. 配置与环境变量

### 13.1 server 模式（`.env`）

```dotenv
# ---- 基本 ----
GA_MODE=server
GA_VERSION_CHECK=true
GA_PUBLIC_URL=https://ga.example.internal
GA_LOG_LEVEL=info
GA_LOG_FORMAT=json

# ---- 数据库 ----
DATABASE_URL=postgresql+asyncpg://ga:${PG_PASSWORD}@postgres:5432/gameagent
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20

# ---- Redis ----
REDIS_URL=redis://redis:6379/0

# ---- 安全（必须自己生成，切勿用默认）----
GA_SECRET_KEY=<openssl rand -base64 32>
GA_SESSION_TTL_HOURS=168
GA_COOKIE_SECURE=true
GA_ALLOWED_HOSTS=ga.example.internal,localhost

# ---- 认证（D17：OIDC + 密码共存）----
GA_AUTH_MODE=both                      # password | oidc | both
OIDC_ALLOWED_EMAIL_DOMAINS=yourcompany.com
OIDC_AUTO_JOIN_TEAM=game-team
OIDC_DEFAULT_ROLE=developer
OIDC_GROUP_ROLE_MAP={"ga-admins":"admin","ga-devs":"developer"}
OIDC_AUTO_CREATE_USER=true

# ---- 工作区 ----
PROJECTS_ROOT=/data/projects          # ★ 所有 api/worker 必须挂同一份
BACKUP_GIT_MIRROR=/mnt/backup/repos   # 本地 git 的镜像备份目标（NAS/网络盘，可空）
WORKER_ID=worker-1
WORKER_CONCURRENCY=1                  # ★ 3–10 人场景保持 1（受项目锁约束，开大只占内存）
PROJECT_LOCK_TTL_S=120
PROJECT_LOCK_HEARTBEAT_S=30
QUEUE_WAIT_TIMEOUT_S=1800

# ---- 对象存储（可选）----
STORAGE_BACKEND=s3                    # s3 | local
S3_ENDPOINT=http://minio:9000
S3_BUCKET=gameagent-artifacts
S3_ACCESS_KEY=...
S3_SECRET_KEY=...

# ---- 预算与配额 ----
TEAM_MONTHLY_TOKENS=20000000
TEAM_MONTHLY_IMAGES=2000
PLACEHOLDER_ENABLED=true
MAX_IMAGE_PER_RUN=20
MAX_TTS_CHARS_PER_RUN=8000
MAX_TOKEN_PER_RUN=3000000
MAX_RUN_MINUTES=60
MAX_FIX_ROUNDS=3
MAX_HANDOFF_DEPTH=4
MAX_TOOL_ROUNDS=20

# ---- 命令白名单 ----
CMD_ALLOW=python,python3,py,pip,pytest,ruff,mypy,black,flake8,git,ffmpeg
CMD_DENY=rm -rf,del /f,format,shutdown,rmdir /s,pip install --user,git push --force

# ---- 通知 ----
SMTP_URL=
WEBHOOK_VERIFY_SSL=true

# ---- 观测 ----
METRICS_ENABLED=true
SENTRY_DSN=
EVENTS_RETENTION_DAYS=90
```

### 13.2 lite 模式（`~/.trace/config.toml`）

```toml
[mode]
engine = "lite"

[server]
host = "127.0.0.1"
port = 8765
auto_open_browser = true

[store]
data_dir = "~/.trace"
db_path  = "~/.trace/gameagent.db"
projects_root = "~/.trace/projects"
backup_git_mirror = ""          # 可选：NAS / 网络盘路径（本地 git 的镜像备份）

[auth]
mode = "password"               # lite 默认仅密码；要 SSO 改成 both 并配 OIDC_* 环境变量
session_ttl_hours = 168

[placeholder]
enabled = true
image_bg = "#2B3A67"

[budget]
max_images_per_run = 20
max_tts_chars_per_run = 8000
max_tokens_per_run = 3000000
on_exceed = "ask"
```

> lite 模式**同样有账号体系**（多用户可用），只是不适合 3 人以上并发。生产/团队请用 server 模式。

---

## 14. 安全

| 类别 | 措施 |
|---|---|
| 认证 | Argon2id；会话 token 只存 hash；Cookie `HttpOnly+Secure+SameSite=Lax`；5 次失败锁 15 分钟；登录/失败均审计 |
| 授权 | 每个路由显式 `require(perm)`；`team_id` 只从认证上下文取；跨团队访问统一返回 **404**（不泄露存在性）；单元测试覆盖矩阵 |
| CSRF | 双提交 Cookie；写操作校验 header token |
| 密钥 | `GA_SECRET_KEY` 来自 env/secret manager；数据库里只存 AES-GCM 密文；解密后仅在内存中短暂存在；日志与异常统一脱敏（`sk-…`, `Bearer …`） |
| 传输 | 全站 HTTPS（Caddy 自动）；HSTS；`GA_COOKIE_SECURE=true` |
| 输入 | Pydantic 严格校验；路径参数一律 resolve + 前缀校验（防穿越、防符号链接逃逸） |
| SSRF | 模型/供应商的 `base_url` 做允许清单（禁 `169.254.0.0/16`、`127.0.0.00/8` 以外的内网探测需 admin 显式开启） |
| 命令执行 | 白名单前缀 + deny 正则；未检测 venv 拒绝 `pip install`；高危命令走审批 |
| 子进程隔离 | 只传白名单 env（不传任何 API key）；工作目录锁定项目根；资源限制（Linux `RLIMIT_CPU/AS` 可选） |
| 桥接 | token 绑定 `(run_id, worker_id)`，仅内网/回环，run 结束失效 |
| 限流 | 每用户每分钟 run 创建数、每 IP 登录尝试数（Redis INCR + TTL） |
| 配额 | 团队月度 token/图片/TTS 配额；单 run 上限；超限拒绝并提示（不静默降级） |
| 审计 | 全配置变更 + 成员变更 + run 启停 + 审批 + 密钥字段变更（不记值） |
| 依赖 | `pip-audit` / `npm audit` 在 CI；锁定版本；镜像定期重建 |
| 容器 | 非 root 用户运行；只读根文件系统（除数据卷）；禁 `--privileged` |
| 备份 | DB 与 `PROJECTS_ROOT` 分别备份；**密钥文件单独备份**（丢了等于所有供应商 key 失效） |

---

## 15. 打包、部署与运维

### 15.1 两种交付形态（D13：**双轨，同期交付**）

| 形态 | 产物 | 目标机要求 | 定位 |
|---|---|---|---|
| **A · server** | `docker-compose.yml` + `.env` + 镜像 | Linux、Docker 24+、4C8G 起 | **团队生产主线** |
| **B · lite** | `GameAgent-<ver>-win64.zip` / `-linux.tar.gz` | Windows 10+/Linux、Python 3.11+ | **单机试用 / 演示 / 离线 / 小团队兜底** |

**「都要」的具体含义**（避免做成两个产品）：

1. **同一份代码**，仅 `GA_MODE` 与后端实现不同（五接口抽象，§2.1）。
2. **同一个里程碑验收**：M10 必须两条线都跑通，不是 lite 做完就算完。
3. **文档双主线**：`DEPLOY.md` 的 server 与 lite 各有一套完整步骤，任一条都能从零走通。
4. **数据可迁移**：lite 的 SQLite 提供 `ga-admin export` → 导入 Postgres（`--from sqlite`），团队长大了可以升级到 server，不用重来。
5. **lite 不做的**：不承诺多机、不承诺 3 人以上并发、不接 OIDC（默认关，可手动开）。

### 15.2 server 部署（团队生产）

```
git clone / 解压发布包
cp .env.example .env && 编辑（DATABASE_URL / REDIS_URL / GA_SECRET_KEY）
docker compose up -d
docker compose ps          # 等 healthy
docker compose run --rm api python -m game_agent.migrate   # 迁移（首次与每次升级）
访问 https://host/setup    # 用启动日志里的一次性 token 创建 owner
```

`docker-compose.yml` 服务清单：

| 服务 | 镜像/构建 | 副本 | 说明 |
|---|---|---|---|
| `caddy` | caddy:2 | 1 | TLS + 反代 + 限流；80/443 |
| `api` | 本项目 Dockerfile（api 入口） | 1–N | 无状态；healthcheck `/readyz` |
| `worker` | 同一镜像（worker 入口） | 1–M | 挂 `PROJECTS_ROOT`；不要暴露端口 |
| `scheduler` | 同镜像（单例） | 1 | 清理过期分支/事件归档/通知重试 |
| `postgres` | postgres:15 | 1 | 数据卷 `pgdata` |
| `redis` | redis:7-alpine | 1 | `appendonly yes` |
| `minio` | minio/minio | 1 | 素材归档（可选） |

**Dockerfile 多阶段**：

```
stage 1 (node:20)  → npm ci && npm run build → /app/web
stage 2 (python:3.12-slim) → pip install .（含 web 产物）→ 非 root 用户 → CMD
```

**单机资源分配建议**（D14：3–10 人，4C8G 推荐档）：

| 服务 | 内存上限 | CPU | 备注 |
|---|---|---|---|
| postgres | 2G（`shared_buffers=512MB`） | 1.0 | 数据量与事件表增长慢，够用 |
| redis | 512M（`maxmemory-policy allkeys-lru`） | 0.5 | 只放锁/在线态/短流，不落大盘数据 |
| api | 1G | 1.0 | 单副本够；发版时先起第二副本 |
| worker ×1 | 2G | 1.5 | 默认 1 个；多项目并行时才加第二个 |
| caddy | 128M | 0.2 | TLS + 反代 |
| minio（可选） | 512M | 0.5 | 不用对象存储就删掉这个服务 |

> 总内存 ≈ 6G，留 2G 给系统与突发。**不要给 worker 开大并发**：受项目锁约束，同项目仍串行，开大只会让内存被占满而吞吐不变（`WORKER_CONCURRENCY=1` 是 3–10 人场景的正确值）。

### 15.3 lite 部署（一键包）

**一键打包 `deploy/windows/package.cmd`**：

```
0  校验 python>=3.11 / node>=20 / npm / git
1  清理 dist/ build/ backend/game_agent/web/
2  创建/复用 .venv（独立环境，D7）
3  .venv\Scripts\pip install -e ".[build]"
4  cd frontend && npm ci && npm run build   → backend/game_agent/web/
5  写入 build_info.json（版本 + git sha + 构建时间）
6  .venv\Scripts\python -m pytest -q        （不过则中止，不出包）
7  .venv\Scripts\python -m build --wheel
8  组装 dist\GameAgent-<ver>-win64\
     wheel/  start.cmd  stop.cmd  status.cmd  config.example.toml  docs/  README.md
9  压缩为 GameAgent-<ver>-win64.zip
10 打印产物路径 + 大小 + sha256
```

退出码：`0` 成功；`1` 环境缺失；`2` 前端构建失败；`3` 测试失败；`4` 打包失败。

`package.sh` 同理（linux/mac）。

**启停脚本契约**（`.cmd` 与 `.sh` 行为一致）：

| 脚本 | 行为 | 退出码 |
|---|---|---|
| `start` | ①PID+health 双检，已在运行则打印 URL 退出 0 ②端口占用检查（非本服务则报错） ③校验 venv/依赖，缺失则创建安装（需 `-y`/交互确认） ④后台启动写 PID ⑤轮询 `/healthz` ≤60s ⑥开浏览器 ⑦打印 URL/PID/日志路径 | 0/2/3/4 |
| `stop` | ①读 PID ②`SIGTERM`/`CTRL_BREAK` ③等 15s ④未退 `taskkill /T /F` 或 `kill -9` ⑤删 PID | 0/1 |
| `status` | 状态/PID/端口/版本/迁移版本/数据目录/最近 20 行日志 | 0 运行 / 1 停止 |
| `backup` / `restore` | 见 §15.7 | 0/1 |

**优雅关停**：lite 用 `POST /api/admin/shutdown`（仅回环 + 本机 token 文件），要做的收尾顺序：停止领新任务 → 等在途 run 到安全点（或标记 orphaned）→ flush 事件 → SQLite `wal_checkpoint` → 杀子进程树 → 释放锁 → 退出。server 模式禁用该接口，用 `docker compose stop`（worker 收 SIGTERM 后走同一套收尾）。

### 15.4 共享文件系统（多机部署必须读）

| 场景 | 要求 |
|---|---|
| 单机 compose | 宿主机目录 bind mount 到 `api` 与 `worker`，路径一致 |
| 多机 worker | 必须挂同一共享文件系统到同一路径（NFS v4 / CephFS / EFS / Azure Files） |
| K8s | 必须 **RWX** PVC；RWO 会导致多副本 worker 看到不同目录 |
| 备选 | worker 亲和：项目绑定到固定 worker 组（`projects.pinned_worker_group`，v2） |

**自检**：worker 启动时向 `PROJECTS_ROOT` 写一个 `<WORKER_ID>.probe`，并扫描其它 worker 的 probe 文件；若 30s 内看不到任何其它 worker 的 probe 且配置了多 worker → 打 **WARNING 日志 + `/readyz` 降级**，提示「疑似未共享挂载」。

### 15.5 一键脚本清单

| 平台 | 脚本 |
|---|---|
| Windows (server) | `install.cmd`（检查 docker → 生成 .env → `compose up -d`）、`start.cmd`、`stop.cmd`、`status.cmd`、`logs.cmd` |
| Windows (lite) | `package.cmd`、`start.cmd`、`stop.cmd`、`status.cmd`、`backup.cmd` |
| Linux/macOS | `install.sh`、`package.sh`、`start.sh`、`stop.sh`、`status.sh`、`backup.sh`、`restore.sh` |
| 服务化 | Windows: `install_service.cmd`（nssm 包装 `start.cmd`）；Linux: `gameagent.service`（systemd unit，含 `Restart=always`、`LimitNOFILE`、`EnvironmentFile`） |

### 15.6 数据库迁移与升级

- Alembic 管理；`python -m game_agent.migrate` 在部署流水线里**先于新版本启动**执行（compose 用 `depends_on` + 独立 one-off 容器）。
- 启动时 `readyz` 校验 `alembic_version` == 代码期望版本；不一致 → 不就绪（避免半升级状态接流量）。
- 迁移前自动 `pg_dump` 快照（`migrations/backup/`，保留最近 5 份）。
- **回滚**：代码回滚 + DB 恢复到迁移前快照（不做 down-migration 自动化，风险高于收益）。
- 升级顺序：备份 → 迁移 → 滚动重启 api → 滚动重启 worker → 冒烟脚本。

### 15.7 备份与恢复

| 对象 | 方式 | 频率 | 保留 |
|---|---|---|---|
| PostgreSQL | `pg_dump -Fc` 全量 + 可选 WAL 归档（PITR） | 每日 | 30 天 |
| `PROJECTS_ROOT` | **本地 git（D15）没有远端兜底 = 唯一副本**，必须主动备份：`git bundle --all` + 目录 rsync/snapshot 双份 | 每日 + 每次合并后 | 30 天 |
| 仓库另存 | 可选 `BACKUP_GIT_MIRROR=/mnt/backup/repos`（网络盘/NAS），每次合并后 `git push --mirror` | 每次合并 | — |
| MinIO/S3 | 桶版本化 | — | 90 天 |
| `GA_SECRET_KEY` | **单独**存密码管理器/KMS | 变更时 | 永久 |

**本地 git 的备份设计（关键，对应风险 R16）**：

```bash
# 每日：bundle 全仓（含所有分支，单文件，可离线恢复）
git -C $PROJECTS_ROOT/$slug bundle create $BACKUP/$slug-$(date +%F).bundle --all

# 每次合并后：可选推一份裸镜像到 NAS
git -C $PROJECTS_ROOT/$slug push --mirror $BACKUP_GIT_MIRROR/$slug.git
```

- `backup.sh` 里默认**都做**：`pg_dump` + 每仓 `git bundle` + 目录 tar；`restore.sh` 支持从 bundle 恢复（`git clone bundle → 放回 PROJECTS_ROOT`）。
- 备份完成写一条 `settings.last_backup_at`；UI 上 admin 能看到「上次备份：2 小时前」，超过 48 小时未备份显示黄色告警。
- **演练要求**：M10 验收必须真的做一次 `backup → 删库 → restore` 全流程，不是只检查脚本存在。


`restore.sh`：停 api/worker → 恢复 DB → 校验 `alembic_version` → 起服务 → 冒烟。

### 15.8 监控与告警

| 指标 | 阈值建议 |
|---|---|
| `ga_queue_depth{project}` | >5 持续 10 分钟 → 告警（人手不足或卡死） |
| `ga_run_duration_p95` | >45 分钟 → 关注 |
| `ga_run_failure_ratio` | >30%（1 小时窗口）→ 告警 |
| `ga_lock_wait_p95` | >10 分钟 → 告警 |
| `ga_llm_error_ratio` | >10% → 供应商/密钥问题 |
| `ga_placeholder_ratio` | 长期 100% → 提醒配供应商 |
| `ga_monthly_cost` | 超团队配额 80% → 提醒 |
| PG: 连接数/慢查询/磁盘 | 标准告警 |
| Redis: 内存/命中率/持久化失败 | 标准告警 |

日志：JSON 结构化，字段贯穿 `request_id / team_id / project_id / run_id / task_id / user_id`。容器日志走 stdout，由 Docker/云日志采集。

### 15.9 扩容路径

1. 先加 `worker` 副本（受项目锁限制，同项目仍串行；多项目才真正并行）。
2. 再加 `api` 副本（WS 无需 sticky，因为事件走 Redis 广播 + DB 补拉）。
3. PG 读写分离 / PgBouncer。
4. 事件表分区 + 冷数据归档到对象存储。
5. 超 50 人 → K8s + worker HPA（按队列深度）+ RWX PVC。

### 15.10 `docs/DEPLOY.md` 章节（M10 产出，结构已定）

1. 环境要求与端口
2. 两种形态选择（server / lite）
3. server 全新部署（Linux，逐步命令）
4. lite 全新部署（Windows / Linux）
5. 离线/内网部署（镜像 `docker save/load`；lite 用 `vendor/wheels/` + `--no-index --find-links`）
6. 首次启动引导清单（owner → 团队 → 项目 → 模型档位 → 智能体绑定 → 供应商 → 试跑一句）
7. 启停与开机自启（systemd / nssm / 计划任务）
8. 反向代理与 TLS（Caddyfile / Nginx 示例）、局域网与公网访问差异
9. 目录、数据与备份恢复
10. 升级与回滚（含迁移）
11. 监控接入与告警规则
12. 常见故障排查表（端口占用、迁移未执行、锁未释放、worker 看不到工作区、401/403 定位、占位素材误判）
13. 卸载与数据保留

---

## 16. 里程碑与验收

分三个阶段，每个阶段结束都是一个「可演示、可交付」的状态。

### 阶段一 · 单团队可用（M0–M6，约 13.5 天）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0** 骨架（1.5d） | 项目结构、配置、DB 抽象层（Repository/EventBus/Queue/Lock/Storage 五接口 + lite 实现）、Alembic 初始迁移、`/healthz`、前端壳、**`examples/cardgame/` 示例工程（D16）** | `python -m game_agent` 起来，`/healthz` 通，空库自动迁移成功；`pytest examples/cardgame` 能跑绿 |
| **M1** 身份与租户（3d） | 用户/团队/成员/RBAC、密码登录、会话、首启引导、**OIDC（D17）**、PAT、项目 CRUD、审计写入 | 三种角色登录后看到不同界面；越权接口一律 403/404；**用 Keycloak/Entra 演练一次 OIDC 登录并自动入组**；审计有记录 |
| **M2** 配置口（1.5d） | 团队级模型档位（加密+测试）、智能体 prompt 编辑、供应商驱动注册表 + schema 表单、能力探测 | 5 个角色绑不同模型都测通；供应商页能存配置并测试 |
| **M3** 对话与执行流（2d） | 会话、WS 扇出、事件落库与补拉、PlannerRuntime、任务树、时间线 | 发一句话能看到完整执行流；断网重连不丢事件；点停止能停 |
| **M4** 开发执行与分支（2.5d） | 原生 fs 工具、`run_command`、git 分支创建/提交（本地 git）、ChangeReport、Diff 视图 | **在 `examples/cardgame/` 上**说「加稀有度字段并在列表按稀有度显示」→ 改对文件、提交到 `agent/run-x`、diff 可见、`pytest` 绿 |
| **M5** 素材链路（2d） | 占位驱动、Artist/Voice 角色、`request_art`/`request_audio`、ArtBible、素材库、重画 | 零 key 跑通全链路且标占位；配 key 后一键重画变真图、代码不改 |
| **M6** 测试回环（1.5d） | Tester、checklist 渲染、失败回环 ≤3 轮、升级兜底 | 故意留 bug 能抓到并修好 |

### 阶段二 · 团队协作（M7–M8，约 4.5 天）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M7** 并发与锁（1.5d） | Redis 锁 + fencing、排队与位置展示、取消、幂等键、孤儿 run 恢复 | 两人同时发需求 → 一个跑一个排队；杀 worker 后锁自动释放；重复请求不重复烧钱 |
| **M8** 协作与评审（3d） | presence、评论/@提及、通知、审批（合并/预算）、**本地 git 自建评审：文件树双栏 diff + 行内评论 + 合并门槛校验（§5.3.1）**、用量看板、审计查询 | 多人同看一条流并评论；行内评论可 resolved；合并必须先审批且通过检查项；看板能看到团队本月消耗 |

### 阶段三 · 生产交付（M9–M11，约 6 天）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M9** 编码 CLI 接入（1.5d） | claude code / opencode 适配器、三通道桥接 | 装上 CLI 后工具自动出现并跑通 |
| **M10** 部署与运维（3.5d，**双轨**） | Dockerfile、compose、caddy、迁移 job、备份/恢复（含 `git bundle`）、Prometheus、`DEPLOY.md` 双主线、一键脚本、lite 打包、**SQLite→Postgres 导迁** | **两条线都要跑通**：①全新 Linux 服务器 30 分钟上线；②干净 Windows 上解压 lite zip 能起；③`backup.sh → 删库 → restore.sh` 真演练通过；④lite 数据能导入 server |
| **M11** 收尾（1d） | 错误文案、诊断包、README、演示脚本、端到端演练 | 新机器从零到「发一句话出可用分支」全流程走通；`examples/cardgame` 演示脚本可一键复现 |

**合计约 24 个工作日**（≈4.8 周）。按 1.4 倍冗余（含调试与返工）≈ **34 人日**。

| 阶段 | 工期 | 累计 | 里程碑状态 |
|---|---|---|---|
| 一 · 单团队可用 | 13.5d | 13.5d | 能登录、能配角色、能对话、能改代码、能出素材、能测试 |
| 二 · 团队协作 | 4.5d | 18d | 多人并发安全、能评审合并、有审批与用量 |
| 三 · 生产交付 | 6d | **24d** | 双轨可部署、可备份恢复、可运维 |

**建议节奏**：阶段一走完先内部试用一周，再进阶段二。并发和协作的坑，只有真跑起来才暴露。

---

## 17. 风险与对策

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| R1 | 多人并发改同一工作区 → 代码错乱 | **极高** | 项目读写锁 + run 串行 + fencing token + 每 run 独立分支（§6） |
| R2 | 多机 worker 未共享挂载 → 静默数据不一致 | **极高** | 部署文档硬性要求 + worker 启动 probe 自检 + `/readyz` 降级 |
| R3 | 权限越界（跨团队/越级操作） | 高 | 路由级 `require()` + `team_id` 只取自上下文 + 越权返回 404 + 矩阵单测 |
| R4 | 密钥泄露 | 高 | env/KMS 主密钥 + DB 密文 + 日志脱敏 + 审计只记字段不记值 |
| R5 | worker 崩溃导致 run 半途而废 | 高 | 状态机 + task 幂等跳过 + 锁 TTL 自动释放 + `orphaned` 可恢复 |
| R6 | 项目锁没释放 → 项目卡死 | 高 | 心跳续租 + TTL + admin 强制释放（审批 + 审计） |
| R7 | 原生模式下模型改错文件 | 高 | 未读不许写 + 唯一命中替换 + 交差前自检 + git 分支兜底（可整体回滚） |
| R8 | 上下文爆掉 | 中 | 工具结果截断 + 摘要压缩 |
| R9 | 占位素材被误当成品 | 高 | 硬标记 + 角标 + 汇报单列 + 素材库筛选 |
| R10 | 成本失控 | 高 | 团队配额 + 单 run 上限 + 用量台账 + 80% 告警 + 超限拒绝 |
| R11 | 测试角色「假装通过」 | 高 | 强制 evidence + 无证据判 blocked |
| R12 | 升级时库结构变更导致故障 | 中 | Alembic + 迁移前备份 + `readyz` 版本校验 + 停流量再迁 |
| R13 | 事件表膨胀拖慢库 | 中 | 按月分区 + 90 天保留 + 归档 |
| R14 | 模型不按格式调工具 | 中 | schema 极简 + 结构化纠错 + 可换强模型 |
| R15 | 双模式代码分叉 | 中 | 五接口抽象 + 业务层禁止 `if mode`，CI 里两个模式都跑冒烟 |
| R16 | **本地 git 无远端 = 代码唯一副本**（D15） | **高** | 每日 `git bundle --all` + 目录快照 + 可选 `push --mirror` 到 NAS；UI 显示上次备份时间；M10 必须做一次真实恢复演练 |
| R17 | OIDC 配错把所有人锁在门外（D17） | 高 | `GA_AUTH_MODE=both` 默认保留密码登录；本地 `admin` 账号不可删除；改 OIDC 配置前先「测试发现」；配置变更写审计 |
| R18 | 评审流于形式（没有远端 MR 界面） | 高 | 自建 diff + 行内评论 + 合并门槛（测试 pass / 无冲突 / 审批）+ 强制记录审批人 |

---

## 18. 已确认事项与遗留问题

### 18.1 已确认

| 时间 | 决定 |
|---|---|
| 第一轮 | 试点 Python；原生模式优先；供应商只开配置口；React 前端；先文档后编码 |
| 第二轮 | 数据目录/端口用默认；**独立 venv**；交付含**一键打包 + 启停脚本 + 部署文档** |
| 第三轮 | **团队协作型**（多人共享同一项目工作区）→ 服务端部署 + PostgreSQL + Redis + 权限体系 + 并发锁 |
| 第四轮 | 部署**双轨都要**（Linux 服务器 + Windows 单机包）；团队**3–10 人**（不上 K8s）；代码托管**本地 git**；**要示例项目**；**要 OIDC** |

**第四轮决定带来的具体改动**（已写进正文，便于核对）：

| 决定 | 落到哪 |
|---|---|
| D13 双轨都要 | §15.1 五条「同一产品」约束 + §16 M10 四项验收（含 lite↔server 导迁） |
| D14 3–10 人 | §2.5 重新定标 + 资源规格表 + 明确「不引入 K8s/Kafka/ES/PgBouncer」 |
| D15 本地 git | §5.3 本地 merge 流程 + **§5.3.1 自建评审**（否则审批流形同虚设）+ §15.7 `git bundle` 备份 + R16 |
| D16 示例项目 | §12 `examples/cardgame/` + M0 产出 + M4 用它验收 |
| D17 OIDC | §3.4.1 完整设计（授权码+PKCE/绑定入组/配置项/安全）+ M1 落地 + R17 |

### 18.2 仍未定（不阻塞开工，越早定越省事）

1. **OIDC 用哪家 IdP**：Keycloak / Authentik 自建，还是 Entra ID / 飞书 / 钉钉？
   → 影响 §3.4.1 哪个模板先做透（通用 OIDC 一定做，预置模板挑一个先验证）。
2. **服务器位置与域名**：内网 IP 还是内网域名？有没有可用的 TLS 证书（自签 / 企业 CA / Let's Encrypt）？
   → 影响 Caddy 配置与 M10 验收方式。
3. **`examples/cardgame/` 的玩法方向**：纯终端卡牌对战，还是 tkinter 图形界面？
   → 终端版更容易做无头测试（对测试角色友好），图形版演示观感更好。**建议终端版 + 预留 UI 层**。
4. **首批成员与角色分配**：谁能做 admin？需要几个 viewer（策划/制作）？
   → 影响首启引导的最小可用配置。


---

> **设计原则（v0.3 修订）**
>
> **① 不透明的东西不会被执行者信任** —— 所有步骤、参数、输出都要能看到；多人同看一条流。
> **② 每个人只做一件事，边界用硬校验卡死** —— 人类靠 RBAC，AI 靠工具白名单，都不靠"自觉"。
> **③ 外部依赖全部可降级** —— 没 CLI 用原生、没 key 出占位、没 Docker 用 lite。
> **④ 花钱的地方必须有硬闸门** —— 项目锁防重复劳动，配额防护钱包，审批防越权。
> **⑤ 假东西必须一眼能认出来** —— 占位素材不许伪装成品。
> **⑥ 共享可变状态必须加锁** —— 工作区、git 分支、配置，全部纳入锁与版本管理。
> **⑦ AI 的产出必须人能审** —— 分支 + diff + 审批，是团队采用 AI 写代码的前提。
