#!/usr/bin/env bash
# deploy/unix/package.sh —— 打包发布物：构建前端 + 收集后端 wheel/源码包。
set -e
cd "$(dirname "$0")/../.."

echo "[1/3] 前端构建 -> web/dist"
( cd frontend && npm install && npm run build )

echo "[2/3] 构建后端分发包"
python -m pip install -q build
python -m build -o dist/

echo "[3/3] 完成。产物：dist/*.whl 与 web/dist/"
ls -la dist/ web/dist/
