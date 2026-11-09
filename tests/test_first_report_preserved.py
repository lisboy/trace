# -*- coding: utf-8 -*-
"""「第一份正确答复被纠正重跑顶掉」修复（run 9a9656bc 回归）。

真实事故：用户问的是纯代码问答（「一次接口请求能完成查重处理和编制完成吗？为什么
有时候请求一次没用？」）。模型 5 分钟给出正确答案并 report_to_user；但
· _needs_action 因为「处理」被判成动手指令（名词误判）→ 触发「只勘察不动手」纠正；
· 纠正把整轮重跑，第二轮 report_to_user 覆盖 _summaries、且最终只落一条 _post_reply；
结果用户聊天里根本看不到那 5 分钟的正确答复。

两条修复各自钉死：
  1. 名词「处理」出现在问句里不再判成动手指令（纯问答不触发纠正）；
  2. 纠正重跑前先把第一轮已交卷的汇报落成聊天消息，最终答复与它不同则两条都在、
     相同则只落一条（幂等去重）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Coroutine

import pytest

from game_agent.config import PROJECTS_ROOT
from game_agent.core.lite.container import build_lite_services
from game_agent.db.models import Message, Project, Run, Session, Team, User
from game_agent.llm.base import LLM, LLMResponse, ToolCall
from game_agent.orchestration.orchestrator import Orchestrator

RUN_TIMEOUT_S = 180


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 120) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


def _tc(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)


async def _seed(session_factory, requirement: str) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    repo_path = f"first-report-{suffix}"
    async with session_factory() as s:
        user = User(email=f"fr-{suffix}@example.com", display_name="首报")
        team = Team(name="首报团队", slug=f"fr-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="首报项目", slug=repo_path,
                          repo_path=repo_path, project_type="python", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="首报会话",
                          created_by=user.id)
        s.add(session)
        await s.flush()
        message = Message(session_id=session.id, author_type="user", author_id=user.id,
                          content=requirement)
        s.add(message)
        await s.flush()
        run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                  triggered_by=user.id, trigger_msg_id=message.id,
                  run_type="modify", status="queued")
        s.add(run)
        await s.commit()
        ids = {"team_id": str(team.id), "project_id": str(project.id),
               "session_id": str(session.id), "run_id": str(run.id), "repo_path": repo_path}
    workspace = PROJECTS_ROOT / repo_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("# 首报项目\n", encoding="utf-8")
    return ids


class _ScriptedBrain(LLM):
    """按「带工具的 agent 轮次」依次吐脚本；收尾后的经验沉淀判定返回不值得记。"""

    def __init__(self, script: list[list[ToolCall]]) -> None:
        self.script = script
        self.agent_rounds = 0

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        if not tools:  # 收尾后的「值得沉淀改动经验吗」判定
            return LLMResponse(text='{"worth": false, "reason": "重放测试", "entries": []}',
                               usage={})
        self.agent_rounds += 1
        idx = self.agent_rounds - 1
        calls = self.script[idx] if idx < len(self.script) else []
        return LLMResponse(tool_calls=calls,
                           usage={"prompt_tokens": 100, "completion_tokens": 50})


def _execute(session_factory, requirement: str, script: list[list[ToolCall]]):
    from unittest.mock import patch
    import game_agent.agents.runtime as rt_mod
    import game_agent.orchestration.orchestrator as orch_mod

    brain = _ScriptedBrain(script)

    def _factory(role, ctx=None):  # noqa: ANN001, ANN202
        return brain

    async def _flow():
        ids = await _seed(session_factory, requirement)
        svcs = await build_lite_services(session_factory, "fr-worker")
        orch = Orchestrator(svcs, "fr-worker", session_factory)
        with patch.object(rt_mod, "build_llm", _factory), \
                patch.object(orch_mod, "build_llm", _factory):
            await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        messages = await svcs.repo.list_messages(ids["session_id"])
        return run, messages, brain

    return _run(_flow(), timeout=RUN_TIMEOUT_S)


@pytest.mark.slow
def test_pure_question_does_not_force_a_rerun(session_factory) -> None:
    """名词「处理」出现在问句里 → 纯问答，不触发「只勘察不动手」纠正。"""
    q = "这一块，一次接口请求，能完成查重处理和编制完成吗？为什么有时候请求一次没用？"
    script = [[_tc("report_to_user", report="## 结论\n\n一次请求做不到：查重是异步任务。")]]
    run, messages, brain = _execute(session_factory, q, script)

    assert brain.agent_rounds == 1, f"纯问答被强制重跑了，共 {brain.agent_rounds} 轮"
    assert run.status == "succeeded", f"{run.status} {run.error}"
    agent_msgs = [m for m in messages if m.author_type == "agent"]
    assert len(agent_msgs) == 1, agent_msgs
    assert "一次请求做不到" in agent_msgs[0].content


@pytest.mark.slow
def test_first_report_survives_correction_rerun(session_factory) -> None:
    """真要动手时，第一轮的汇报不能被纠正重跑顶掉：两条答复都在、顺序正确。"""
    first = "## 初步诊断\n\n问题在异步回调，我准备改 compileComplete。"
    second = "## 已修复\n\ncompileComplete 已重写，测试通过。"
    script = [
        [_tc("report_to_user", report=first)],  # 第 1 轮：先汇报（本轮零落地 → 触发纠正）
        [_tc("write_file", path="feature_agent.py", content="# fixed\n"),
         _tc("report_to_user", report=second)],  # 第 2 轮：真落地并给最终汇报
    ]
    run, messages, brain = _execute(session_factory, "改一下排序 bug", script)

    assert run.status == "succeeded", f"{run.status} {run.error}"
    assert brain.agent_rounds == 2, f"应纠正一次后完成，实际 {brain.agent_rounds} 轮"
    assert "已修复" in (run.final_summary or ""), run.final_summary
    agent_msgs = [m for m in messages if m.author_type == "agent"]
    contents = [m.content for m in agent_msgs]
    assert first in contents, f"第一轮的正确答复丢了：{contents}"
    assert second in contents, f"最终答复缺失：{contents}"
    assert contents.index(first) < contents.index(second), "两条答复的顺序不对"


@pytest.mark.slow
def test_identical_report_is_not_posted_twice(session_factory) -> None:
    """纠正重跑若产出与第一轮完全相同的答复，只落一条（幂等去重）。"""
    same = "## 报告\n\n结论：不需要改代码，只是异步任务。"
    script = [
        [_tc("report_to_user", report=same)],
        [_tc("write_file", path="feature_agent.py", content="# touch\n"),
         _tc("report_to_user", report=same)],
    ]
    run, messages, _brain = _execute(session_factory, "改一下排序 bug", script)

    assert run.status == "succeeded", f"{run.status} {run.error}"
    matches = [m for m in messages if m.author_type == "agent" and same in m.content]
    assert len(matches) == 1, f"同一段答复被重复落了 {len(matches)} 条"


def test_long_reply_is_not_truncated_at_4000(session_factory) -> None:
    """聊天消息不能被硬切在 4000 字（run 9a9656bc：4338 字报告在「后续建议」处被截断）。"""
    from game_agent.core.context import RunContext

    async def _flow():
        ids = await _seed(session_factory, "回归：长报告不截断")
        svcs = await build_lite_services(session_factory, "fr-worker")
        orch = Orchestrator(svcs, "fr-worker", session_factory)
        run = await svcs.repo.get_run(ids["run_id"])
        ctx = RunContext(run_id=ids["run_id"], team_id=ids["team_id"],
                         project_id=ids["project_id"], session_id=ids["session_id"],
                         triggered_by=run.triggered_by, run_type="modify",
                         worker_id="fr-worker", repo=svcs.repo, bus=svcs.bus,
                         locks=svcs.locks, store=svcs.store)
        body = "尾" * 5000 + "\n\n## 后续建议\n\n这一整段不能被截断。"
        await orch._post_reply(ctx, run, body)
        # 同一段再发一次：幂等去重
        await orch._post_reply(ctx, run, body)
        return await svcs.repo.list_messages(ids["session_id"]), body

    messages, body = _run(_flow())
    agent_msgs = [m for m in messages if m.author_type == "agent"]
    assert len(agent_msgs) == 1, f"重复调用应去重，实际 {len(agent_msgs)} 条"
    assert agent_msgs[0].content == body, "长报告被截断了"
    assert agent_msgs[0].content.endswith("这一整段不能被截断。")
