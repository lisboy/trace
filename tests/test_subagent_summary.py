# -*- coding: utf-8 -*-
"""子agent 结论回传链路 + 后台派发（2026-11-12 用户要求）。

用真实 Orchestrator + 脚本化大脑钉死：
  1. spawn_agents 派发即返回：主agent 不被整批子agent 堵住，能继续跑自己的轮次；
  2. 子agent 收工后结论自动注入主agent 对话（模型下一轮就能汇总）；
  3. task_done(role=sub) 事件带 summary —— 前端弹窗/行内预览据此显示「子agent 的结论」；
  4. 子agent 的汇报不会顶掉主agent 的最终汇报（_sub_summaries 与 _summaries 分离）；
  5. 子agent 还在跑时用户插话 → 主agent 立刻醒来处理（不再等整批结束）。
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
from game_agent.orchestration import steering
from game_agent.orchestration.orchestrator import Orchestrator

RUN_TIMEOUT_S = 120

MAIN_REPORT = "## 总汇报\n\n两个子agent 的结论都收到了，结论一致：未发现问题。"
WAIT_TEXT = "两个子agent 正在后台核对，结论回来我马上汇总。"
SUB_CONCLUSIONS = {
    "核对 A": "## 子agent 结论\n\n核对 A：读了 a.py:1-20，未发现问题。",
    "核对 B": "## 子agent 结论\n\n核对 B：读了 b.py:5-9，发现 1 处风险（b.py:7）。",
}


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 90) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


def _tc(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)


async def _seed(session_factory, requirement: str) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    repo_path = f"sub-summary-{suffix}"
    async with session_factory() as s:
        user = User(email=f"ss-{suffix}@example.com", display_name="子结论")
        team = Team(name="子结论团队", slug=f"ss-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="子结论项目", slug=repo_path,
                          repo_path=repo_path, project_type="python", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="子结论会话",
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
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "b.py").write_text("y = 2\n", encoding="utf-8")
    return ids


class _MainBrain(LLM):
    """第 1 轮派两个子agent；第 2 轮先说「在等」；第 3 轮（结论已回收）汇报收尾。"""

    def __init__(self) -> None:
        self.tool_rounds = 0
        self.messages_seen: list[list[dict]] = []

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        if not tools:  # 收尾后的记忆/经验沉淀判定
            return LLMResponse(text='{"worth": false, "reason": "replay", "entries": []}',
                               usage={})
        self.tool_rounds += 1
        self.messages_seen.append([dict(m) for m in messages])
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        if self.tool_rounds == 1:
            return LLMResponse(tool_calls=[_tc(
                "spawn_agents",
                items=[{"name": "查A", "question": "核对 A"},
                       {"name": "查B", "question": "核对 B"}])], usage=usage)
        if self.tool_rounds == 2:
            # 子agent 还在后台跑：只吐正文 → 运行时落成临时回复并等批次回收
            return LLMResponse(text=WAIT_TEXT, usage=usage)
        return LLMResponse(tool_calls=[_tc("report_to_user", report=MAIN_REPORT)],
                           usage=usage)


class _SubBrain(LLM):
    """子agent：先慢慢核对（sleep 模拟真实耗时），再 report_to_user 交结论。"""

    def __init__(self, instruction: str, delay: float = 0.5) -> None:
        self.instruction = instruction
        self.delay = delay

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        body = SUB_CONCLUSIONS.get(self.instruction.split("\n")[0].strip())
        if body is None:
            body = f"## 子agent 结论\n\n{self.instruction}：未发现问题。"
        return LLMResponse(tool_calls=[_tc("report_to_user", report=body)],
                           usage={"prompt_tokens": 50, "completion_tokens": 20})


async def _run_with_brains(session_factory, requirement: str, main_brain: LLM,
                           sub_delay: float = 0.5, interject: str = "",
                           interject_after: float = 0.25):
    """装配真实 Orchestrator 跑一轮，返回 (run, tasks, events, messages)。"""
    from unittest.mock import patch
    import game_agent.agents.runtime as rt_mod
    import game_agent.orchestration.orchestrator as orch_mod
    from game_agent.orchestration.intent import IntentResult

    def _factory(role, ctx=None):  # noqa: ANN001, ANN202
        if role == "sub":
            return _SubBrain(getattr(ctx, "requirement", "") or "", sub_delay)
        return main_brain

    async def _classify(req, profile=None, **k):  # noqa: ANN001, ANN202
        # survey：本轮按只读调研跑，避免「没落地 → 强制纠正重跑」干扰脚本
        return IntentResult(intent="survey", mode="build", reason="test")

    ids = await _seed(session_factory, requirement)
    svcs = await build_lite_services(session_factory, "ss-worker")
    orch = Orchestrator(svcs, "ss-worker", session_factory)
    patches = [patch.object(rt_mod, "build_llm", _factory),
               patch.object(orch_mod, "build_llm", _factory),
               patch.object(orch_mod, "classify_intent", _classify)]
    for p in patches:
        p.start()

    async def _inject() -> None:  # 模拟用户在子agent 还在跑时插话
        await asyncio.sleep(interject_after)
        steering.push(ids["run_id"], interject)

    injector = asyncio.create_task(_inject()) if interject else None
    try:
        await orch.execute_run(ids["run_id"])
    finally:
        if injector is not None:
            injector.cancel()
        for p in patches:
            p.stop()
    await asyncio.sleep(0.2)  # 事件是 fire-and-forget 落库的
    run = await svcs.repo.get_run(ids["run_id"])
    tasks = await svcs.repo.list_tasks(ids["run_id"])
    events = await svcs.repo.get_events_since(ids["run_id"], 0)
    return run, tasks, events


@pytest.mark.slow
def test_sub_summary_back_to_main_and_ui_events(session_factory) -> None:
    main_brain = _MainBrain()
    run, tasks, events = _run(
        _run_with_brains(session_factory, "并行核对两个文件", main_brain),
        timeout=RUN_TIMEOUT_S)

    # 1) 主agent 的最终汇报不被子agent 顶掉
    assert run.status == "succeeded", f"{run.status} {run.error}"
    assert MAIN_REPORT in (run.final_summary or ""), run.final_summary

    # 2) 结论不是同步返回的：spawn 的回执只说「已派出」（第 2 轮才进上下文），
    #    结论在后续轮次作为 user 消息注入；主agent 收尾前那一轮一定已经看到两份结论。
    round2 = "\n\n".join(str(m.get("content")) for m in main_brain.messages_seen[1]
                         if m.get("role") == "tool")
    assert "已派出 2 个只读子agent" in round2, round2[:500]
    last_msgs = "\n\n".join(str(m.get("content")) for m in main_brain.messages_seen[-1])
    assert "已全部回收" in last_msgs, last_msgs[-800:]
    for body in SUB_CONCLUSIONS.values():
        assert body in last_msgs, f"主agent 没收到子agent 结论：{body}"

    # 3) 等待期间主agent 的过渡答复落成了 interim_reply（聊天流可见，不算收尾）
    assert any(e.type == "message_new" and (e.payload_json or {}).get("kind") == "interim_reply"
               for e in events), "缺少 interim_reply 消息"

    # 4) task_done(role=sub) 带 summary（前端显示数据源），且与任务记录同源
    sub_done = [e for e in events
                if e.type == "task_done"
                and (e.payload_json or {}).get("role") == "sub"]
    assert len(sub_done) == 2, sub_done
    for e in sub_done:
        assert (e.payload_json or {}).get("summary") in SUB_CONCLUSIONS.values(), e.payload_json
    sub_tasks = [t for t in tasks if t.agent_key == "sub"]
    assert len(sub_tasks) == 2
    for t in sub_tasks:
        assert (t.result_json or {}).get("text") in SUB_CONCLUSIONS.values(), t.result_json
        assert t.title.startswith("子agent · "), t.title


class _InterjectMainBrain(LLM):
    """派完子agent 后停在「等结论」；插话到达后必须能在自己的上下文里看到它。"""

    def __init__(self, note: str) -> None:
        self.note = note
        self.tool_rounds = 0
        self.saw_interjection = False

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        if not tools:
            return LLMResponse(text='{"worth": false, "reason": "replay", "entries": []}',
                               usage={})
        self.tool_rounds += 1
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        if self.tool_rounds == 1:
            return LLMResponse(tool_calls=[_tc(
                "spawn_agents",
                items=[{"name": "查A", "question": "核对 A"},
                       {"name": "查B", "question": "核对 B"}])], usage=usage)
        joined = "\n".join(str(m.get("content")) for m in messages)
        self.saw_interjection = self.saw_interjection or self.note in joined
        if self.tool_rounds == 2:
            return LLMResponse(text=WAIT_TEXT, usage=usage)
        return LLMResponse(tool_calls=[_tc("report_to_user", report=MAIN_REPORT)],
                           usage=usage)


@pytest.mark.slow
def test_interjection_handled_while_subagents_running(session_factory) -> None:
    """子agent 还在跑时插话：主agent 立刻醒来看到这句话（而不是等整批结束）。"""
    note = "顺便再看一下 c.py 的旧版本"
    brain = _InterjectMainBrain(note)
    run, tasks, events = _run(
        _run_with_brains(session_factory, "并行核对两个文件", brain,
                         sub_delay=1.2, interject=note),
        timeout=RUN_TIMEOUT_S)

    assert run.status == "succeeded", f"{run.status} {run.error}"
    assert brain.saw_interjection, "主agent 的上下文里没有出现用户插话（等整批结束才处理 = 失败）"
    assert MAIN_REPORT in (run.final_summary or ""), run.final_summary
    # 插话在子agent 收工前就被看见：主agent 至少跑过一轮在「子agent 未回收」的上下文里
    sub_done = [e for e in events
                if e.type == "task_done" and (e.payload_json or {}).get("role") == "sub"]
    assert len(sub_done) == 2, sub_done
