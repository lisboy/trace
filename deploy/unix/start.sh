#!/usr/bin/env bash
# deploy/unix/start.sh —— server 模式（Linux）：docker compose 拉起全套。
set -e
cd "$(dirname "$0")/../.."
docker compose up -d --build
echo "循迹（Trace） 已启动。访问 https://<你的域名>（Caddy 反代）或 http://localhost:8765"
