"""2026-10-28「无效排查」回归测试。

真实事故（run 27a8a2bf，用户只发了一句「出战提示操作失败」+ 一张截图）：
- 49 分钟、348 次工具调用、210 轮 LLM，产品代码 0 改动，最后用户手动取消；
- 其中 search_history 78 次（绝大多数无匹配），把「检索历史」刷成瀑布；
- 写了 23 个 target/_probe_*.mjs 探针脚本，却被 _landed_actions 算作「已落地」；
- 第 108 轮上下文压缩把「用户原始需求 + 截图说明」压没了，模型开始考古找任务。

这里把四条修复各自钉死：
1. 过程文件不算落地；
2. 故障描述（「…操作失败」）必须触发「只勘察不动手」守卫；
3. search_history 同关键词不重复返回、整轮有上限、并告知真实可搜范围；
4. 上下文压缩必须原样保留第一条用户需求。
后面又补了两条（同一会话的另一半账）：
5. 疑问式故障描述（「首页为啥还有滚动条」）也要触发这两道守卫；
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
    """只描述现象的 bug 报告也要触发「必须动手」守卫（原来判 False）。"""
    assert Orchestrator._needs_action("出战提示操作失败")
    assert Orchestrator._needs_action("点全部翻开卡死了")
    assert Orchestrator._needs_action("页面白屏")
    # 明确只要分析/不要改的仍然不触发
    assert not Orchestrator._needs_action("只分析一下这个报错，不要改代码")
    assert not Orchestrator._needs_action("这是什么")


def test_fix_task_scope_for_exploration_brake() -> None:
    """「只排查不动手」刹车只对修 bug/小改动生效，批量任务和出图不被误伤。"""
    from game_agent.agents.runtime import _is_fix_task
    assert _is_fix_task("出战提示操作失败")
    assert _is_fix_task("点全部翻开卡死了")
    assert _is_fix_task("改一下结算界面适配手机")
    assert not _is_fix_task("给所有角色配上语音")
    assert not _is_fix_task("生成 20 张角色立绘")
    assert not _is_fix_task("对全量 536 条语音做重构")


# ---------------------------------------------------------------- 1·五 疑问式故障描述
def test_interrogative_symptom_arms_the_guards() -> None:
    """「首页为啥还有滚动条」要的是把滚动条弄掉，不是要一个解释。

    2026-10-28 那次会话里这句话一个动作词都没有，两道守卫（收尾的 _needs_action、
    过程中的 _is_fix_task 刹车）全程判 False，模型查了半天没改一行。
    """
    from game_agent.agents.runtime import _is_fix_task, is_symptom_complaint
    assert is_symptom_complaint("首页为啥还有滚动条")
    assert Orchestrator._needs_action("首页为啥还有滚动条")
    assert _is_fix_task("首页为啥还有滚动条")
    assert Orchestrator._needs_action("这个滚动条为什么去不掉")
    assert _is_fix_task("按钮怎么还是点不动")


def test_plain_questions_stay_questions() -> None:
    """没有「问题还在」这一截，就还是纯提问/纯问答，不该被逼着动手。"""
    from game_agent.agents.runtime import _is_fix_task
    for t in ("这个项目是做什么的？", "这个函数为什么要这样写",
              "首页的排版是怎么做的"):
        assert not Orchestrator._needs_action(t), t
        assert not _is_fix_task(t), t
    # 「这个报错怎么回事」里「报错」是故障词，收尾守卫按纯提问放行（_needs_action False），
    # 但过程刹车照旧启用——在 modify run 里问报错，默认就是要你把它弄好。
    assert not Orchestrator._needs_action("这个报错怎么回事")
    assert _is_fix_task("这个报错怎么回事")


def test_explicit_no_action_still_wins() -> None:
    """用户明说只要分析/别动代码时，疑问式描述也不触发守卫；跑命令类交付物不变。"""
    from game_agent.agents.runtime import _is_fix_task
    for t in ("首页为啥还有滚动条，只分析原因", "首页为啥还有滚动条？先别动代码",
              "首页为啥还有滚动条（别改，我自己来）"):
        assert not Orchestrator._needs_action(t), t
        assert not _is_fix_task(t), t
    assert not Orchestrator._needs_action("跑一遍单测")
    assert not _is_fix_task("生成 20 张角色立绘")


def test_noun_wording_in_question_is_not_an_action_request() -> None:
    """纯问答里的「处理」是名词，不是动手指令（run 9a9656bc 回归）。

    用户原话：「这一块，一次接口请求，能完成查重处理和编制完成吗？为什么有时候请求一次没用？」
    ——「处理」属于「查重处理」（名词），句子是一个问句，要的是解释。旧实现因为
    _EXPLICIT_ACTION_WORDS 含「处理」，跳过了纯提问豁免 → 判成动手指令 → 把 5 分钟的
    正确答案强行推翻重做成 23 分钟的代码改造。修法：疑问句豁免里不再把「处理」当例外。
    """
    q = "这一块，一次接口请求，能完成查重处理和编制完成吗？为什么有时候请求一次没用？"
    assert not Orchestrator._needs_action(q), "纯问答被判成动手指令了"
    # 真·祈使式（无问号 / 明确「帮我」）仍必须是动手指令
    assert Orchestrator._needs_action("帮我处理这个问题")
    assert Orchestrator._needs_action("把这个 bug 处理一下")
    assert Orchestrator._needs_action("改一下 compileComplete")
    # 疑问式故障描述仍按动手指令处理（is_symptom_complaint 不受影响）
    assert Orchestrator._needs_action("首页为啥还有滚动条")



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
