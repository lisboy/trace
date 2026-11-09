#!/usr/bin/env bash
# deploy/unix/stop.sh —— 停止 server 模式容器（保留数据卷）。
set -e
cd "$(dirname "$0")/../.."
docker compose down
