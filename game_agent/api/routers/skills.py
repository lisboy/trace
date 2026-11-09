"""技能管理 API：清单 / 源目录 / 全局技能 CRUD / 正文预览。

技能 = SKILL.md 技能包（渐进式披露：清单只带名称+描述，正文按需加载）。
来源三级：项目工作区 .trace/skills/ → 全局 DATA_DIR/skills/ → 用户添加的任意文件夹。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from game_agent.agents.roles import ROLE_TOOLS
from game_agent.api.deps import require
from game_agent.auth.rbac import PERM_AGENT_CONFIG, PERM_VIEW
from game_agent.config import PROJECTS_ROOT
from game_agent.core import skills
from game_agent.core.context import RequestContext
from game_agent.db.models import Project
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api", tags=["skills"])

ROLE_HINTS = {k: ", ".join(v) for k, v in ROLE_TOOLS.items()}


async def _workspace(project_id: str | None, ctx: RequestContext) -> str | None:
    if not project_id:
        return None
    if project_id not in ctx.project_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    async with SessionLocal() as s:
        p = await s.get(Project, _u(project_id))
        if p is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return str(PROJECTS_ROOT / p.repo_path)


@router.get("/skills")
async def list_skills(project_id: str | None = None,
                      ctx: RequestContext = Depends(require(PERM_VIEW))):
    ws = await _workspace(project_id, ctx)
    data = await skills.list_skills(ws)
    data["tool_roles"] = {r: ts for r, ts in ROLE_TOOLS.items() if "load_skill" in ts}
    return data


class RootIn(BaseModel):
    path: str


@router.post("/skills/roots")
async def add_root(body: RootIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """添加技能源文件夹（绝对路径）。文件夹必须已存在；同名技能按「项目 > 全局 > 自定义」优先。"""
    path = (body.path or "").strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="路径必填")
    p = Path(path).expanduser()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{p}")
    cur = await skills.get_extra_roots()
    if str(p) in cur or str(p.resolve()) in {str(Path(x).resolve()) for x in cur}:
        raise HTTPException(status_code=409, detail="该目录已在清单里")
    cur.append(str(p))
    await skills.set_extra_roots(cur)
    return {"roots": cur}


@router.delete("/skills/roots")
async def remove_root(body: RootIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    cur = await skills.get_extra_roots()
    target = (body.path or "").strip().strip('"')
    rest = [x for x in cur if x != target and str(Path(x).resolve()) != str(Path(target).resolve())]
    if len(rest) == len(cur):
        raise HTTPException(status_code=404, detail="不在清单里")
    await skills.set_extra_roots(rest)
    return {"roots": rest}


@router.get("/skills/{name}/content")
async def skill_content(name: str, project_id: str | None = None,
                        ctx: RequestContext = Depends(require(PERM_VIEW))):
    """技能正文（面板预览与调试用；run 内由 load_skill 工具按需加载）。"""
    if not skills.SKILL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="技能名非法")
    ws = await _workspace(project_id, ctx)
    try:
        body = await skills.skill_body(name, ws)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"技能不存在：{name}")
    pool = (await skills.list_skills(ws))["skills"]
    meta = next((sk for sk in pool if sk["name"] == name), None)
    return {"name": name, "content": body, "meta": meta}


class GlobalSkillIn(BaseModel):
    name: str
    description: str = ""
    content: str


@router.post("/skills/global")
async def save_global(body: GlobalSkillIn,
                      ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """创建/覆盖全局技能（DATA_DIR/skills/<name>/SKILL.md）。所有项目可用。"""
    try:
        path = skills.save_global_skill(body.name.strip(), body.description, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": str(path)}


@router.delete("/skills/global/{name}")
async def delete_global(name: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    try:
        skills.delete_global_skill(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
