"""续跑前沿 + run 用量落库的回归测试。

对着 2026-10-29 kpgame 会话的浪费钉死：
1. 上一轮的收尾报告被截断时砍掉了尾部——「剩余工作两步」正好在尾部，下一轮只能重新勘察；
2. 上一轮的 plan_json / artifacts 从来没交给过下一轮，用户重新提需求时等于冷启动；
3. 「继续」按钮的续跑说明已经带齐前沿，不能再叠加一份；
4. runs.usage_json 从来没人写，界面看不到一次 run 的调用次数与耗时构成。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from game_agent.core.repository import DbRepository
from game_agent.db.models import Artifact, Message, Project, Run, Session, Team, User
from game_agent.orchestration.orchestrator import (
    Orchestrator, _clip_keep_tail, _merge_usage, _run_usage_json,
)


def _run(coro, timeout: float = 30):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


# ---------------------------------------------------------------- 纯函数

def test_clip_keeps_head_and_tail() -> None:
    text = "开头结论。" + "噪声" * 500 + "剩余工作两步：补 standee 与 dynamic。"
    out = _clip_keep_tail(text, 200)
    assert out.startswith("开头结论。")
    assert out.endswith("剩余工作两步：补 standee 与 dynamic。")
    assert "中间省略" in out
    assert len(out) <= 260


def test_clip_leaves_short_text_alone() -> None:
    assert _clip_keep_tail("短", 200) == "短"


def test_run_usage_json_matches_frontend_fields() -> None:
    out = _run_usage_json({"calls": 74, "prompt_tokens": 4_855_896,
                           "completion_tokens": 217_610, "cached_tokens": 4_668_800,
                           "latency_ms": 1_554_204})
    assert out["calls"] == 74 and out["prompt"] == 4_855_896
    assert out["completion"] == 217_610 and out["cached"] == 4_668_800
    assert out["tokens_per_sec"] == round(217_610 / 1554.204, 1)
    assert out["cache_hit_rate"] == round(4_668_800 * 100 / 4_855_896)
    assert _run_usage_json(None)["calls"] == 0


def test_merge_usage_accumulates_retries() -> None:
    a = _merge_usage({}, {"calls": 2, "prompt_tokens": 10, "completion_tokens": 5,
                          "cached_tokens": 3, "latency_ms": 100})
    b = _merge_usage(a, {"calls": 1, "prompt_tokens": 7, "completion_tokens": 2,
                         "cached_tokens": 1, "latency_ms": 50})
    assert b == {"calls": 3, "prompt_tokens": 17, "completion_tokens": 7,
                 "cached_tokens": 4, "latency_ms": 150}


# ---------------------------------------------------------------- 前沿注入

async def _seed_two_runs(session_factory, cont_msg: bool):
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as s:
        user = User(email=f"rf-{suffix}@example.com", display_name="前沿测试")
        team = Team(name="前沿团队", slug=f"rf-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="前沿项目", slug=f"rf-{suffix}",
                          repo_path=f"rf-{suffix}", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="前沿会话",
                          created_by=user.id)
        s.add(session)
        await s.flush()
        prev = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                   triggered_by=user.id, run_type="modify", status="cancelled",
                   plan_json=[
                       {"id": "p1", "content": "汇总 6 角色 × 6 类资产清单", "status": "completed",
                        "note": "6×6=36 张"},
                       {"id": "p2", "content": "生成 6 角色头像 + 卡面共 12 张",
                        "status": "in_progress", "note": ""},
                       {"id": "p3", "content": "补抠图立绘（standee）", "status": "pending",
                        "note": "与 portrait 同动作"},
                   ])
        s.add(prev)
        await s.flush()
        s.add(Artifact(team_id=team.id, project_id=project.id, run_id=prev.id,
                       asset_kind="image", slug="ax-portrait-v3",
                       rel_path="assets/art/character/ax-portrait-v3_v1.png"))
        meta = ({"continue_from": str(prev.id)} if cont_msg else {})
        msg = Message(session_id=session.id, author_type="user",
                      content="继续完成任务目标" if cont_msg else "把其他立绘也出了吧",
                      meta_json=meta)
        s.add(msg)
        await s.flush()
        cur = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                  triggered_by=user.id, run_type="modify", status="queued",
                  trigger_msg_id=msg.id)
        s.add(cur)
        await s.commit()
        return str(cur.id)


def test_resume_block_carries_plan_and_artifacts(session_factory) -> None:
    orch = Orchestrator(SimpleNamespace(repo=DbRepository(session_factory)),
                        "frontier-test", session_factory)

    async def _flow():
        run_id = await _seed_two_runs(session_factory, cont_msg=False)
        async with session_factory() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            return await orch._resume_block(run)

    text = _run(_flow())
    assert "上一轮停在哪" in text
    assert "生成 6 角色头像 + 卡面共 12 张" in text      # 未完成项
    assert "6×6=36 张" in text                          # 已完成项 note 里的前一轮发现
    assert "1/3 完成" in text
    assert "ax-portrait-v3_v1.png" in text              # 已产出文件，别重出


def test_resume_block_skips_continue_brief(session_factory) -> None:
    """点「继续」时 routers/runs.py 已经写了计划进度+事实+最后动作，不能叠加第二份。"""
    orch = Orchestrator(SimpleNamespace(repo=DbRepository(session_factory)),
                        "frontier-test", session_factory)

    async def _flow():
        run_id = await _seed_two_runs(session_factory, cont_msg=True)
        async with session_factory() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            return await orch._resume_block(run)

    assert _run(_flow()) == ""
