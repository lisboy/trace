<div align="center">

# 循迹 · Trace

**团队协作型游戏开发智能体**

一句话需求 → 读代码、改代码、编译、起服务、截图验收，全部由 AI 助手自己完成；
出图 / 出音频 / 出视频直接生成；改动落成快照，供人审阅与回滚。

Python 3.12 · FastAPI · React 18 + TS · SQLite / MySQL · 可打包单机运行

</div>

---

## 目录

- [什么是循迹](#什么是循迹)
- [核心特性](#核心特性)
- [它如何工作](#它如何工作)
- [界面预览](#界面预览)
- [快速开始](#快速开始)
  - [方式一：Windows 一键启动（lite）](#方式一windows-一键启动lite)
  - [方式二：从源码运行](#方式二从源码运行)
  - [方式三：server 模式（团队部署）](#方式三server-模式团队部署)
- [配置](#配置)
- [打包分发](#打包分发)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [文档](#文档)
- [版本](#版本)

---

## 什么是循迹

循迹（Trace）是一个**服务端部署、多人协作的游戏开发智能体**。团队成员共享同一个游戏项目
工作区，每个人只需发一句话需求，AI 助手就会像一名真正的开发者那样**从读到写、从改到验**，
端到端完成改动：

- 读项目结构 → 定位文件 → 写/改代码 → 编译、跑单测；
- 需要美术 / 配音 / 视频时**直接调用**图像、语音、视频工具生成素材并落盘；
- 起服务 → 用浏览器截图 → **自己看截图判断对错**（不对就改、重启、重截）；
- 最后交付一份**结构化变更清单**：这次动了哪些文件、净增删多少行、测试结论、新增素材。

> 平台**不接管你的 git**。代码直接写进工作区，但每次 run 在写文件前都会自动留下**改动前快照**，
> 因此任何改动随时可以**审阅 diff 并一键回滚**。

## 核心特性

- **单一执行者，端到端交付** —— 一个助手对用户负责：查文件、改代码、编译、起服务、截图验收全包，
  不再有「策划→开发→测试」多角色接力带来的多轮往返。
- **媒体直连生成** —— 助手可直接调用 `generate_image` / `generate_audio` / `generate_video`，
  按「美术模型 / 配音模型」绑定生成图片、语音、音效、BGM、视频并落到工作区。
- **零 key 也能跑通全链路** —— 未配置素材供应商时自动生成同路径同名的**占位素材**；
  之后配好 key 一键重画，只覆盖内容，代码零改动。
- **供应商驱动注册表** —— 图像 / 语音 / 音效 / 音乐均可插拔：加一个文件 + 一个装饰器即可接入新供应商，
  前端自动渲染配置表单。
- **改动快照 + 变更报告** —— 工具层写前快照，任何项目（无论有没有 `.git`）都能审阅、回滚，
  并生成结构化变更清单。
- **项目记忆** —— 跨任务保留项目入口事实（起服务命令、测试账号、登录入口），上下文只给标题索引、按需取，避免爆上下文。
- **长效执行** —— 没有「轮次预算 / 墙钟上限」，只有明确的停止、卡死检测、静默结束才会终止。
  过程事件（思考、压缩、经验沉淀、重试）以行内时间线显示在 trace 中。
- **双运行模式** —— `lite`（单机 SQLite，1–3 人）与 `server`（多机共用 MySQL，团队生产）。
- **多用户与权限** —— 团队 / 成员 / owner / admin 角色，支持密码登录与 OIDC 单点登录。
- **可打包分发** —— Windows 免安装 exe、Windows 便携版（需 Python）、Linux 便携包三套产物一键构建。

## 它如何工作

```
用户：给主界面加一个卡牌详情弹窗
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  助手（唯一执行者）                                          │
  │   1. list_dir / grep / read_file   读项目、定位文件          │
  │   2. write_file / edit_file        改代码（写前自动快照）     │
  │   3. run_command                   编译 / 跑单测             │
  │   4. generate_image / generate_audio 缺素材时直接生成        │
  │   5. start_service / screenshot_page 起服务 → 截图 → 自检    │
  │   6. report_to_user                交付变更清单 + 验证证据   │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  变更报告：改了哪些文件 / 净增删行数 / 测试结论 / 新增素材
        │
        ▼
  用户：查看 diff 快照 → 满意保留，不满意一键回滚
```

## 界面预览

| 计划与思考 | 提问面板 | 展开的推理 |
|---|---|---|
| ![计划与思考](docs/screenshots/plan-and-thinking.png) | ![提问面板](docs/screenshots/ask-user-panel.png) | ![推理](docs/screenshots/thinking-expanded.png) |

## 快速开始

### 方式一：Windows 一键启动（lite）

最简单的方式，无需手动装依赖：

```bat
:: 双击 start.cmd（或命令行执行）
start.cmd
```

首次启动会自动创建 `.venv`、复制 `.env.example` 为 `.env`、用 SQLite 建库，并打开浏览器访问
**http://127.0.0.1:18888**。菜单提供：启动服务、查看账号、重置密码、停止服务。

> ⚠️ 启动后需要在「设置 → 模型配置」里添加一个大模型供应商（填地址 + Key），
> 再到「设置 → 智能体」把「对话模型」绑上即可使用——程序只调用真实大模型，没有离线 mock 模式。

### 方式二：从源码运行

```bash
# 1) 创建虚拟环境（需 Python 3.12+）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2) 安装依赖
pip install -r requirements.txt

# 3) 准备配置
copy .env.example .env          # Windows（Linux 用 cp）

# 4) 启动
python -m game_agent.main
```

打开 **http://127.0.0.1:18888** 即可。

### 方式三：server 模式（团队部署）

团队生产环境使用 MySQL（多机共用一套库）：

```bash
# .env 中设置
GA_MODE=server
DATABASE_URL=mysql+aiomysql://user:pass@host:3306/game_agent?charset=utf8mb4
GA_SECRET_KEY=<随机长字符串>
```

```bash
pip install aiomysql
python -m game_agent.main
```

建表在启动时自动完成（`init_db()`，幂等）。首次启动会打印一次性引导 token，
访问 `/setup` 并填入 token 即可创建第一个 owner 账号。
`GA_MODE` 未设或为 `lite` 时使用单机 SQLite（数据目录默认 `~/.trace`）。

## 配置

所有配置通过环境变量 / `.env` 提供，完整示例见 [`.env.example`](.env.example)。常用项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `GA_MODE` | 运行模式：`lite` / `server` | `lite` |
| `GA_DATA_DIR` | lite 数据目录（db / 工作区 / 密钥） | `~/.trace` |
| `GA_SECRET_KEY` | 会话 / 令牌签名与加密主密钥（生产必填） | 空 |
| `DATABASE_URL` | 数据库连接串；留空则 lite 用 SQLite | 空 |
| `PROJECTS_ROOT` | 项目工作区根目录（各项目所在） | `<DATA_DIR>/projects` |
| `GA_HOST` / `GA_PORT` | 监听地址 / 端口 | `127.0.0.1` / `18888` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `GA_LLM_MODEL` | 可选的全局模型兜底 | 空 / OpenAI / `gpt-4o-mini` |
| `GA_SEED_DEMO` | 打包版首启播种示例账号与项目 | `0` |

素材供应商（图像 / 语音 / 音效 / 音乐）建议在界面「设置」中配置，未配置时走占位素材。

## 打包分发

```bat
package.cmd
```

菜单可产出三套产物到 `dist_release\`（含 `.sha256` 校验）：

| 产物 | 目标环境 |
|---|---|
| `Trace-win64-exe-<ver>.zip` | Windows 单文件 exe，零安装（PyInstaller） |
| `Trace-win64-lite-<ver>.zip` | Windows 便携版，目标机需 Python 3.12+ |
| `Trace-lite-linux-<ver>.tar.gz` | Linux 便携包（bash 脚本启停） |

前端会在打包时统一构建一次，供三套产物复用。

## 项目结构

```
game_agent/            # 后端
  agents/              #   单助手角色、工具实现、工具注册表
  api/                 #   FastAPI 应用与各路由（runs/sessions/projects/...）
  assets/              #   图像 / 语音 / 视频 / 素材供应商驱动
  auth/                #   密码、RBAC、认证服务
  core/                #   抽象、lite 容器、锁、队列、事件总线、技能、记忆
  db/                  #   SQLAlchemy 模型、会话、迁移
  llm/                 #   OpenAI / Anthropic 适配层、上下文、transcript
  orchestration/       #   编排器、快照、变更报告、项目记忆、策略
frontend/              # React 18 + TS + Vite + Tailwind 前端源码
web/dist/              # 前端构建产物（后端直接托管）
examples/cardgame/     # 自带示例项目（改代码 → 出素材 → 跑测试的靶子）
packaging/             # 三套打包脚本（windows-lite / exe / linux-lite）
deploy/unix/           # Linux 启停 / 备份脚本
scripts/               # 开发与运维脚本（重置密码、种子数据等）
tests/                 # pytest 测试
docs/                  # 设计文档与截图
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy 2 (async) · asyncio |
| 数据 | SQLite（lite）· MySQL 8+（server，`aiomysql`） |
| 前端 | React 18 · TypeScript · Vite · Tailwind · Zustand |
| 大模型 | OpenAI 兼容 · Anthropic（统一适配层） |
| 素材 | 供应商驱动注册表（Gemini / OpenAI 图像 / Seedream / Edge TTS / IndexTTS2 / MiniMax 音乐 / 本地音效库 / 外置 HTTP），零 key 走占位素材 |
| 视频 | 本地 ComfyUI（Wan2.2 等） |
| 打包 | PyInstaller（exe）· 便携 zip / tar.gz 一键构建 |

## 文档

- [设计文档 v0.3](docs/DESIGN.md)
- [桌面端评估](docs/DESIGN-DESKTOP.md)
- [断点续跑与记忆](docs/DESIGN-RESUME-MEMORY.md)
- [语音接入说明](docs/语音接入.md)

## 版本

当前版本 **v0.3.6**（首个公开发布版）。

> 这是一个仍在快速迭代的项目。首个版本聚焦「单助手端到端执行 + 媒体直连 + 快照审阅」的核心闭环，
> 后续会持续完善团队协作、评审合并与部署运维。

## License

本项目暂未附带开源许可证。在添加明确许可证之前，默认保留所有权利。
