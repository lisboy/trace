"""WebSocket 实时事件扇出（设计 §8.1）。DB 是权威源；此处为加速通道。

这个版本没有登录，所以也不再校验 token——本机就是本人。
唯一保留的检查是「这个项目存在且属于本机工作区」，避免拿别的 project_id 乱订阅。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from game_agent.api.state import state
from game_agent.auth.local import ensure_local_identity
from game_agent.db.models import Project
from game_agent.db.session import SessionLocal

router = APIRouter()


@router.websocket("/ws/projects/{project_id}")
async def project_events(websocket: WebSocket, project_id: str):
    await websocket.accept()
    async with SessionLocal() as s:
        _user, team = await ensure_local_identity(s)
        project = await s.get(Project, _u(project_id))
        if project is None or str(project.team_id) != str(team.id):
            await websocket.close(code=4404)
            return
    await websocket.send_json({"type": "connected", "project_id": project_id})
    try:
        async for env in state.services.bus.subscribe(project_id):
            await websocket.send_json({
                "seq": env.payload.get("_seq"),
                "type": env.type,
                "run_id": str(env.run_id) if env.run_id else None,
                "task_id": str(env.task_id) if env.task_id else None,
                "agent": env.payload.get("agent_key"),
                "payload": env.payload,
            })
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        return


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
