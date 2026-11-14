#!/usr/bin/env bash
# 循迹（Trace）· lite 模式一键启动（单机个人版，SQLite + 进程内服务）。
# 用法： bash start.sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[setup] 创建虚拟环境 .venv"
  python3 -m venv .venv
fi
. .venv/bin/activate

if [ ! -f .env ]; then
  echo "[setup] 未发现 .env，复制 .env.example（请按需修改）"
  cp .env.example .env
fi

echo "[setup] 安装依赖"
pip install -q -r requirements.txt

export GA_MODE=lite
echo "[run] 启动 循迹（Trace） (http://${GA_HOST:-127.0.0.1}:${GA_PORT:-8765}，端口被占用/保留时会自动顺延并打印实际端口)"
exec python -m game_agent.main
