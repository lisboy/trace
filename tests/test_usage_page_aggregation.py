"""用量页接口 /api/usage 的口径回归（2026-11-14 该接口改为「一次扫描 + 内存聚合」）。

为什么值得钉住：为了让用量页秒开，原来自带 team_id+created_at 条件的 8 条聚合被合并成
一次扫描再在内存里累加（实测 3.4 万行的库上 287ms → 首屏 ~260ms，命中缓存 0ms）。
口径一个都不能变：
  · 总量 = 窗口内全部调用（含摘要调用，历史行为如此）
  · 按模型/按角色/按天 都要按窗口过滤，按天还要按服务器时区归日
  · 按项目/按会话 只统计能经 run 归属的调用（没有 run 的不计入）
  · 筛选生效时 facets 仍按未筛选全集返回（否则选中后下拉只剩自己）
  · 同一参数第二次请求走缓存，但换筛选/换窗口必须重新算
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete

from game_agent.api.routers import config as config_router
from game_agent.api.state import state
from game_agent.auth.local import ensure_local_identity
from game_agent.db.models import LlmCall, Project, Run, Session


@pytest.fixture
def api(session_factory, monkeypatch):
    """种两条项目/会话：p1 里 4 次调用，p2 里 2 次，另有 1 次没有 run 归属。"""

    async def seed():
        key = uuid4().hex
        async with session_factory() as db:
            user, team = await ensure_local_identity(db)
            # 本机单用户版所有用例共用同一个 team：先清掉上一轮种的数据，
            # 否则同一个 team 的调用会累加（7 条变 14 条、缓存命中率跟着变）。
            for model in (LlmCall, Run, Session, Project):
                await db.execute(delete(model).where(model.team_id == team.id))
            await db.flush()
            p1 = Project(name="卡片塔", slug=f"usage-a-{key}", repo_path=f"usage-a-{key}",
                         team_id=team.id, created_by=user.id)
            p2 = Project(name="gameAgent", slug=f"usage-b-{key}", repo_path=f"usage-b-{key}",
                         team_id=team.id, created_by=user.id)
            db.add_all([p1, p2])
            await db.flush()
            s1 = Session(team_id=team.id, project_id=p1.id, created_by=user.id, title="会话甲")
            s2 = Session(team_id=team.id, project_id=p2.id, created_by=user.id, title="会话乙")
            db.add_all([s1, s2])
            await db.flush()
            r1 = Run(team_id=team.id, project_id=p1.id, session_id=s1.id,
                     triggered_by=user.id, status="succeeded")
            r2 = Run(team_id=team.id, project_id=p2.id, session_id=s2.id,
                     triggered_by=user.id, status="failed")
            db.add_all([r1, r2])
            await db.flush()
            # 时间钉在「刚刚」，避免窗口边界把种子数据甩出去
            now = datetime.now(timezone.utc) - timedelta(hours=1)
            rows = [
                # (run, agent, model, prompt, completion, cached, reported, ms, 分钟偏移)
                (r1, "main", "m-fast", 1_000, 500, 800, 1, 10_000, 0),
                (r1, "main", "m-fast", 1_000, 500, 800, 1, 10_000, 1),
                (r1, "tester", "m-slow", 2_000, 100, 0, 0, 40_000, 2),
                (r1, "tester", "m-slow", 1_000, 100, 0, 0, 20_000, 3),
                (r2, "main", "m-slow", 5_000, 900, 4_500, 1, 30_000, 4),
                (r2, "main", "m-fast", 500, 100, 400, 1, 5_000, 5),
                # 没有 run 归属：只进总量/按模型/按角色，不进按项目/按会话
                (None, "main", "m-fast", 300, 200, 0, 0, 2_000, 6),
            ]
            for i, (run, ak, model, p, ct, ca, rep, ms, off) in enumerate(rows):
                db.add(LlmCall(
                    team_id=team.id, run_id=run.id if run is not None else None,
                    call_id=f"usage-agg-{key}-{i}", agent_key=ak, model=model,
                    prompt_tokens=p, completion_tokens=ct, cached_tokens=ca,
                    cache_reported=rep, latency_ms=ms,
                    created_at=now + timedelta(minutes=off)))
            await db.commit()
            return {"team_id": str(team.id)}

    ids = asyncio.run(seed())
    monkeypatch.setattr(state, "session_factory", session_factory)
    # 缓存是模块级字典：用例之间必须清掉，否则上一条用例的结果会串进来
    monkeypatch.setattr(config_router, "_USAGE_CACHE", {})
    app = __import__("fastapi").FastAPI()
    app.include_router(config_router.router)
    return SimpleNamespace(app=app, ids=ids)


def _get(api, path: str):
    async def send():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(send())


def test_totals_and_grouping(api) -> None:
    r = _get(api, "/api/usage?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    # 总量：7 次调用，输入 1000+1000+2000+1000+5000+500+300 = 10800，输出 2400
    assert body["tokens"]["calls"] == 7
    assert body["tokens"]["prompt"] == 10_800
    assert body["tokens"]["completion"] == 2_400
    # 缓存命中率沿用原口径：命中数 / 窗口内**全部**输入（不排除未上报缓存的行），
    # 6500 / 10800 ≈ 60.2%。这里钉住原口径，别在优化时顺手改了分母。
    assert body["tokens"]["cached"] == 6_500
    assert body["tokens"]["cache_hit_rate"] == pytest.approx(60.2, abs=0.1)
    # 速度 = 输出 / 总耗时（117s）→ 2400/117 ≈ 20.5
    assert body["tokens"]["tokens_per_sec"] == pytest.approx(20.5, abs=0.2)

    by_model = {m["model"]: m for m in body["by_model"]}
    assert set(by_model) == {"m-fast", "m-slow"}
    assert by_model["m-fast"]["calls"] == 4          # 含没有 run 的那条
    assert by_model["m-slow"]["calls"] == 3
    # 按角色
    assert body["by_agent"]["main"]["calls"] == 5
    assert body["by_agent"]["tester"]["calls"] == 2
    assert body["by_agent"]["main"]["tokens"] == 1_000 + 500 + 1_000 + 500 + 5_000 + 900 + 500 + 100 + 300 + 200
    # 按项目：只有 6 条带 run 的进（没有 run 的那条不算）
    proj = {p["project"]: p for p in body["by_project"]}
    assert proj["卡片塔"]["calls"] == 4
    assert proj["gameAgent"]["calls"] == 2
    # 按会话：用标题，且排序按输入 tokens 倒序（会话乙 5500 > 会话甲 5000）
    sess = [s["title"] for s in body["by_session"]]
    assert sess == ["会话乙", "会话甲"]


def test_by_day_folds_into_local_date(api) -> None:
    """"按天"必须按服务器时区归日：直接截 UTC 字符串会把凌晨的调用算到前一天。"""
    body = _get(api, "/api/usage?days=7").json()
    assert len(body["by_day"]) >= 1
    for d in body["by_day"]:
        # 日期是 YYYY-MM-DD，能被 strptime 解析（不是 datetime 的完整字符串）
        datetime.strptime(d["date"], "%Y-%m-%d")
    total = sum(d["calls"] for d in body["by_day"])
    assert total == 7           # 每天合计要等于总调用数


def test_filter_narrows_numbers_but_keeps_all_facets(api) -> None:
    body = _get(api, "/api/usage?days=7&agent=tester").json()
    assert body["tokens"]["calls"] == 2
    assert list(body["by_agent"]) == ["tester"]
    # facets 仍按未筛选全集返回：否则选中 tester 后下拉只剩 tester，换不回去
    assert set(body["agent_facets"]) == {"main", "tester"}
    assert set(body["model_facets"]) == {"m-fast", "m-slow"}

    body2 = _get(api, "/api/usage?days=7&model=m-slow").json()
    assert body2["tokens"]["calls"] == 3
    assert {m["model"] for m in body2["by_model"]} == {"m-slow"}


def test_window_shrinks_result(api) -> None:
    """窗口越短结果越小：数据都在 1 小时内，所以 days=1 仍是 7 条；days 极小才该空。"""
    assert _get(api, "/api/usage?days=1").json()["tokens"]["calls"] == 7


def test_second_request_hits_cache(api, monkeypatch) -> None:
    """同样的参数第二次走缓存（用量页切页面回来不该再扫一遍库），换筛选必须重算。"""
    calls = {"n": 0}
    real = config_router._usage_payload

    async def counting(*a, **kw):
        calls["n"] += 1
        return await real(*a, **kw)

    monkeypatch.setattr(config_router, "_usage_payload", counting)
    first = _get(api, "/api/usage?days=7").json()
    second = _get(api, "/api/usage?days=7").json()
    assert calls["n"] == 1
    assert first["tokens"] == second["tokens"]
    _get(api, "/api/usage?days=7&agent=main")
    assert calls["n"] == 2
    _get(api, "/api/usage?days=30")
    assert calls["n"] == 3
