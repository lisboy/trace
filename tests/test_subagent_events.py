"""子agent 的对话详情：`GET /runs/{id}/events?task_id=…` 只回这一个子agent 的事件（2026-11-16）。

用户要求：「子agent 要能点击，并弹窗查看对话详情」。运行面板的「子agent」清单里只有
task 级元数据（名字/问题/状态/耗时），弹窗要看对话详情就得单独拉这个 task 的事件流。

两个口径必须钉住：
1. **只回这个 task 的**：同一 run 里并行 3 个子agent 时不能把别人的思考和工具调用混进来，
   主 agent 的行更不能混进来（否则弹窗里会出现「明明不是它干的」步骤）。
2. **不能靠 Event.task_id 单列过滤**：`task_start`/`task_done` 由 orchestrator 广播，
   行上的 task_id 是空的、真正的 task_id 只在 payload 里。漏掉它们前端就拼不出组头
   （标题 / 问题 / 起止时刻 / 收尾结论 summary 全没了）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from game_agent.api.routers import runs as runs_router
from game_agent.auth.local import ensure_local_identity
from game_agent.db.models import Event, Project, Run, Session, Task


@pytest.fixture
def api(session_factory, monkeypatch):
    async def seed():
        key = uuid4().hex
        async with session_factory() as db:
            user, team = await ensure_local_identity(db)
            project = Project(name="subev", slug=f"subev-p-{key}", repo_path=f"subev-{key}",
                              team_id=team.id, created_by=user.id)
            db.add(project)
            await db.flush()
            session = Session(team_id=team.id, project_id=project.id, created_by=user.id)
            db.add(session)
            await db.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                      triggered_by=user.id, status="succeeded")
            db.add(run)
            await db.flush()
            t0 = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
            main_task = Task(run_id=run.id, kind="main", agent_key="main", status="done",
                             title="主任务", created_at=t0, started_at=t0)
            sub_a = Task(run_id=run.id, kind="sub", agent_key="sub", status="done",
                         title="子agent · 查物料", result_json={"question": "物料齐吗？"},
                         created_at=t0 + timedelta(seconds=1),
                         started_at=t0 + timedelta(seconds=1),
                         finished_at=t0 + timedelta(seconds=5))
            sub_b = Task(run_id=run.id, kind="sub", agent_key="sub", status="done",
                         title="子agent · 查经济", result_json={"question": "数值合理吗？"},
                         created_at=t0 + timedelta(seconds=1),
                         started_at=t0 + timedelta(seconds=1),
                         finished_at=t0 + timedelta(seconds=6))
            db.add_all([main_task, sub_a, sub_b])
            await db.flush()

            def ev(seq: int, type_: str, payload: dict, task_id=None) -> Event:
                return Event(team_id=team.id, project_id=project.id, run_id=run.id,
                             task_id=task_id, seq=seq, type=type_, payload_json=payload)

            db.add_all([
                # task_start/task_done 广播事件：行上 task_id 为空，task_id 只在 payload 里
                ev(1, "task_start", {"task_id": str(sub_a.id), "role": "sub",
                                     "title": "子agent · 查物料", "question": "物料齐吗？"}),
                ev(2, "task_start", {"task_id": str(sub_b.id), "role": "sub",
                                     "title": "子agent · 查经济", "question": "数值合理吗？"}),
                # A 自己的往返
                ev(3, "agent_thinking", {"agent_key": "sub", "round": 0,
                                         "reasoning": "先读物料表"}, sub_a.id),
                ev(4, "tool_call", {"agent_key": "sub", "round": 0, "tool": "read_file",
                                    "args": {"path": "data/items.json"}}, sub_a.id),
                ev(5, "tool_result", {"agent_key": "sub", "round": 0, "tool": "read_file",
                                      "result": "ok", "duration_ms": 12}, sub_a.id),
                # B 自己的往返
                ev(6, "agent_thinking", {"agent_key": "sub", "round": 0,
                                         "reasoning": "先看经济数值"}, sub_b.id),
                ev(7, "tool_call", {"agent_key": "sub", "round": 0, "tool": "grep",
                                    "args": {"pattern": "gold"}}, sub_b.id),
                # 主 agent 的行：绝不能出现在子agent 弹窗里
                ev(8, "tool_call", {"agent_key": "main", "round": 1, "tool": "run_command",
                                    "args": {"command": "npm run build"}}, main_task.id),
                ev(9, "task_done", {"task_id": str(sub_a.id), "role": "sub", "status": "done",
                                    "summary": "物料齐了"}),
                ev(10, "task_done", {"task_id": str(sub_b.id), "role": "sub", "status": "done",
                                     "summary": "数值合理"}),
            ])
            await db.commit()
            return {"run_id": str(run.id), "sub_a": str(sub_a.id), "sub_b": str(sub_b.id)}

    ids = asyncio.run(seed())

    async def wire():
        from game_agent.api.state import state
        from game_agent.core.lite.container import build_lite_services
        monkeypatch.setattr(state, "session_factory", session_factory)
        monkeypatch.setattr(state, "services",
                            await build_lite_services(session_factory, "test-worker"))

    asyncio.run(wire())
    app = FastAPI()
    app.include_router(runs_router.router)
    return app, ids


def _get(app, path: str):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(send())


def test_task_events_only_return_that_subagent(api) -> None:
    app, ids = api
    r = _get(app, f"/api/runs/{ids['run_id']}/events?task_id={ids['sub_a']}")
    assert r.status_code == 200, r.text
    body = r.json()
    types = [e["type"] for e in body]
    # 只含 A 的：task_start、思考、读文件、结果、task_done
    assert types == ["task_start", "agent_thinking", "tool_call", "tool_result", "task_done"]
    paths = [e["payload"].get("args", {}).get("path") for e in body if e["type"] == "tool_call"]
    assert paths == ["data/items.json"]  # 没有 B 的 grep、没有主 agent 的 run_command
    # task_start 虽然行上没 task_id，也必须带回来（前端靠它拼组头）
    assert body[0]["payload"]["task_id"] == ids["sub_a"]
    assert body[0]["payload"]["question"] == "物料齐吗？"
    assert body[-1]["payload"]["summary"] == "物料齐了"


def test_task_events_of_other_subagent_are_separate(api) -> None:
    app, ids = api
    body = _get(app, f"/api/runs/{ids['run_id']}/events?task_id={ids['sub_b']}").json()
    assert [e["type"] for e in body] == ["task_start", "agent_thinking", "tool_call", "task_done"]
    assert body[0]["payload"]["question"] == "数值合理吗？"
    assert body[-1]["payload"]["summary"] == "数值合理"
    # 反向确认：A 与 B 的正文事件不相交
    a = _get(app, f"/api/runs/{ids['run_id']}/events?task_id={ids['sub_a']}").json()
    assert {e["seq"] for e in body} & {e["seq"] for e in a} == set()


def test_without_task_id_still_returns_whole_run(api) -> None:
    """不传 task_id 的老用法不受影响（聊天流的整条轨迹仍靠它）。"""
    app, ids = api
    body = _get(app, f"/api/runs/{ids['run_id']}/events?since=0").json()
    assert len(body) == 10  # 老路径走 state.services.repo…这里退化为真实库查询
