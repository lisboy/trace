"""顶栏「速度」必须是瞬时口径（2026-11-12）。

现场：用户把会话切到更快的模型，顶栏还是 21.7 tok/s —— 那个数是**整个会话的累计
平均值**，几万 token 的历史把新模型那点数据淹没了，看起来就像「切换没生效」。
这里钉死口径：tokens_per_sec = 最近一轮往返；tokens_per_sec_avg = 会话累计平均
（只作对照）；last_round 带回该轮的模型名与耗时，tooltip 要能看出是谁跑的。

这个版本没有登录：请求不带任何凭证，服务端直接用本机那一个身份（``ensure_local_identity``），
所以 fixture 里先把数据挂到「本机身份」的 team 下，接口才看得到。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from game_agent.api.routers import sessions
from game_agent.api.state import state
from game_agent.auth.local import ensure_local_identity
from game_agent.db.models import LlmCall, Project, Run, Session, Team


@pytest.fixture
def api(session_factory, monkeypatch):
    async def seed():
        key = uuid4().hex
        async with session_factory() as db:
            # 无凭证身份：本机 user/team。项目/会话/run 必须挂在这个 team 上，
            # 否则 get_current_context 拿到的 team_id 与数据不一致，接口一律 404。
            user, team = await ensure_local_identity(db)
            project = Project(name="usage", slug=f"usage-p-{key}", repo_path=f"usage-{key}",
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
            t0 = datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)
            rows = [
                # 历史：3 轮老模型，各 10k 输出 / 500s → 20 tok/s
                ("old", 10_000, 500_000, t0),
                ("old", 10_000, 500_000, t0 + timedelta(minutes=1)),
                ("old", 10_000, 500_000, t0 + timedelta(minutes=2)),
                # 刚切到的新模型：900 输出 / 15s → 60 tok/s
                ("fast", 900, 15_000, t0 + timedelta(minutes=3)),
                # 摘要调用比正文更晚、量更大：不能冒充「最近一轮」
                ("fast", 5_000, 1_000, t0 + timedelta(minutes=4)),
            ]
            for i, (model, ct, ms, at) in enumerate(rows):
                db.add(LlmCall(
                    team_id=team.id, run_id=run.id, call_id=f"usage-{key}-{i}",
                    agent_key="main", model=model, prompt_tokens=1_000,
                    completion_tokens=ct, cached_tokens=0, cache_reported=1,
                    latency_ms=ms, purpose="summarize" if i == 4 else "agent",
                    created_at=at))
            await db.commit()
            ids = {"session_id": str(session.id), "user_id": str(user.id),
                   "project_id": str(project.id), "team_id": str(team.id)}
        return ids

    ids = asyncio.run(seed())
    monkeypatch.setattr(state, "session_factory", session_factory)
    app = __import__("fastapi").FastAPI()
    app.include_router(sessions.router)
    return SimpleNamespace(app=app, ids=ids, session_factory=session_factory)


def _get(api, path: str):
    """本机单用户版没有凭证概念：请求不带任何认证头。"""

    async def send():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(send())


def test_speed_is_last_round_not_session_average(api) -> None:
    r = _get(api, f"/api/sessions/{api.ids['session_id']}/usage")
    assert r.status_code == 200, r.text
    body = r.json()
    # 瞬时 = 最近一轮正文往返：900 / 15s = 60 tok/s
    assert body["tokens_per_sec"] == 60.0
    assert body["last_round"]["model"] == "fast"
    assert body["last_round"]["completion_tokens"] == 900
    assert body["last_round"]["latency_ms"] == 15_000
    # 累计平均 = 全部往返（含摘要压缩这次隐形开销）：
    # (30000 + 900 + 5000) / (1500 + 15 + 1)s ≈ 23.7，只作对照
    assert body["tokens_per_sec_avg"] == pytest.approx(23.7, abs=0.05)
    # 摘要调用（更晚、量更大）不能混进瞬时口径
    assert body["last_round"]["completion_tokens"] != 5_000


def test_usage_of_other_team_session_is_404(api) -> None:
    """没有角色之分了，但 team 维度的隔离仍在：别的团队的会话照样看不见。"""

    async def seed():
        key = uuid4().hex
        async with api.session_factory() as db:
            team = Team(name="Foreign", slug=f"usage-foreign-{key}")
            db.add(team)
            await db.flush()
            project = Project(name="foreign", slug=f"usage-f-{key}",
                              repo_path=f"usage-foreign-{key}", team_id=team.id)
            db.add(project)
            await db.flush()
            session = Session(team_id=team.id, project_id=project.id)
            db.add(session)
            await db.commit()
            return str(session.id)

    sid = asyncio.run(seed())
    assert _get(api, f"/api/sessions/{sid}/usage").status_code == 404
    assert _get(api, f"/api/sessions/{uuid4()}/usage").status_code == 404
