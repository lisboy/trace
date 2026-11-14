"""2026-10-28「无效排查」回归测试。

真实事故（run 27a8a2bf，用户只发了一句「出战提示操作失败」+ 一张截图）：
- 49 分钟、348 次工具调用、210 轮 LLM，产品代码 0 改动，最后用户手动取消；
- 其中 search_history 78 次（绝大多数无匹配），把「检索历史」刷成瀑布；
- 写了 23 个 target/_probe_*.mjs 探针脚本，却被 _landed_actions 算作「已落地」；
- 第 108 轮上下文压缩把「用户原始需求 + 截图说明」压没了，模型开始考古找任务。

这里把四条修复各自钉死：
1. 过程文件不算落地；
2. 「要动手还是只勘察」由**模型推理**判定（见 test_intent_*），不再用关键词；
3. search_history 同关键词不重复返回、整轮有上限、并告知真实可搜范围；
4. 上下文压缩必须原样保留第一条用户需求。
后面又补了两条（同一会话的另一半账）：
5. 意图判定走模型推理：survey=只勘察/答疑，modify=动手落地（含「移除…导入…」这类词表外的表达）；
6. 只读查询工具的描述教模型「互不依赖的一次问全」，少几趟串行等待。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from game_agent.agents.tools import ToolContext, is_scratch_path
from game_agent.agents.tools.native import SEARCH_HISTORY_MAX_PER_RUN, search_history
from game_agent.core.context import RunContext
from game_agent.db.models import Message, Project, Session, Team, User
from game_agent.llm.context_window import DIGEST_MARK, compress_messages
from game_agent.orchestration.orchestrator import Orchestrator


def _run(coro, timeout: float = 30):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


# ---------------------------------------------------------------- 1. 过程文件
def test_scratch_paths_are_not_product_landing() -> None:
    """探针/扫描/截图/临时产物都不算产品改动。"""
    for p in ("target/_probe_battle.mjs", "target/_scan_stages2.mjs",
              "target/_verify_reveal_final2.mjs", ".trace/tmp/foo.py",
              ".trace/screenshots/shot.png", "target\\_check_users.mjs"):
        assert is_scratch_path(p), p
    for p in ("web/js/settle.js", "tools/serve.mjs", "src/Shiyuan.Core/Program.cs",
              "docs/DESIGN.md", "content/stages.json"):
        assert not is_scratch_path(p), p


def test_failure_report_counts_as_action_request() -> None:
    """（2026-11-10 起）意图判定改用模型推理，不再靠关键词。

    旧实现用 _ACTION_WORDS 关键词判「要动手」，这里原本钉的是「出战提示操作失败」这类
    现象描述也要触发守卫。现在判定统一走 classify_intent（见 test_intent_model.py），
    本用例仅保留一个不变式：只要不是空串，模型无法判断时按「动手」安全兜底。
    """
    assert Orchestrator._needs_action("出战提示操作失败")  # 兼容壳：非空 → 按动手兜底
    assert not Orchestrator._needs_action("")


def test_fix_task_scope_for_exploration_brake() -> None:
    """意图分流（修 bug vs 创建/生成）改由模型推理给出 mode，不再用关键词。

    这里只钉一个不变式：废弃的 _is_fix_task 兼容壳恒为 False（真正的开关是
    AgentRuntime.engage/fix_task，由编排器从 IntentResult 传入）。
    """
    from game_agent.agents.runtime import _is_fix_task
    assert _is_fix_task("任何内容") is False


# ---------------------------------------------------------------- 1·五 意图盘查只当提示
def test_intent_classification_never_blocks_when_model_down() -> None:
    """盘查保留判别，但**模型不可用时一律放行**（判 modify 兜底），绝不误禁手（2026-11-14）。

    用户选了「保留判别但不作为闸门」：盘查可以判 survey（只换一句软提示），
    但判不出来时必须按「放行」处理——老实现的兜底方向就是对的，这里钉住它别回退。
    """
    import asyncio

    import pytest

    from game_agent.orchestration import intent as intent_mod

    def _boom(*a, **k):  # pragma: no cover - 被调用就说明想调模型
        raise AssertionError("命中关键词就不该再调模型")

    # 明确动手 → 关键词短路判 modify，不调模型、不额外等一次往返
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(intent_mod, "build_llm", _boom, raising=False)
        res = asyncio.run(intent_mod.classify_intent(
            "PmsHistoryBatch 给我移除所有和这个表相关的内容，导入历史项目"))
    assert res.intent == "modify" and res.decided_by == "keyword"

    # 模型挂了 → 兜底放行（modify），不会把「要改」误降级成「只勘察」
    class _Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("down")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(intent_mod, "build_llm", lambda role, ctx=None: _Boom(), raising=False)
        res = asyncio.run(intent_mod.classify_intent("先别改，先看看问题出在哪"))
    assert res.intent == "modify" and res.decided_by == "fallback"


def test_landed_action_guard_needs_model_commitment() -> None:
    """收尾纠正不再按系统预判的意图触发，只看模型自己说过什么。

    「claimed_action」= 汇报/清单里自称已改；命中却零落地才纠正。
    纯结论/方案汇报不该命中，否则又会变成系统逼无关任务去改代码。
    """
    class _Res:
        def __init__(self, text: str):
            self.text = text

    assert Orchestrator._claims_action(_Res("已修改 web/src/App.tsx，改动如下…"), "")
    assert Orchestrator._claims_action(_Res(""), "已生成 6 张立绘，路径在 assets/art/")
    # 纯结论/方案的汇报：不应被当成「说了改了」
    assert not Orchestrator._claims_action(
        _Res("结论：登录失败是因为 token 过期后没有刷新。建议在拦截器里加续期。"), "")




# ---------------------------------------------------------------- 1·六 一次问全
def test_query_tools_advertise_batching() -> None:
    """只读查询工具的描述里要带「同一次一起调用」，否则模型不知道能并行。"""
    from game_agent.agents.tools.registry import build_tool_specs
    specs = {s.name: s.description for s in
             build_tool_specs(["read_file", "grep", "glob", "list_dir", "run_command", "edit_file"])}
    for name in ("read_file", "grep", "glob", "list_dir"):
        assert "一起调用" in specs[name], name
    # 写类/命令类不挂这条提示（同一次里混着改文件是有依赖的）
    assert "一起调用" not in specs["edit_file"]
    assert "一起调用" not in specs["run_command"]


# ---------------------------------------------------------------- 2. search_history
def _tool_ctx(session_id: str) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=session_id,
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None)  # type: ignore[arg-type]
    return ToolContext(run_ctx=rc, workspace=None, role="main", task_id="t")  # type: ignore[arg-type]


async def _new_session(session_factory, user_content: str) -> str:
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as s:
        user = User(email=f"eff-{suffix}@example.com", display_name="效率测试")
        team = Team(name="效率团队", slug=f"eff-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="效率项目", slug=f"eff-{suffix}",
                          repo_path=f"eff-{suffix}", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="效率会话",
                          created_by=user.id)
        s.add(session)
        await s.flush()
        s.add(Message(session_id=session.id, author_type="user", author_id=user.id,
                      content=user_content))
        await s.commit()
        return str(session.id)


def test_search_history_dedupes_and_caps(session_factory) -> None:
    async def _flow():
        session_id = await _new_session(session_factory, "出战提示操作失败")
        t = _tool_ctx(session_id)
        first = await search_history({"keyword": "出战"}, t)
        again = await search_history({"keyword": "出战"}, t)
        misses = [await search_history({"keyword": f"无关词{i}"}, t) for i in range(10)]
        return first, again, misses

    first, again, misses = _run(_flow())
    assert "共 1 条" in first
    assert "本会话共" in first  # 告知真实可搜范围，模型才知道不是数据库
    assert "已搜过" in again  # 同关键词不再重复返回
    assert any("上限" in m for m in misses), misses
    # 上限之后所有调用都不再查库，只返回停用提示
    assert all("无匹配" not in m for m in misses[SEARCH_HISTORY_MAX_PER_RUN - 1:])


def test_search_history_reports_empty_scope(session_factory) -> None:
    async def _flow():
        session_id = await _new_session(session_factory, "你好")
        return await search_history({"keyword": "山门小径"}, _tool_ctx(session_id))

    out = _run(_flow())
    assert "无匹配" in out
    assert "不要换关键词重搜" in out


# ---------------------------------------------------------------- 3. 上下文压缩
def test_compression_keeps_original_requirement() -> None:
    """压缩后第一条用户需求必须原样保留（含截图说明），不能被摘要吞掉。"""
    requirement = ("出战提示操作失败（用户随本条需求附上 1 张图片，已作为视觉输入提供）\n"
                   "截图里连点出战按钮，每条都弹「进军中……」然后是「操作失败」。")
    messages = [{"role": "system", "content": "系统提示"},
                {"role": "user", "content": requirement}]
    # 造 60 轮「读文件」历史，把窗口撑爆
    for i in range(60):
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "read_file",
                                                      "arguments": '{"path": "web/js/settle.js"}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}",
                         "content": "x" * 2000})
    messages.append({"role": "user", "content": "继续"})

    out = _run(compress_messages(messages, 8000, summarizer=None, report={}, tools=None))
    assert any(m.get("role") == "user" and requirement in str(m.get("content"))
               for m in out), "原始需求被压缩掉了"
    # 摘要仍然存在，且位置在原始需求之前
    digest_idx = next(i for i, m in enumerate(out)
                      if m.get("role") == "system" and str(m.get("content", "")).startswith(DIGEST_MARK))
    req_idx = next(i for i, m in enumerate(out)
                   if m.get("role") == "user" and requirement in str(m.get("content")))
    assert digest_idx < req_idx


def test_compression_noop_when_within_budget() -> None:
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "小需求"}]
    assert _run(compress_messages(messages, 128_000, summarizer=None, report={})) == messages


# ---------------------------------------------------------------- 7. 看图不算勘察
def test_image_views_do_not_count_as_exploration() -> None:
    """看图是验收动作，不是「只读勘察」：kpgame 2026-10-29 因为在验图时撞了 90 次
    只读硬停，run 被强制收尾，6 张立绘最后只能写「未能目视确认」。"""
    from game_agent.agents.runtime import _explore_weight
    assert _explore_weight(["view_image", "view_images"]) == 0
    assert _explore_weight(["view_images", "read_file"]) == 1
    assert _explore_weight(["read_file", "grep", "glob"]) == 3
    assert _explore_weight([]) == 0


def test_view_verdict_nudge_points_at_a_file() -> None:
    """看图不落字时要求写 .trace/art-review.md，而不是「写进正文」——
    实测该档位 74/74 轮正文为空，写在思考里的判断下一轮就没了。"""
    from game_agent.agents.runtime import _VIEW_VERDICT_MSG, VIEW_NUDGE_AT
    assert ".trace/art-review.md" in _VIEW_VERDICT_MSG
    assert "{n}" in _VIEW_VERDICT_MSG
    assert _VIEW_VERDICT_MSG.format(n=VIEW_NUDGE_AT)
