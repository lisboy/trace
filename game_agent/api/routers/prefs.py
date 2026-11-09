"""用户偏好（外观：主题/背景/字体）+ 壁纸源（上传 / Wallpaper Engine 工坊）。

背景三种来源：
- 上传：图片或视频（mp4/webm）存 DATA_DIR/uploads，经 /uploads 静态服务
- URL：外链图片
- Wallpaper Engine：探测 Steam workshop content/431960（+用户手填目录），
  解析 project.json 列出可用的视频/图片/网页壁纸；scene 类取 preview.jpg 当静态图
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from game_agent.auth.rbac import PERM_VIEW
from game_agent.config import DATA_DIR
from game_agent.core.context import RequestContext
from game_agent.api.deps import require
from game_agent.db.models import Settings, User
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api", tags=["prefs"])

UPLOADS_DIR = DATA_DIR / "uploads"
WE_APPID = "431960"  # Wallpaper Engine 的 Steam AppId
WE_SETTINGS_KEY = "wallpaper_we_dir"
MAX_WALLPAPER_BYTES = 100 * 1024 * 1024  # 视频壁纸可能几十 MB，放宽到 100MB

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
_VID_EXTS = {".mp4", ".webm", ".mov", ".m4v"}


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


# ---------------------------------------------------------------- 个人偏好
@router.get("/me/prefs")
async def get_prefs(ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        u = await s.get(User, _u(ctx.user_id))
        return {"prefs": (u.prefs_json or {}) if u else {}}


class PrefsIn(BaseModel):
    prefs: dict


@router.put("/me/prefs")
async def put_prefs(body: PrefsIn, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """整体保存外观偏好（前端负责合并，这里不做字段级 merge，保持「所见即所存」）。"""
    import datetime as _dt
    prefs = body.prefs or {}
    if not isinstance(prefs, dict):
        raise HTTPException(status_code=400, detail="prefs 必须是对象")
    if len(json.dumps(prefs)) > 20000:
        raise HTTPException(status_code=400, detail="偏好数据过大")
    async with SessionLocal() as s:
        u = await s.get(User, _u(ctx.user_id))
        if u is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        u.prefs_json = prefs
        await s.commit()
    return {"ok": True, "prefs": prefs}


# ---------------------------------------------------------------- 壁纸上传
@router.post("/me/wallpaper")
async def upload_wallpaper(file: UploadFile,
                           ctx: RequestContext = Depends(require(PERM_VIEW))):
    """上传背景图/背景视频（≤100MB）。返回可直接放进 prefs.bgSrc 的 /uploads/... 地址。"""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix.lower()
    ctype = (file.content_type or "").lower()
    if ext not in _IMG_EXTS and ext not in _VID_EXTS:
        if ctype.startswith("image/"):
            ext = ".png"
        elif ctype.startswith("video/"):
            ext = ".mp4"
        else:
            raise HTTPException(status_code=400, detail="只支持图片或视频文件（jpg/png/webp/gif/mp4/webm）")
    data = await file.read()
    if len(data) > MAX_WALLPAPER_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 100MB，请压缩后再传")
    kind = "video" if ext in _VID_EXTS else "img"
    # 文件名按【内容哈希】生成：换一张新壁纸 → 新文件名 → 新 URL。
    # 以前按「用户+类型」固定命名，重新上传会原地覆盖同名文件，而 /uploads 又带
    # Cache-Control（浏览器把这旧图缓存住了），于是页面永远渲染第一次上传的那张。
    # 用内容哈希：不同内容必得不同 URL，天然绕过缓存；同内容重复上传则复用同一文件。
    digest = hashlib.sha1(data).hexdigest()[:16]
    uid = str(ctx.user_id).replace("-", "")[:12]
    name = f"wallpaper_{uid}_{digest}{ext}"
    (UPLOADS_DIR / name).write_bytes(data)
    # 顺手清掉这个用户以前上传的、内容不同的旧壁纸（避免无限堆积）；尽力而为，失败不影响上传
    try:
        for old in UPLOADS_DIR.glob(f"wallpaper_{uid}_*"):
            if old.name != name:
                old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return {"url": f"/uploads/{name}", "kind": kind, "size": len(data)}


# ---------------------------------------------------------------- Wallpaper Engine
def _candidate_workshop_roots() -> list[Path]:
    """探测常见 Steam 库目录下的 Wallpaper Engine 工坊内容目录。"""
    out: list[Path] = []
    seen: set[str] = set()
    for drive in ("C", "D", "E", "F", "G", "H"):
        for base in (Path(f"{drive}:/Program Files (x86)/Steam"),
                     Path(f"{drive}:/Steam"),
                     Path(f"{drive}:/SteamLibrary"),
                     Path(f"{drive}:/SteamGames")):
            wr = base / "steamapps" / "workshop" / "content" / WE_APPID
            key = str(wr).lower()
            if key not in seen and wr.is_dir():
                seen.add(key)
                out.append(wr)
    return out


async def _configured_we_root() -> str:
    async with SessionLocal() as s:
        row = await s.get(Settings, WE_SETTINGS_KEY)
        return str((row.value_json or {}).get("path") or "") if row else ""


def _we_roots(extra: str = "") -> list[Path]:
    roots: list[Path] = []
    if extra:
        roots.append(Path(extra))
    roots.extend(_candidate_workshop_roots())
    return roots


def _classify(dirname: Path) -> dict | None:
    """解析一个 WE 壁纸项目：project.json 优先，文件探测兜底。"""
    meta: dict = {}
    pj = dirname / "project.json"
    if pj.is_file():
        try:
            meta = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            meta = {}
    title = str(meta.get("title") or dirname.name)
    wtype = str(meta.get("type") or "").lower()
    file_field = str(meta.get("file") or "")

    media, kind = "", wtype
    f = dirname / file_field
    if file_field and f.is_file() and f.suffix.lower() in _VID_EXTS:
        media, kind = f.name, "video"
    elif file_field and f.is_file() and f.suffix.lower() in _IMG_EXTS:
        media, kind = f.name, "image"
    if not media:
        for child in sorted(dirname.iterdir()):
            if child.is_file() and child.suffix.lower() in _VID_EXTS:
                media, kind = child.name, "video"
                break
    if not media:
        has_html = any(x.name.lower() in ("index.html", "index.htm") for x in dirname.iterdir()
                       if x.is_file())
        has_scene = (dirname / "scene.pkg").is_file()
        if has_html:
            kind = kind or "web"
        elif has_scene:
            kind = kind or "scene"
        else:
            for child in sorted(dirname.iterdir()):
                if child.is_file() and child.suffix.lower() in _IMG_EXTS:
                    media, kind = child.name, "image"
                    break
    preview = ""
    for cand in ("preview.jpg", "preview.png", "preview.jpeg", "thumbnail.jpg"):
        if (dirname / cand).is_file():
            preview = cand
            break
    return {
        "id": dirname.name,
        "title": title[:120],
        "type": kind or "unknown",
        "media": media or None,      # 相对路径；scene 类通常没有 → 前端用 preview 当静态图
        "preview": preview or None,
        "dir": str(dirname),
    }


@router.get("/wallpapers/engines")
async def we_list(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """列出 Wallpaper Engine 工坊壁纸（可用 = 有 preview 或可直接播的视频/图片）。"""
    configured = await _configured_we_root()
    roots = _we_roots(configured)
    items: list[dict] = []
    seen: set[str] = set()
    scanned = 0
    for root in roots:
        try:
            dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in sorted(dirs, key=lambda x: x.name):
            if d.name in seen:
                continue
            seen.add(d.name)
            info = _classify(d)
            if info:
                info["root"] = str(root)
                items.append(info)
            scanned += 1
            if scanned >= 4000:  # 工坊订阅太多时别把请求拖死
                break
    return {
        "configured": configured or None,
        "detected": [str(r) for r in _candidate_workshop_roots()],
        "items": items,
    }


class WeRootIn(BaseModel):
    path: str


@router.put("/wallpapers/engines/root")
async def we_set_root(body: WeRootIn, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """手填 Wallpaper Engine 工坊目录（探测不到 Steam 库时的兜底，自服务）。"""
    import datetime as _dt
    p = (body.path or "").strip().strip('"')
    if p and not Path(p).is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{p}")
    async with SessionLocal() as s:
        row = await s.get(Settings, WE_SETTINGS_KEY)
        if row is None:
            row = Settings(key=WE_SETTINGS_KEY, value_json={})
            s.add(row)
        row.value_json = {"path": p}
        row.updated_at = _dt.datetime.now(_dt.timezone.utc)
        await s.commit()
    return {"ok": True, "path": p}


@router.get("/wallpapers/file")
async def we_file(path: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """读取 WE 壁纸文件（media/preview）。前端用带鉴权的 fetch→blob 播放，路径必须在工坊根内。"""
    try:
        rp = Path(path).resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="路径非法")
    roots = [r.resolve() for r in _we_roots(await _configured_we_root()) if r.exists()]
    inside = any(rp == root or root in rp.parents for root in roots)
    if not inside:
        raise HTTPException(status_code=403, detail="路径不在 Wallpaper Engine 工坊目录内")
    if not rp.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    media_type = "video/webm" if rp.suffix.lower() == ".webm" else None
    return FileResponse(str(rp), media_type=media_type)
