"""工具结果记忆（同 run 内同工具同参数只真跑一次）的回归测试。

依据：近 40 个 run 的 2093 轮里有 311 轮（15%）含完全重复的调用，重复 grep/read 大仓库一次
几百毫秒到几秒，重复结果还会再塞一遍上下文。但缓存必须**只对只读工具**、且**写操作后立刻作废**，
否则「改完再读还是旧内容」会比不缓存更糟——这两点就是本文件要钉死的东西。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Coroutine

import pytest

import game_agent.agents.runtime as rt_mod
from game_agent.agents.runtime import _memo_sig
from game_agent.config import PROJECTS_ROOT
from game_agent.db.models import Message, Project, Run, Session, Team, User


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 180) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


async def _seed(session_factory, requirement: str) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    repo_path = f"memo-{suffix}"
    async with session_factory() as s:
        user = User(email=f"memo-{suffix}@example.com", display_name="记忆测试")
        team = Team(name="记忆团队", slug=f"memo-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="记忆项目", slug=repo_path, repo_path=repo_path,
                          project_type="python", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="记忆会话",
                          created_by=user.id)
        s.add(session)
        await s.flush()
        message = Message(session_id=session.id, author_type="user",
                          author_id=user.id, content=requirement)
        s.add(message)
        await s.flush()
        run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                  triggered_by=user.id, trigger_msg_id=message.id,
                  run_type="modify", status="queued")
        s.add(run)
        await s.commit()
        run_id, session_id = run.id, session.id
    workspace = PROJECTS_ROOT / repo_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.txt").write_text("v1\n", encoding="utf-8")
    return {"run_id": run_id, "session_id": session_id, "repo_path": repo_path}


class _MemoBrain:
    """第 1、2 轮读同一个文件；第 3 轮写；第 4 轮再读（必须看到新内容，不能吃旧缓存）。"""

    def __init__(self) -> None:
        self.round = 0
        self.memo_hits = 0
        self.saw_fresh_after_write = False

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        from game_agent.llm.base import LLMResponse, ToolCall

        if not tools:  # 收尾/经验沉淀之类的附带调用
            return LLMResponse(text='{"worth": false, "reason": "测试", "entries": []}')

        # 让假大脑看得见上一轮的工具结果
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "本次未重新执行" in joined:
            self.memo_hits = max(self.memo_hits, joined.count("本次未重新执行"))
        if "v2" in joined:
            self.saw_fresh_after_write = True

        self.round += 1
        if self.round == 1:
            return LLMResponse(tool_calls=[ToolCall(
                id="r1", name="read_file", arguments={"path": "a.txt"})])
        if self.round == 2:
            # 完全相同的参数：应命中记忆，不再真读
            return LLMResponse(tool_calls=[ToolCall(
                id="r2", name="read_file", arguments={"path": "a.txt"})])
        if self.round == 3:
            return LLMResponse(tool_calls=[ToolCall(
                id="r3", name="write_file",
                arguments={"path": "a.txt", "content": "v2\n"})])
        if self.round == 4:
            # 写完再读同一路径：记忆必须已作废，读到 v2
            return LLMResponse(tool_calls=[ToolCall(
                id="r4", name="read_file", arguments={"path": "a.txt"})])
        return LLMResponse(tool_calls=[ToolCall(
            id="r5", name="report_to_user",
            arguments={"report": "## 完成\n\n已读过、写过、再读过 a.txt。"})])


@pytest.mark.slow
def test_repeat_read_is_memoized_and_invalidated_by_write(session_factory) -> None:
    from unittest.mock import patch
    import game_agent.orchestration.orchestrator as orch_mod
    from game_agent.core.lite.container import build_lite_services
    from game_agent.orchestration.orchestrator import Orchestrator

    brain = _MemoBrain()

    def _factory(role, ctx=None):  # noqa: ANN001, ANN202
        return brain

    async def _flow():
        ids = await _seed(session_factory, "读两次同一个文件，改一下，再读一次")
        svcs = await build_lite_services(session_factory, "memo-worker")
        orch = Orchestrator(svcs, "memo-worker", session_factory)
        with patch.object(rt_mod, "build_llm", _factory), \
                patch.object(orch_mod, "build_llm", _factory):
            await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        return run, ids

    run, ids = _run(_flow())

    assert brain.memo_hits >= 1, "完全相同的第二次 read_file 没有命中结果记忆"
    assert brain.saw_fresh_after_write, "写操作之后没有读到新内容（记忆没作废，最危险的回归）"
    assert run.status == "succeeded", f"{run.status} {run.error}"
    landed = PROJECTS_ROOT / ids["repo_path"] / "a.txt"
    assert landed.read_text(encoding="utf-8") == "v2\n"


def test_memo_sig_is_exact_not_normalized() -> None:
    """记忆签名必须区分 offset：翻页读到的是不同内容，不能共用一份缓存。"""
    a = _memo_sig({"path": "x.py", "offset": 300, "limit": 120})
    b = _memo_sig({"path": "x.py", "offset": 420, "limit": 120})
    assert a != b, "不同 offset 必须是不同签名"
    # 键序不影响
    assert _memo_sig({"path": "x.py", "n": 1}) == _memo_sig({"n": 1, "path": "x.py"})
    # 非 JSON 类型也不能炸
    assert isinstance(_memo_sig({"p": object()}), str)


def test_sub_role_gets_no_thinking_by_default_but_main_keeps_default(monkeypatch) -> None:
    """sub 只读调研默认关思考（实测 -32% 延迟、-61% 输出 token）；主agent 不替用户降质量。"""
    from game_agent.config import LLM_SUB_MAX_OUTPUT_TOKENS
    from game_agent.llm.factory import build_llm

    sub = build_llm("sub", None)
    main = build_llm("main", None)
    assert sub.reasoning_effort == "none", sub.reasoning_effort
    assert main.reasoning_effort == "", "主agent 不能默认关思考（质量取舍归用户）"
    # 输出上限默认不设：截断会把工具调用 JSON 切碎（实测 sub 的长输出全是思考 token，
    # text/arguments 都很短，硬砍只会让整轮参数解析失败）
    assert LLM_SUB_MAX_OUTPUT_TOKENS == 0
    assert sub.max_tokens == 0 and main.max_tokens == 0

    # 档位显式配了就以档位为准（前端里给某个档位设 high 不被覆盖）
    ctx = type("C", (), {"model_profile": {"params": {"reasoning_effort": "high",
                                                      "max_tokens": 1234}}})()
    llm = build_llm("sub", ctx)
    assert llm.reasoning_effort == "high"
    assert llm.max_tokens == 1234
