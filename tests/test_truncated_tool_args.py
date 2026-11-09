# -*- coding: utf-8 -*-
"""「工具参数被截断 → 空参数重试三连 → 死循环掐停 → 文档没落盘」修复回归。

真实事故（2026-11-06 山西可研 run）：
  模型想把一份很长的评估文档一次 write_file 落盘，参数 JSON 在生成/传输时被截断；
  适配层**静默**把解析失败吞成 arguments={}，write_file 于是抛 KeyError:'path'，
  返回一条没营养的「工具错误」；模型原样重发同样的巨型调用三次 → 三次相同签名被
  死循环检测掐停 → 文档根本没落盘，最后只能口述「评估文档尚未落盘，这是没做完的部分」。

修复要点（分层钉死）：
  1. 适配层：参数解析失败先按 strict=False 放宽（裸换行能救回来），救不回来就带
     arguments_error 原因，绝不静默当空参数；
  2. write_file：缺 path/content 给明确报错，且支持 mode=append 以分块写长文档；
  3. runtime：arguments_error 的调用不执行、给「分块写」纠正、且不计入死循环签名；
  4. 落地判定：参数没解析出来的调用不算落地（不能把一次失败的写当成「改过代码」）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Coroutine

import pytest

from game_agent.config import PROJECTS_ROOT
from game_agent.llm.base import parse_tool_arguments


# ---------------- 1. 适配层参数解析 ----------------

def test_strict_fallback_recovers_unescaped_newlines() -> None:
    """正文里带未转义的裸换行（大段文档最常见）——严格 json 拒，放宽后要能救回来。"""
    raw = '{"path": "docs/评估.md", "content": "第一行\n第二行\n第三行"}'
    args, err = parse_tool_arguments(raw)
    assert err == "", err
    assert args["path"] == "docs/评估.md"
    assert "第一行\n第二行" in args["content"]


def test_truncated_json_reports_reason_instead_of_silent_empty() -> None:
    """被长度上限截断的 JSON → 不返回参数，但要带回原因（供 runtime 给纠正）。"""
    raw = '{"path": "docs/评估.md", "content": "' + "正文" * 500  # 没有收尾
    args, err = parse_tool_arguments(raw)
    assert args == {}
    assert err, "截断必须报错，不能静默当空参数"
    assert "截断" in err or "解析失败" in err


def test_empty_arguments_is_not_an_error() -> None:
    args, err = parse_tool_arguments("")
    assert args == {} and err == ""


# ---------------- 2. write_file：明确报错 + append 分块 ----------------

def _ctx(workspace):
    from game_agent.agents.tools import ToolContext
    from game_agent.core.context import RunContext
    run_ctx = RunContext(run_id="fake", team_id="t", project_id="p", session_id="s",
                         triggered_by="u", run_type="modify", worker_id="w",
                         repo=None, bus=None, locks=None, store=None)
    return ToolContext(run_ctx=run_ctx, workspace=workspace, role="main", task_id="tk")


def test_write_file_missing_path_says_so(tmp_path) -> None:
    from game_agent.agents.tools.native import write_file
    from game_agent.agents.tools import ToolError
    t = _ctx(tmp_path)
    with pytest.raises(ToolError) as ei:
        asyncio.run(write_file({}, t))
    assert "path" in str(ei.value)


def test_write_file_append_builds_a_long_document(tmp_path) -> None:
    """分块写：第一块 write，后续块 append，最终文件是完整拼接。"""
    from game_agent.agents.tools.native import write_file
    t = _ctx(tmp_path)
    asyncio.run(write_file({"path": "docs/report.md", "content": "# 报告\n\n第一段\n"}, t))
    asyncio.run(write_file({"path": "docs/report.md", "content": "第二段\n",
                            "mode": "append"}, t))
    asyncio.run(write_file({"path": "docs/report.md", "content": "第三段\n",
                            "mode": "append"}, t))
    text = (tmp_path / "docs" / "report.md").read_text(encoding="utf-8")
    assert text == "# 报告\n\n第一段\n第二段\n第三段\n"


def test_write_file_append_allows_rewriting_without_preread(tmp_path) -> None:
    """append 不清空原内容，不需要先读；整体覆盖仍需先读。"""
    from game_agent.agents.tools.native import write_file
    from game_agent.agents.tools import ToolError
    t = _ctx(tmp_path)
    (tmp_path / "exist.md").write_text("原有内容\n", encoding="utf-8")
    # 没读过就整体覆盖 → 拒绝
    with pytest.raises(ToolError):
        asyncio.run(write_file({"path": "exist.md", "content": "x"}, t))
    # append 允许
    asyncio.run(write_file({"path": "exist.md", "content": "追加\n", "mode": "append"}, t))
    assert (tmp_path / "exist.md").read_text(encoding="utf-8") == "原有内容\n追加\n"


# ---------------- 3/4. 端到端：参数截断不再掐停、文档最终能落盘 ----------------

def _run(coro: Coroutine[Any, Any, Any], timeout: float = 180) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


async def _seed(session_factory, requirement: str) -> dict[str, str]:
    from game_agent.db.models import Message, Project, Run, Session, Team, User
    suffix = uuid.uuid4().hex[:8]
    repo_path = f"trunc-args-{suffix}"
    async with session_factory() as s:
        user = User(email=f"ta-{suffix}@example.com", display_name="截断参数")
        team = Team(name="截断参数团队", slug=f"ta-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="截断参数项目", slug=repo_path,
                          repo_path=repo_path, project_type="python", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="截断参数会话",
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
    (workspace / "README.md").write_text("# 截断参数项目\n", encoding="utf-8")
    return ids


class _TruncatingBrain:
    """第 1、2 轮吐「参数解析失败」的 write_file；第 3 轮改成分块 append 写成功并汇报。"""

    def __init__(self) -> None:
        self.agent_rounds = 0
        self.saw_arg_error_hint = False

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None,
                   *, timeout_s=None, max_attempts=None):
        from game_agent.llm.base import LLMResponse, ToolCall
        if not tools:  # 收尾后的经验沉淀判定
            return LLMResponse(text='{"worth": false, "reason": "重放测试", "entries": []}')
        self.agent_rounds += 1
        rnd = self.agent_rounds
        # 检查纠正提示确实进了上下文（第 2 轮起应能看到 _ARG_RETRY_HINT 的关键词）
        joined = "\n".join(str(m.get("content") or "") for m in messages)
        if "参数 JSON 没解析成功" in joined or "分块" in joined:
            self.saw_arg_error_hint = True
        if rnd in (1, 2):
            # 模拟「内容太长被截断」：arguments 解析失败，arguments_error 非空
            return LLMResponse(tool_calls=[ToolCall(
                id=f"c{rnd}", name="write_file", arguments={},
                arguments_error="工具参数 JSON 解析失败（大概率被长度上限截断了）")])
        if rnd == 3:
            return LLMResponse(tool_calls=[
                ToolCall(id="c3a", name="write_file",
                         arguments={"path": "docs/eval.md", "content": "# 评估\n\n第一段\n"}),
                ToolCall(id="c3b", name="write_file",
                         arguments={"path": "docs/eval.md", "content": "第二段\n", "mode": "append"}),
            ])
        if rnd == 4:
            return LLMResponse(tool_calls=[ToolCall(
                id="c4", name="report_to_user",
                arguments={"report": "## 完成\n\n评估文档已分块落盘 docs/eval.md。"})])
        return LLMResponse(text="不应到这里")


@pytest.mark.slow
def test_truncated_args_nudge_then_chunked_write_lands(session_factory) -> None:
    from unittest.mock import patch
    import game_agent.agents.runtime as rt_mod
    import game_agent.orchestration.orchestrator as orch_mod
    from game_agent.core.lite.container import build_lite_services
    from game_agent.orchestration.orchestrator import Orchestrator

    brain = _TruncatingBrain()

    def _factory(role, ctx=None):  # noqa: ANN001, ANN202
        return brain

    async def _flow():
        ids = await _seed(session_factory, "帮我写一份很长的评估文档并落盘")
        svcs = await build_lite_services(session_factory, "ta-worker")
        orch = Orchestrator(svcs, "ta-worker", session_factory)
        with patch.object(rt_mod, "build_llm", _factory), \
                patch.object(orch_mod, "build_llm", _factory):
            await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        return run, ids, svcs

    run, ids, svcs = _run(_flow())

    workspace = PROJECTS_ROOT / ids["repo_path"]
    landed = workspace / "docs" / "eval.md"
    # 关键闭环：文档真的落到了磁盘（不再是「口述说没落盘」）
    assert landed.exists(), "评估文档没有真正落盘"
    assert landed.read_text(encoding="utf-8") == "# 评估\n\n第一段\n第二段\n"
    # 参数截断被当成「换写法重做」，没有直接掐停成收尾
    assert brain.saw_arg_error_hint, "截断后没有给模型分块写的纠正提示"
    assert run.status == "succeeded", f"{run.status} {run.error}"
    # 死循环检测没有把前两次空参数调用当成「三次相同调用」掐停
    assert brain.agent_rounds >= 4, f"过早收尾，仅 {brain.agent_rounds} 轮"


# ---------------- 落地判定：解析失败的写不算落地 ----------------

def test_failed_arg_write_is_not_counted_as_landed(session_factory) -> None:
    """参数没解析出来的 write_file 不能被 _landed_actions 算成「改过产品代码」。"""
    from game_agent.core.lite.container import build_lite_services
    from game_agent.orchestration.orchestrator import Orchestrator

    async def _flow():
        ids = await _seed(session_factory, "随便")
        svcs = await build_lite_services(session_factory, "ta-worker")
        orch = Orchestrator(svcs, "ta-worker", session_factory)
        from game_agent.core.abstractions import EventEnvelope
        async def _ev(payload):
            await svcs.repo.append_event(EventEnvelope(
                team_id=ids["team_id"], project_id=ids["project_id"],
                run_id=ids["run_id"], task_id=None, type="tool_call", payload=payload))
        await _ev({"tool": "write_file", "args": {}, "arguments_error": "解析失败"})
        n_bad = await orch._landed_actions(ids["run_id"])
        await _ev({"tool": "write_file", "args": {"path": "src/a.py", "content": "x"}})
        n_good = await orch._landed_actions(ids["run_id"])
        return n_bad, n_good

    n_bad, n_good = _run(_flow())
    assert n_bad == 0, "参数解析失败的写被误算成落地了"
    assert n_good == 1
