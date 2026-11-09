========================================================
 Trace · Linux lite 单机部署包
========================================================

本包为「lite 单机模式」：单进程（API + 执行器一体），
默认 SQLite（零安装数据库）+ 内置前端页面，浏览器即用。

--------------------------------------------------------
一、目标机要求
--------------------------------------------------------
- 64 位 Linux（glibc 系统即可，Debian/Ubuntu/CentOS/RHEL/Fedora/Arch/Alpine 等）
- Python 3.12 或更高（含 venv、pip 模块）
  * 如果包内已经带了 .venv 目录，则无需另装 Python。
- 内存建议 >= 2GB；磁盘 >= 2GB（含依赖与项目工作区）
- 可联网（首次 pip 装依赖需要；除非包内附 wheels/ 或 .venv）
- 一个可用的 LLM 接口（OpenAI 兼容协议）：API Key + Base URL + 模型名

--------------------------------------------------------
二、三步启动
--------------------------------------------------------
1) 解压到固定目录，例如 /opt/trace：
     sudo mkdir -p /opt/trace
     sudo tar -xzf Trace-lite-linux-*.tar.gz -C /opt/trace
     # 建议用普通用户运行（不要用 root 跑业务）
     sudo chown -R $USER:$USER /opt/trace
     cd /opt/trace

2) 安装依赖：
     bash install.sh
   * 网络慢可换镜像：
     bash install.sh --index https://pypi.tuna.tsinghua.edu.cn/simple
   * 完全离线（自带 wheels 目录）：
     bash install.sh --offline
   * 允许脚本用 sudo 自动装系统 Python（仅当 sudo 免密时）：
     bash install.sh --auto

3) .env（install 已自动生成）可以先留空，启动后在界面里配模型（推荐）：
     设置 → 模型配置：加一个供应商（填地址 + Key）；
     设置 → 智能体：把「对话模型」绑到该档位，即时生效、不用重启。
   也可在 .env 里用全局兜底（不想进界面配时）：
     OPENAI_API_KEY=你的key
     OPENAI_BASE_URL=https://你的中转或官方/v1
     GA_LLM_MODEL=模型名
   然后启动：
     bash run.sh            # 前台（Ctrl+C 停止）
     bash run.sh --daemon   # 后台（日志: <数据目录>/logs/run.out）
   浏览器访问 http://<主机IP>:18888/

4) 首次使用：打开 http://<主机IP>:18888/setup
   用【启动日志打印的一次性引导 token】创建第一个 owner 账号。
   （也可提前在 .env 设 GA_BOOTSTRAP_TOKEN=固定值 再启动）

--------------------------------------------------------
三、「运行环境不满足会怎样」——这是关键说明
--------------------------------------------------------
本包分两类依赖，处理方式不同：

[第一类] Python 依赖（pip 装的库）
  → 缺失时 install.sh 会【自动帮你安装】（这就是它的主要工作）。
  → 若装失败，会打印具体报错并给出补救命令（换镜像 / 装编译工具），
    不会静默失败。

[第二类] 系统环境（Python 解释器本体、venv 模块、ffmpeg、浏览器、node 等，需 root 才能装）
  → 默认【只检测并给出精确的安装命令】，不会擅自 sudo 改你的系统。
     例：缺 Python 3.12 时打印
         sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
     你照抄执行即可。
  → 如果你加 --auto 启动安装，且当前用户 sudo 免密（sudo -n 能过），
    脚本才会替你执行上述系统安装命令；否则仍只提示。
  → 无论哪种缺失，脚本都【明确告诉你缺什么、怎么装】，不会中途莫名其妙退出。

汇总（缺什么 → 会怎样）：
  缺 Python 3.12+   : 停止并打印各发行版安装命令（可选 --auto 代装）
  缺 venv 模块      : 停止并提示安装 python3-venv（可选 --auto）
  缺 pip 依赖       : 自动安装；失败则报错+补救建议
  缺 ffmpeg         : 仅提示（语音转码等可选功能不可用），照常启动
  缺 Chrome/Chromium: 仅提示（网页截图 screenshot_page 不可用），照常启动
  缺 node/npx       : 仅提示（MCP 工具、前端重建不可用），运行本身不需要
  缺 LLM key 配置   : 能启动，但对话会报鉴权错误；请改 .env

一句话：库我自动装；系统级的东西我只精确提示，除非你显式 --auto 且 sudo 免密。

--------------------------------------------------------
四、几个常见发行版的系统依赖命令（手动装时照抄）
--------------------------------------------------------
Debian/Ubuntu:
   sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
   # 若 python3 版本 <3.12（如 Ubuntu 22.04），用 deadsnakes：
   #   sudo add-apt-repository ppa:deadsnakes/ppa -y
   #   sudo apt-get install -y python3.12 python3.12-venv
CentOS/RHEL/Fedora/Rocky/Alma:
   sudo dnf install -y python3 python3-pip     # 旧系统用 yum
   # 仓库 python 太旧时，用 pyenv 或 AppStream 装 3.12
Arch:
   sudo pacman -Sy --noconfirm python python-pip
openSUSE:
   sudo zypper install -y python3 python3-pip
Alpine:
   sudo apk add python3 py3-pip
推荐通用做法（任意发行版，不碰系统自带 python）：
   用 pyenv 装 3.12：curl https://pyenv.run | bash，然后 pyenv install 3.12.7

--------------------------------------------------------
五、.env 关键项说明
--------------------------------------------------------
- GA_MODE=lite                     运行模式（不要改）
- DATABASE_URL=                    留空 = 自动用 SQLite（推荐单机）
                                   要 MySQL 才填 mysql+aiomysql://user:pass@host:3306/game_agent?charset=utf8mb4
                                   （用 MySQL 还需 .venv/bin/pip install aiomysql，并先建库 utf8mb4）
- GA_HOST=127.0.0.1                只监听本机
- GA_PORT=18888                     端口
   给同网段访问：GA_HOST=0.0.0.0，并放行防火墙 18888
- GA_DATA_DIR=                     数据目录（见第六节），默认 ~/.trace
- GA_MAX_CONCURRENT_RUNS=3         同时执行的任务数
- GA_SECRET_KEY=                   生产建议设随机长字符串

--------------------------------------------------------
六、数据保存在哪（备份就拷这个目录）
--------------------------------------------------------
默认：~/.trace/   (即 $HOME/.trace)
   game_agent.db       数据库（SQLite）
   projects/           各项目工作区
   run_snapshots/      写前快照（审阅/回滚）
   logs/               运行日志
换位置：.env 里设 GA_DATA_DIR=/data/trace（换机搬家整个目录拷走即可）

--------------------------------------------------------
七、开机自启（systemd）
--------------------------------------------------------
把下面存为 /etc/systemd/system/trace.service
（按需改 WorkingDirectory / User / 路径）：
--------------------------------------------------------------------
[Unit]
Description=Trace Lite
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trace
WorkingDirectory=/opt/trace
EnvironmentFile=/opt/trace/.env
Environment=GA_MODE=lite
ExecStart=/opt/trace/.venv/bin/python -m game_agent.main
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
--------------------------------------------------------------------
启用：
   sudo systemctl daemon-reload
   sudo systemctl enable --now trace
   sudo systemctl status trace        # 看状态
   journalctl -u trace -f             # 看日志
   注意：EnvironmentFile 要求 KEY=VALUE 形式；本包 .env 基本符合。
        若某值含空格，请用引号包起来。用 systemd 时不必再跑 run.sh。

--------------------------------------------------------
八、可选增强（不装也能用，只是对应功能降级）
--------------------------------------------------------
- 本地生图 ComfyUI：.env 里 COMFYUI_BASE_URL=http://127.0.0.1:8588（否则出占位图）
- 本地语音 IndexTTS2：.env 里 TTS_BASE_URL=... 与 TTS_HOME=<安装目录>（否则占位静音）
- 网页截图 screenshot_page：需系统有 Chrome/Chromium/Edge，或设 GA_BROWSER_PATH
- 音视频转换：需 ffmpeg/ffprobe 在 PATH
- MCP 工具：需 node(npx) 且已装 mcp 包（install.sh 会尝试装 mcp）
- 图像与语音共用一张显卡：保持 GA_GPU_EXCLUSIVE=true（自动互斥）

--------------------------------------------------------
九、停止 / 重启 / 升级
--------------------------------------------------------
- 停止：bash stop.sh
- 后台重启：bash stop.sh && bash run.sh --daemon
- 升级：保留 .env 和 ~/.trace（数据）, 用新包覆盖程序文件,
        重跑 bash install.sh（数据库结构启动时自动补列迁移，无需手动）。

--------------------------------------------------------
十、常见问题
--------------------------------------------------------
1) 启动后打不开 / 显示未连接
   → 看 run.sh 前台输出或 <数据目录>/logs/run.out 是否有 Python 报错；
   → 确认端口： ss -ltnp | grep 18888  或  netstat -ltnp | grep 18888
   → 若监听在 127.0.0.1，外部访问需 GA_HOST=0.0.0.0 + 防火墙放行。

2) pip 安装报编译错误（如 psutil / pillow / argon2 要编译）
   → Debian/Ubuntu: sudo apt-get install -y build-essential python3-dev libffi-dev
   → 多数情况下预编译 wheel 可直接装，不需编译。

3) 报 “externally-managed-environment”
   → 那是因为用系统 python 直接装；本包用 .venv 不受影响。请确认用 bash install.sh（它会建 venv）。

4) 对话报 LLM 401/403 或 “Key 无效”
   → 检查 OPENAI_API_KEY / OPENAI_BASE_URL / GA_LLM_MODEL；
     中转站若在 Cloudflare 后面，代码已默认带浏览器 UA，一般无需额外设置。

5) 想让局域网同事用
   → .env 设 GA_HOST=0.0.0.0，放行 18888（firewalld: firewall-cmd --add-port=18888/tcp --permanent; --reload；
     或 ufw allow 18888/tcp），同事访问 http://<本机IP>:18888

========================================================
