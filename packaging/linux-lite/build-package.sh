#!/usr/bin/env bash
# =============================================================================
# build-package.sh  --  在【开发机】把项目打成 Linux lite 部署包 (.tar.gz)
#
# 用法（仓库根目录）:
#   bash packaging/linux-lite/build-package.sh
# 选项:
#   --out DIR        输出目录（默认 ./dist）
#   --include-venv   把 .venv 一起打进去（体积大；目标机需同架构/同 Python 小版本才可靠）
#   --no-build       不重建前端（直接用现有 web/dist）
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
STAGE="$HERE/payload"
OUT_DIR="$ROOT/dist"
INCLUDE_VENV=0; DO_BUILD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --out) shift; OUT_DIR="${1:-$OUT_DIR}" ;;
    --include-venv) INCLUDE_VENV=1 ;;
    --no-build) DO_BUILD=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac; shift
done

echo "[build] 仓库根: $ROOT"

need() { [ -e "$1" ] || { echo "[build][x] 缺少: $2 ($1)"; exit 1; }; }
need "$ROOT/game_agent" "后端代码"
need "$ROOT/requirements.txt" "requirements.txt"
need "$ROOT/pyproject.toml" "pyproject.toml"
need "$ROOT/examples/cardgame" "示例工程"
need "$ROOT/.env.example" ".env.example"

# 1) 前端构建
if [ "$DO_BUILD" = "1" ]; then
  need "$ROOT/frontend/package.json" "前端源码（--no-build 可跳过）"
  echo "[build] 构建前端 (npm ci + build)…"
  pushd "$ROOT/frontend" >/dev/null
  if [ ! -d node_modules ]; then
    if command -v npm >/dev/null 2>&1; then npm ci; else echo "[build][x] 需要 node/npm 构建前端，或加 --no-build 使用现有 dist"; exit 1; fi
  fi
  npm run build
  popd >/dev/null
fi
need "$ROOT/web/dist/index.html" "web/dist/index.html（前端未构建；去掉 --no-build 或先 npm run build）"
echo "[build] 前端 dist 就绪"

# 2) 清空并重建 payload
rm -rf "$STAGE"; mkdir -p "$STAGE"
EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' --exclude='.pytest_cache'
          --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='.venv' --exclude='venv'
          --exclude='node_modules' --exclude='.git' --exclude='*.log' --exclude='*.db'
          --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='.env')

echo "[build] 复制后端代码…"
tar -C "$ROOT" -cf - "${EXCLUDES[@]}" game_agent | tar -C "$STAGE" -xf -
echo "[build] 复制前端 dist…"
tar -C "$ROOT" -cf - "${EXCLUDES[@]}" web/dist | tar -C "$STAGE" -xf -
echo "[build] 复制示例工程…"
tar -C "$ROOT" -cf - "${EXCLUDES[@]}" examples | tar -C "$STAGE" -xf -
for f in requirements.txt pyproject.toml README.md; do
  [ -f "$ROOT/$f" ] && cp "$ROOT/$f" "$STAGE/$f"
done
[ "$INCLUDE_VENV" = "1" ] && { echo "[build] 复制 .venv（体积大）…"; tar -C "$ROOT" -cf - .venv | tar -C "$STAGE" -xf -; }

# 3) 配置模板 + 脚本
cp "$ROOT/.env.example" "$STAGE/env.template"
for f in install.sh run.sh stop.sh README.txt; do
  cp "$HERE/$f" "$STAGE/$f"
done
chmod +x "$STAGE"/install.sh "$STAGE"/run.sh "$STAGE"/stop.sh

# 4) 版本号
VER="0.3.1"
if grep -qE '^version\s*=' "$ROOT/pyproject.toml"; then
  VER="$(grep -E '^version\s*=' "$ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
fi

# 5) 打包
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$OUT_DIR/Trace-lite-linux-${VER}-${STAMP}.tar.gz"
echo "[build] 打包 -> $OUT"
tar -C "$STAGE" -czf "$OUT" .
( cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" 2>/dev/null || true )

SIZE="$(du -h "$OUT" | awk '{print $1}')"
echo
echo "================ 打包完成 ================"
echo "  产物: $OUT"
echo "  版本: $VER   大小: $SIZE"
echo "  目标机: 解压 -> bash install.sh -> 改 .env -> bash run.sh"
echo "========================================="
