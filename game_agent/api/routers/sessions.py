"""会话 / 对话 / 发起 run（设计 §5 / §9）。

用户发一句话 → 建/复用会话 → 写 user 消息 → 建 run（queued）→ 入队。
analyze（只分析不改代码）与 modify（改动）都走此入口，run_type 区分。
"""
from __future__ import annotations

import io
import json
import re
import stat
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from game_agent.api.deps import _u, get_current_context, require
from game_agent.api.state import state
from game_agent.agents.roles import ROLE_LABELS
from game_agent.auth.rbac import PERM_RUN_CREATE, PERM_VIEW
from game_agent.config import CHAT_UPLOADS_ROOT, PROJECTS_ROOT
from game_agent.core.context import RequestContext
from game_agent.db.models import (Agent, Event, LlmCall, LlmProvider, LlmTranscript, Message,
                                  ModelProfile, Project, Run, RunQuestion, Session, Task)
from game_agent.db.session import SessionLocal
from game_agent.orchestration.caps import normalize_caps
from game_agent.orchestration import policy as policy_mod
from game_agent.llm.context_window import (BUDGET_RATIO, budget_for,
                                           merge_model_catalog_params, resolve_window)

router = APIRouter(prefix="/api", tags=["sessions/runs"])


def _profile_params(prof, providers_by_id: dict) -> dict | None:
    """档位 params，窗口/视觉以供应商模型目录按模型名的配置为准（目录优先）。

    与运行时 orchestrator._load_profile 同一口径（merge_model_catalog_params），
    否则统计页会与压缩用的窗口打架（显示 256k、实际按 128k 压缩之类）。
    """
    if prof is None:
        return None
    params = prof.params_json or {}
    pv = providers_by_id.get(str(prof.provider_id)) if prof.provider_id else None
    if pv is not None:
        params = merge_model_catalog_params(params, pv.models_json, prof.model)
    return params


# 动作分桶：顶栏的会话级统计和单条 run 轨迹摘要头是同一句话（「运行了 N 条命令，
# 读取了 N 个文件…」），两边必须用同一个口径，否则收起/展开一行轨迹数字就会变。
_READ_TOOLS = {"read_file", "grep", "glob", "list_dir"}
_WRITE_TOOLS = {"write_file", "edit_file", "delete_file"}


def _tool_bucket(name: str | None) -> str:
    if name == "run_command":
        return "commands"
    if name in _READ_TOOLS:
        return "reads"
    if name in _WRITE_TOOLS:
        return "writes"
    return "other"


def _plain_json(v):
    """json_extract 返回的是带引号的 JSON 文本（如 "\"read_file\""）。
    统一剥掉两端引号再参与分桶/展示（工具名、角色名里不会有引号）。"""
    if isinstance(v, str) and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def _empty_activity() -> dict:
    return {"commands": 0, "reads": 0, "writes": 0, "other": 0, "total": 0, "subagents": 0}


class FileIn(BaseModel):
    path: str
    name: str = ""
    size: int = 0


class ChatIn(BaseModel):
    content: str
    analyze: bool = False
    # 运行能力开关：{test, web, media}，默认全开（每个新会话都是开启测试的）
    caps: dict | None = None
    # 输入框右下角模型选择器：本轮指定模型档位 / 推理等级（空=跟随角色绑定与档位默认）
    model_profile_key: str | None = None
    reasoning_effort: str | None = None
    session_id: str | None = None
    images: list[str] = []  # 附图（data:image/...;base64 dataURL），需视觉模型才生效
    files: list[FileIn] = []  # 附件（先经 /uploads 落到工作区 .trace/uploads/，这里只传路径）
    # 会话挂载技能（渐进式披露）。None=不改/未指定（跟随项目默认）；[] 或名单=显式指定
    skills: list[str] | None = None
    # 会话权限作用域：workspace=仅工作区 / full=全部权限。None=不改（沿用会话已存值/设置默认）
    permission_scope: str | None = None


MAX_IMAGES = 4
MAX_IMAGE_CHARS = 8 * 1024 * 1024  # base64 约 6MB 原图
# 入库/发送的那一版：原图另存工作区文件，进上下文的一律重编码到这张以内。
# 为什么要压：附图会随 run 的**每一轮**请求重发，实测单张 4.72MB base64 直接把
# 网关顶到 413（整轮白跑，run 判失败）。视觉判断用不到更细的像素。
IMAGE_LONG_EDGE = 1280
MAX_IMAGE_STORED_CHARS = 480_100  # dataURL 字符上限（≈352KB 二进制）
MAX_SESSION_SKILLS = 12
MAX_FILES = 6
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_UPLOADS_DIR = ".trace/uploads"
_FILE_KINDS = {"xlsx": "excel", "xls": "excel", "csv": "excel", "docx": "word", "doc": "word",
               "pdf": "pdf", "txt": "text", "md": "text", "json": "text"}
_RESERVED_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", re.I)


def _shrink_image_data_url(url: str) -> str:
    """把附图 dataURL 重编码成「进上下文的那一版」：长边 ≤1280、JPEG、≤~350KB。

    原图不丢：调用方会把原始字节另存进工作区，模型要看细节可以 view_image 重开。
    PIL 不可用 / 解不出图时原样返回（外层还有 8MB 硬上限兜着）。
    """
    head, _, b64 = url.partition(",")
    if not head.startswith("data:image/"):
        return url
    try:
        import base64

        from PIL import Image
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001 - 解不开就按原样发，别让上传本身失败
        return url
    if img.mode != "RGB":
        # 透明像素摊白底：视觉模型看的是构图/配色，留 alpha 只会让 JPEG 编码失败
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        img = flat
    # 阶梯降质：正常截图/插画第一档就够；纯噪声图（JPEG 最坏情况）要退到 768 才收得下
    best = url
    for edge, q in ((IMAGE_LONG_EDGE, 82), (IMAGE_LONG_EDGE, 72),
                    (1024, 66), (900, 60), (768, 54)):
        im = img
        if max(im.size) > edge:
            im = im.copy()
            im.thumbnail((edge, edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True)
        cand = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        if len(best) > len(cand):
            best = cand
        if len(cand) <= MAX_IMAGE_STORED_CHARS:
            return cand
    return best


def _save_image_original(team_id: str | None, idx: int, url: str) -> str:
    """原图字节落盘到稳定目录，返回文本标记 ``chat-uploads/<team>/<file>``（失败返回空串）。

    见 game_agent/core/chat_images.py：附图与工作区生命周期解耦，重装/换机器只要带上
    DATA_DIR 就还在；另存原图也为了模型「省略像素」后还能 view_image 重开、灯箱能看高清。
    """
    from game_agent.core import chat_images
    return chat_images.save_data_url(team_id, idx, url)


def _validate_images(images: list[str], team_id: str | None = None,
                     ws: Path | None = None) -> tuple[list[str], list[str]]:
    """返回（进上下文的一版, 原图存放标记）。

    ``ws`` 仅为兼容旧调用点保留，不再用于落盘——原图统一存到 CHAT_UPLOADS_ROOT。
    """
    out: list[str] = []
    paths: list[str] = []
    for i, u in enumerate(images[:MAX_IMAGES]):
        if not isinstance(u, str) or not u.startswith("data:image/"):
            raise HTTPException(status_code=400, detail="图片必须是 data:image/ 开头的 dataURL")
        if len(u) > MAX_IMAGE_CHARS:
            raise HTTPException(status_code=400, detail="单张图片过大（>6MB），请压缩后再传")
        p = _save_image_original(team_id, i, u)
        if p:
            paths.append(p)
        out.append(_shrink_image_data_url(u))
    return out, paths


def _validate_skills(skills: list[str] | None) -> list[str] | None:
    if skills is None:
        return None
    from game_agent.agents.tools.native import _SKILL_NAME_RE
    out = []
    for n in skills[:MAX_SESSION_SKILLS]:
        n = str(n).strip()
        if not n:
            continue
        if not _SKILL_NAME_RE.match(n):
            raise HTTPException(status_code=400, detail=f"技能名非法：{n[:40]}")
        if n not in out:
            out.append(n)
    return out


def _safe_file_name(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join("_" if c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C")
                   else c for c in name).strip(". ")[:120].rstrip(". ")


def _validate_files(files: list[FileIn]) -> list[dict]:
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"附件不能超过 {MAX_FILES} 个")
    out = []
    for f in files:
        p = f.path.replace("\\", "/")
        if (not p.startswith(_UPLOADS_DIR + "/")
                or any(c in '<>:"|?*' or unicodedata.category(c).startswith("C") for c in p)
                or any(part in ("", ".", "..") or part != part.rstrip(". ")
                       or _RESERVED_NAME.fullmatch(part) for part in p.split("/"))):
            raise HTTPException(status_code=400, detail="附件路径非法")
        item = {"path": p, "name": _safe_file_name(f.name) or p.rsplit("/", 1)[-1],
                "size": max(0, f.size)}
        if item not in out:
            out.append(item)
    return out


async def _upload_workspace(s: AsyncSession, project_id: str, ctx: RequestContext) -> Path:
    if project_id not in ctx.project_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    project = await s.get(Project, _u(project_id))
    if project is None or str(project.team_id) != ctx.team_id:
        raise HTTPException(status_code=404, detail="项目不存在")
    return PROJECTS_ROOT / project.repo_path


async def _authorized_session(s: AsyncSession, session_id: str, ctx: RequestContext,
                              project_id: str | None = None) -> Session:
    try:
        sid = UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    sess = await s.get(Session, sid)
    if (sess is None or str(sess.team_id) != ctx.team_id
            or str(sess.project_id) not in ctx.project_ids or sess.status == "deleted"
            or (project_id is not None and str(sess.project_id) != project_id)):
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


def _inside(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="附件路径无法解析") from None
    if not resolved.is_relative_to(root) or resolved == root:
        raise HTTPException(status_code=400, detail="附件路径越界")
    return resolved


def _upload_directory(ws: Path) -> tuple[Path, Path]:
    ws = ws.resolve(strict=True)
    updir = _inside(ws / _UPLOADS_DIR, ws)
    # 不允许 .trace 或 uploads 重定向到其他目录（包括 junction）。
    if updir != ws / _UPLOADS_DIR:
        raise HTTPException(status_code=400, detail="附件目录不能重定向")
    return ws, updir


def _resolve_files(ws: Path, files: list[dict]) -> list[dict]:
    if not files:
        return []
    ws, updir = _upload_directory(ws)
    out = []
    for item in files:
        dest = _inside(ws / item["path"], updir)
        try:
            info = dest.stat()
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail="附件不存在") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HTTPException(status_code=400, detail="附件必须是独立的普通文件")
        if info.st_size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="附件过大（>30MB）")
        if not info.st_size:
            raise HTTPException(status_code=400, detail="空文件")
        canonical = {"path": dest.relative_to(ws).as_posix(), "name": dest.name,
                     "size": info.st_size, "original_name": _safe_file_name(item["name"])}
        if not any(f["path"] == canonical["path"] for f in out):
            out.append(canonical)
    return out


def _exclude_uploads(ws: Path) -> None:
    """附件目录不进用户的版本库：只往 `.git/info/exclude` 追加一条规则（本机生效、自身不被跟踪）。

    这不是 git 接管的一部分（运行时不看仓库状态、不建分支、不提交），
    只是平台文件的卫生：`.trace/uploads` 是平台自己写的，不该出现在用户 `git status` 里。
    工作区不是 git 仓库就什么都不写。
    """
    git = _inside(ws / ".git", ws)
    if git != ws / ".git":
        raise HTTPException(status_code=400, detail="Git 目录不能重定向")
    if not git.exists():
        return
    if git.is_file():
        try:
            pointer = git.read_text(encoding="utf-8").strip()
        except UnicodeError:
            raise HTTPException(status_code=400, detail="无效的 .git 文件") from None
        if not pointer.startswith("gitdir: ") or any(
                unicodedata.category(c).startswith("C") for c in pointer):
            raise HTTPException(status_code=400, detail="无效的 .git 文件")
        git = _inside(ws / pointer[8:], ws)
        # 不写外部或共享的 worktree 元数据，也不修改 Git 配置。
        if not git.is_dir() or (git / "commondir").exists():
            raise HTTPException(status_code=400, detail="无法安全设置 worktree 附件忽略规则")
    info = _inside(git / "info", git)
    exclude = _inside(info / "exclude", info)
    if info != git / "info" or exclude != info / "exclude":
        raise HTTPException(status_code=400, detail="Git exclude 不能重定向")
    if exclude.exists():
        entry = exclude.stat()
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise HTTPException(status_code=400, detail="不安全的 Git exclude 文件")
    info.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+b") as fh:
        fh.seek(0)
        content = fh.read()
        if not {b".trace/uploads/", b"/.trace/uploads/"}.intersection(content.splitlines()):
            fh.write((b"\n" if content and not content.endswith(b"\n") else b"")
                     + b".trace/uploads/\n")


def _save_upload(ws: Path, stream: BinaryIO, filename: str | None) -> dict:
    ws, updir = _upload_directory(ws)
    raw = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="附件过大（>30MB），请压缩后再传")
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    orig = _safe_file_name(filename or "")
    if not orig:
        raise HTTPException(status_code=400, detail="缺少文件名")
    stem = Path(orig).stem.encode("utf-8")[:150].decode("utf-8", errors="ignore")
    final_name = f"{uuid4().hex}-{stem}{Path(orig).suffix[:16]}"
    _exclude_uploads(ws)
    updir.mkdir(parents=True, exist_ok=True)
    _, updir = _upload_directory(ws)
    dest = _inside(updir / final_name, updir)
    with dest.open("xb") as fh:
        fh.write(raw)
    kind = _FILE_KINDS.get(dest.suffix.lower().lstrip("."), "file")
    return {"path": dest.relative_to(ws).as_posix(), "name": orig,
            "size": dest.stat().st_size, "kind": kind}


@router.post("/projects/{project_id}/uploads")
async def upload_project_file(project_id: str, file: UploadFile,
                              ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """附件仅落在授权项目的 .trace/uploads，并通过本地 exclude 忽略。"""
    try:
        async with SessionLocal() as s:
            ws = await _upload_workspace(s, project_id, ctx)
        return await run_in_threadpool(_save_upload, ws, file.file, file.filename)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="附件写入或 Git 忽略规则更新失败") from exc
    finally:
        await file.close()


@router.post("/projects/{project_id}/chat")
async def chat(project_id: str, body: ChatIn, request: Request,
               ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    async with SessionLocal() as s:
        ws = await _upload_workspace(s, project_id, ctx)
        sess = (await _authorized_session(s, body.session_id, ctx, project_id)
                if body.session_id is not None else None)
        if not body.content.strip() and not body.images and not body.files:
            raise HTTPException(status_code=400, detail="内容不能为空")
        # 重编码 + 落原图都在工作线程里做：一张 4.7MB PNG 解码缩放要几百毫秒，
        # 放在事件循环上会把所有会话的流式推送一起卡住
        images, image_paths = await run_in_threadpool(_validate_images, body.images, ctx.team_id, ws)
        try:
            files = await run_in_threadpool(_resolve_files, ws, _validate_files(body.files))
        except OSError as exc:
            raise HTTPException(status_code=400, detail="附件不可访问") from exc
        attached_skills = _validate_skills(body.skills)
        idem = request.headers.get("Idempotency-Key")
        if idem:
            old = await s.scalar(select(Run).where(
                Run.project_id == _u(project_id), Run.team_id == _u(ctx.team_id),
                Run.idempotency_key == idem))
            if old:
                return {"run_id": str(old.id), "session_id": str(old.session_id),
                        "status": old.status, "dup": True}
        # 会话权限作用域：未显式指定时，新会话取「设置」里的默认值
        req_scope = None
        if body.permission_scope is not None:
            req_scope = policy_mod.normalize_scope(body.permission_scope)
        if sess is None:
            init_scope = req_scope
            if init_scope is None:
                try:
                    init_scope = (await policy_mod.load_policy())["permission_scope"]
                except Exception:  # noqa: BLE001 - 设置读不到就用安全默认
                    init_scope = policy_mod.DEFAULT_SCOPE
            sess = Session(team_id=_u(ctx.team_id), project_id=_u(project_id),
                          title=body.content[:40] or ("（附件消息）" if files else "（图片消息）"),
                          created_by=_u(ctx.user_id),
                          skills_json=attached_skills,
                          permission_scope=init_scope)
            s.add(sess)
            await s.flush()
            await s.refresh(sess)
        if attached_skills is not None:
            sess.skills_json = attached_skills
        if req_scope is not None:
            sess.permission_scope = req_scope
        # 新消息即「最近活跃」：侧栏按这个时间排序/显示，后台一有新动作就能刷新出来
        sess.last_active = datetime.now(timezone.utc)
        meta = {}
        if images:
            meta["images"] = images
        if image_paths:
            # 原图在工作区的位置：像素被上下文省略后，模型还能按路径 view_image 重开
            meta["image_paths"] = image_paths
        if files:
            meta["files"] = files
        msg = Message(session_id=sess.id, run_id=None, author_type="user",
                      author_id=_u(ctx.user_id), content=body.content,
                      meta_json=meta)
        s.add(msg)
        await s.flush()
        await s.refresh(msg)
        run_opts: dict = {"caps": normalize_caps(body.caps),
                          "permission_scope": sess.permission_scope or policy_mod.DEFAULT_SCOPE}
        if body.model_profile_key:
            # 只接受本团队、kind=llm 的档位；乱传的 key 不让它静默回退到别的模型
            from game_agent.db.models import ModelProfile
            from sqlalchemy import select as _select
            prof = await s.scalar(_select(ModelProfile).where(
                ModelProfile.team_id == _u(ctx.team_id),
                ModelProfile.key == body.model_profile_key,
                ModelProfile.kind == "llm"))
            if prof is None:
                raise HTTPException(status_code=400, detail="所选模型档位不存在")
            run_opts["model_profile_key"] = body.model_profile_key
        if body.reasoning_effort:
            if body.reasoning_effort not in ("none", "minimal", "low", "medium", "high"):
                raise HTTPException(status_code=400, detail="推理等级不合法")
            run_opts["reasoning_effort"] = body.reasoning_effort
        run = Run(team_id=_u(ctx.team_id), project_id=_u(project_id), session_id=sess.id,
                  triggered_by=_u(ctx.user_id), trigger_msg_id=msg.id,
                  run_type="analyze" if body.analyze else "modify",
                  options_json=run_opts,
                  status="queued", idempotency_key=idem)
        s.add(run)
        await s.commit()
        await s.refresh(run)
        run_id = str(run.id)
        session_id = str(sess.id)
    # 入队
    await state.services.queue.enqueue(run_id, priority=0)
    return {"run_id": run_id, "session_id": session_id, "status": "queued", "dup": False}


@router.get("/projects/{project_id}/sessions")
async def list_sessions(project_id: str, archived: bool = Query(False),
                        ctx: RequestContext = Depends(require(PERM_VIEW))):
    """archived=False 列活跃会话，archived=True 列已归档会话；已删除的永不返回。

    ``running`` = 该会话是否有正在执行/排队的 run（前端据此显示转圈动画）。

    工作区（项目）被删除后，它下面的会话记录不再出现在会话面板里（含「未分组」）；
    项目恢复后会话原样回来。
    """
    status = "archived" if archived else "active"
    async with SessionLocal() as s:
        proj = await s.get(Project, _u(project_id))
        if proj is None or str(proj.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        if proj.archived_at is not None:
            return []
        rows = await s.execute(select(Session).where(
            Session.project_id == _u(project_id), Session.status == status
        ).order_by(Session.last_active.desc()))
        sessions = list(rows.scalars().all())
        busy_ids: set[str] = set()
        if sessions:
            busy = await s.execute(select(Run.session_id).where(
                Run.session_id.in_([x.id for x in sessions]),
                Run.status.in_(("queued", "claimed", "running"))).distinct())
            busy_ids = {str(r) for r in busy.scalars().all()}
        return [{"id": str(x.id), "title": x.title, "status": x.status,
                 "running": str(x.id) in busy_ids,
                 "skills": list(x.skills_json or []) if x.skills_json is not None else None,
                 "permission_scope": x.permission_scope or policy_mod.DEFAULT_SCOPE,
                 "created_at": str(x.created_at),
                 "last_active": str(x.last_active or x.created_at)} for x in sessions]


@router.get("/sessions/{session_id}/usage")
async def session_usage(session_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """会话级用量：总 token / 缓存命中率 / 生成速度 + 上下文窗口占用（是否该压缩了）。

    数据源 llm_calls（每次 LLM 往返即时落库，含 cached_tokens），按会话聚合该会话所有 run。
    上下文占用取每个角色「最近一次往返的 prompt_tokens」——那就是模型这次真正看到的
    上下文大小（压缩后），拿它比窗口即可判断离裁剪阈值还有多远。
    """
    async with SessionLocal() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or str(sess.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="会话不存在")
        row = (await s.execute(
            select(func.count(),
                   func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cached_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0),
                   func.coalesce(func.sum(LlmCall.cache_reported), 0))
            .select_from(LlmCall)
            .join(Run, Run.id == LlmCall.run_id)
            .where(Run.session_id == sess.id))).one()
        run_count = await s.scalar(
            select(func.count()).select_from(Run).where(Run.session_id == sess.id)) or 0
        # 会话累计「动作」：顶栏那条「运行了 N 条命令，读取了 N 个文件…」的会话级版本。
        # 分桶口径与前端 RunTraceInline.actionSummary 一致，两边数出来必须对得上。
        # 直接聚合 events 的 tool_call（实测 4.7k 事件的会话 11ms），所以并进 /usage
        # 一次带回，不另开接口、也不建缓存表。
        # 注意：SQLite 没有 json_unquote，会报 no such function。
        # 所以「取原始 JSON 文本再在 Python 里剥引号」（_plain_json），语义一致。
        tool_rows = (await s.execute(
            select(func.json_extract(Event.payload_json, "$.tool"),
                   func.count())
            .select_from(Event)
            .join(Run, Run.id == Event.run_id)
            .where(Run.session_id == sess.id, Event.type == "tool_call")
            .group_by(func.json_extract(Event.payload_json, "$.tool")))).all()
        run_span = (await s.execute(
            select(Run.created_at, Run.finished_at, Run.status)
            .where(Run.session_id == sess.id))).all()
        # 逐角色取最近若干次往返的上下文规模。llm_calls 行很小（没有正文列），
        # 排序安全；messages 那种带 base64 的巨型行才有 filesort 风险。
        # 排除摘要调用（purpose='summarize'）：压缩历史时额外发的那次 prompt 只是
        # 「被压的那一段历史」（几 k），不是模型真正看到的上下文。以前用 llm_transcripts
        # 关联来排除，但那张表只保留 14 天，过期后顶栏上下文又会被摘要调用带成 3%。
        # 现在直接按 llm_calls.purpose 过滤，持久可靠。
        recent = (await s.execute(
            select(LlmCall.agent_key, LlmCall.model, LlmCall.prompt_tokens, LlmCall.call_id,
                   LlmCall.completion_tokens, LlmCall.latency_ms, LlmCall.created_at,
                   LlmCall.run_id, LlmCall.ttft_ms,
                   # 末尾追加（不要插在中间）：下面的解包与下标都按位置读，插中间会连锁错位
                   LlmCall.cached_tokens, LlmCall.cache_reported)
            .select_from(LlmCall)
            .join(Run, Run.id == LlmCall.run_id)
            .where(Run.session_id == sess.id, LlmCall.purpose != "summarize")
            .order_by(LlmCall.created_at.desc(), LlmCall.id.desc())
            .limit(500))).all()
        # 每个角色「最近一次调用」所在 run 的档位覆盖（输入框选过模型 / 运行中切换过）：
        # 实际压缩用的窗口来自那个档位，顶栏的占用百分比也必须按同一个窗口算，
        # 否则会出现「顶栏 1%、却已经压缩」——显示用绑定档位、压缩用运行档位，两处打架。
        newest_run: dict[str, str] = {}
        for r in recent:
            if r[7]:
                newest_run.setdefault(r[0] or "", str(r[7]))
        override_keys: dict[str, str] = {}
        if newest_run:
            opt_rows = (await s.execute(
                select(Run.id, Run.options_json).where(
                    Run.id.in_([_u(x) for x in set(newest_run.values())])))).all()
            opts_by_run = {str(rid): (opts or {}) for rid, opts in opt_rows}
            for agent_key, rid in newest_run.items():
                pk = str((opts_by_run.get(rid) or {}).get("model_profile_key") or "").strip()
                if pk:
                    override_keys[agent_key] = pk
        # 角色 → 绑定的档位（含窗口参数）
        agents_cfg = (await s.execute(
            select(Agent).where(Agent.team_id == sess.team_id))).scalars().all()
        pkeys = {a.model_profile_key for a in agents_cfg if a.model_profile_key}
        # 运行档位（输入框选过模型）也要一起取：窗口按实际压缩用的档位算
        pkeys |= set(override_keys.values())
        profiles = {}
        if pkeys:
            profiles = {p.key: p for p in (await s.execute(
                select(ModelProfile).where(ModelProfile.team_id == sess.team_id,
                                           ModelProfile.key.in_(pkeys)))).scalars().all()}
        # 供应商模型目录（窗口/视觉在这里按模型名配）：档位 params 缺省时从这里补齐，
        # 保证显示层读到的窗口与运行时（orchestrator._load_profile）完全一致。
        providers_by_id: dict[str, LlmProvider] = {}
        if pkeys:
            pids = {str(p.provider_id) for p in profiles.values() if p.provider_id}
            if pids:
                providers_by_id = {str(pv.id): pv for pv in (await s.execute(
                    select(LlmProvider).where(LlmProvider.id.in_(
                        [_u(x) for x in pids])))).scalars().all()}
    calls, prompt, completion, cached, latency, reported = (int(x or 0) for x in row)
    total = prompt + completion
    # 会话累计平均速度：全历史（含重试）——换模型后几乎不动，只作工具提示里的对照。
    avg_speed = round(completion / (latency / 1000), 1) if latency > 0 and completion > 0 else None
    # 瞬时速度：最近一次正文往返的「输出 token / 该轮耗时」。用户 2026-11-12 实测：
    # 切到更快的模型后顶栏还显示 21.7 tok/s（会话平均值被历史拖住），以为切换没生效。
    # 顶栏「速度」显示这个瞬时值；累计平均留在 tooltip 里备份对照。
    last_call = recent[0] if recent else None
    instant_speed = None
    if last_call is not None:
        _ct, _ms = int(last_call[4] or 0), int(last_call[5] or 0)
        if _ct > 0 and _ms > 0:
            instant_speed = round(_ct / (_ms / 1000), 1)
    # 首 token 耗时（TTFT）：最近一轮实测值 + 会话内所有测到过的一轮的中位数。
    # 用中位数而不是平均：偶发的一次排队/限流重试会把平均拉爆，中位数才是「常态等多久」。
    _ttfts = sorted(int(x[8] or 0) for x in recent if int(x[8] or 0) > 0)
    ttft_last = int(last_call[8] or 0) if last_call is not None else 0
    ttft_median = (_ttfts[len(_ttfts) // 2] if _ttfts else 0)

    activity = _empty_activity()
    for name, n in tool_rows:
        inc = int(n or 0)
        activity[_tool_bucket(_plain_json(name))] += inc
        activity["total"] += inc
    # 任务墙钟合计：还在跑的按「现在」算。并行跑的多个任务各算一份，
    # 所以这条读数叫「累计执行时长」，不等于用户在屏幕前等了的总时长。
    now_utc = datetime.now(timezone.utc)
    run_secs = 0
    for created, finished, _st in run_span:
        if not created:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        end = finished or now_utc
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        run_secs += max(0, int((end - created).total_seconds()))
    activity["run_seconds"] = run_secs

    # 按角色聚合：prompt_tokens 序列保持「新→旧」（recent 是倒序）
    stats: dict[str, dict] = {}
    for agent_key, model, pt, _cid, _ct, _ms, _created, _rid, _ttft, _cached, _rep in recent:
        key = agent_key or ""
        st = stats.setdefault(key, {"prompts": [], "model": model, "rows": []})
        st["prompts"].append(int(pt or 0))
        st.setdefault("model", model)
        # 逐行留一份 (输入, 缓存, 输出, 是否上报)：下面按窗口过滤掉「端点把累计量当单次
        # usage 上报」的异常行后再求和，否则按角色的合计会比会话总量还大。
        st["rows"].append((int(pt or 0), int(_cached or 0), int(_ct or 0), bool(_rep)))

    cfg_by_key = {a.key: a for a in agents_cfg}
    by_agent = []
    for key, st in stats.items():
        cfg = cfg_by_key.get(key)
        # 优先用「这个角色最近一次调用所用的档位」：压缩按它算窗口，这里也必须按它算，
        # 否则用户看到顶栏 1%、实际却已越过预算被压缩（两处窗口不同）。
        used_key = override_keys.get(key) or (cfg.model_profile_key if cfg else None)
        prof = profiles.get(used_key) if used_key else None
        window, configured = resolve_window(_profile_params(prof, providers_by_id))
        budget = budget_for(window)
        # 中转站偶尔把整轮的用量累计塞进最后一条响应的 usage（实测出现过 prompt_tokens
        # ≈ 窗口 10 倍的单行）。这种行不能当「当前上下文」，否则占用条直接爆表。
        plausible = [p for p in st["prompts"] if 0 < p <= window * 1.5]
        skewed = len([p for p in st["prompts"] if p > window * 1.5])
        last = plausible[0] if plausible else 0
        peak = max(plausible, default=0)
        # 合计只算「可信行」（输入量在窗口 1.5 倍以内）：端点把整轮累计量塞进单条 usage 的
        # 那种行，加进合计会让按角色表比会话总量还大，用户一眼就看出数不对。
        good = [r for r in st["rows"] if 0 < r[0] <= window * 1.5]
        n_cached = sum(r[1] for r in good)
        n_prompt = sum(r[0] for r in good)
        by_agent.append({
            "agent_key": key or "unknown",
            "agent_label": ROLE_LABELS.get(key, key or "未知角色"),
            "model": st["model"] or (prof.model if prof else None),
            "profile_key": used_key,
            "profile_label": prof.label if prof else None,
            "rounds": len(st["prompts"]),
            # 该角色的 token/缓存合计（只统计 recent 取的最近若干轮，不是全历史）
            "prompt_tokens": n_prompt,
            "completion_tokens": sum(r[2] for r in good),
            "cached_tokens": min(n_cached, n_prompt),
            "cache_reported_rounds": sum(1 for r in good if r[3]),
            "context_window": window,
            # False = 档位没填 context_window，按 DEFAULT_CONTEXT_WINDOW 估算（猜的）
            "window_configured": configured,
            "budget_tokens": budget,
            "last_prompt_tokens": last,
            "peak_prompt_tokens": peak,
            "used_pct": round(last / window * 100, 1),
            "peak_pct": round(peak / window * 100, 1),
            "remaining_tokens": max(0, budget - last),
            # 越过预算线后 compress_messages 会把较早的历史合并成摘要
            "will_compress": last >= budget,
            "near_threshold": budget * 0.85 <= last < budget,
            # 端点上报异常（累计值）的轮数；非 0 说明这个比例的参考意义下降
            "skewed_usage_calls": skewed,
        })
    # 头条取占用比例最高的角色——它才是「这个会话离压缩还有多远」的答案
    by_agent.sort(key=lambda x: -x["used_pct"])
    context = by_agent[0] if by_agent else None

    return {
        "session_id": str(sess.id),
        "runs": int(run_count),
        "calls": calls,
        "prompt_tokens": prompt,          # 输入 token（含命中缓存部分）
        "completion_tokens": completion,  # 输出 token
        "cached_tokens": cached,          # 命中前缀缓存的输入 token
        # 未命中缓存的输入 = 输入 - 命中。写进弹层是为了让「总量 = 未缓存输入 + 缓存读取 + 输出」
        # 这个加法关系在界面上能自己核对，而不是只给一个百分比让人猜分母。
        "uncached_tokens": max(0, prompt - cached),
        "total_tokens": total,            # 总消耗 token = 输入 + 输出
        "latency_ms": latency,
        # tokens_per_sec = 瞬时速度（最近一轮往返）；换模型后下一个思考轮次就会变。
        # tokens_per_sec_avg = 会话累计平均（全历史、含重试），只作对照，别再拿来当主显示。
        "tokens_per_sec": instant_speed if instant_speed is not None else avg_speed,
        "tokens_per_sec_avg": avg_speed,
        # ---- 首 token 耗时（TTFT）----
        # ttft_ms = 最近一轮实测（0 = 该轮没测到，如非流式端点）；ttft_median_ms = 会话内中位数。
        # 前端顶栏显示「首字」那一格，悬浮里同时给「总往返」做对照。
        "ttft_ms": ttft_last,
        "ttft_median_ms": ttft_median,
        "ttft_calls": len(_ttfts),
        "last_round": ({
            "agent_key": last_call[0] or "unknown",
            "agent_label": ROLE_LABELS.get(last_call[0] or "", last_call[0] or "未知角色"),
            "model": last_call[1],
            "completion_tokens": int(last_call[4] or 0),
            "latency_ms": int(last_call[5] or 0),
            "ttft_ms": int(last_call[8] or 0),
            # 最近一轮的输入/缓存明细：弹层里「这一轮为什么这么慢/这么贵」看的就是这几格
            "prompt_tokens": int(last_call[2] or 0),
            "cached_tokens": int(last_call[9] or 0),
            "cache_reported": int(last_call[10] or 0) > 0,
            "created_at": str(last_call[6]),
        } if last_call is not None else None),
        # 端点没上报缓存字段时返回 None（前端显示"未知"），避免把"没上报"假报成 0% 命中
        # 上限压到 100%：端点把累计量当单次用量上报时，cached 偶尔会大于汇总后的 prompt
        "cache_hit_rate": round(min(cached, prompt) / prompt * 100, 1)
        if (prompt > 0 and reported > 0) else None,
        "cache_reported": reported > 0,
        # ---- 会话累计动作（命令/读取/修改/其它 + 任务墙钟合计）----
        "activity": activity,
        # ---- 上下文窗口占用 ----
        "context": context,
        "context_by_agent": by_agent,
        "compress_at_pct": round(BUDGET_RATIO * 100, 1),
    }


@router.get("/sessions/{session_id}/context-stats")
async def session_context_stats(
    session_id: str,
    run_id: str | None = Query(None),
    ctx: RequestContext = Depends(require(PERM_VIEW)),
):
    """会话级上下文统计：最近一次请求由什么构成、逐轮占用趋势、模型用了哪些工具。

    数据源是 llm_transcripts.stats_json——每轮往返落库时就按「系统提示词 / 工具定义 /
    历史 / 工具结果」算好了占比，这里只做跨轮汇总，不再碰 LONGTEXT 大字段。
    est_* 是自己按字符估的（判断谁吃掉了上下文），prompt_tokens 是端点上报的真实值
    （判断端点到底收到多大），两者并列返回，偏差本身就是信号。

    run_id 非空时只统计这条 run（运行面板用）；留空则是整个会话的趋势。
    """
    async with SessionLocal() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or str(sess.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="会话不存在")
        cond = [Run.session_id == sess.id, LlmTranscript.purpose == "agent"]
        if run_id:
            cond.append(LlmTranscript.run_id == _u(run_id))
        rows = (await s.execute(
            select(LlmTranscript.id, LlmTranscript.call_id, LlmTranscript.agent_key,
                   LlmTranscript.model, LlmTranscript.round_no, LlmTranscript.compressed,
                   LlmTranscript.dropped_msgs, LlmTranscript.req_bytes,
                   LlmTranscript.stats_json, LlmTranscript.created_at,
                   LlmTranscript.run_id)
            .select_from(LlmTranscript)
            .join(Run, Run.id == LlmTranscript.run_id)
            .where(*cond)
            .order_by(LlmTranscript.created_at.desc(), LlmTranscript.id.desc())
            .limit(1000))).all()
        # 每轮所在 run 的档位覆盖（输入框选模型 / 运行中切换）：那一轮实际发送用的窗口
        # 来自它，占用百分比必须按同一窗口算，否则「顶栏 21%，却在按 128k 预算压缩」。
        run_opts: dict[str, dict] = {}
        run_ids = {str(r[10]) for r in rows if r[10]}
        if run_ids:
            for rid, opts in (await s.execute(
                select(Run.id, Run.options_json).where(
                    Run.id.in_([_u(x) for x in run_ids])))).all():
                run_opts[str(rid)] = opts or {}
        override_keys = {str((o or {}).get("model_profile_key") or "").strip()
                         for o in run_opts.values()}
        override_keys.discard("")
        call_ids = [r[1] for r in rows if r[1]]
        real_by_call: dict[str, int] = {}
        if call_ids:
            got = (await s.execute(select(LlmCall.call_id, LlmCall.prompt_tokens)
                                   .where(LlmCall.call_id.in_(call_ids)))).all()
            real_by_call = {cid: int(pt or 0) for cid, pt in got}
        agents_cfg = (await s.execute(
            select(Agent).where(Agent.team_id == sess.team_id))).scalars().all()
        pkeys = {a.model_profile_key for a in agents_cfg if a.model_profile_key}
        pkeys |= override_keys
        profiles = {}
        if pkeys:
            profiles = {p.key: p for p in (await s.execute(
                select(ModelProfile).where(ModelProfile.team_id == sess.team_id,
                                           ModelProfile.key.in_(pkeys)))).scalars().all()}
        # 与 /usage 同源：供应商模型目录按模型名补齐档位缺省的窗口/视觉。
        providers_by_id: dict[str, LlmProvider] = {}
        if pkeys:
            pids = {str(p.provider_id) for p in profiles.values() if p.provider_id}
            if pids:
                providers_by_id = {str(pv.id): pv for pv in (await s.execute(
                    select(LlmProvider).where(LlmProvider.id.in_(
                        [_u(x) for x in pids])))).scalars().all()}

    # 摘要调用是「隐形开销」：它不写 llm_calls（runtime 只给自己的往返记账），
    # 但确实烧了端点的 token。这里单独统计，否则用户会看到总量对不上。
    async with SessionLocal() as s:
        cond2 = [Run.session_id == sess.id, LlmTranscript.purpose == "summarize"]
        if run_id:
            cond2.append(LlmTranscript.run_id == _u(run_id))
        summ = (await s.execute(
            select(LlmTranscript.stats_json)
            .select_from(LlmTranscript)
            .join(Run, Run.id == LlmTranscript.run_id)
            .where(*cond2))).all()
    summarize_est = sum(int((r[0] or {}).get("est_prompt_tokens") or 0) for r in summ)

    cfg_by_key = {a.key: a for a in agents_cfg}

    def occupancy(agent_key: str | None, used: int, run_id: str | None = None) -> dict:
        """把「这一轮发出去多少 token」换成「窗口占用百分之几、该不该压缩」。

        窗口按「这一轮所在 run 实际用的档位」取（run.options_json.model_profile_key
        覆盖角色绑定），与运行时压缩用的是同一个值。
        """
        cfg = cfg_by_key.get(agent_key or "")
        used_key = str((run_opts.get(str(run_id or "")) or {}).get("model_profile_key")
                       or "").strip() or (cfg.model_profile_key if cfg else None)
        prof = profiles.get(used_key) if used_key else None
        window, configured = resolve_window(_profile_params(prof, providers_by_id))
        budget = budget_for(window)
        return {
            "profile_key": used_key,
            "context_window": window,
            "window_configured": configured,
            "budget_tokens": budget,
            "used_pct": round(used / window * 100, 1),
            "will_compress": used >= budget,
        }

    rows = list(reversed(rows))  # 趋势按时间正序，前端从左往右画
    cat_total: dict[str, dict] = {}
    tool_total: dict[str, int] = {}
    latest_by_agent: dict[str, dict] = {}
    trend: list[dict] = []
    compress_rounds = 0
    for (tid, cid, agent_key, model, round_no, compressed, dropped,
         req_b, stats, created, rid) in rows:
        st = stats or {}
        for name, v in (st.get("categories") or {}).items():
            acc = cat_total.setdefault(name, {"tokens": 0, "rounds": 0})
            acc["tokens"] += int((v or {}).get("tokens") or 0)
            acc["rounds"] += 1
        for name, n in (st.get("tool_calls") or {}).items():
            tool_total[name] = tool_total.get(name, 0) + int(n or 0)
        est = int(st.get("est_prompt_tokens") or 0)
        real = real_by_call.get(cid or "", 0)
        used = real or est
        if compressed:
            compress_rounds += 1
        trend.append({
            "transcript_id": str(tid), "agent_key": agent_key or "unknown",
            "round_no": round_no, "created_at": str(created),
            "est_tokens": est, "prompt_tokens": used, "est_only": real == 0,
            "compressed": bool(compressed), "dropped_msgs": int(dropped or 0),
            "req_kb": round(int(req_b or 0) / 1024),
        })
        # 正序遍历，后写覆盖前写 → 每个角色留下的就是最近一轮
        latest_by_agent[agent_key or "unknown"] = {
            "transcript_id": str(tid), "agent_key": agent_key or "unknown",
            "agent_label": ROLE_LABELS.get(agent_key or "", agent_key or "未知角色"),
            "model": model, "round_no": round_no, "created_at": str(created),
            "categories": st.get("categories") or {},
            "est_prompt_tokens": est, "prompt_tokens": used,
            "messages": int(st.get("messages") or 0),
            "tools": int(st.get("tools") or 0),
            "images": int(st.get("images") or 0),
            "tool_calls": st.get("tool_calls") or {},
            "compressed": bool(compressed), "dropped_msgs": int(dropped or 0),
            **occupancy(agent_key, used, str(rid) if rid else None),
        }

    by_agent = sorted(latest_by_agent.values(), key=lambda x: -x["used_pct"])
    latest = max(by_agent, key=lambda x: x["created_at"]) if by_agent else None
    return {
        "session_id": str(sess.id),
        "rounds": len(rows),
        # 只统计最近 1000 轮；命中说明更早的往返没进趋势
        "history_capped": len(rows) >= 1000,
        # 跨轮累加的估算 token（同一份历史每轮都重发，所以这个数远大于窗口，
        # 看的是「钱花在哪儿」，不是「当前占了多少」）
        "categories_cumulative": dict(sorted(cat_total.items(),
                                             key=lambda kv: -kv[1]["tokens"])),
        # 最近一次请求的构成：这才是「当前上下文被谁吃掉」的答案
        "categories_current": (latest or {}).get("categories") or {},
        "tool_calls": dict(sorted(tool_total.items(), key=lambda kv: -kv[1])[:30]),
        "trend": trend[-200:],
        "latest": latest,
        "by_agent": by_agent,
        "compress_rounds": compress_rounds,
        # 摘要调用（压缩历史时额外发的那次）不在 llm_calls 里记账，单列出来才对得上总账
        "summarize_rounds": len(summ),
        "summarize_est_tokens": summarize_est,
        "compress_at_pct": round(BUDGET_RATIO * 100, 1),
    }


class SessionStatusIn(BaseModel):
    status: str | None = None  # active|archived（可选，不再强制）
    permission_scope: str | None = None  # workspace|full（可选）


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionStatusIn,
                         ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    if body.status is not None and body.status not in ("active", "archived"):
        raise HTTPException(status_code=400, detail="status 只能为 active 或 archived")
    if body.permission_scope is not None and \
            policy_mod.normalize_scope(body.permission_scope, "") != body.permission_scope:
        raise HTTPException(status_code=400, detail="permission_scope 只能为 workspace 或 full")
    if body.status is None and body.permission_scope is None:
        raise HTTPException(status_code=400, detail="没有要修改的字段")
    async with SessionLocal() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or str(sess.team_id) != ctx.team_id or sess.status == "deleted":
            raise HTTPException(status_code=404, detail="会话不存在")
        if body.status is not None:
            sess.status = body.status
        if body.permission_scope is not None:
            sess.permission_scope = policy_mod.normalize_scope(body.permission_scope)
        await s.commit()
        return {"id": str(sess.id), "status": sess.status,
                "permission_scope": sess.permission_scope or policy_mod.DEFAULT_SCOPE}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """软删：保留消息与 run 关联，列表不再展示。"""
    async with SessionLocal() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or str(sess.team_id) != ctx.team_id or sess.status == "deleted":
            raise HTTPException(status_code=404, detail="会话不存在")
        sess.status = "deleted"
        await s.commit()
    return {"ok": True}


@router.get("/sessions/{session_id}/runs")
async def session_runs(session_id: str, limit: int = Query(30, ge=1, le=200),
                       ctx: RequestContext = Depends(require(PERM_VIEW))):
    """该会话的 run 列表（新→旧）。

    为什么不复用 `/projects/{pid}/runs`：那个接口只给项目最近 50 条 run，
    老会话的 run 早被挤出窗口 → 前端认不出「这个会话上一次跑成什么样」，
    于是既不显示运行卡片，也不显示失败原因，看起来就是「对话内容空白」。
    这里按会话查，永远查得到。
    """
    async with SessionLocal() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or str(sess.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="会话不存在")
        rows = (await s.execute(
            select(Run).where(Run.session_id == sess.id)
            .order_by(Run.created_at.desc()).limit(limit))).scalars().all()
        # 每条 run 的动作分桶 + 参与角色：让聊天流里「已结束的轨迹」收起时只有一行摘要，
        # 也能直接报出「运行了 N 条命令，读取了 N 个文件」——否则收起态要么数字空白，
        # 要么每条历史 run 都必须拉全量事件（实测一条 run 1448 个事件 / 1.5MB，
        # 一个 17 条 run 的会话切进来就是几十 MB 解析 + 上万 DOM 节点，界面直接卡死）。
        # 口径与 actionSummary 一致；整个会话一次聚合实测 ~30ms（见 /usage 同类查询）。
        ids = [r.id for r in rows]
        act: dict[str, dict] = {}
        who: dict[str, list[str]] = {}
        if ids:
            # 注意：不能用 MySQL 专属的 json_unquote（SQLite 直接 500）。
            # 取 json_extract 原始值，统一在 Python 侧剥掉 JSON 字符串的引号。
            tool = func.json_extract(Event.payload_json, "$.tool")
            agent = func.coalesce(
                func.json_extract(Event.payload_json, "$.agent"),
                func.json_extract(Event.payload_json, "$.agent_key"))
            for rid, name, n in (await s.execute(
                select(Event.run_id, tool, func.count()).select_from(Event)
                .where(Event.run_id.in_(ids), Event.type == "tool_call")
                .group_by(Event.run_id, tool))).all():
                d = act.setdefault(str(rid), _empty_activity())
                inc = int(n or 0)
                d[_tool_bucket(_plain_json(name))] += inc
                d["total"] += inc
            # 只统计真正执行过的（工具/思考）角色；用户插话不算「这轮是谁跑的」
            for rid, a, _n in (await s.execute(
                select(Event.run_id, agent, func.count()).select_from(Event)
                .where(Event.run_id.in_(ids), Event.type.in_(("tool_call", "agent_thinking")))
                .group_by(Event.run_id, agent)
                .order_by(Event.run_id, func.count().desc()))).all():
                a = _plain_json(a)
                if not a or a == "user":
                    continue
                who.setdefault(str(rid), []).append(str(a))
            # 派出的只读子agent 数（Task.kind='sub' 的任务数）：收起态的摘要头加一句
            # 「派了 N 个子agent 并行核对」——这是这轮有没有用并行调研的事实，不是猜的。
            for rid, n in (await s.execute(
                select(Task.run_id, func.count()).select_from(Task)
                .where(Task.run_id.in_(ids), Task.kind == "sub")
                .group_by(Task.run_id))).all():
                act.setdefault(str(rid), _empty_activity())["subagents"] = int(n or 0)
        return [{
            "id": str(r.id), "session_id": str(r.session_id) if r.session_id else None,
            "status": r.status, "run_type": r.run_type,
            "final_summary": r.final_summary or "",
            "error": r.error,
            "created_at": str(r.created_at),
            "finished_at": str(r.finished_at) if r.finished_at else None,
            "activity": act.get(str(r.id)) or _empty_activity(),
            "agents": who.get(str(r.id), []),
        } for r in rows]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str,
                        limit: int = Query(0),
                        before: str | None = Query(None),
                        before_id: str | None = Query(None),
                        ctx: RequestContext = Depends(require(PERM_VIEW))):
    """会话消息。附图的 base64 不再内联返回（单个老会话能到 1MB，切换/轮询都卡），
    改回 ``meta.image_urls``（按需走 /projects/{id}/file?mode=raw 加载）；
    ``meta.images`` 保留兼容旧前端，但这里置空以缩小响应体。

    分页：默认（limit=0）返回**全部**消息，保持老行为不变。带 ``limit`` 时只返回
    最近 limit 条（``before``/``before_id``=上页最老那条的 (created_at, id)，用于「加载更早」），
    ``has_more`` 表明还有更早的。这样大会话首屏只拿最近一屏，不必一次吐几百条。
    用 (created_at, id) 复合游标：同一秒内可能有多条消息（实测有并列），只用时间做
    ```` < before ``` 会在页边界把并列的那条静默漏掉。"""
    async with SessionLocal() as s:
        sess = await _authorized_session(s, session_id, ctx)
        sel = select(Message).where(Message.session_id == sess.id)
        if before:
            # 归一成「naive UTC」再比较：库里 DATETIME 不存时区，若把带 +00:00 的字符串
            # 直接丢给 created_at < '...'，字符串→datetime 的隐式转换不可靠，可能退化成
            # 不受限比较（第二页=第一页、has_more 永远为真）。这里解析成 UTC 后去掉 tzinfo。
            dt = None
            try:
                dt = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                dt = None
            if dt is not None:
                if before_id:
                    # 复合游标：严格早于 (dt, before_id)，并列同一时刻的后续消息仍能被取到
                    sel = sel.where(or_(
                        Message.created_at < dt,
                        and_(Message.created_at == dt, Message.id < _msg_uuid(before_id)),
                    ))
                else:
                    sel = sel.where(Message.created_at < dt)
            else:
                sel = sel.where(Message.created_at < before)
        n = max(0, int(limit or 0))
        if n:
            # 取「最近 n 条」：按时间倒序取，再翻回正序
            q = sel.order_by(Message.created_at.desc(), Message.id.desc()).limit(n)
            rows = list((await s.execute(q)).scalars().all())
            has_more = len(rows) == n
            if has_more:
                # 精确判断是否真的还有更早的（避免正好 n 条时误判；同样用复合边界）
                oldest = rows[-1]
                cnt = await s.execute(
                    select(func.count()).select_from(Message).where(
                        Message.session_id == sess.id,
                        or_(Message.created_at < oldest.created_at,
                            and_(Message.created_at == oldest.created_at,
                                 Message.id < oldest.id))))
                has_more = (cnt.scalar() or 0) > 0
            rows.reverse()
        else:
            q = sel.order_by(Message.created_at)
            rows = list((await s.execute(q)).scalars().all())
            has_more = False
        out = []
        for m in rows:
            meta = dict(m.meta_json or {})
            imgs = meta.get("images") if isinstance(meta.get("images"), list) else []
            paths = meta.get("image_paths") if isinstance(meta.get("image_paths"), list) else []
            if imgs:
                # 有原始文件路径就只给 URL；没有路径的（老数据）才退回内联，避免图片丢失。
                if paths:
                    from game_agent.core import chat_images
                    meta["image_urls"] = [chat_images.serve_url(p, str(sess.project_id))
                                          for p in paths]
                    meta["images"] = []
                else:
                    meta["image_urls"] = list(imgs)
            out.append({
                "id": str(m.id), "author_type": m.author_type, "agent_key": m.agent_key,
                "author_id": str(m.author_id) if m.author_id else None,
                "run_id": str(m.run_id) if m.run_id else None,
                "content": m.content, "meta": meta,
                "created_at": str(m.created_at),
            })
        if n:
            return {"messages": out, "has_more": has_more,
                    "oldest": out[0]["created_at"] if out else None,
                    "oldest_id": out[0]["id"] if out else None}
        return out


# 流式分片与逐条进度事件只服务「打字机/进度条」体验，落进导出文件就是海量噪音
# （一条 run 几千个 delta，正文又和最终消息重复），其余事件类型全量保留
_EXPORT_SKIP_EVENTS = {"message_delta", "agent_thinking_delta", "agent_message", "art_progress"}


def _fmt_dt(v) -> str:  # type: ignore[no-untyped-def]
    return str(v).replace("+00:00", " UTC") if v else "—"


def _author_label(m: Message) -> str:
    if m.author_type == "user":
        return "用户"
    if m.author_type == "agent":
        return ROLE_LABELS.get(m.agent_key or "", m.agent_key or "agent")
    return "系统"


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """打包下载会话记录（zip）：conversation.md 给人读，session.json 留全量数据。

    messages / runs / events / 待答问题全在 json 里；用户附图的 base64 也保留
    （记录完整性优先，压缩后体积可控）。不导出 llm_transcripts 正文——那是
    排查上下文问题的巨型原始报文，走运行面板单条看即可。
    """
    async with SessionLocal() as s:
        sess = await s.get(Session, _u(session_id))
        if sess is None or str(sess.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="会话不存在")
        proj = await s.get(Project, sess.project_id)
        msgs = (await s.execute(select(Message).where(
            Message.session_id == sess.id).order_by(Message.created_at))).scalars().all()
        runs = (await s.execute(select(Run).where(
            Run.session_id == sess.id).order_by(Run.created_at))).scalars().all()
        events: list[Event] = []
        questions: list[RunQuestion] = []
        if runs:
            events = list((await s.execute(select(Event).where(
                Event.run_id.in_([r.id for r in runs]),
                Event.type.notin_(_EXPORT_SKIP_EVENTS)).order_by(Event.seq))).scalars().all())
            questions = list((await s.execute(select(RunQuestion).where(
                RunQuestion.session_id == sess.id).order_by(RunQuestion.created_at)))
                .scalars().all())

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    run_by_id = {r.id: r for r in runs}
    events_by_run: dict = {}
    for e in events:
        events_by_run.setdefault(e.run_id, []).append(e)
    questions_by_run: dict = {}
    for q in questions:
        questions_by_run.setdefault(q.run_id, []).append(q)

    # ---------- conversation.md ----------
    lines: list[str] = [
        f"# 会话记录：{sess.title or '（无标题）'}",
        "",
        f"- 项目：{proj.name if proj else '未知'}",
        f"- 创建：{_fmt_dt(sess.created_at)} · 最近活跃：{_fmt_dt(sess.last_active)}",
        f"- 导出：{exported_at} · 消息 {len(msgs)} 条 · Run {len(runs)} 个 · 轨迹事件 {len(events)} 条",
        "",
        "---",
        "",
        "## 消息",
        "",
    ]
    for m in msgs:
        r = run_by_id.get(m.run_id) if m.run_id else None
        head = f"### [{_fmt_dt(m.created_at)}] {_author_label(m)}"
        if r is not None:
            head += f"（run `{str(r.id)[:8]}` · {r.run_type} · {r.status}）"
        lines += [head, "", m.content or "（空）", ""]
        n_img = len((m.meta_json or {}).get("images") or [])
        if n_img:
            lines += [f"*（附图 {n_img} 张，原图见 session.json）*", ""]
        lines += ["---", ""]
    if runs:
        lines += ["## Run 一览", "",
                  "| # | Run | 类型 | 状态 | 发起 | 结束 |", "|---|-----|------|------|------|------|"]
        for i, r in enumerate(runs, 1):
            lines.append(f"| {i} | `{str(r.id)[:8]}` | {r.run_type} | {r.status} "
                         f"| {_fmt_dt(r.created_at)} | {_fmt_dt(r.finished_at)} |")
        lines += ["", "## Run 详情", ""]
    for i, r in enumerate(runs, 1):
        lines += [f"### Run {i} · `{str(r.id)[:8]}` · {r.run_type} · {r.status}", ""]
        if r.error:
            lines += [f"**错误**：{r.error}", ""]
        if r.final_summary:
            lines += ["**收尾摘要**：", "", r.final_summary, ""]
        plan = r.plan_json or []
        if plan:
            lines += ["**计划清单**："]
            lines += [f"- [{'x' if it.get('status') == 'completed' else ' '}] {it.get('content', '')}"
                      for it in plan]
            lines.append("")
        rq = questions_by_run.get(r.id) or []
        if rq:
            lines += ["**待答问题**："]
            for q in rq:
                if q.status == "answered":
                    opts = q.options_json or []
                    ans = (q.answer_text
                           or (str(opts[q.answer_index].get("label"))
                               if q.answer_index is not None and 0 <= q.answer_index < len(opts)
                               else "—"))
                    lines.append(f"- {q.question} → **{ans}**")
                else:
                    lines.append(f"- {q.question}（{q.status}）")
            lines.append("")
        evs = events_by_run.get(r.id) or []
        if evs:
            lines += ["**执行轨迹**（流式分片已省略）：", "", "```"]
            for e in evs:
                p = e.payload_json or {}
                brief = ""
                if e.type == "tool_call":
                    brief = str(p.get("tool") or "") + " " + str(p.get("args") or "")[:120]
                elif e.type == "tool_result":
                    brief = str(p.get("tool") or "") + " → " + str(p.get("result") or "")[:120]
                elif e.type == "agent_thinking":
                    brief = str(p.get("reasoning") or "")[:120]
                elif e.type == "llm_retry":
                    brief = f"第 {p.get('attempt')}/{p.get('max_attempts')} 次，等 {p.get('delay_ms')}ms"
                elif e.type == "run_done":
                    brief = f"status={p.get('status')}"
                lines.append(f"{e.seq} {_fmt_dt(e.created_at)} {e.type} {brief}".rstrip())
            lines += ["```", ""]

    md = "\n".join(lines)

    # ---------- session.json ----------
    payload = {
        "app": "trace",
        "exported_at": exported_at,
        "session": {
            "id": str(sess.id), "team_id": str(sess.team_id),
            "project_id": str(sess.project_id),
            "project_name": proj.name if proj else None,
            "title": sess.title, "status": sess.status,
            "created_at": str(sess.created_at), "last_active": str(sess.last_active),
        },
        "messages": [{
            "id": str(m.id), "run_id": str(m.run_id) if m.run_id else None,
            "author_type": m.author_type, "agent_key": m.agent_key,
            "author_id": str(m.author_id) if m.author_id else None,
            "content": m.content, "meta": m.meta_json or {},
            "created_at": str(m.created_at),
        } for m in msgs],
        "runs": [{
            "id": str(r.id), "run_type": r.run_type, "status": r.status,
            "trigger_msg_id": str(r.trigger_msg_id) if r.trigger_msg_id else None,
            "triggered_by": str(r.triggered_by) if r.triggered_by else None,
            "plan": r.plan_json or [], "change_report": r.change_report_json,
            "final_summary": r.final_summary, "usage": r.usage_json,
            "error": r.error, "has_placeholder": r.has_placeholder,
            "created_at": str(r.created_at), "finished_at": str(r.finished_at)
            if r.finished_at else None,
            "questions": [{
                "question": q.question, "options": q.options_json or [],
                "status": q.status, "answer_index": q.answer_index,
                "answer_text": q.answer_text, "created_at": str(q.created_at),
            } for q in (questions_by_run.get(r.id) or [])],
            "events": [{
                "seq": e.seq, "type": e.type, "payload": e.payload_json,
                "created_at": str(e.created_at),
            } for e in (events_by_run.get(r.id) or [])],
        } for r in runs],
    }

    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", sess.title or "")[:30].strip("-") or "session"
    fname = f"会话-{slug}-{datetime.now(timezone.utc):%Y%m%d-%H%M}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("conversation.md", md)
        z.writestr("session.json",
                   json.dumps(payload, ensure_ascii=False, indent=1, default=str))
    # starlette 头只认 latin-1：ASCII 回退名不能带中文，中文文件名全走
    # filename*=UTF-8''（现代浏览器都认），quote 后纯 ASCII
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"session-export.zip\"; "
                f"filename*=UTF-8''{quote(fname)}",
        },
    )


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _msg_uuid(v: str):  # type: ignore[no-untyped-def]
    """分页游标的 id 解析：message.id 是 UUID 列，直接拿 str 去比会 'str' has no attribute 'hex'。
    解析失败就退回原值（让无 Schema 的老库也不至于 500）。"""
    return _u(v)
