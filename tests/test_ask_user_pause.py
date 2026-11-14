"""ask_user「同一 run 内暂停」回归测试（2026-11-10 用户诉求）。

用户反馈（原话）：「回答和提问的，样式不行，不应算结束上一轮对话，而是暂停等待用户
回答才是，也还在上一轮对话内容，并且怎么计划直接因为提问就没了」。

旧实现：ask_user 落一条问题后**抛 AskUserStop 中止本轮**，run 落 blocked，用户作答后
起一个**新 run** 续跑。结果——待办清单（挂在旧 run 上）消失、轨迹/对话被切成两轮。

新实现：ask_user 登记一个进程内 future，**原地挂起同一 run**；作答端点 set_result 唤醒，
循环带着答案继续。只有拿不到 future（服务重启 / 多进程）才回退旧路径。

本文钉死：
- 工具层：拿到 answer → 返回「用户已回答…」且不抛 AskUserStop；拿不到 → 抛 AskUserStop 回退；
- 编排器：ask_user 挂起 + resolve_question 唤醒 → 返回答案、发 question_answered 事件；
- has_pending_question 的状态流转（提问中 True，作答后 False）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from game_agent.agents.tools import ToolContext
from game_agent.agents.tools.question import AskUserStop, ask_user as ask_user_tool
from game_agent.config import PROJECTS_ROOT
from game_agent.core.context import RunContext
from game_agent.core.lite.container import build_lite_services
from game_agent.db.models import Project, Run, RunQuestion, Session, Team, User
from game_agent.orchestration.orchestrator import Orchestrator


def _run(coro, timeout: float = 30) -> Any:
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(_wrapped())


# ---------------------------------------------------------------- 工具层分支
class _FakeOrch:
    def __init__(self, record: dict) -> None:
        self.record = record
        self.calls: list[dict] = []

    async def ask_user(self, run_ctx, question, options, allow_text=True,
                       agent_key="main", progress=None):
        self.calls.append({"question": question, "options": options})
        if progress:
            progress()  # 验证回调被透传（推进活进展计数）
        return dict(self.record)


def _tctx(orch) -> ToolContext:
    from pathlib import Path
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=None, run_type="modify", worker_id="t",
                    repo=None, bus=None, locks=None, store=None)
    return ToolContext(run_ctx=rc, workspace=Path("."), role="main", task_id="t",
                       orchestrator=orch)


def test_tool_returns_answer_and_continues() -> None:
    """拿到 answer → 不结束本轮：返回「用户已回答…」，不抛 AskUserStop。"""
    orch = _FakeOrch({"id": "q1", "question": "选A还是B", "answer": "选A"})
    t = _tctx(orch)
    out = _run(ask_user_tool({"question": "选A还是B", "options": [{"label": "A"}, {"label": "B"}]}, t))
    assert "用户已回答：选A" in out
    assert t.progress_seq == 1, "progress 回调应被透传并推进活进展计数"


def test_tool_falls_back_to_stop_when_no_answer() -> None:
    """拿不到 answer（服务重启/多进程）→ 抛 AskUserStop，走旧的 blocked + 新 run 路径。"""
    orch = _FakeOrch({"id": "q2", "question": "选A还是B"})
    t = _tctx(orch)
    with pytest.raises(AskUserStop) as ei:
        _run(ask_user_tool({"question": "选A还是B", "options": [{"label": "A"}]}, t))
    assert ei.value.question["id"] == "q2"


# ---------------------------------------------------------------- 编排器暂停/唤醒
async def _seed(session_factory) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    repo_path = f"pause-{suffix}"
    async with session_factory() as s:
        user = User(email=f"pause-{suffix}@example.com", display_name="暂停用户")
        team = Team(name="暂停团队", slug=f"pause-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="暂停项目", slug=repo_path,
                          repo_path=repo_path, project_type="python", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="暂停会话",
                          created_by=user.id)
        s.add(session)
        await s.flush()
        run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                  triggered_by=user.id, run_type="modify", status="running")
        s.add(run)
        await s.commit()
        ids = {"team_id": str(team.id), "project_id": str(project.id),
               "session_id": str(session.id), "run_id": str(run.id), "user_id": str(user.id)}
    (PROJECTS_ROOT / repo_path).mkdir(parents=True, exist_ok=True)
    return ids


def _ctx(ids: dict[str, str], svcs) -> RunContext:
    return RunContext(
        run_id=ids["run_id"], team_id=ids["team_id"], project_id=ids["project_id"],
        session_id=ids["session_id"], triggered_by=None, run_type="modify",
        worker_id="pause-worker", repo=svcs.repo, bus=svcs.bus,
        locks=svcs.locks, store=svcs.store, meta={})


def test_orchestrator_pause_and_resolve(session_factory) -> None:
    async def flow():
        ids = await _seed(session_factory)
        svcs = await build_lite_services(session_factory, "pause-worker")
        orch = Orchestrator(svcs, "pause-worker", session_factory)
        ctx = _ctx(ids, svcs)

        task = asyncio.create_task(orch.ask_user(ctx, "选 A 还是 B？",
                                                 [{"label": "A"}, {"label": "B"}]))
        # 等它把问题落库、登记 future
        qid = None
        for _ in range(100):
            await asyncio.sleep(0.02)
            async with session_factory() as s:
                row = (await s.execute(
                    __import__("sqlalchemy").select(RunQuestion).where(
                        RunQuestion.run_id == uuid.UUID(ids["run_id"])))).scalars().first()
            if row is not None:
                qid = str(row.id)
                break
        assert qid, "问题没有落库"
        assert orch.has_pending_question(qid) is True
        assert task.done() is False, "ask_user 不应结束，而应挂起等回答"

        # 作答 → 唤醒同一 run
        assert orch.resolve_question(qid, "选 A") is True
        rec = await asyncio.wait_for(task, timeout=5)
        assert rec["answer"] == "选 A"
        assert orch.has_pending_question(qid) is False

        # 事件里既有 question_asked 也有 question_answered（前端据此切换暂停态）
        events = await svcs.repo.get_events_since(ids["run_id"], 0)
        types = [e.type for e in events]
        assert "question_asked" in types and "question_answered" in types, types
        return rec

    _run(flow())


def test_resolve_unknown_question_returns_false(session_factory) -> None:
    """没有在途暂停 → resolve 返回 False（作答端点据此走「起新 run」回退路径）。"""
    async def flow():
        ids = await _seed(session_factory)
        svcs = await build_lite_services(session_factory, "pause-worker")
        orch = Orchestrator(svcs, "pause-worker", session_factory)
        return orch.resolve_question(str(uuid.uuid4()), "x")

    assert _run(flow()) is False


# ---------------------------------------------------------------- 端到端：整轮不结束
class _AskThenWriteBrain:
    """第 1 轮发起 ask_user；第 2 轮（拿到答案后）写文件并汇报。"""

    def __init__(self) -> None:
        self.round = 0

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        from game_agent.llm.base import LLMResponse, ToolCall
        if not tools:  # 意图判别 / 经验沉淀判定：返回不可解析 → 意图兜底 modify
            return LLMResponse(text='{"worth": false, "entries": []}', usage={})
        self.round += 1
        mk = lambda n, **a: ToolCall(id=f"c{uuid.uuid4().hex[:8]}", name=n, arguments=a)
        if self.round == 1:
            return LLMResponse(tool_calls=[mk("ask_user", question="用 A 方案还是 B 方案？",
                                              options=[{"label": "A"}, {"label": "B"}])],
                               usage={"prompt_tokens": 10, "completion_tokens": 5})
        return LLMResponse(tool_calls=[
            mk("write_file", path="feature_agent.py", content="# answered\n"),
            mk("report_to_user", report="## 完成\n\n按 A 方案做完了。"),
        ], usage={"prompt_tokens": 10, "completion_tokens": 5})


@pytest.mark.slow
def test_run_continues_after_answer_in_same_run(session_factory) -> None:
    """整条 run 在提问处暂停、作答后**同一个 run** 继续直到成功（不落 blocked、不起新 run）。"""
    from unittest.mock import patch
    import sqlalchemy as sa
    import game_agent.agents.runtime as rt_mod
    import game_agent.orchestration.orchestrator as orch_mod

    async def flow():
        ids = await _seed(session_factory)
        # 让 run 处于可执行状态
        async with session_factory() as s:
            r = await s.get(Run, uuid.UUID(ids["run_id"]))
            r.status = "queued"
            await s.commit()
        svcs = await build_lite_services(session_factory, "pause-worker")
        orch = Orchestrator(svcs, "pause-worker", session_factory)
        brain = _AskThenWriteBrain()

        def _factory(role, ctx=None):
            return brain

        async def _resolver():
            # 轮询到问题落库 → 作答（模拟前端 /questions/{id}/answer 走同 run 解冻）
            for _ in range(300):
                await asyncio.sleep(0.02)
                async with session_factory() as s:
                    row = (await s.execute(sa.select(RunQuestion).where(
                        RunQuestion.run_id == uuid.UUID(ids["run_id"])))).scalars().first()
                if row is not None:
                    assert orch.resolve_question(str(row.id), "A") is True
                    return
            raise AssertionError("始终没有看到问题落库")

        with patch.object(rt_mod, "build_llm", _factory), \
                patch.object(orch_mod, "build_llm", _factory):
            resolver = asyncio.create_task(_resolver())
            await asyncio.wait_for(orch.execute_run(ids["run_id"]), timeout=30)
            await resolver

        run = await svcs.repo.get_run(ids["run_id"])
        async with session_factory() as s:
            n_runs = (await s.execute(sa.select(Run).where(
                Run.session_id == uuid.UUID(ids["session_id"])))).scalars().all()
        return run, len(n_runs), brain.round

    run, run_count, rounds = _run(flow(), timeout=60)
    assert run.status == "succeeded", f"{run.status} {run.error}"
    assert run_count == 1, f"应仍是同一条 run，不该新建（实际 {run_count} 条）"
    assert rounds >= 2, "拿到答案后应继续执行第二轮（写文件+汇报）"

