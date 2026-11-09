"""素材：列表 / 重画 / 下载 / 导入 / 外置源拉取（设计 §7.8 / §7.9）。"""
from __future__ import annotations

import io
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.api.deps import get_current_context, require
from game_agent.api.state import state
from game_agent.auth.rbac import PERM_AGENT_CONFIG, PERM_DOWNLOAD, PERM_VIEW
from game_agent.assets.comfyui import ComfyUIError, generate_image_bytes as comfy_generate
from game_agent.assets.placeholder import _parse_size, generate_placeholder_image
from game_agent.config import (
    COMFYUI_BASE_URL, COMFYUI_NEGATIVE_PROMPT, COMFYUI_TIMEOUT_S, COMFYUI_WORKFLOW_MODE,
    PLACEHOLDER_ENABLED, PROJECTS_ROOT,
)
from game_agent.core.context import RequestContext
from game_agent.db.models import Artifact, AssetProvider, Project, Run, Session
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api", tags=["artifacts"])

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
MAX_ASSET_BYTES = 20 * 1024 * 1024  # 单文件 20MB
# 视频天生大，单独给更高上限（本地 Wan 出片 3~5s 常常 3~15MB）
MAX_VIDEO_BYTES = 200 * 1024 * 1024


def _classify(ext: str, kind_hint: str) -> str | None:
    """按扩展名 + 意图归类素材 kind；无法判定返回 None。"""
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMG_EXTS:
        return "icon" if kind_hint == "icon" else "image"
    if kind_hint == "icon":
        return "icon"
    if kind_hint == "image":
        return "image"
    if kind_hint == "video":
        return "video"
    if kind_hint in ("audio", "music", "voice", "sfx"):
        return "audio"
    return None


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", Path(name or "asset").stem)[:40].strip("_")
    return stem or "asset"


@router.get("/artifacts")
async def list_artifacts(run_id: str | None = Query(None),
                        project_id: str | None = Query(None),
                        asset_kind: str | None = Query(None),
                        session_id: str | None = Query(None),
                        placeholder: bool = Query(False),
                        limit: int = Query(0),
                        before: str | None = Query(None),
                        before_id: str | None = Query(None),
                        ctx: RequestContext = Depends(require(PERM_VIEW))):
    """素材列表。默认（limit=0）返回全部，保持老行为（轨迹块按 run_id 拉产物需要全量）。
    素材库带 ``limit`` 分页：按 created_at 新→旧取一页，``before``/``before_id``
    为上页最后一条的 (created_at, id) 复合游标——同一时刻并列的多条也不会漏。
    ``asset_kind`` / ``session_id`` 在**服务端**过滤：分页时若把筛选留给前端，
    只在「已加载的这一页」里筛，会看起来像「素材变少了」。"""
    kind = (asset_kind or "").strip()
    want_kinds = None
    if kind and kind != "all":
        if kind == "image":
            want_kinds = ["image", "icon"]  # 「图片」含图标，与前端 isImageKind 一致
        else:
            want_kinds = [kind]
    want_session = (session_id or "").strip()

    def _filters() -> list:
        fs = [Artifact.team_id == _u(ctx.team_id)]
        if run_id:
            fs.append(Artifact.run_id == _u(run_id))
        if project_id:
            fs.append(Artifact.project_id == _u(project_id))
        if want_kinds:
            fs.append(Artifact.asset_kind.in_(want_kinds))
        if want_session:
            if want_session == "__none__":
                fs.append(Run.session_id.is_(None))
            else:
                fs.append(Run.session_id == _u(want_session))
        if placeholder:
            fs.append(Artifact.is_placeholder.is_(True))
        return fs

    async with SessionLocal() as s:
        # 带上 run/session：素材库要按「来源会话」筛选（同一次对话产出的图/语音归在一组）。
        q = (select(Artifact, Run.session_id, Session.title)
             .outerjoin(Run, Artifact.run_id == Run.id)
             .outerjoin(Session, Run.session_id == Session.id)
             .where(*_filters()))
        n = max(0, int(limit or 0))
        has_more = False
        if n:
            if before:
                dt = None
                try:
                    dt = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                except ValueError:
                    dt = None
                if dt is not None:
                    if before_id:
                        bid = _u(before_id)
                        q = q.where(or_(
                            Artifact.created_at < dt,
                            and_(Artifact.created_at == dt, Artifact.id < bid)))
                    else:
                        q = q.where(Artifact.created_at < dt)
                else:
                    q = q.where(Artifact.created_at < before)
            q = q.order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(n)
            rows = (await s.execute(q)).all()
            if len(rows) == n:
                last_a = rows[-1][0]
                cnt = await s.execute(
                    select(func.count()).select_from(Artifact)
                    .outerjoin(Run, Artifact.run_id == Run.id)
                    .where(*_filters(),
                           or_(Artifact.created_at < last_a.created_at,
                               and_(Artifact.created_at == last_a.created_at,
                                    Artifact.id < last_a.id))))
                has_more = (cnt.scalar() or 0) > 0
        else:
            # 新到旧：素材库最新产出的排最前
            q = q.order_by(Artifact.created_at.desc())
            rows = (await s.execute(q)).all()
        out = []
        for a, sid, title in rows:
            params = a.params_json or {}
            out.append({
                "id": str(a.id), "asset_kind": a.asset_kind, "slug": a.slug,
                "rel_path": a.rel_path, "is_placeholder": a.is_placeholder,
                "provider": a.provider, "size": a.size,
                # 配音产物把「念的台词」存在 params_json.text：轨迹里音频播放器要显示台词
                # 而不是一串文件路径（用户反馈「显示台词，不要显示路径」）。只回这一个标量，
                # 不把整个 params_json 抛给前端（里面可能有好几百字的音色描述等）。
                "text": str(params.get("text") or "")[:500],
                # 前端按 created_at 排序 + 按工具行时间窗归属产物；缺了它
                # 时间窗过滤会把全部产物当成 0 时刻直接丢掉（2026-10-29 用户反馈「新图没加进来」）
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "run_id": str(a.run_id) if a.run_id else None,
                "session_id": str(sid) if sid else None,
                "session_title": title,
            })
        if n:
            return {"artifacts": out, "has_more": has_more,
                    "oldest": out[-1]["created_at"] if out else None,
                    "oldest_id": out[-1]["id"] if out else None}
        return out


@router.get("/artifacts/session-sources")
async def artifact_session_sources(project_id: str | None = Query(None),
                                   ctx: RequestContext = Depends(require(PERM_VIEW))):
    """素材库「来源会话」下拉项：只回 {session_id, title} 的轻量列表。
    分页后前端手里只有一页素材，不能再靠它去去重出全部会话，所以单独给这个接口。"""
    async with SessionLocal() as s:
        q = (select(Run.session_id, Session.title)
             .select_from(Artifact)
             .join(Run, Artifact.run_id == Run.id)
             .join(Session, Run.session_id == Session.id)
             .where(Artifact.team_id == _u(ctx.team_id),
                    Run.session_id.is_not(None))
             .distinct())
        if project_id:
            q = q.where(Artifact.project_id == _u(project_id))
        rows = (await s.execute(q)).all()
        return [{"session_id": str(sid), "title": title} for sid, title in rows if sid]


@router.post("/artifacts/{artifact_id}/regenerate")
async def regenerate(artifact_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """素材库重画：配置了 ComfyUI 走真实生图（失败回退占位），否则占位重画。"""
    async with SessionLocal() as s:
        a = await s.get(Artifact, _u(artifact_id))
        if a is None or str(a.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="素材不存在")
        if not PLACEHOLDER_ENABLED and not COMFYUI_BASE_URL:
            raise HTTPException(status_code=400, detail="占位模式已关闭且无真实供应商")
        # 重画走的是文生图/图生图管线，只能作用于图片类素材；音频/视频写进去会把文件类型弄乱
        if a.asset_kind not in ("image", "icon"):
            raise HTTPException(status_code=400,
                                detail="只有图片素材支持「重画」；音频到「语音生成」、视频到「视频生成」里重新生成")
        project = await s.get(Project, a.project_id)
    workspace = PROJECTS_ROOT / project.repo_path
    rel = a.rel_path
    params = a.params_json or {}

    comfy_err = ""
    if COMFYUI_BASE_URL and a.asset_kind == "image":
        try:
            w, h = _parse_size(params.get("size", "512x256"))
            # 重画以原图为参考图（img2img denoise=0.6）：保持角色/构图一致性；
            # 原图缺失则退回纯文生图
            ref_bytes: bytes | None = None
            try:
                old = (workspace / rel)
                if old.is_file():
                    ref_bytes = old.read_bytes()
            except Exception:  # noqa: BLE001
                ref_bytes = None
            data = await comfy_generate(
                params.get("prompt", ""), negative_prompt=COMFYUI_NEGATIVE_PROMPT,
                width=w, height=h, base_url=COMFYUI_BASE_URL,
                workflow_mode=COMFYUI_WORKFLOW_MODE,
                timeout_s=COMFYUI_TIMEOUT_S, reference=ref_bytes,
                reference_mode="img2img")
            (workspace / rel).write_bytes(data)
            async with SessionLocal() as s2:
                row = await s2.get(Artifact, _u(artifact_id))
                if row is not None:
                    row.provider, row.is_placeholder, row.size = "comfyui", False, len(data)
                    await s2.commit()
            return {"ok": True, "rel_path": rel, "placeholder": False, "provider": "comfyui"}
        except ComfyUIError as e:
            comfy_err = str(e)
            if not PLACEHOLDER_ENABLED:
                raise HTTPException(status_code=502, detail=f"ComfyUI 生成失败：{comfy_err}")

    info = generate_placeholder_image(str(workspace / rel), a.slug,
                                     params.get("size", "512x256"),
                                     params.get("transparent", False),
                                     prompt=params.get("prompt", ""))
    return {"ok": True, "rel_path": rel, "placeholder": True, "provider": "placeholder",
            "note": f"ComfyUI 失败已回退占位：{comfy_err}" if comfy_err else ""}


@router.get("/artifacts/{artifact_id}/file")
async def download(artifact_id: str, ctx: RequestContext = Depends(require(PERM_DOWNLOAD))):
    async with SessionLocal() as s:
        a = await s.get(Artifact, _u(artifact_id))
        project = await s.get(Project, a.project_id)
    workspace = PROJECTS_ROOT / project.repo_path
    path = (workspace / a.rel_path).resolve()
    if not str(path).startswith(str(workspace.resolve())):
        raise HTTPException(status_code=400, detail="非法路径")
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    # 重画会原地改写同一个文件：ETag 用 mtime+size，浏览器每次回源校验，
    # 文件没变走 304，变了立刻拿到新图——避免「缩略图是新的、点开大图是旧的」。
    st = path.stat()
    etag = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    return FileResponse(str(path), headers={
        "Cache-Control": "public, max-age=0, must-revalidate", "ETag": etag})


THUMB_DIRNAME = ".trace/thumbnails"   # 缩略图缓存随其它操作目录一起收进 .trace/
_THUMB_MAX_SRC = 30 * 1024 * 1024  # 原图超过 30MB 不生成缩略图（直接回原图）


def _thumb_path(workspace: Path, artifact_id: str, w: int, token: str = "") -> Path:
    """缩略图缓存路径：token=原图 mtime+size。原图被重画后 token 变化 → 自动换新文件，
    不会再把旧图的缩略图挂在新图旁边（用户反馈「点开大图和缩略图不一致」）。"""
    suffix = f"_{token}" if token else ""
    return workspace / THUMB_DIRNAME / f"{artifact_id}_{w}{suffix}.jpg"


def _make_thumb(src: Path, dest: Path, w: int) -> Path:
    """把 src 缩到长边=w 存成缩略图；有透明通道存 PNG（保留透明），否则 JPEG。
    返回实际落盘路径（扩展名可能从 .jpg 换成 .png）。Pillow 不可用/解不开时抛异常由上层兜底。"""
    from PIL import Image

    im = Image.open(src)
    im.load()
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if max(im.size) > w:
        im.thumbnail((w, w), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if has_alpha:
        out = dest.with_suffix(".png")
        tmp = out.with_suffix(out.suffix + ".tmp")
        im.save(tmp, format="PNG", optimize=True)
        tmp.replace(out)
        return out
    if im.mode != "RGB":
        im = im.convert("RGB")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    im.save(tmp, format="JPEG", quality=80, optimize=True)
    tmp.replace(dest)
    return dest


@router.get("/artifacts/{artifact_id}/thumb")
async def thumb(artifact_id: str, w: int = Query(320),
                ctx: RequestContext = Depends(require(PERM_VIEW))):
    """素材缩略图：按长边 w(=160/320/640) 生成到工作区 .trace/thumbnails/。

    缓存按「原图 mtime+size」作版本：原图重画后自动重新生成，配合 ETag 让浏览器
    每次回源校验——不会出现缩略图是旧版、点开大图是新版的情况。
    列表/网格/消息附图都用它，点开大图才加载 /file 原图——这是把「一屏上百张
    1–2MB 原图」从几百 MB 降到几 MB 的关键。生成失败一律回退原图（不挡显示）。"""
    w = max(64, min(int(w), 1280))
    async with SessionLocal() as s:
        a = await s.get(Artifact, _u(artifact_id))
        if a is None or str(a.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="素材不存在")
        project = await s.get(Project, a.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    workspace = (PROJECTS_ROOT / project.repo_path).resolve()
    src = (workspace / a.rel_path).resolve()
    if not str(src).startswith(str(workspace)):
        raise HTTPException(status_code=400, detail="非法路径")
    if not src.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if a.asset_kind not in ("image", "icon"):
        # 音频/视频等非图片：没有缩略图，原样回文件（FileResponse 按后缀推断 mime，mp4→video/mp4）
        return FileResponse(str(src))
    st = src.stat()
    token = f"{st.st_mtime_ns:x}_{st.st_size:x}"
    dest = _thumb_path(workspace, str(a.id), w, token)
    cache_hdr = {"Cache-Control": "public, max-age=0, must-revalidate", "ETag": token}
    if dest.is_file() and dest.stat().st_size > 0:
        return FileResponse(str(dest), media_type="image/jpeg", headers=cache_hdr)
    png_dest = dest.with_suffix(".png")
    if png_dest.is_file() and png_dest.stat().st_size > 0:
        return FileResponse(str(png_dest), media_type="image/png", headers=cache_hdr)
    try:
        if st.st_size > _THUMB_MAX_SRC:
            return FileResponse(str(src), headers=cache_hdr)
        # Pillow 是同步 CPU 活，丢线程池，别卡事件循环
        from starlette.concurrency import run_in_threadpool
        out = await run_in_threadpool(_make_thumb, src, dest, w)
        media = "image/png" if str(out).endswith(".png") else "image/jpeg"
        return FileResponse(str(out), media_type=media, headers=cache_hdr)
    except Exception:  # noqa: BLE001 - 生成失败就回原图，绝不挡素材显示
        return FileResponse(str(src), headers=cache_hdr)


def _delete_artifact_files(workspace: Path, artifact_id: str, rel_path: str) -> None:
    """删除素材本体文件 + 该素材的缩略图缓存。

    文件不存在 / 路径越界 / 权限异常一律吞掉：删除以「数据库记录」为准，
    不能因为某个文件删不掉就把这条素材留在库里（用户会以为没删掉）。"""
    try:
        target = (workspace / rel_path).resolve()
        if str(target).startswith(str(workspace.resolve())) and target.is_file():
            target.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        tdir = workspace / THUMB_DIRNAME
        if tdir.is_dir():
            for p in tdir.glob(f"{artifact_id}_*"):
                try:
                    p.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str,
                          ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """删除单个素材：连同工作区文件、缩略图缓存一起清掉（仅限本团队）。"""
    async with SessionLocal() as s:
        a = await s.get(Artifact, _u(artifact_id))
        if a is None or str(a.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="素材不存在")
        project = await s.get(Project, a.project_id)
        aid, rel_path = str(a.id), a.rel_path
        await s.delete(a)
        await s.commit()
    if project is not None:
        workspace = (PROJECTS_ROOT / project.repo_path).resolve()
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(_delete_artifact_files, workspace, aid, rel_path)
    return {"ok": True, "id": artifact_id}


class _ClearIn(BaseModel):
    project_id: str
    asset_kind: str = "all"
    session_id: str = "all"
    placeholder: bool = False


@router.post("/artifacts/clear")
async def clear_artifacts(body: _ClearIn,
                          ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """按当前筛选清空素材库（删库内记录 + 工作区文件 + 缩略图缓存）。

    只清本团队、指定项目、且命中筛选（类型 / 会话 / 仅占位）的素材；
    筛选全为 all 时即清空整个项目的素材库。返回实际删除条数。"""
    kind = (body.asset_kind or "").strip()
    want_kinds = None
    if kind and kind != "all":
        want_kinds = ["image", "icon"] if kind == "image" else [kind]
    want_session = (body.session_id or "").strip()
    if want_session == "all":
        want_session = ""

    def _filters() -> list:
        fs = [Artifact.team_id == _u(ctx.team_id), Artifact.project_id == _u(body.project_id)]
        if want_kinds:
            fs.append(Artifact.asset_kind.in_(want_kinds))
        if want_session:
            if want_session == "__none__":
                fs.append(Run.session_id.is_(None))
            else:
                fs.append(Run.session_id == _u(want_session))
        if body.placeholder:
            fs.append(Artifact.is_placeholder.is_(True))
        return fs

    async with SessionLocal() as s:
        project = await s.get(Project, _u(body.project_id))
        if project is None or str(project.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        rows = (await s.execute(
            select(Artifact).outerjoin(Run, Artifact.run_id == Run.id).where(*_filters())
        )).scalars().all()
        targets = [(str(a.id), a.rel_path) for a in rows]
        for a in rows:
            await s.delete(a)
        await s.commit()
    # 文件删除丢线程池：素材可能上百个，别在一次请求里卡住事件循环
    workspace = (PROJECTS_ROOT / project.repo_path).resolve()
    from starlette.concurrency import run_in_threadpool
    for aid, rel in targets:
        await run_in_threadpool(_delete_artifact_files, workspace, aid, rel)
    return {"ok": True, "deleted": len(targets)}


class _ProjectCheck:
    """校验项目归属并返回 (project, workspace)。"""
    def __init__(self, project_id: str, ctx: RequestContext):
        self.project_id, self.ctx = project_id, ctx

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        async with SessionLocal() as s:
            p = await s.get(Project, _u(self.project_id))
            if p is None or str(p.team_id) != self.ctx.team_id:
                raise HTTPException(status_code=404, detail="项目不存在")
            self.project = p
        self.workspace = (PROJECTS_ROOT / p.repo_path).resolve()
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return False


# ---------------- 导入本地素材 ----------------
@router.post("/projects/{project_id}/assets/import")
async def import_assets(project_id: str,
                        files: list[UploadFile] = File(...),
                        kind: str = Form("auto"),
                        ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """直接导入本地音频/图片/图标文件到素材库（无 run 关联）。"""
    if kind not in ("auto", "image", "icon", "audio", "video"):
        raise HTTPException(status_code=400, detail="kind 只能是 auto|image|icon|audio|video")
    async with _ProjectCheck(project_id, ctx) as pc:
        dest_dir = pc.workspace / "assets" / "import"
        dest_dir.mkdir(parents=True, exist_ok=True)
        imported, skipped = [], []
        async with SessionLocal() as s:
            for f in files[:10]:
                ext = Path(f.filename or "").suffix.lower()
                akind = _classify(ext, kind)
                if akind is None:
                    skipped.append({"name": f.filename, "reason": "无法识别的文件类型"})
                    continue
                data = await f.read()
                cap = MAX_VIDEO_BYTES if akind == "video" else MAX_ASSET_BYTES
                if len(data) > cap:
                    skipped.append({"name": f.filename,
                                    "reason": f"超过 {cap // (1024 * 1024)}MB"})
                    continue
                fname = f"{int(time.time())}_{_safe_stem(f.filename or 'asset')}{ext}"
                rel = f"assets/import/{fname}"
                (dest_dir / fname).write_bytes(data)
                a = Artifact(team_id=_u(pc.ctx.team_id), project_id=_u(project_id),
                             run_id=None, asset_kind=akind, slug=_safe_stem(f.filename or "asset"),
                             rel_path=rel, provider="import", is_placeholder=False,
                             params_json={"source": "import"}, size=len(data))
                s.add(a)
                await s.flush()
                imported.append({"id": str(a.id), "slug": a.slug, "rel_path": rel,
                                 "asset_kind": akind})
            await s.commit()
    return {"imported": imported, "skipped": skipped}


# ---------------- 外置素材源拉取 ----------------
class FetchIn(BaseModel):
    source_id: str
    query: str = ""
    limit: int = 6


@router.post("/projects/{project_id}/assets/fetch")
async def fetch_from_source(project_id: str, body: FetchIn,
                            ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """从配置的外置素材源（开放平台）按关键词拉取音频/图片/图标素材入库。

    素材源复用 asset_providers 表（driver_key=external_http），config_json：
      base_url     必填，列表接口或目录根
      api_style    json（默认，GET base_url?q=&kind=&limit= → {"items":[{name,url}]} 或数组）
                   template（url_template 含 {query}/{n}，逐个下载 1..limit）
      url_template 模板模式必填
      auth_header / auth_value 可选请求头（如 Bearer Token）
    """
    async with _ProjectCheck(project_id, ctx) as pc:
        async with SessionLocal() as s:
            src = await s.get(AssetProvider, _u(body.source_id))
            if src is None or str(src.team_id) != ctx.team_id or not src.enabled:
                raise HTTPException(status_code=404, detail="素材源不存在或未启用")
            cfg = dict(src.config_json or {})
        base_url = str(cfg.get("base_url") or "").strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="素材源未配置 base_url")
        api_style = str(cfg.get("api_style") or "json")
        headers = {}
        if cfg.get("auth_header") and cfg.get("auth_value"):
            headers[str(cfg["auth_header"])] = str(cfg["auth_value"])
        limit = max(1, min(int(body.limit or 6), 12))

        try:
            if api_style == "template":
                tpl = str(cfg.get("url_template") or "").strip()
                if not tpl:
                    raise HTTPException(status_code=400, detail="模板模式需配置 url_template")
                from urllib.parse import quote
                items = [{"name": f"{src.label}-{n}",
                          "url": tpl.replace("{query}", quote(body.query)).replace("{n}", str(n))}
                         for n in range(1, limit + 1)]
            else:
                url = base_url.rstrip("/")
                if cfg.get("list_path"):
                    url += "/" + str(cfg["list_path"]).lstrip("/")
                async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                             headers=headers) as client:
                    resp = await client.get(url, params={"q": body.query, "kind": src.kind,
                                                         "limit": limit})
                    resp.raise_for_status()
                    data = resp.json()
                raw = data.get("items") if isinstance(data, dict) else data
                items = [it for it in (raw or []) if isinstance(it, dict) and it.get("url")][:limit]
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"素材源访问失败：{e}")

        if not items:
            return {"imported": [], "skipped": [], "message": "素材源未返回可用条目"}

        src_dir = re.sub(r"[^\w-]+", "_", src.label)[:30] or "source"
        dest_dir = pc.workspace / "assets" / "external" / src_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        imported, skipped = [], []
        ts = int(time.time())
        async with SessionLocal() as s:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                         headers=headers) as client:
                for i, it in enumerate(items):
                    url = str(it["url"])
                    name = str(it.get("name") or f"{src.label}-{i + 1}")
                    ext = Path(urlparse(url).path).suffix.lower()
                    akind = _classify(ext, str(src.kind))
                    if akind is None:
                        ext = ".png" if str(src.kind) in ("image", "icon") else ".mp3"
                        akind = "icon" if str(src.kind) == "icon" else (
                            "image" if str(src.kind) == "image" else "audio")
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = resp.content
                    except Exception as e:  # noqa: BLE001
                        skipped.append({"name": name, "reason": f"下载失败：{e}"})
                        continue
                    if len(data) > MAX_ASSET_BYTES:
                        skipped.append({"name": name, "reason": "超过 20MB"})
                        continue
                    fname = f"{ts}_{i}_{_safe_stem(name)}{ext}"
                    (dest_dir / fname).write_bytes(data)
                    a = Artifact(team_id=_u(ctx.team_id), project_id=_u(project_id),
                                 run_id=None, asset_kind=akind,
                                 slug=_safe_stem(name)[:40], rel_path=f"assets/external/{src_dir}/{fname}",
                                 provider=f"external:{src.label}"[:40], is_placeholder=False,
                                 params_json={"source": "external", "url": url, "query": body.query},
                                 size=len(data))
                    s.add(a)
                    await s.flush()
                    imported.append({"id": str(a.id), "slug": a.slug, "rel_path": a.rel_path,
                                     "asset_kind": akind})
            await s.commit()
    return {"imported": imported, "skipped": skipped}


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
