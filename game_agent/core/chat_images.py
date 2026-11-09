"""聊天附图原图的存放与解析（放在工作区之外，随 DATA_DIR 一起走）。

背景：以前用户贴的附图原图存在**项目工作区**的 ``.trace/uploads/``。可是工作区会被
run 收尾、快照回滚、切分支、`git clean`、换机器、清缓存整片重来——附图跟着消失，用户过几天
再点开就只剩「附图已失效 / 文件已清理」。打包版尤其明显：换台机器把整个工作区重新拉一遍，
历史消息还在、图全没了。

现在原图落到 ``DATA_DIR/chat_uploads/<team>/<file>``（与 db、日志同级），只在数据库里记一个
不依赖工作区的相对标记 ``chat-uploads/<team>/<file>``。读取时：
  · 新标记 → 直接拼 DATA_DIR；
  · 老标记 ``.trace/uploads/<file>`` → 先找工作区（旧数据原地且在），找不到再回退到
    DATA_DIR/chat_uploads/<team>/<file>（启动迁移会把旧文件搬过去，搬完两条路都通）。

本模块只依赖 config，不 import 任何路由/编排代码，避免循环引用。
"""
from __future__ import annotations

import re
import hmac
import base64
import hashlib
from pathlib import Path
from urllib.parse import quote

from game_agent.config import CHAT_UPLOADS_ROOT, PROJECTS_ROOT, SECRET_KEY

CHAT_IMG_PREFIX = "chat-uploads"
_OLD_PREFIX = ".trace/uploads/"
# 与 /projects/{id}/file?mode=raw 的 24MB 预览上限对齐；超出连缩略图都生成不了，不必存
MAX_ORIGINAL_BYTES = 24 * 1024 * 1024

# 签名密钥：优先用部署的 GA_SECRET_KEY（server 模式必须配），lite 模式是固定开发密钥。
# 文件读接口用它对 ref+过期时间签名，做成「无 cookie 也能取」的自授权 URL（见 serve_url）。
_MIN_SIG_KEY = b"trace-chat-img-sign-v1"


def _sign_key() -> bytes:
    return hashlib.sha256((SECRET_KEY or "").encode("utf-8") + _MIN_SIG_KEY).digest()


def sign_ref(ref: str, exp: int = 0) -> str:
    """给 ref（+可选过期秒）生成 HMAC 签名，放到位图 URL 里做自授权。

    <img> 标签既发不出 Authorization 头、也可能没有可用的会话 cookie（换 cookie 名/单点登录
    / 浏览器隐私策略都会让 cookie 带不上），只靠 cookie 鉴权时图片就会被 401 挡掉（用户截图
    就是这个：API 能看、图一片空）。签名 URL 不依赖 cookie，只要是在登录态下生成的地址就能取图。
    """
    msg = f"{ref}|{int(exp or 0)}".encode("utf-8")
    mac = hmac.new(_sign_key(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def verify_ref(ref: str, exp: int, sig: str) -> bool:
    """校验签名（常量时间比较）。exp=0 表示不限期，>0 表示过期时间戳。"""
    if not sig:
        return False
    if exp and int(exp) < 0:
        return False
    return hmac.compare_digest(sign_ref(ref, exp), str(sig))


def _team_dir(team_id: str | None) -> Path:
    base = CHAT_UPLOADS_ROOT / (str(team_id) if team_id else "_")
    base.mkdir(parents=True, exist_ok=True)
    return base


def save_data_url(team_id: str | None, idx: int, data_url: str) -> str:
    """把 dataURL 的原始字节存到稳定目录，返回标记 ``chat-uploads/<team>/<file>``（失败返回 ""）。"""
    head, _, b64 = (data_url or "").partition(",")
    m = re.match(r"data:image/([a-z0-9.+-]+);base64$", head, re.I)
    if not m:
        return ""
    ext = {"jpeg": "jpg"}.get(m.group(1).lower(), m.group(1).lower())[:5] or "img"
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return ""
    if not raw or len(raw) > MAX_ORIGINAL_BYTES:
        return ""
    name = f"{_uuid4_hex()}-{idx}.{ext}"
    try:
        d = _team_dir(team_id)
        dest = (d / name).resolve()
        if not dest.is_relative_to(d.resolve()):
            return ""
        with dest.open("xb") as fh:
            fh.write(raw)
    except OSError:
        return ""
    return f"{CHAT_IMG_PREFIX}/{team_id or '_'}/{name}"


def _uuid4_hex() -> str:
    from uuid import uuid4
    return uuid4().hex


def resolve_ref(ref: str, workspace: Path | None, team_id: str | None) -> Path | None:
    """把一条 image_path 标记解析成磁盘文件；解析不到返回 None。

    ref 可能是新标记（chat-uploads/...）、或老标记（.trace/uploads/...）。
    """
    s = str(ref or "").strip().replace("\\", "/")
    if not s:
        return None
    if s.startswith(CHAT_IMG_PREFIX + "/"):
        # chat-uploads/<team>/<file> → DATA_DIR/chat_uploads/<team>/<file>
        rest = s[len(CHAT_IMG_PREFIX) + 1:]
        cand = (CHAT_UPLOADS_ROOT / rest).resolve()
        if cand.is_file():
            return cand
        # 兜底：team 目录名对不上时按文件名在团队目录里找
        name = rest.rsplit("/", 1)[-1]
        alt = (_team_dir(team_id) / name)
        return alt if alt.is_file() else None
    # 老标记：先找工作区原址
    if s.startswith(_OLD_PREFIX) and workspace is not None:
        cand = (workspace / s)
        if cand.is_file():
            return cand
        # 启动迁移后旧文件会搬去 chat_uploads/<team>/<name>
        name = s[len(_OLD_PREFIX):].rsplit("/", 1)[-1]
        if team_id:
            alt = (_team_dir(team_id) / name)
            if alt.is_file():
                return alt
        # team_id 未知时（如 view_image 只有 workspace、没有团队上下文）：
        # 按文件名在任意团队目录里兜底找一遍，迁移后一定落在某个 <team>/ 下。
        for td in CHAT_UPLOADS_ROOT.glob(f"*/{name}"):
            if td.is_file():
                return td
        return None
    if workspace is not None:
        cand = (workspace / s)
        if cand.is_file():
            return cand
    return None


def serve_url(ref: str, project_id: str) -> str:
    """给前端 <img>/灯箱用的服务端 URL。

    新标记走 ``/api/uploads/chat``（只读稳定目录，不依赖项目工作区是否存在），并**带上签名**：
    <img> 发不出 Authorization、也可能带不上会话 cookie，靠签名自授权才能稳定出图。
    老标记仍走项目文件接口（原地文件还在时兼容）。
    """
    s = str(ref or "").strip()
    if s.startswith(CHAT_IMG_PREFIX + "/"):
        return f"/api/uploads/chat?ref={quote(s)}&sig={quote(sign_ref(s))}"
    return f"/api/projects/{project_id}/file?mode=raw&path={quote(s)}"
