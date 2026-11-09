"""项目记忆（长期记忆）：查看 / 手动添加 / 删除，以及工作区里的「改动经验」md。

run 成功后编排器会把要点自动沉淀为 auto 记忆；成员可在此补充 manual 记忆。
两者都会在后续编排时注入策划上下文（见 orchestrator._load_context）。

「改动经验」是另一层：run 收尾先判断本轮值不值得记，值得才按类别追加到
工作区 `.trace/notes/{decisions,pitfalls,conventions,playbook}.md`，
这里提供只读查看与按标题删除（用户手动删条目是常态，不给编辑框）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import require
from game_agent.auth.rbac import PERM_RUN_CREATE, PERM_VIEW
from game_agent.config import PROJECTS_ROOT
from game_agent.core.context import RequestContext
from game_agent.db.models import Project, ProjectMemory
from game_agent.db.session import SessionLocal
from game_agent.orchestration import project_memory, project_notes

router = APIRouter(prefix="/api", tags=["memory"])

MAX_LEN = 2000


async def _check_project(project_id: str, ctx: RequestContext) -> Project:
    async with SessionLocal() as s:
        p = await s.get(Project, _u(project_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        return p


@router.get("/projects/{project_id}/memory")
async def list_memory(project_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    await _check_project(project_id, ctx)
    async with SessionLocal() as s:
        rows = (await s.execute(select(ProjectMemory).where(
            ProjectMemory.project_id == _u(project_id)
        ).order_by(ProjectMemory.created_at.desc()).limit(100))).scalars().all()
        return [{"id": str(m.id), "content": m.content, "kind": m.kind,
                 "title": m.title or "", "tags": m.tags or "",
                 "session_id": m.session_id,
                 "updated_at": str(m.updated_at or m.created_at),
                 "source_run_id": str(m.source_run_id) if m.source_run_id else None,
                 "created_at": str(m.created_at)} for m in rows]


class MemoryIn(BaseModel):
    content: str


@router.post("/projects/{project_id}/memory")
async def add_memory(project_id: str, body: MemoryIn,
                     ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    await _check_project(project_id, ctx)
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    async with SessionLocal() as s:
        m = ProjectMemory(project_id=_u(project_id), content=content[:MAX_LEN], kind="manual")
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return {"id": str(m.id)}


@router.delete("/projects/{project_id}/memory/{memory_id}")
async def delete_memory(project_id: str, memory_id: str,
                        ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    await _check_project(project_id, ctx)
    async with SessionLocal() as s:
        m = await s.get(ProjectMemory, _u(memory_id))
        if m is None or str(m.project_id) != project_id:
            raise HTTPException(status_code=404, detail="记忆不存在")
        await s.delete(m)
        await s.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/memory/consolidate")
async def consolidate_memory(project_id: str,
                             ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """立即让模型融合一次记忆（合并跨对话的重复条目）。

    run 收尾本来就会自动融合；这里给「跑完几轮后想马上整理」或「手动补了几条后合并」用。
    """
    await _check_project(project_id, ctx)
    res = await project_memory.consolidate(project_id, force=True)
    return {"ok": True, **res}


@router.get("/projects/{project_id}/notes")
async def list_notes(project_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """工作区里的「改动经验」条目（run 收尾判断后分类落在 .trace/notes/*.md）。"""
    p = await _check_project(project_id, ctx)
    return project_notes.list_entries(PROJECTS_ROOT / p.repo_path)


@router.delete("/projects/{project_id}/notes/{category}/{key}")
async def delete_note(project_id: str, category: str, key: str,
                      ctx: RequestContext = Depends(require(PERM_RUN_CREATE))):
    """删一条经验（改标题即视为新条目，所以只给删除，不给在线编辑）。"""
    p = await _check_project(project_id, ctx)
    if not project_notes.delete_entry(PROJECTS_ROOT / p.repo_path, category, key):
        raise HTTPException(status_code=404, detail="经验条目不存在（或已被删除）")
    return {"ok": True}


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
