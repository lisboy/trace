"""空间 / 项目 路由（设计 §3 / §9）。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.api.deps import _u, get_current_context, get_optional_context, require
from game_agent.api.state import state
from game_agent.auth.rbac import (
    PERM_PROJECT_CREATE, PERM_RUN_CREATE, PERM_TEAM_DELETE, PERM_VIEW,
)
from game_agent.config import CHAT_UPLOADS_ROOT, MODE, PROJECTS_ROOT, default_python_bin
from game_agent.core.context import RequestContext
from game_agent.db.models import McpServer, Project, Run, Team, TeamMember

router = APIRouter(prefix="/api", tags=["teams/projects"])

# 工作区文件预览：文本扩展名白名单 + 图片/音频直接回文件
TEXT_EXTS = {".md", ".txt", ".py", ".json", ".js", ".ts", ".tsx", ".jsx", ".css",
             ".html", ".csv", ".log", ".yml", ".yaml", ".toml", ".cfg", ".ini"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
# 原图直出上限：以前对**所有**文件卡 2MB，手机截图/高清聊天附图动辄 2–6MB，缩略图能出
# （thumb 分支在限流之前），点开看原图却 400「文件过大」——灯箱一片黑。图片/音频本来就是
# 给人看/听的，按 run_raw 的同一条 24MB 上限放行；2MB 只留给文本预览。
RAW_MAX_BYTES = 24 * 1024 * 1024
TEXT_PREVIEW_MAX = 2 * 1024 * 1024


# ---------------- 个人空间 ----------------
# 个人版：一个账号对应一个工作区（内部仍用 teams 表存 id，供各表按空间分区）。
# 前端登录后调这个接口拿自己的空间 id，之后所有请求都带上 X-Team-Id。
@router.get("/teams")
async def list_teams(ctx: RequestContext = Depends(get_current_context)):
    async with state.session_factory() as s:
        rows = await s.execute(
            select(Team.id, Team.name, Team.slug, TeamMember.role)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == _u(ctx.user_id))
        )
        return [{"id": str(t[0]), "name": t[1], "slug": t[2], "role": t[3]} for t in rows.all()]


# ---------------- 项目 ----------------
class ProjectIn(BaseModel):
    name: str
    slug: str
    repo_path: str                     # PROJECTS_ROOT 下的相对路径
    project_type: str = "python"
    python_bin: str | None = None
    commands_json: dict = {}


@router.post("/teams/{team_id}/projects")
async def create_project(team_id: str, body: ProjectIn,
                         ctx: RequestContext = Depends(require(PERM_PROJECT_CREATE))):
    if team_id != ctx.team_id:
        raise HTTPException(status_code=403, detail="不能在其他工作区建项目")
    workspace = PROJECTS_ROOT / body.repo_path
    workspace.mkdir(parents=True, exist_ok=True)
    commands = body.commands_json or {
        "build": "python -m compileall -q .", "test": "python -m pytest -q",
    }
    async with state.session_factory() as s:
        proj = Project(
            team_id=_u(team_id), name=body.name, slug=body.slug,
            repo_path=body.repo_path,
            project_type=body.project_type, python_bin=body.python_bin or default_python_bin() or None,
            commands_json=commands,
        )
        s.add(proj)
        await s.commit()
        await s.refresh(proj)
        pid = str(proj.id)
    return {"id": pid, "name": body.name, "slug": body.slug, "repo_path": body.repo_path}


@router.post("/projects/{project_id}/archive")
async def archive_project(project_id: str,
                          ctx: RequestContext = Depends(require(PERM_PROJECT_CREATE))):
    """移除项目（软删除/归档）：从项目列表消失，工作区文件保留，可恢复。

    用户口径：删除的工作区不能在会话面板留下会话记录——会话列表接口按项目
    归档状态过滤（见 list_sessions），恢复项目后历史会话原样回来。
    有未结束的 run 时拒绝移除，避免孤儿运行。"""
    async with state.session_factory() as s:
        p = await s.get(Project, _u(project_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        if p.archived_at is not None:
            return {"id": str(p.id), "archived": True}
        running = await s.scalar(select(func.count()).select_from(Run).where(
            Run.project_id == p.id, Run.status.in_(("queued", "claimed", "running", "blocked"))))
        if running:
            raise HTTPException(status_code=400,
                                detail=f"该项目还有 {running} 个未结束的运行，请等待完成或取消后再移除")
        p.archived_at = datetime.now(timezone.utc)  # 与全局 UTC 存储约定一致（func.now() 是会话本地时区）
        await s.commit()
    return {"id": project_id, "archived": True}


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str,
                          ctx: RequestContext = Depends(require(PERM_PROJECT_CREATE))):
    """恢复已移除（归档）的项目。"""
    async with state.session_factory() as s:
        p = await s.get(Project, _u(project_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        p.archived_at = None
        await s.commit()
    return {"id": project_id, "archived": False}


@router.post("/projects/{project_id}/reveal")
async def reveal_project(project_id: str,
                         ctx: RequestContext = Depends(require(PERM_VIEW))):
    """在系统文件管理器里打开项目工作区目录。

    只对本机（lite）模式有意义：server 模式的「工作区」在服务端机器上，
    浏览器所在客户端打不开，直接拒绝。返回实际打开的绝对路径，便于前端提示。"""
    if MODE != "lite":
        raise HTTPException(status_code=400, detail="仅本机（lite）模式支持打开文件夹")
    async with state.session_factory() as s:
        ws = await _project_workspace(s, project_id, ctx)
    if not ws.exists():
        raise HTTPException(status_code=404, detail="工作区目录不存在")
    import subprocess as _sub
    import sys as _sys
    try:
        if _sys.platform == "win32":
            # explorer 打开目录后即使成功也常返回非 0，不能据此判断失败
            _sub.Popen(["explorer", str(ws)])
        elif _sys.platform == "darwin":
            _sub.Popen(["open", str(ws)])
        else:
            _sub.Popen(["xdg-open", str(ws)])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开文件夹失败：{type(e).__name__}: {e}")
    return {"ok": True, "path": str(ws)}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with state.session_factory() as s:
        p = await s.get(Project, _u(project_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        return {
            "id": str(p.id), "name": p.name, "slug": p.slug, "repo_path": p.repo_path,
            "project_type": p.project_type,
            "python_bin": p.python_bin, "commands_json": p.commands_json,
            "archived_at": str(p.archived_at) if p.archived_at else None,
        }


# ---------------- 目录浏览（选择仓库文件夹） ----------------
@router.get("/fs/dirs")
async def list_dirs(path: str = Query(""), ctx: RequestContext = Depends(require(PERM_VIEW))):
    """列出指定目录下的子文件夹（不含文件/隐藏目录），供「添加仓库」选择文件夹。

    lite（本机）模式允许浏览任意位置；server 模式仅限 PROJECTS_ROOT 内。"""
    if MODE != "lite":
        base = PROJECTS_ROOT.resolve()
        target = ((base / path).resolve() if path else base)
        if not str(target).startswith(str(base)):
            raise HTTPException(status_code=400, detail="server 模式仅能浏览 PROJECTS_ROOT 内目录")
    else:
        target = (Path(path).resolve() if path else PROJECTS_ROOT.resolve())
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")
    entries: list[dict] = []
    if not path:
        # 顶层视图：附盘符（Windows）便于跳到任意磁盘
        for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{c}:\\")
            if root.is_dir():
                entries.append({"name": f"磁盘 {c}:", "path": str(root)})
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                entries.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass  # 无权限目录静默跳过
    return {"path": str(target), "parent": str(target.parent) if path else "", "dirs": entries}


@router.get("/teams/{team_id}/projects")
async def list_projects(team_id: str, archived: bool = Query(False),
                        ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with state.session_factory() as s:
        q = select(Project).where(Project.team_id == _u(team_id))
        q = q.where(Project.archived_at.is_not(None)) if archived else q.where(Project.archived_at.is_(None))
        q = q.order_by(Project.created_at.desc())  # 工作区按创建时间倒序：最新添加的排最上面
        rows = await s.execute(q)
        projects = rows.scalars().all()
        out = []
        for p in projects:
            cnt = await s.scalar(select(func.count()).select_from(Run).where(Run.project_id == p.id))
            out.append({"id": str(p.id), "name": p.name, "slug": p.slug,
                        "repo_path": p.repo_path, "run_count": cnt,
                        "archived_at": str(p.archived_at) if p.archived_at else None})
        return out


@router.get("/projects/{project_id}/runs")
async def list_runs(project_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with state.session_factory() as s:
        rows = await s.execute(select(Run).where(Run.project_id == _u(project_id))
                               .order_by(Run.created_at.desc()).limit(50))
        return [_run_dict(r) for r in rows.scalars().all()]


async def _resolve_chat_upload_path(path: str, ctx: RequestContext | None,
                                    allow_any_team: bool = False) -> Path | None:
    """把 ``chat-uploads/<team>/<file>`` 形式的路径解析到 DATA_DIR/chat_uploads，并做团队校验。

    供两条路复用：①新的 /api/uploads/chat；②老前端打到的 /projects/{id}/file（向后兼容）。
    ``allow_any_team=True`` 时跳过团队校验（仅用于已通过签名校验的自授权 URL）。
    未命中/越权/不存在一律返回 None，由调用方决定 404。"""
    s = str(path or "").strip().replace("\\", "/")
    prefix = "chat-uploads/"
    if not s.startswith(prefix):
        return None
    rest = s[len(prefix):]
    if "/" not in rest:
        return None
    team_id = rest.split("/", 1)[0]
    if not allow_any_team:
        if ctx is None:
            return None
        # <img> 无法携带 X-Team-Id，先按 ref 内嵌 team 校验成员关系（而非只认 header）
        if team_id != str(ctx.team_id):
            async with state.session_factory() as db:
                ok = await db.scalar(select(TeamMember.role).where(
                    TeamMember.team_id == _u(team_id),
                    TeamMember.user_id == _u(ctx.user_id)))
            if ok is None:
                return None
    target = (CHAT_UPLOADS_ROOT / rest).resolve()
    team_dir = (CHAT_UPLOADS_ROOT / team_id).resolve()
    if team_dir not in target.parents or not target.is_file():
        return None
    return target


@router.get("/projects/{project_id}/file")
async def read_project_file(project_id: str, path: str = Query(...),
                            mode: str = Query("text"),
                            thumb: int = Query(0),
                            ctx: RequestContext = Depends(require(PERM_VIEW))):
    """读取工作区内文件用于预览：mode=text 返回 {content}（文本白名单），
    mode=raw 直接回文件（图片/音频）。路径越界、超限一律拒绝。

    thumb=长边像素（如 320）：图片按需缩放成 JPEG 并缓存到 .trace/thumbnails/，
    消息附图/截图缩略图都走它，避免每次把几 MB 原图塞进列表。"""
    async with state.session_factory() as s:
        p = await s.get(Project, _u(project_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
    workspace = (PROJECTS_ROOT / p.repo_path).resolve()
    target = (workspace / path).resolve()
    if not str(target).startswith(str(workspace)):
        raise HTTPException(status_code=400, detail="非法路径")
    if not target.is_file():
        # 向后兼容：老前端 / 老后端会用本接口加载聊天附图（path=chat-uploads/<team>/<file>），
        # 但附图实际存在 DATA_DIR/chat_uploads 里、不在工作区，按工作区解析必然 404（这正是
        # 「刚发的图也显示已失效」在版本错配下的成因）。这里转发到同一份稳定目录，旧 URL 不再失效。
        legacy = await _resolve_chat_upload_path(path, ctx)
        if legacy is not None:
            ext2 = legacy.suffix.lower()
            if thumb and ext2 in IMG_EXTS:
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(_chat_upload_thumb, legacy, ext2, thumb)
            return FileResponse(str(legacy), headers={
                "Cache-Control": "public, max-age=0, must-revalidate"})
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = target.suffix.lower()
    is_img = ext in IMG_EXTS
    # 缩略图：只对图片生效，失败回退原图（Pillow 是 CPU 活，丢线程池）
    if thumb and is_img:
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_project_thumb, workspace, target, ext, thumb)
    if mode == "raw" or is_img or ext in AUDIO_EXTS:
        # 原图/附图/音频直出：按 24MB 放行（svg 也走这里，见下方按 ext 判断媒体类型）
        if target.stat().st_size > RAW_MAX_BYTES:
            raise HTTPException(status_code=400, detail="文件过大，不支持预览")
        # 原图/附图可能被重画原地改写：用 ETag 每次回源校验，变了立刻拿新图，没变走 304。
        st = target.stat()
        etag = f"{st.st_mtime_ns:x}_{st.st_size:x}"
        return FileResponse(str(target), headers={
            "Cache-Control": "public, max-age=0, must-revalidate", "ETag": etag})
    if ext not in TEXT_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持预览的文件类型：{ext}")
    if target.stat().st_size > TEXT_PREVIEW_MAX:
        raise HTTPException(status_code=400, detail="文件过大，不支持预览")
    return {"path": path, "content": target.read_text(encoding="utf-8", errors="replace")}


@router.get("/uploads/chat")
async def serve_chat_image(ref: str = Query(...), thumb: int = Query(0),
                           sig: str = Query(""), exp: int = Query(0),
                           ctx: RequestContext | None = Depends(get_optional_context)):
    """聊天附图原图：从 DATA_DIR/chat_uploads 直出（与工作区是否还在无关）。

    以前附图原图存在项目工作区的 .trace/uploads 里，跑完/回滚/换机器就没了，灯箱只能黑屏。
    现在独立存放，前端通过 ``?ref=chat-uploads/<team>/<file>`` 读。**两种放行方式**：
      ① 带有效签名 sig（serve_url 生成）：自授权（<img> 带不了自定义头，签名最省事）；
      ② 无签名：按本机工作区校验（这个版本没有登录，ctx 恒存在）。
    thumb=长边像素出缩略图。"""
    from starlette.concurrency import run_in_threadpool
    from game_agent.core import chat_images

    signed = chat_images.verify_ref(ref, exp, sig)
    if not signed and ctx is None:
        raise HTTPException(status_code=404, detail="附图不存在")
    target = await _resolve_chat_upload_path(ref, ctx, allow_any_team=signed)
    if target is None:
        raise HTTPException(status_code=404, detail="附图不存在")
    ext = target.suffix.lower()
    if thumb and ext in IMG_EXTS:
        return await run_in_threadpool(_chat_upload_thumb, target, ext, thumb)
    st = target.stat()
    etag = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    return FileResponse(str(target), headers={
        "Cache-Control": "public, max-age=0, must-revalidate", "ETag": etag})


def _chat_upload_thumb(target: Path, ext: str, w: int):  # type: ignore[no-untyped-def]
    """聊天附图缩略图：缓存到 CHAT_UPLOADS_ROOT/.thumbs/<hash>_<w>_<版本>.jpg（版本取 mtime+size）。"""
    import hashlib

    w = max(64, min(int(w), 1280))
    st = target.stat()
    token = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    h = hashlib.sha1(str(target.name).encode("utf-8")).hexdigest()[:20]
    cache = CHAT_UPLOADS_ROOT / ".thumbs"
    dest = cache / f"{h}_{w}_{token}.jpg"
    cache_hdr = {"Cache-Control": "public, max-age=0, must-revalidate", "ETag": token}
    if dest.is_file() and dest.stat().st_size > 0:
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    png_dest = dest.with_suffix(".png")
    if png_dest.is_file() and png_dest.stat().st_size > 0:
        return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
    try:
        if st.st_size > 30 * 1024 * 1024:
            return FileResponse(str(target), headers=cache_hdr)
        from PIL import Image
        im = Image.open(target)
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if max(im.size) > w:
            im.thumbnail((w, w), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if has_alpha:
            tmp = png_dest.with_suffix(".png.tmp")
            im.save(tmp, format="PNG", optimize=True)
            tmp.replace(png_dest)
            return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
        if im.mode != "RGB":
            im = im.convert("RGB")
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        im.save(tmp, format="JPEG", quality=80, optimize=True)
        tmp.replace(dest)
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    except Exception:  # noqa: BLE001 - 生成失败回原图
        return FileResponse(str(target), headers=cache_hdr)


def _project_thumb(workspace: Path, target: Path, ext: str, w: int):  # type: ignore[no-untyped-def]
    """工作区图片的按需缩略图：缓存到 <workspace>/.trace/thumbnails/<hash>_<w>_<版本>.jpg。

    版本取原图 mtime+size：附图/截图文件被重画或重新上传后自动重建缩略图，
    不会出现「缩略图还是旧图、点开原图是新图」的错位。"""
    import hashlib

    w = max(64, min(int(w), 1280))
    try:
        rel = target.relative_to(workspace)
    except ValueError:
        rel = target
    h = hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:20]
    st = target.stat()
    token = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    dest = workspace / ".trace" / "thumbnails" / f"{h}_{w}_{token}.jpg"
    cache_hdr = {"Cache-Control": "public, max-age=0, must-revalidate", "ETag": token}
    if dest.is_file() and dest.stat().st_size > 0:
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    png_dest = dest.with_suffix(".png")
    if png_dest.is_file() and png_dest.stat().st_size > 0:
        return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
    try:
        if st.st_size > 30 * 1024 * 1024:
            return FileResponse(str(target), headers=cache_hdr)
        from PIL import Image
        im = Image.open(target)
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if max(im.size) > w:
            im.thumbnail((w, w), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if has_alpha:
            tmp = png_dest.with_suffix(".png.tmp")
            im.save(tmp, format="PNG", optimize=True)
            tmp.replace(png_dest)
            return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
        if im.mode != "RGB":
            im = im.convert("RGB")
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        im.save(tmp, format="JPEG", quality=80, optimize=True)
        tmp.replace(dest)
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    except Exception:  # noqa: BLE001 - 生成失败回原图
        return FileResponse(str(target), headers=cache_hdr)


# ---------------- MCP 服务器配置（可配置界面） ----------------
import json as _json


class McpServerIn(BaseModel):
    name: str
    transport: str = "stdio"  # stdio | http
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    enabled: bool = True


def _mcp_dict(m: McpServer) -> dict:
    return {
        "id": str(m.id), "name": m.name, "transport": m.transport,
        "command": m.command or "", "args": _json.loads(m.args_json or "[]"),
        "env": _json.loads(m.env_json or "{}"), "url": m.url or "",
        "enabled": bool(m.enabled),
    }


@router.get("/projects/{project_id}/mcp")
async def list_mcp(project_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with state.session_factory() as s:
        rows = (await s.execute(
            select(McpServer).where(McpServer.project_id == _u(project_id))
            .order_by(McpServer.created_at))).scalars().all()
    return [_mcp_dict(m) for m in rows]


@router.post("/projects/{project_id}/mcp")
async def create_mcp(project_id: str, body: McpServerIn,
                     ctx: RequestContext = Depends(require(PERM_PROJECT_CREATE))):
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_\-]{1,64}$", body.name):
        raise HTTPException(400, "名称限字母/数字/下划线/中划线（1-64 字符）")
    if body.transport not in ("stdio", "http"):
        raise HTTPException(400, "transport 仅支持 stdio / http")
    if body.transport == "stdio" and not body.command.strip():
        raise HTTPException(400, "stdio 传输必须填 command")
    if body.transport == "http" and not body.url.strip():
        raise HTTPException(400, "http 传输必须填 url")
    async with state.session_factory() as s:
        dup = (await s.execute(
            select(McpServer).where(McpServer.project_id == _u(project_id),
                                    McpServer.name == body.name))).scalar_one_or_none()
        if dup:
            raise HTTPException(400, f"同名服务器已存在：{body.name}")
        m = McpServer(project_id=_u(project_id), name=body.name, transport=body.transport,
                      command=body.command.strip() or None,
                      args_json=_json.dumps(body.args, ensure_ascii=False),
                      env_json=_json.dumps(body.env, ensure_ascii=False),
                      url=body.url.strip() or None, enabled=body.enabled)
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return _mcp_dict(m)


@router.patch("/projects/{project_id}/mcp/{server_id}")
async def patch_mcp(project_id: str, server_id: str, body: McpServerIn,
                    ctx: RequestContext = Depends(require(PERM_PROJECT_CREATE))):
    async with state.session_factory() as s:
        m = (await s.execute(
            select(McpServer).where(McpServer.id == _u(server_id),
                                    McpServer.project_id == _u(project_id))
        )).scalar_one_or_none()
        if m is None:
            raise HTTPException(404, "MCP 服务器不存在")
        m.transport = body.transport if body.transport in ("stdio", "http") else "stdio"
        m.command = body.command.strip() or None
        m.args_json = _json.dumps(body.args, ensure_ascii=False)
        m.env_json = _json.dumps(body.env, ensure_ascii=False)
        m.url = body.url.strip() or None
        m.enabled = body.enabled
        await s.commit()
        return _mcp_dict(m)


@router.delete("/projects/{project_id}/mcp/{server_id}")
async def delete_mcp(project_id: str, server_id: str,
                     ctx: RequestContext = Depends(require(PERM_PROJECT_CREATE))):
    async with state.session_factory() as s:
        m = (await s.execute(
            select(McpServer).where(McpServer.id == _u(server_id),
                                    McpServer.project_id == _u(project_id))
        )).scalar_one_or_none()
        if m is None:
            raise HTTPException(404, "MCP 服务器不存在")
        await s.delete(m)
        await s.commit()
        return {"ok": True}


def _run_dict(r: Run) -> dict:
    return {
        "id": str(r.id), "status": r.status, "run_type": r.run_type,
        "session_id": str(r.session_id),
        "priority": r.priority, "has_placeholder": r.has_placeholder,
        "created_at": str(r.created_at), "finished_at": str(r.finished_at) if r.finished_at else None,
        "final_summary": (r.final_summary or "")[:500],
    }


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


# ---------- 项目脚本：工作区根目录 run.bat|start.bat / stop.bat 的运行与停止 ----------

_RUN_SCRIPTS = ("run.bat", "start.bat")
_STOP_SCRIPTS = ("stop.bat",)


async def _project_workspace(s, project_id: str, ctx: RequestContext):
    """校验项目归属并返回工作区绝对路径（repo_path 兼容绝对路径）。"""
    p = await s.get(Project, _u(project_id))
    if p is None or str(p.team_id) != ctx.team_id:
        raise HTTPException(status_code=404, detail="项目不存在")
    return (PROJECTS_ROOT / p.repo_path).resolve()


@router.get("/projects/{project_id}/scripts")
async def project_scripts(project_id: str,
                          ctx: RequestContext = Depends(require(PERM_VIEW))):
    """检测项目工作区根目录的启动/停止脚本存在性（run.bat|start.bat、stop.bat）。"""
    try:
        async with state.session_factory() as s:
            ws = await _project_workspace(s, project_id, ctx)
        run_s = next((n for n in _RUN_SCRIPTS if (ws / n).is_file()), None)
        stop_s = next((n for n in _STOP_SCRIPTS if (ws / n).is_file()), None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"检测脚本失败：{type(e).__name__}: {e}")
    return {"run_script": run_s, "stop_script": stop_s}


@router.post("/projects/{project_id}/scripts/{action}")
async def run_project_script(project_id: str, action: str,
                             ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """执行项目工作区根目录的 bat 脚本（action: run|stop）。分离启动不等待——
    启动脚本通常是长驻服务；结果不回传（用户可通过服务自身验证）。"""
    import subprocess as _sub

    if action not in ("run", "stop"):
        raise HTTPException(status_code=400, detail="action 只支持 run|stop")
    async with state.session_factory() as s:
        ws = await _project_workspace(s, project_id, ctx)
    names = _RUN_SCRIPTS if action == "run" else _STOP_SCRIPTS
    script = next((n for n in names if (ws / n).is_file()), None)
    if script is None:
        raise HTTPException(status_code=404, detail=f"工作区根目录没有 {names[0]}")
    flags = _sub.CREATE_NEW_PROCESS_GROUP | _sub.DETACHED_PROCESS
    try:
        # bat 经 cmd /c 启动；输出丢弃，长驻服务在后台跑
        proc = _sub.Popen(["cmd", "/c", script], cwd=str(ws),
                          stdout=_sub.DEVNULL, stderr=_sub.DEVNULL,
                          creationflags=flags, close_fds=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"脚本启动失败：{e}")
    return {"started": True, "script": script, "pid": proc.pid}
