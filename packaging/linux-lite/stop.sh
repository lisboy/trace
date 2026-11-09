#!/usr/bin/env bash
# =============================================================================
# stop.sh  --  停止 Trace Linux lite
#   优先用 .run.pid；否则按命令行精确匹配 "game_agent.main" 结束（不误伤其它 python）
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -t 1 ]; then RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; RST=$'\e[0m'; else RED=; YEL=; GRN=; RST=; fi

PIDF="$PKG_DIR/.run.pid"
STOPPED=0

if [ -f "$PIDF" ]; then
  PID="$(cat "$PIDF" 2>/dev/null)"
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    printf "%s[stop]%s 已停止 PID %s\n" "$GRN" "$RST" "$PID"; STOPPED=1
  fi
  rm -f "$PIDF"
fi

# 兜底：按命令行匹配（覆盖非本脚本启动的实例）
PIDS="$(ps -eo pid=,args= 2>/dev/null | grep -E 'python[0-9.]* -m game_agent\.main' | grep -v grep | awk '{print $1}')"
if [ -n "$PIDS" ]; then
  for p in $PIDS; do
    printf "%s[stop]%s 结束匹配进程 PID %s\n" "$YEL" "$RST" "$p"
    kill "$p" 2>/dev/null; sleep 0.3; kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
    STOPPED=1
  done
fi

[ "$STOPPED" = "1" ] && printf "%s[stop]%s 完成\n" "$GRN" "$RST" || printf "%s[stop]%s 未发现运行中的 Trace\n" "$YEL" "$RST"
