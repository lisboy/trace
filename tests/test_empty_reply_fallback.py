"""空收尾兜底回归测试（2026-10-30，用户截图反馈）。

真实事故（run 7c35a9af）：模型干了 92 次工具调用、改了 29 次文件，最后两轮只输出
reasoning、text 为空（completion_tokens=0）。旧逻辑的「空回复重问」只有一次额度，
第 50 轮用掉后，第 54 轮真正收尾再空就直接落成：

    （这次执行没有返回任何内容 —— 端点可能吞掉了回复或调用协议……）

用户看到的上方轨迹全是真实改动，却被一句甩锅文案打发，完全不知道改了什么。

这里钉死四条：
1. 有真实落地动作时，兜底文案按执行轨迹汇总「改了什么」，并明确不是模型原话；
2. 没有任何落地动作时，提示「模型没有输出文字结果」而不是甩锅端点协议；
3. 过程文件（target/_probe 等）不算产品落地，不能进汇总；
4. 空回复重问额度按「有进展的执行段」重置，不会一次用光。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from game_agent.agents.runtime import AgentRuntime, AgentResult, MAX_EMPTY_NUDGES
from game_agent.llm.base import LLM, LLMResponse, ToolCall
from game_agent.orchestration.orchestrator import Orchestrator


def _runtime_stub() -> AgentRuntime:
    """不跑 __init__（会构建真实 LLM），只测纯记账/文案方法。"""
    rt = AgentRuntime.__new__(AgentRuntime)
    rt._trace_files = {}
    rt._trace_assets = []
    rt._trace_commands = 0
    rt._trace_screens = 0
    rt._trace_tool_counts = {}
    return rt


def test_final_text_prefers_model_text() -> None:
    rt = _runtime_stub()
    text, fallback = rt._final_text("已经改好了")
    assert text == "已经改好了"
    assert fallback is False


def test_final_text_without_landing_says_no_model_output() -> None:
    """没落地也没正文：说清「模型没有输出文字结果」，不再断言「端点吞了协议」。"""
    rt = _runtime_stub()
    text, fallback = rt._final_text("")
    assert fallback is True
    assert "模型没有输出文字结果" in text
    assert "端点可能吞掉" not in text


def test_final_text_with_landing_summarizes_real_trace() -> None:
    """有真实改动时按轨迹汇总，用户能看到到底改了什么文件。"""
    rt = _runtime_stub()
    rt._note_trace("edit_file", {"path": "web/style.css"}, "已修改 web/style.css（1 处替换）")
    rt._note_trace("write_file", {"path": "web/js/core.js"}, "已写入 web/js/core.js（新建 120 字符）")
    rt._note_trace("edit_file", {"path": "target/_probe_check.mjs"}, "已修改 target/_probe_check.mjs")
    rt._note_trace("run_command", {"command": "node target/_check.mjs"}, "[exit=0]\nok")
    rt._note_trace("screenshot_page", {"url": "http://127.0.0.1:9998/"}, "截图已保存")

    text, fallback = rt._final_text("")
    assert fallback is True
    assert "系统按本轮执行轨迹汇总，不是模型原话" in text
    assert "web/style.css（修改）" in text
    assert "web/js/core.js（新增/覆盖）" in text
    assert "改动文件 2 个" in text
    # 过程文件不能算产品改动，但命令/截图要作为验证证据出现
    assert "_probe_check" not in text
    assert "运行命令 1 次" in text
    assert "页面截图 1 次" in text


def test_trace_ignores_failed_tool_calls() -> None:
    """工具报错的调用不能记成「已落地」。"""
    rt = _runtime_stub()
    rt._note_trace("edit_file", {"path": "web/style.css"}, "工具错误：未读不许写")
    assert rt._trace_files == {}


def test_asset_trace_lists_generated_media() -> None:
    rt = _runtime_stub()
    rt._note_trace("generate_image", {"slug": "hero_banner"}, "已生成占位图：assets/hero_banner.png")
    text, fallback = rt._final_text("")
    assert fallback is True
    assert "生成素材 1 个" in text
    assert "hero_banner" in text


def test_empty_nudge_budget_is_reset_per_progress_segment() -> None:
    """常量口径：每段有进展的执行都有 2 次重问额度（曾经 bool 一次用光）。"""
    assert MAX_EMPTY_NUDGES >= 2


def test_orchestrator_prefers_report_to_user_summary() -> None:
    """模型通过 report_to_user 汇报过、收尾却只有兜底文案时，用汇报正文而不是兜底。"""
    orch = Orchestrator.__new__(Orchestrator)
    orch._summaries = {"run-1": "## 目标\n改好了\n## 验证\n测试通过"}
    result = AgentResult(text="（系统按本轮执行轨迹汇总，不是模型原话……）", fallback=True)
    assert orch._prefer_summary(result, "run-1").startswith("## 目标")

    # 模型有正常原话时仍以原话为准
    normal = AgentResult(text="模型自己的总结", fallback=False)
    orch._summaries["run-2"] = "旧汇报"
    assert orch._prefer_summary(normal, "run-2") == "模型自己的总结"


class _SilentBrain(LLM):
    """前两轮真实改文件，之后连续只返回 reasoning（复现 run 7c35a9af 的空收尾）。"""

    def __init__(self) -> None:
        self.round = 0

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None):
        self.round += 1
        if self.round == 1:
            return LLMResponse(tool_calls=[ToolCall(
                id=f"call_{self.round}", name="write_file",
                arguments={"path": "feature_empty.py", "content": "# from silent brain\n"})])
        return LLMResponse(text="", reasoning="我还在想怎么改……", usage={})


@pytest.mark.slow
def test_agent_runtime_empty_finish_still_summarizes_real_work(session_factory) -> None:
    """端到端：模型连续空收尾时，最终回复仍是「按轨迹汇总的真实改动」，不是空气泡。"""
    from game_agent.agents.roles import ROLE_TOOLS
    from game_agent.agents.tools import ToolContext
    from game_agent.config import PROJECTS_ROOT
    from game_agent.core.context import RunContext
    from game_agent.db.models import Project, Team, User

    async def _flow() -> str:
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"empty-finish-{suffix}"
        async with session_factory() as s:
            user = User(email=f"empty-{suffix}@example.com", display_name="空收尾")
            team = Team(name="空收尾团队", slug=f"empty-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="空收尾项目", slug=repo_path,
                              repo_path=repo_path, project_type="python", created_by=user.id)
            s.add(project)
            await s.commit()
            team_id, project_id = str(team.id), str(project.id)
        workspace = PROJECTS_ROOT / repo_path
        workspace.mkdir(parents=True, exist_ok=True)
        rc = RunContext(run_id=str(uuid.uuid4()), team_id=team_id, project_id=project_id,
                        session_id=str(uuid.uuid4()), triggered_by=str(uuid.uuid4()),
                        run_type="modify", worker_id="test", repo=None, bus=None,
                        locks=None, store=None)  # type: ignore[arg-type]
        tctx = ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")
        from game_agent.llm.base import Ctx
        ctx = Ctx(role="main", run_type="modify", workspace=str(workspace),
                  requirement="写一个文件")
        rt = AgentRuntime("main", ROLE_TOOLS["main"], "系统提示", "写一个文件", tctx, ctx)
        rt.llm = _SilentBrain()
        result = await rt.run()
        assert (workspace / "feature_empty.py").exists()
        return result.text

    text = asyncio.run(asyncio.wait_for(_flow(), timeout=60))
    assert "系统按本轮执行轨迹汇总，不是模型原话" in text
    assert "feature_empty.py（新增/覆盖）" in text


@pytest.mark.slow
def test_orchestrator_end_to_end_empty_finish_message(session_factory, monkeypatch) -> None:
    """编排器全链路：空收尾的 run 落库的 final_summary / 聊天消息必须是真实轨迹汇总。"""
    from game_agent.agents.roles import ROLE_TOOLS
    from game_agent.config import PROJECTS_ROOT
    from game_agent.core.lite.container import build_lite_services
    from game_agent.db.models import Message, Project, Run, Session, Team, User

    class _SilentBrain(LLM):
        def __init__(self) -> None:
            self.round = 0

        async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None):
            self.round += 1
            if self.round == 1:
                return LLMResponse(tool_calls=[ToolCall(
                    id=f"call_{self.round}", name="write_file",
                    arguments={"path": "feature_orch.py", "content": "# e2e\n"})])
            return LLMResponse(text="", reasoning="思考中……")

    import game_agent.agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "build_llm", lambda role, ctx=None: _SilentBrain())

    async def _flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"empty-orch-{suffix}"
        async with session_factory() as s:
            user = User(email=f"eo-{suffix}@example.com", display_name="空收尾编排")
            team = Team(name="空收尾编排团队", slug=f"eo-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="空收尾编排项目", slug=repo_path,
                              repo_path=repo_path, project_type="python", created_by=user.id)
            s.add(project)
            await s.flush()
            session = Session(team_id=team.id, project_id=project.id, title="空收尾会话",
                              created_by=user.id)
            s.add(session)
            await s.flush()
            msg = Message(session_id=session.id, author_type="user", author_id=user.id,
                          content="给项目加一个文件")
            s.add(msg)
            await s.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                      triggered_by=user.id, trigger_msg_id=msg.id,
                      run_type="modify", status="queued")
            s.add(run)
            await s.commit()
            ids = {"run_id": str(run.id), "session_id": str(session.id), "repo_path": repo_path}
        workspace = PROJECTS_ROOT / repo_path
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "README.md").write_text("# e2e\n", encoding="utf-8")
        svcs = await build_lite_services(session_factory, "empty-orch-worker")
        orch = Orchestrator(svcs, "empty-orch-worker", session_factory)
        await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        messages = await svcs.repo.list_messages(ids["session_id"])
        return run, messages, workspace

    run, messages, workspace = asyncio.run(asyncio.wait_for(_flow(), timeout=90))
    assert (workspace / "feature_orch.py").exists()
    assert run.status == "succeeded"
    assert "系统按本轮执行轨迹汇总，不是模型原话" in (run.final_summary or "")
    assert "feature_orch.py（新增/覆盖）" in (run.final_summary or "")
    agent_msgs = [m for m in messages if m.author_type == "agent"]
    assert agent_msgs, "空收尾也必须给用户一条可读的回复消息"
    assert "端点可能吞掉了回复或调用协议" not in agent_msgs[-1].content
    assert "feature_orch.py" in agent_msgs[-1].content


# ---------------------------------------------------------------- 汇报即收尾
class _ReportThenWorkBrain(LLM):
    """先 report_to_user 汇报，然后还想接着读文件——复现「已汇报？然后下面继续的？」。

    旧行为：report_to_user 只是普通工具，存下汇报后循环继续，模型接着 read_file，
    轨迹上「✓汇报」下面挂一串动作。新行为：清单无未完成项时汇报即收尾，后面那轮
    根本不该被请求。
    """

    def __init__(self) -> None:
        self.round = 0
        self.messages_seen: list[list[dict]] = []

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None):
        self.round += 1
        self.messages_seen.append([dict(m) for m in messages])
        if self.round == 1:
            return LLMResponse(tool_calls=[ToolCall(
                id="c1", name="write_file",
                arguments={"path": "feature_report.py", "content": "# done\n"})])
        if self.round == 2:
            return LLMResponse(tool_calls=[ToolCall(
                id="c2", name="report_to_user",
                arguments={"report": "改好了：新增 feature_report.py，已自测通过。"})])
        # 汇报之后模型还企图继续干活
        return LLMResponse(tool_calls=[ToolCall(
            id="c3", name="read_file",
            arguments={"path": "feature_report.py"})])


@pytest.mark.slow
def test_report_to_user_finalizes_run_when_plan_complete(session_factory) -> None:
    """清单已完成时，report_to_user 就是收尾：汇报后不再请求下一轮、不再执行后续工具。"""
    from game_agent.agents.roles import ROLE_TOOLS
    from game_agent.agents.tools import ToolContext
    from game_agent.config import PROJECTS_ROOT
    from game_agent.core.context import RunContext
    from game_agent.db.models import Project, Team, User
    from game_agent.llm.base import Ctx

    async def _flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"report-finish-{suffix}"
        async with session_factory() as s:
            user = User(email=f"rf-{suffix}@example.com", display_name="汇报收尾")
            team = Team(name="汇报收尾团队", slug=f"rf-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="汇报收尾项目", slug=repo_path,
                              repo_path=repo_path, project_type="python", created_by=user.id)
            s.add(project)
            await s.commit()
            team_id, project_id = str(team.id), str(project.id)
        workspace = PROJECTS_ROOT / repo_path
        workspace.mkdir(parents=True, exist_ok=True)
        rc = RunContext(run_id=str(uuid.uuid4()), team_id=team_id, project_id=project_id,
                        session_id=str(uuid.uuid4()), triggered_by=str(uuid.uuid4()),
                        run_type="modify", worker_id="test", repo=None, bus=None,
                        locks=None, store=None)  # type: ignore[arg-type]
        tctx = ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")

        class _OrchStub:
            def __init__(self) -> None:
                self.summaries: dict[str, str] = {}

            async def set_final_summary(self, run_id: str, text: str) -> None:
                self.summaries[run_id] = text

        tctx.orchestrator = _OrchStub()  # type: ignore[assignment]
        ctx = Ctx(role="main", run_type="modify", workspace=str(workspace),
                  requirement="写一个文件并汇报")
        rt = AgentRuntime("main", ROLE_TOOLS["main"], "系统提示", "写一个文件并汇报", tctx, ctx)
        brain = _ReportThenWorkBrain()
        rt.llm = brain
        result = await rt.run()
        return result, brain, tctx.orchestrator

    result, brain, orch_stub = asyncio.run(asyncio.wait_for(_flow(), timeout=60))
    # 汇报正文成为最终结果（不是工具回执「已汇报给用户。」）
    assert result.text.startswith("改好了"), result.text
    assert result.fallback is False
    # 汇报之后那轮 read_file 根本没被请求：总轮次 = 2（write_file + report_to_user）
    assert brain.round == 2, f"汇报后仍在执行，共请求 {brain.round} 轮"
    # 汇报正文确实存进了 orchestrator（_summaries），收尾时 _prefer_summary 会用它
    assert next(iter(orch_stub.summaries.values())).startswith("改好了")


class _StageReportBrain(LLM):
    """清单还有未完成项时汇报 → 应该被当成阶段汇报，继续做，而不是收工。"""

    def __init__(self) -> None:
        self.round = 0

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None):
        self.round += 1
        if self.round == 1:
            return LLMResponse(tool_calls=[ToolCall(
                id="c1", name="update_plan",
                arguments={"items": [
                    {"content": "第一步", "status": "in_progress"},
                    {"content": "第二步", "status": "pending"},
                ]})])
        if self.round == 2:
            # 只做完第一步就汇报（阶段汇报）
            return LLMResponse(tool_calls=[
                ToolCall(id="c2", name="update_plan",
                         arguments={"items": [
                             {"content": "第一步", "status": "completed"},
                             {"content": "第二步", "status": "pending"},
                         ]}),
                ToolCall(id="c3", name="report_to_user",
                         arguments={"report": "第一步做完了，继续第二步。"}),
            ])
        if self.round == 3:
            return LLMResponse(tool_calls=[
                ToolCall(id="c4", name="update_plan",
                         arguments={"items": [
                             {"content": "第一步", "status": "completed"},
                             {"content": "第二步", "status": "completed"},
                         ]}),
                ToolCall(id="c5", name="report_to_user",
                         arguments={"report": "两步都做完了，交付完成。"}),
            ])
        return LLMResponse(text="不该走到这里")


@pytest.mark.slow
def test_report_to_user_is_stage_report_when_plan_open(session_factory) -> None:
    """清单未完成时的汇报是阶段汇报：继续做，做完后的最终汇报才收尾。"""
    from game_agent.agents.roles import ROLE_TOOLS
    from game_agent.agents.tools import ToolContext
    from game_agent.config import PROJECTS_ROOT
    from game_agent.core.context import RunContext
    from game_agent.db.models import Project, Team, User
    from game_agent.llm.base import Ctx

    async def _flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"stage-report-{suffix}"
        async with session_factory() as s:
            user = User(email=f"sr-{suffix}@example.com", display_name="阶段汇报")
            team = Team(name="阶段汇报团队", slug=f"sr-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="阶段汇报项目", slug=repo_path,
                              repo_path=repo_path, project_type="python", created_by=user.id)
            s.add(project)
            await s.commit()
            team_id, project_id = str(team.id), str(project.id)
        workspace = PROJECTS_ROOT / repo_path
        workspace.mkdir(parents=True, exist_ok=True)
        rc = RunContext(run_id=str(uuid.uuid4()), team_id=team_id, project_id=project_id,
                        session_id=str(uuid.uuid4()), triggered_by=str(uuid.uuid4()),
                        run_type="modify", worker_id="test", repo=None, bus=None,
                        locks=None, store=None)  # type: ignore[arg-type]
        tctx = ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")

        class _OrchStub:
            def __init__(self) -> None:
                self.summaries: dict[str, str] = {}
                self.plan: list[dict] = []

            async def set_final_summary(self, run_id: str, text: str) -> None:
                self.summaries[run_id] = text

            async def update_plan(self, run_ctx, items, summary="", agent_key="main"):
                self.plan = [dict(i) for i in items]
                return items

        orch = _OrchStub()
        tctx.orchestrator = orch  # type: ignore[assignment]

        class _RunStub:
            plan_json = []

        posted: list[dict] = []

        class _RepoStub:
            async def get_run(self, _run_id):
                _RunStub.plan_json = orch.plan
                return _RunStub()

            async def create_message(self, **kw):
                # 阶段汇报会落一条 agent 消息：捕获下来供断言
                posted.append(kw)

                class _Msg:
                    id = uuid.uuid4()

                return _Msg()

        rc.repo = _RepoStub()  # type: ignore[assignment]
        ctx = Ctx(role="main", run_type="modify", workspace=str(workspace),
                  requirement="分两步做一个功能")
        rt = AgentRuntime("main", ROLE_TOOLS["main"], "系统提示", "分两步做一个功能", tctx, ctx)
        brain = _StageReportBrain()
        rt.llm = brain
        result = await rt.run()
        return result, brain, posted

    result, brain, posted = asyncio.run(asyncio.wait_for(_flow(), timeout=60))
    # 阶段汇报不算收尾：第 3 轮（做完第二步）必须被请求到
    assert brain.round == 3, f"阶段汇报被当成收尾了，共请求 {brain.round} 轮"
    assert result.text.startswith("两步都做完了"), result.text
    # 阶段汇报要在会话流里落一条完整的 Markdown 消息（方案 2）：
    # 正文 = 那次 report_to_user 的 report；meta.kind=stage_report；带未完成项数
    stage_msgs = [m for m in posted if (m.get("meta_json") or {}).get("kind") == "stage_report"]
    assert len(stage_msgs) == 1, f"阶段汇报应恰好落一条消息，实际 {len(stage_msgs)}"
    sm = stage_msgs[0]
    assert sm["content"].startswith("第一步做完了"), sm["content"]
    assert sm["author_type"] == "agent"
    assert sm["meta_json"]["open_count"] == 1  # 第二步仍未完成
    # 最终汇报「两步都做完了」是收尾，不作为阶段汇报再落一条
    assert not any(
        (m.get("meta_json") or {}).get("kind") == "stage_report"
        and m["content"].startswith("两步都做完了") for m in posted
    )
