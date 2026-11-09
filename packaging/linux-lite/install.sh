#!/usr/bin/env bash
# =============================================================================
# install.sh  --  Trace Linux lite 一键安装（在【目标机】解压目录内运行）
#
# 行为原则（不擅自改你系统）：
#   - Python 依赖(pip)      -> 自动安装
#   - 系统级 Python(需 root) -> 默认【只检测并给出精确安装命令】；加 --auto 且 sudo 免密时才代装
#   - 一切「缺什么、怎么办」都明确打印；脚本可反复重跑，幂等
#
# 用法:
#   bash install.sh                      # 交互式（有终端会问你）
#   bash install.sh --yes                # 非交互，接受默认（不自动 sudo 装系统包）
#   bash install.sh --auto               # 允许用 sudo 自动装系统依赖（仅当 sudo 免密）
#   bash install.sh --offline            # 只从 ./wheels 离线装 pip 依赖
#   bash install.sh --index URL          # 指定 PyPI 镜像
#   bash install.sh --skip-deps          # 跳过 pip 依赖（假定已就绪）
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PKG_DIR"

# ---------- 颜色 / 输出 ----------
if [ -t 1 ]; then
  RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; CYN=$'\e[36m'; BLD=$'\e[1m'; RST=$'\e[0m'
else
  RED=; GRN=; YEL=; CYN=; BLD=; RST=
fi
info() { printf "%s[install]%s %s\n" "$CYN" "$RST" "$*"; }
ok()   { printf "%s[ ok ]%s %s\n" "$GRN" "$RST" "$*"; }
warn() { printf "%s[warn]%s %s\n" "$YEL" "$RST" "$*"; }
err()  { printf "%s[ err]%s %s\n" "$RED" "$RST" "$*"; }
die()  { err "$*"; exit 1; }

# ---------- 参数 ----------
ASSUME_YES=0; AUTO_SUDO=0; OFFLINE=0; SKIP_DEPS=0; PIP_INDEX=""
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --auto) AUTO_SUDO=1 ;;
    --offline) OFFLINE=1 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --index) shift; PIP_INDEX="${1:-}" ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) warn "忽略未知参数: $1" ;;
  esac
  shift
done

confirm() {  # confirm "问题" -> 0=yes
  [ "$ASSUME_YES" = "1" ] && return 0
  [ -t 0 ] || return 1
  printf "%s[y/n]%s %s " "$YEL" "$RST" "$1"
  local a; read -r a
  case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---------- 检测发行版 / 包管理器 ----------
detect_pkgmgr() {
  if command -v apt-get >/dev/null 2>&1; then echo apt
  elif command -v dnf >/dev/null 2>&1; then echo dnf
  elif command -v yum >/dev/null 2>&1; then echo yum
  elif command -v pacman >/dev/null 2>&1; then echo pacman
  elif command -v zypper >/dev/null 2>&1; then echo zypper
  elif command -v apk >/dev/null 2>&1; then echo apk
  else echo unknown; fi
}
PKGMGR="$(detect_pkgmgr)"
info "包管理器: ${PKGMGR}"

# 系统包名映射（python3.12 / venv / pip）
sys_pkg_names() {
  case "$PKGMGR" in
    apt) echo "python3 python3-venv python3-pip" ;;
    dnf|yum) echo "python3 python3-pip" ;;
    pacman) echo "python python-pip" ;;
    zypper) echo "python3 python3-pip" ;;
    apk) echo "python3 py3-pip" ;;
    *) echo "python3 python3-venv python3-pip" ;;
  esac
}
sys_install_cmd() {  # 输出用于提示的一行「怎么装系统依赖」
  local pkgs; pkgs="$(sys_pkg_names)"
  case "$PKGMGR" in
    apt) echo "sudo apt-get update && sudo apt-get install -y $pkgs" ;;
    dnf) echo "sudo dnf install -y $pkgs" ;;
    yum) echo "sudo yum install -y $pkgs" ;;
    pacman) echo "sudo pacman -Sy --noconfirm $pkgs" ;;
    zypper) echo "sudo zypper install -y $pkgs" ;;
    apk) echo "sudo apk add $pkgs" ;;
    *) echo "# 未知发行版，请手动安装 Python 3.12+ 及 venv/pip" ;;
  esac
}

# ---------- Python 版本判断（>=3.12）----------
py_version_ok() {  # $1=解释器路径，返回 0=满足
  "$1" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)
PY
}

find_python() {
  local c
  for c in python3.13 python3.12 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && py_version_ok "$(command -v "$c")"; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

# ---------- 1. 找系统 Python ----------
VENV_PY="$PKG_DIR/.venv/bin/python"
SYS_PY=""
if [ -x "$VENV_PY" ]; then
  info "发现随包 .venv，复用: $VENV_PY"
  SYS_PY="$VENV_PY"
else
  SYS_PY="$(find_python)" || SYS_PY=""
  if [ -z "$SYS_PY" ]; then
    warn "未找到 Python >= 3.12。"
    # 看看有没有低版本，给更精确提示
    if command -v python3 >/dev/null 2>&1; then
      warn "系统当前 python3 版本: $(python3 -V 2>&1)（低于 3.12，不能直接用）"
    fi
    INSTALL_CMD="$(sys_install_cmd)"
    echo
    echo "  ${BLD}需要安装 Python 3.12+ 及 venv/pip。${RST}推荐执行："
    echo "      ${CYN}${INSTALL_CMD}${RST}"
    echo "  注意：部分发行版仓库的 python3 可能仍 <3.12（如 Ubuntu 22.04 是 3.10）。"
    echo "  若如此，推荐用 pyenv 或 deadsnakes PPA 装 3.12（见 README）。"
    echo
    if [ "$AUTO_SUDO" = "1" ]; then
      if sudo -n true 2>/dev/null; then
        info "--auto：尝试用 sudo 自动安装系统依赖…"
        # shellcheck disable=SC2086
        bash -c "$INSTALL_CMD" || warn "系统依赖安装失败，请按上面命令手动安装。"
        SYS_PY="$(find_python)" || SYS_PY=""
      else
        warn "--auto 需要免密 sudo（当前不可用）。请手动执行上面命令，完成后重跑本脚本。"
      fi
    fi
    [ -z "$SYS_PY" ] && die "缺少 Python 3.12+，已停止。装好后重新运行: bash install.sh"
  fi
  ok "使用 Python: $SYS_PY ($("$SYS_PY" -V 2>&1))"
fi

# ---------- 2. 建 venv + pip 依赖 ----------
if [ "$SKIP_DEPS" = "1" ]; then
  info "--skip-deps：跳过依赖安装"
  VENV_PY="$PKG_DIR/.venv/bin/python"
  [ -x "$VENV_PY" ] || die "指定了 --skip-deps 但没有 .venv。"
else
  if [ ! -x "$VENV_PY" ]; then
    info "创建虚拟环境 .venv …"
    if ! "$SYS_PY" -m venv "$PKG_DIR/.venv" 2>"$PKG_DIR/.venv_create.err"; then
      ERRTXT="$(cat "$PKG_DIR/.venv_create.err" 2>/dev/null)"
      rm -f "$PKG_DIR/.venv_create.err"
      err "创建 venv 失败：$ERRTXT"
      echo "  多数是缺少 venv 模块。请执行: ${CYN}$(sys_install_cmd)${RST}"
      die "venv 创建失败"
    fi
    rm -f "$PKG_DIR/.venv_create.err"
  fi
  if [ ! -x "$VENV_PY" ]; then
    err "venv 创建后找不到 $VENV_PY"
    echo "  多为该 Python 缺少 venv/ensurepip 模块（精简版发行版常见）。请安装 venv："
    echo "      ${CYN}$(sys_install_cmd)${RST}   （或 sudo <python> -m ensurepip）"
    die "venv 不可用"
  fi

  info "升级 pip …"
  "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip 升级失败（继续）"

  INSTALL_ARGS=(-m pip install -r "$PKG_DIR/requirements.txt")
  if [ "$OFFLINE" = "1" ]; then
    [ -d "$PKG_DIR/wheels" ] || die "要求离线安装但缺少 ./wheels（在联网机: pip download -r requirements.txt -d wheels）"
    INSTALL_ARGS+=(--no-index --find-links "$PKG_DIR/wheels")
  elif [ -n "$PIP_INDEX" ]; then
    INSTALL_ARGS+=(-i "$PIP_INDEX")
  fi
  info "安装依赖（可能要几分钟）…"
  if ! "$VENV_PY" "${INSTALL_ARGS[@]}"; then
    err "pip 依赖安装失败。"
    echo "  若不满足编译依赖，可能需要系统构建工具（apt: build-essential python3-dev 等）。"
    echo "  或换镜像重试: bash install.sh --index https://pypi.tuna.tsinghua.edu.cn/simple"
    die "依赖安装失败"
  fi

  # mcp 不在 requirements.txt，单独补装（可失败不致命）
  info "补装 mcp（MCP 工具用；失败不致命）…"
  MCP_ARGS=(-m pip install mcp)
  if [ "$OFFLINE" = "1" ]; then MCP_ARGS+=(--no-index --find-links "$PKG_DIR/wheels"); fi
  [ -n "$PIP_INDEX" ] && [ "$OFFLINE" != "1" ] && MCP_ARGS+=(-i "$PIP_INDEX")
  "$VENV_PY" "${MCP_ARGS[@]}" >/dev/null 2>&1 || warn "mcp 未安装成功：MCP 工具不可用，其余功能不受影响。"
  ok "依赖安装完成"
fi

# ---------- 3. .env ----------
ENV_PATH="$PKG_DIR/.env"; TPL="$PKG_DIR/env.template"
if [ -f "$ENV_PATH" ]; then
  info ".env 已存在，保留不覆盖"
else
  [ -f "$TPL" ] || die "缺少 env.template"
  cp "$TPL" "$ENV_PATH"
  ok ".env 已生成（来自 env.template）。请编辑并至少设置 GA_LLM_MODE=real / OPENAI_API_KEY / OPENAI_BASE_URL / GA_LLM_MODEL"
fi
if grep -qE '^\s*DATABASE_URL\s*=\s*mysql' "$ENV_PATH" 2>/dev/null; then
  warn "检测到 DATABASE_URL 用 MySQL：还需 .venv/bin/pip install aiomysql，并先建库 game_agent(utf8mb4)。"
fi

# ---------- 4. 可选组件体检（不阻断）----------
echo; info "可选组件体检（缺失只影响对应功能，不影响启动）:"
if command -v ffmpeg >/dev/null 2>&1; then ok "ffmpeg: $(command -v ffmpeg)"; else warn "ffmpeg 未安装（音视频转换不可用）：$(sys_install_cmd | sed 's/python3[^ ]*//g') 或 apt install ffmpeg"; fi
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 || command -v microsoft-edge >/dev/null 2>&1; then
  ok "浏览器: 已找到（screenshot_page 可用）"
else
  warn "未找到 Chrome/Chromium/Edge（screenshot_page 网页截图不可用）。可装 chromium 或设 GA_BROWSER_PATH。"
fi
if command -v node >/dev/null 2>&1; then ok "node: $(node -v)（MCP npx 可用）"; else warn "node/npx 未安装（MCP 工具、前端重建不可用；运行本身不需要 node）"; fi

# ---------- 5. 完成 ----------
echo
printf "%s================ 安装完成 ================%s\n" "$GRN" "$RST"
echo "  目录: $PKG_DIR"
echo "  下一步:"
echo "    1) 编辑 .env 填好 LLM（GA_LLM_MODE=real, OPENAI_API_KEY, OPENAI_BASE_URL, GA_LLM_MODEL）"
echo "    2) 启动:  bash run.sh     （默认 http://127.0.0.1:18888）"
echo "    3) 浏览器开 /setup，用启动日志打印的一次性 token 创建 owner"
echo "  停止: bash stop.sh  |  开机自启: 见 README 的 systemd 段"
printf "%s=========================================%s\n" "$GRN" "$RST"
