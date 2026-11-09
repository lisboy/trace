"""WebSocket 实时事件扇出（设计 §8.1）。DB 是权威源；此处为加速通道。"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from game_agent.api.state import state
from game_agent.db.models import Project, User
from game_agent.db.session import SessionLocal

router = APIRouter()


@router.websocket("/ws/projects/{project_id}")
async def project_events(websocket: WebSocket, project_id: str):
    await websocket.accept()
    token = websocket.query_params.get("token")
    team_id = websocket.query_params.get("team_id")
    if not token:
        await websocket.close(code=4401)
        return
    user = await state.auth.get_user_by_pat(token)
    if user is None:
        await websocket.close(code=4401)
        return
    async with SessionLocal() as s:
        project = await s.get(Project, _u(project_id))
        if project is None or str(project.team_id) != team_id:
            await websocket.close(code=4403)
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
