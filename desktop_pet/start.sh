#!/usr/bin/env bash
# 循迹桌宠启动脚本（Linux / macOS）
# 首次运行会自动装依赖；之后直接起窗。
set -e
cd "$(dirname "$0")"

if [ ! -d node_modules/electron ]; then
  echo "[pet] 未检测到 Electron，正在安装依赖……"
  npm install
fi

echo "[pet] 启动循迹桌宠……右键宠物可打开菜单，菜单里可退出。"
# macOS/Linux 上 electron 包会把可执行文件路径写进 path.txt
npx electron .
