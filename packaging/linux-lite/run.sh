#!/usr/bin/env bash
# =============================================================================
# run.sh  --  Trace Linux lite 启动脚本
#   用法: bash run.sh            （前台，Ctrl+C 停止）
#         bash run.sh --daemon   （后台，日志写到 <DATA_DIR>/logs/run.out）
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PKG_DIR"

if [ -t 1 ]; then RED=$'\e[31m'; YEL=$'\e[33m'; CYN=$'\e[36m'; GRN=$'\e[32m'; RST=$'\e[0m'; else RED=; YEL=; CYN=; GRN=; RST=; fi
die() { printf "%s[err]%s %s\n" "$RED" "$RST" "$*"; exit 1; }

PY="$PKG_DIR/.venv/bin/python"
[ -x "$PY" ] || die "未找到 .venv，请先运行: bash install.sh"
[ -f "$PKG_DIR/.env" ] || { printf "%s[warn]%s 未找到 .env，正在从 env.template 生成...\n" "$YEL" "$RST"; cp "$PKG_DIR/env.template" "$PKG_DIR/.env"; die "请编辑 .env（LLM 配置）后重新运行 bash run.sh"; }

export GA_MODE="${GA_MODE:-lite}"

# 数据目录（用于 --daemon 日志位置）
DATA_DIR="${GA_DATA_DIR:-$HOME/.trace}"
mkdir -p "$DATA_DIR/logs" 2>/dev/null || true

DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1

if [ "$DAEMON" = "1" ]; then
  OUT="$DATA_DIR/logs/run.out"
  PIDF="$PKG_DIR/.run.pid"
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
    printf "%s[run]%s 已在运行 (PID %s)。如需重启先 bash stop.sh\n" "$YEL" "$RST" "$(cat "$PIDF")"; exit 0
  fi
  nohup "$PY" -m game_agent.main >"$OUT" 2>&1 &
  echo $! > "$PIDF"
  printf "%s[run]%s 后台启动 PID %s，日志: %s\n" "$GRN" "$RST" "$(cat "$PIDF")" "$OUT"
  # 端口可能自动顺延（默认 18888，被占用就换），从前几行日志里读实际端口，别写死。
  for _ in $(seq 1 24); do
    URL="$(grep -Eo 'Uvicorn running on http://[0-9.]+:[0-9]+' "$OUT" 2>/dev/null | tail -1 | grep -Eo 'http://[0-9.]+:[0-9]+')"
    [ -n "$URL" ] && break
    sleep 0.5
  done
  if [ -n "${URL:-}" ]; then
    printf "%s[run]%s 已监听: %s/  （首次开 %s/setup 建 owner）\n" "$GRN" "$RST" "$URL" "$URL"
  else
    printf "%s[run]%s 启动中… 默认 http://127.0.0.1:18888/（若被占用会自动顺延，见日志 %s）\n" "$YEL" "$RST" "$OUT"
  fi
else
  printf "%s[run]%s 启动中… 默认 http://127.0.0.1:18888/（被占用会自动顺延，端口见启动日志）（Ctrl+C 停止）\n" "$CYN" "$RST"
  exec "$PY" -m game_agent.main
fi
