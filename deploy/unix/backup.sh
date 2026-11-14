#!/usr/bin/env bash
# deploy/unix/backup.sh —— 每日备份：git bundle（项目工作区）+ 数据库 dump。
# 配合 cron： 0 3 * * * /path/deploy/unix/backup.sh
set -e
cd "$(dirname "$0")/../.."
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
PROJECTS_ROOT="${PROJECTS_ROOT:-$HOME/.trace/projects}"

echo "[backup] bundle 各项目 git 仓库"
for d in "$PROJECTS_ROOT"/*/; do
  [ -d "$d/.git" ] || continue
  name="$(basename "$d")"
  git -C "$d" bundle create "$BACKUP_DIR/${name}-${STAMP}.bundle" --all
done

echo "[backup] 数据库快照（SQLite）"
cp "${DATA_DIR:-$HOME/.trace}/game_agent.db" "$BACKUP_DIR/game_agent-${STAMP}.db" 2>/dev/null || true
echo "[backup] 完成 -> $BACKUP_DIR"
