"""供应商配置口（设计 §7.9）：schema 驱动表单 + 团队级配置。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from game_agent.api.deps import get_current_context, require
from game_agent.api.state import state
from game_agent.auth.rbac import PERM_AGENT_CONFIG, PERM_VIEW
from game_agent.assets.drivers import get_driver_specs
from game_agent.core.context import RequestContext
from game_agent.db.models import AssetProvider
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api", tags=["providers"])


@router.get("/providers/drivers")
async def drivers(ctx: RequestContext = Depends(require(PERM_VIEW))):
    return {"drivers": get_driver_specs()}


class ProviderIn(BaseModel):
    driver_key: str
    label: str
    kind: str
    enabled: bool = True
    config_json: dict = {}


@router.get("/providers")
async def list_providers(ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        rows = (await s.execute(select(AssetProvider).where(
            AssetProvider.team_id == _u(ctx.team_id)))).scalars().all()
        out = []
        for p in rows:
            cfg = dict(p.config_json or {})
            if cfg.get("auth_value"):
                cfg["auth_value"] = "***"  # 鉴权值不回传前端
            out.append({"id": str(p.id), "driver_key": p.driver_key, "label": p.label,
                        "kind": p.kind, "enabled": p.enabled, "config_json": cfg})
        return out


@router.post("/providers")
async def create_provider(body: ProviderIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    async with SessionLocal() as s:
        p = AssetProvider(team_id=_u(ctx.team_id), driver_key=body.driver_key,
                          label=body.label, kind=body.kind, enabled=body.enabled,
                          config_json=body.config_json)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return {"id": str(p.id)}


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    async with SessionLocal() as s:
        p = await s.get(AssetProvider, _u(provider_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        await s.delete(p)
        await s.commit()
    return {"ok": True}


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
