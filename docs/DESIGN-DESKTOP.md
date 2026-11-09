# 桌面端方案评估（GameAgent Desktop）

> 问题：如果把 GameAgent 做成电脑桌面端，需要多复杂？
> 说明：本文按「把现有 GameAgent 做成桌面端」评估。如果「翻版」指的是另起一套全新代码重做同款桌面产品，则复用红利不成立，粗略按新项目立项（1 人约 3 个月以上，3 人约 1.5～2 个月，不含长期运维）。
> 结论先行：**不用重写，底子已有七成。** 现有代码已经是「单进程 lite + React SPA + PyInstaller 单文件 EXE」，真正缺的只是**原生窗口壳 + 桌面生命周期 + 安装/更新**三块。推荐用 **pywebview 壳**复用现有后端和前端：
>
> - **MVP（能双击进窗口、能正常用，L2）：3～5 人日**
> - **可对外发布的 v1（托盘、安装包、自动更新，L3）：合计约 2～3 周；含代码签名约 2.5～3.5 周（1 人）**
> - **做成 Electron 式独立客户端或全原生重写：2～3 个月起，不建议**

### 先给结论：按「做到什么程度」分四档

| 档位 | 做法 | 工作量 | 体验 |
|---|---|---|---|
| **L0 现状** | 现有 `GameAgent.exe` + `start.cmd`，后台起服务、默认浏览器打开 | **0（已经有了）** | 能用，但有黑窗、像网页 |
| **L1 轻桌面** | 保留浏览器，做一个专用快捷方式/Edge PWA 安装，隐藏黑窗 | **0.5～1 人日** | 有独立图标和窗口，仍是浏览器 |
| **L2 原生壳（推荐首发）** | pywebview 内嵌 WebView2，同进程带后端；单实例、端口回退、关窗优雅退出 | **3～5 人日** | 真正的桌面窗口，双击即用 |
| **L3 完整桌面产品** | L2 + 托盘、原生通知、安装包、自动更新、代码签名 | **合计约 2.5～3.5 周** | 可对外分发、可自动升级 |

**回答「需要多复杂」**：如果只是「像桌面软件一样双击用」，**L1 半天到 1 天**；如果要做成有原生窗口的正式桌面端（L2），**3～5 人日**；要做到能公开发布、自动更新、过 SmartScreen（L3），**约 2.5～3.5 周 + 证书采购周期**。核心原因是这个项目已经有单进程 lite 后端和 PyInstaller 单文件打包，桌面化主要是加壳，不是重写。

---

## 1. 现状盘点（已经有什么、缺什么）

| 能力 | 现状 | 桌面端要求 | 差距 |
|---|---|---|---|
| 运行形态 | `python -m game_agent.main` 单进程（API + 执行器），lite 模式 SQLite | 双击即开，无控制台黑窗 | 小：现有 `packaging/exe` 已能打 onefile EXE |
| 前端 | React 18 + Vite，构建产物 `web/dist` 由后端静态托管 | 同一套 UI 装进原生窗口 | **零改动**，直接复用 |
| 打包 | `packaging/exe/GameAgent.spec` + `build-exe.ps1`，产出约 42MB 单文件 `GameAgent.exe` | 安装包 / 绿色版 | 小：已有 80% |
| 数据 | `~/.trace/`（SQLite、项目工作区、快照、日志） | 每用户独立数据目录 | 已满足 |
| 启停 | `start.cmd` / `stop.cmd` + pid 文件 | 窗口关闭即优雅退出 | 小改：进程内接管生命周期 |
| 通知 | Web Notification API + Web Audio 提示音 | Windows 原生 Toast / 托盘角标 | 中：需 AppUserModelID、托盘 |
| 更新 | 手动替换 zip | 自动检查 + 下载 + 安装 | 中：需更新服务与签名校验 |
| 签名 | 无 | 代码签名（避免 SmartScreen 拦截） | 中：证书采购 + CI 签名 |
| 多实例/端口 | 固定 `127.0.0.1:8765` | 单实例锁 + 端口冲突自动回退 | 小：启动器里加互斥量与动态端口 |
| 托盘/窗口 | 无（纯浏览器） | 托盘、最小化到托盘、窗口尺寸记忆 | 小：pywebview + pystray |
| 依赖现状 | `.venv` 未安装 `pywebview` / `pystray`（已实测） | 桌面壳需要 | 小：新增 2 个可选依赖，不影响源码运行 |

**一句话**：现在离「桌面端」只差一个壳，而不是差一套产品。

---

## 2. 主流技术路线对比

| 方案 | 做法 | 安装包体积 | 工作量（1 人） | 适用判断 |
|---|---|---|---|---|
| **A. pywebview 壳（推荐）** | Python 原生窗口内嵌系统 WebView2（Win10/11 自带），后端同进程启动 | **~45MB**（在现有 42MB 上几乎不增） | **MVP 3～5 人日；完整版 2.5～3.5 周** | 后端已是 Python，同进程最省事；桌面集成够用 |
| B. Electron | 另起 Node 壳，打包 Chromium，通过 HTTP 连本地或远端后端 | 120～200MB | 4～6 周 | 需要重度桌面 API、或前端团队想独立演进时选 |
| C. Tauri | Rust 壳 + 系统 WebView，Python 后端作为 sidecar | 15～30MB | 4～6 周（无 Rust 经验更久） | 对体积敏感、且能接受 Rust 工具链 |
| D. 全原生重写（WinUI/Qt） | 用原生框架重做全部 UI | 30～60MB | 2～3 个月起 | 不推荐：UI 要重写一遍，收益极低 |

**为什么推荐 A**：
1. 项目后端、打包链、前端全是现成的，pywebview 只是给现有页面开一扇原生窗口；
2. WebView2 是 Windows 10/11 系统组件，目标机不用装浏览器运行时（老 Win10 需带 Evergreen 安装器兜底）；
3. 与 `packaging/exe` 现有的 PyInstaller 流程天然兼容，MVP 改动集中在 1 个新文件和 4 个既有文件（详见第 3 节）。

---

## 3. 具体要改哪些文件（按现有目录组织）

| 文件 | 改动 | 复杂度 |
|---|---|---|
| `game_agent/desktop.py`（新增） | 桌面入口：单实例互斥、选端口、后台线程启动 uvicorn、创建 pywebview 窗口、托盘、关窗时优雅停机 | 中 |
| `game_agent/main.py` | 把现在阻塞的 `asyncio.run(_start(...))` 拆出可编程启动/停止的接口（返回 server 句柄与 shutdown 事件），供桌面包调用；CLI 行为保持不变 | 中 |
| `packaging/exe/launcher.py` | 增加 `GA_DESKTOP=1` 分支：先起后端、等 `http://127.0.0.1:<port>/health` 就绪，再开窗口；不再调用浏览器 | 小 |
| `packaging/exe/GameAgent.spec` | 加 `webview`、`pystray` 等 hiddenimports；`console=False`（当前是 `console=True`，会留黑窗）；加图标与版本资源；打包 WebView2 loader | 小 |
| `game_agent/config.py` | 新增 `GA_DESKTOP`、`GA_WINDOW_WIDTH/HEIGHT`、`GA_MINIMIZE_TO_TRAY` 等开关（默认关闭，源码运行不受影响） | 小 |
| `packaging/desktop/installer.iss`（新增） | Inno Setup 安装包：每用户安装、开始菜单/桌面快捷方式、卸载保留数据目录 | 小 |
| `packaging/desktop/update.py`（新增，P3） | 更新检查：拉版本清单 → 校验签名 → 下载安装包 → 退出后静默安装 | 中 |
| `packaging/exe/start.cmd` / `stop.cmd` | 保留给绿色版；桌面版由窗口接管，不再需要 | 零改动 |

**不需要动的**：`frontend/` 全部、`game_agent/api/` 路由、数据库迁移、`web/dist` 构建方式。

**改动量估算（MVP 口径）**：

| 改动 | 预计代码量 |
|---|---|
| `game_agent/desktop.py` 新增（互斥、端口、线程、窗口、托盘底座） | 250～350 行 |
| `game_agent/main.py` 拆出 start/stop（保留 CLI 行为） | 60～100 行 |
| `packaging/exe/launcher.py` 桌面分支 | 40～60 行 |
| `packaging/exe/GameAgent.spec` + `config.py` + 图标资源 | 20～30 行 |
| **MVP 合计** | **不到 600 行，3～5 人日** |

对比一下：现有后端约 2.4 万行（84 个 Python 文件）、前端约 1.5 万行（51 个 TS/TSX 文件）。桌面化只需要新增几百行外围代码，**不碰任何业务逻辑**——这就是「底子已有七成」的具体含义。

### 最小可行改造（MVP）核心思路

```python
# game_agent/desktop.py（示意，非最终实现）
import webview
from game_agent.main import start_server, wait_ready, request_shutdown

def main():
    single_instance_lock()          # 已有实例则激活它的窗口
    port = pick_free_port(8765)     # 8765 被占用则顺延
    server = start_server(port)     # 后台线程跑 uvicorn
    wait_ready(f"http://127.0.0.1:{port}/health")   # 现有健康检查是 /health
    window = webview.create_window("GameAgent", f"http://127.0.0.1:{port}/",
                                   width=1440, height=900, min_size=(1024, 640))
    window.events.closed += request_shutdown
    webview.start()                 # 阻塞到窗口关闭
    server.stop()                   # 优雅收尾后再退出进程
```

关键点：`main.py` 现在是 `asyncio.run(_start(...))` 一把跑到黑，需要拆成「可启动 / 可停止」两段；这是整个工程唯一需要小心重构的地方，其余都是外围。

---

## 4. 分阶段工作量

| 阶段 | 内容 | 人日（1 人） | 验收标准 |
|---|---|---|---|
| **P0 技术验证** | pywebview 打开本地服务页面（后端托管 `web/dist`）；验证 PyInstaller onefile 下 WebView2 可用 | 0.5～1 | 双击 EXE 出窗口，能登录、能发一条消息 |
| **P1 MVP** | 原生窗口 + 单实例 + 端口回退 + 关窗优雅停机（run 落「服务重启中断」） | 2～3 | 双击即用；重复双击不产生第二个实例；关窗后进程干净退出 |
| **P2 桌面体验** | 系统托盘（显示/隐藏/打开数据目录/退出）、最小化到托盘、开机自启（可选）、窗口尺寸记忆、原生通知 | 3～5 | 托盘菜单可操作；关窗可配置为最小化；通知走 Windows 通知中心 |
| **P3 安装与更新** | Inno Setup 安装包、图标/版本信息、更新检查与静默升级 | 3～5 | 安装向导可完成安装/卸载；旧版能检测并升级到新版 |
| **P4 签名与发布** | 代码签名证书、CI 自动构建、SmartScreen 信誉积累 | 2～3 + 证书采购周期 | 安装包签名有效；首次运行无「已保护你的电脑」强拦截（需信誉积累） |

> **合计：MVP（P0–P1）约 3～5 人日（P0 0.5～1 + P1 2～3，另留 0.5～1 天联调缓冲）；可发布的 v1（再加 P2–P3，含托盘/安装包/自动更新）约 2～3 周；含代码签名与正式发布（再加 P4）约 2.5～3.5 周（含 v1 那 2～3 周），证书采购周期另计。**
> 对比：Electron 方案同口径约 4～6 周起步；全原生重写 2～3 个月起。

---

## 5. 与「团队协作」产品形态的关系

这是最容易被忽略、但必须先定的一点：

- **单人/小团队本地版（lite）**：桌面端 = 把现有单机版装进窗口，SQLite 本地数据，开箱即用。上面评估的就是这条。
- **团队服务器版（server）**：桌面端只是**瘦客户端**——窗口里加载 `https://ga.内网域名`，账号、权限、项目数据都在服务器。实现上和本地版是同一套壳，只多一个「服务器地址」配置页与证书信任处理，**额外约 2～3 人日**。
- 不建议为了桌面端把后端塞进客户端：`server` 模式的 API ×N + worker ×M、Postgres、Redis 是团队并发的根基，搬进单机进程会破坏现有架构。

因此推荐的产品矩阵：

```
┌─────────────────────────────┐
│  GameAgent Desktop (壳)      │
│  ├─ 本地模式 → 内嵌 lite 后端 │
│  └─ 团队模式 → 连远程 server  │
└─────────────────────────────┘
```

---

## 6. 风险点与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| 老 Win10 没有 WebView2 | 窗口白屏 | 安装包内置 Evergreen Bootstrapper，缺则自动安装 |
| PyInstaller onefile + WebView 临时解压 | 首启慢 2～5 秒，偶发杀软误报 | 可改 `--onedir` 换启动速度；加签名与白名单说明 |
| 8765 端口被占用 | 起不来 | 启动器动态选端口，窗口加载实际端口 |
| 关闭窗口时 run 还在执行 | run 卡「运行中」 | 复用 `main.py` 的优雅停机逻辑，落「服务重启中断」提示 |
| 自动更新被劫持 | 安全风险 | 更新包必须签名校验，且只走 HTTPS |
| 代码签名证书成本 | 约 $200～400/年，且 SmartScreen 信誉需时间 | 先内测分发，正式对外前再买 |
| macOS / Linux 需求 | pywebview 在 Linux 需 WebKitGTK，打包复杂度上升 | 先做 Windows（与现有 lite 包一致），跨平台另立版本评估 |

---

## 7. 建议的下一步

1. **先做一个 1 天 spike**：在现有 `packaging/exe` 上加 `GA_DESKTOP=1`，用 pywebview 打开现有页面，验证窗口、登录、消息流、文件选择都正常；
2. spike 通过后按 P1 → P2 → P3 推进，**不要动前端**；
3. 产品侧先确认：桌面端首发只做 Windows 吗？团队模式要不要在同一版里支持？这两点会直接改变 P2 之后的工作量。

**今天就能试的「零代码桌面窗口」**（L1，验证观感用）：先双击 `start.cmd` 起服务，然后命令行执行

```bat
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=http://127.0.0.1:8765
```

会得到一个没有地址栏/标签页的独立窗口。它仍是浏览器内核，但已经能判断「装进桌面窗口后体验是否可接受」，成本 0。正式版再用 pywebview 替换这层。

---

## 8. 评估依据（本次实测）

| 事实 | 来源 |
|---|---|
| `dist_exe/GameAgent.exe` 已存在，44,029,776 字节（约 42 MiB） | `dir dist_exe` 实测 |
| 打包配置是 PyInstaller onefile，且 `console=True`（会带黑色控制台窗口） | `packaging/exe/GameAgent.spec` |
| 后端已有 `GET /health` 健康检查，可直接用于窗口就绪探测 | `game_agent/api/app.py:44` |
| 前端构建产物 `web/dist/index.html` 存在且由后端 SPA fallback 托管 | `web/dist` + `game_agent/api/app.py:66-84` |
| 当前 `.venv` **未安装** `pywebview` 与 `pystray` | `importlib.util.find_spec` 实测 False |
| 本机已安装 Edge（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`），可直接用 `--app` 做 L1 观感验证 | 文件存在性检查 |
| 现有页面在标准浏览器内核里完整可用：本地起服务后 `/health` 返回 `200 {"ok":true,"mode":"lite"}`，登录页截图正常渲染 | 本次验收：9998 端口实例 + 截图 `.agent/screenshots/desktop-eval-existing-app.png` |
| 服务默认单进程、固定 `127.0.0.1:8765` | `game_agent/main.py:291` + `game_agent/config.py` |
| 数据目录默认 `~/.trace`，可整体备份/迁移 | `game_agent/config.py` |

---

*文档位置：`docs/DESIGN-DESKTOP.md`。本文只做方案评估，未改动任何运行代码。*
