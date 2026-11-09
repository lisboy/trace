@echo off
REM 本地开发：后端（8765）+ 前端 dev server（5173，代理 /api /ws）。
cd /d "%~dp0.."
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
start "trace-backend" python -m game_agent.main
cd frontend
call npm install
call npm run dev
