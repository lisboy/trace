#!/usr/bin/env bash
# 本地开发：前端 dev server（5173，代理 /api /ws 到后端）+ 后端（8765）同开。
set -e
cd "$(dirname "$0")/.."

# 后端（后台）
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
python -m game_agent.main &
BACK=$!

# 前端 dev
cd frontend
npm install
npm run dev

kill $BACK 2>/dev/null || true
