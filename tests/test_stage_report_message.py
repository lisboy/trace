# -*- coding: utf-8 -*-
"""阶段汇报落消息（方案 2）：清单未完成时 report_to_user 必须在会话流里落一条完整的
Markdown「阶段汇报」消息，然后继续执行；最终汇报才收尾。

这条端到端用例跑完整 Orchestrator.execute_run，锁住：
  · 阶段汇报确实落成一条 meta.kind=stage_report 的 agent 消息（正文=report 入参）；
  · 该消息带 open_count，且不影响 run 继续跑完；
  · 最终汇报才是收尾、不作为阶段汇报再落一条。
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
    repo_path = f"stage-msg-{suffix}"
    async with session_factory() as s:
        user = User(email=f"stage-{suffix}@example.com", display_name="阶段汇报")
        team = Team(name="阶段汇报团队", slug=f"stage-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="阶段汇报项目", slug=repo_path,
                          repo_path=repo_path, project_type="python", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="阶段汇报会话",
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
    (workspace / "README.md").write_text("# 阶段汇报项目\n", encoding="utf-8")
    return ids


class _StageBrain(LLM):
    """一轮先做一半就汇报（阶段汇报），下一轮做完再汇报（收尾）。"""

    def __init__(self) -> None:
        self.round = 0
        self.agent_rounds = 0  # 只数「带工具/主要任务」的往返，排除 run 收尾后的经验沉淀调用

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        # 收尾后 orchestrator 会再用同一个 llm 做一次「值得沉淀改动经验吗」的判定，
        # 那一次 tools 为空、消息也是另一段 prompt，不能算进 agent 的轮次。
        if tools:
            self.agent_rounds += 1
            rnd = self.agent_rounds
        else:
            rnd = -1
        self.round += 1
        if rnd == 1:
            return LLMResponse(tool_calls=[
                _tc("list_dir", path="."),
                _tc("write_file", path="step1.py", content="# step 1\n"),
                _tc("update_plan", items=[
                    {"content": "第一步", "status": "completed"},
                    {"content": "第二步", "status": "pending"},
                ]),
            ], usage={"prompt_tokens": 100, "completion_tokens": 50})
        if rnd == 2:
            return LLMResponse(tool_calls=[
                _tc("report_to_user", report="## 阶段汇报\n\n第一步已完成，继续第二步。"),
            ], usage={"prompt_tokens": 100, "completion_tokens": 50})
        if rnd == 3:
            return LLMResponse(tool_calls=[
                _tc("read_file", path="step1.py"),
                _tc("write_file", path="step2.py", content="# step 2\n"),
                _tc("update_plan", items=[
                    {"content": "第一步", "status": "completed"},
                    {"content": "第二步", "status": "completed"},
                ]),
            ], usage={"prompt_tokens": 100, "completion_tokens": 50})
        if rnd == 4:
            return LLMResponse(tool_calls=[
                _tc("report_to_user", report="## 全部完成\n\n两步都做完了，交付完成。"),
            ], usage={"prompt_tokens": 100, "completion_tokens": 50})
        if rnd == -1:  # 收尾后的「改动经验沉淀」判定：返回一个不值得记的 JSON
            return LLMResponse(text='{"worth": false, "reason": "冒烟测试", "entries": []}',
                               usage={})
        return LLMResponse(text="不该走到这里", usage={})


@pytest.mark.slow
def test_stage_report_posts_message_then_continues(session_factory) -> None:
    from unittest.mock import patch
    import game_agent.agents.runtime as rt_mod
    import game_agent.orchestration.orchestrator as orch_mod

    brain = _StageBrain()

    def _factory(role, ctx=None):  # noqa: ANN001, ANN202
        return brain

    async def _flow():
        ids = await _seed(session_factory, "分两步做一个功能")
        svcs = await build_lite_services(session_factory, "stage-worker")
        orch = Orchestrator(svcs, "stage-worker", session_factory)
        with patch.object(rt_mod, "build_llm", _factory), \
                patch.object(orch_mod, "build_llm", _factory):
            await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        messages = await svcs.repo.list_messages(ids["session_id"])
        return run, messages, brain

    run, messages, brain = _run(_flow(), timeout=RUN_TIMEOUT_S)

    # 阶段汇报没有把 run 截断：最终报告在第 4 轮才收尾
    assert brain.agent_rounds == 4, f"阶段汇报被当成收尾了，共请求 {brain.agent_rounds} 轮"
    assert run.status == "succeeded", f"run 未成功：{run.status} {run.error}"
    assert "全部完成" in (run.final_summary or ""), run.final_summary

    stage_msgs = [m for m in messages
                  if m.author_type == "agent"
                  and (m.meta_json or {}).get("kind") == "stage_report"]
    assert len(stage_msgs) == 1, f"阶段汇报应恰好落一条消息，实际 {len(stage_msgs)}: {messages}"
    sm = stage_msgs[0]
    assert "阶段汇报" in sm.content and "第一步已完成" in sm.content
    assert sm.meta_json.get("open_count") == 1
    # 最终汇报不再作为阶段汇报落消息
    assert not any("全部完成" in m.content for m in stage_msgs)
    # 最终 agent 回复里能看到「全部完成」（收尾正文）
    assert any(m.author_type == "agent" and "全部完成" in m.content for m in messages)
