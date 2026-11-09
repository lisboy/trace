"""并发领取 / 工具硬超时 / 联网来源 的回归测试。

覆盖本次三个「并发与互联」相关改动：
1. ``LiteQueue.claim`` 在多 worker 并发下不重领（每条 run 只归一个 worker），且能连续领到不同 run；
2. ``execute_tool_with_timeout`` 在工具挂死时按时返回「工具错误」而不是把 worker 卡死；
3. ``web_search / web_fetch`` 返回末尾带结构化 ``[Sources]`` 来源清单（前端据此渲染可点芯片）。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from game_agent.agents.tools import ToolContext
from game_agent.agents.tools import registry as tool_registry
from game_agent.agents.tools import web as web_tools
from game_agent.core.lite.queue import LiteQueue
from game_agent.db.models import Project, Run, Session, Team, User


def _run(coro, timeout: float = 30):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(_wrapped())


async def _seed_queued_runs(session_factory, n: int) -> list[str]:
    """建 n 条各自独立项目下的 queued run（互不干扰，便于验证「领到的是不同 run」）。

    同一 pytest 会话共用一个 sqlite 库，前面的用例可能留下没领完的 queued run；
    先统一取消，保证每次 claim 只可能领到本用例新造的几条。
    """
    suffix = uuid.uuid4().hex[:8]
    ids: list[str] = []
    async with session_factory() as s:
        from sqlalchemy import update
        await s.execute(update(Run).where(Run.status == "queued").values(status="cancelled"))
        await s.commit()
    async with session_factory() as s:
        user = User(email=f"cc-{suffix}@example.com", display_name="并发用户")
        team = Team(name="并发团队", slug=f"cc-{suffix}")
        s.add_all([user, team])
        await s.flush()
        for i in range(n):
            slug = f"cc-{suffix}-{i}"
            project = Project(team_id=team.id, name=f"并发项目{i}", slug=slug,
                              repo_path=slug, project_type="python", created_by=user.id)
            s.add(project)
            await s.flush()
            session = Session(team_id=team.id, project_id=project.id, title=f"会话{i}",
                              created_by=user.id)
            s.add(session)
            await s.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                      triggered_by=user.id, run_type="modify", status="queued")
            s.add(run)
            await s.flush()
            ids.append(str(run.id))
        await s.commit()
    return ids


def test_claim_returns_distinct_runs_then_empty(session_factory) -> None:
    """顺序领取：每次拿到不同的 run，领完后返回 None。"""

    async def _flow():
        ids = await _seed_queued_runs(session_factory, 3)
        q = LiteQueue(session_factory)
        got = [await q.claim() for _ in range(3)]
        empty = await q.claim()
        return ids, got, empty

    ids, got, empty = _run(_flow())
    assert sorted(got) == sorted(ids), (got, ids)   # 三条各领一次，无重复无遗漏
    assert empty is None


def test_claim_is_atomic_under_concurrency(session_factory) -> None:
    """并发领取：多个 worker 同时 claim，同一条 run 绝不会被领两次。"""

    async def _flow():
        ids = await _seed_queued_runs(session_factory, 6)
        q = LiteQueue(session_factory)
        # 8 个协程同时抢（比 run 数多，模拟 worker 数 > 待领任务）
        got = await asyncio.gather(*(q.claim() for _ in range(8)))
        return ids, [g for g in got if g]

    ids, got = _run(_flow())
    assert len(got) == len(set(got)), f"有 run 被重复领取：{got}"
    assert all(g in ids for g in got)
    # 并发下不丢：6 条都应被领走（SQLite 写锁重试兜底）
    assert set(got) == set(ids), (set(got), set(ids))


def test_claim_skips_run_whose_session_is_busy(session_factory) -> None:
    """同一会话里的两条 run 必须串行：前一条在跑时，后一条不能被另一个 worker 领走。

    现场（用户要求）：多个会话要能同时开多个对话，但同一个对话里连发几条应当按顺序做。
    跨会话不能被误伤——别的会话的 queued run 照旧能立刻领到。
    """
    async def _flow():
        suffix = uuid.uuid4().hex[:8]
        async with session_factory() as s:
            from sqlalchemy import update
            await s.execute(update(Run).where(Run.status == "queued").values(status="cancelled"))
            await s.commit()
        async with session_factory() as s:
            user = User(email=f"ss-{suffix}@example.com", display_name="同会话用户")
            team = Team(name="同会话团队", slug=f"ss-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="同会话项目", slug=f"ss-{suffix}",
                              repo_path=f"ss-{suffix}", project_type="python", created_by=user.id)
            s.add(project)
            await s.flush()
            # 会话 A 里两条（一先一后），会话 B 一条
            sess_a = Session(team_id=team.id, project_id=project.id, title="A", created_by=user.id)
            sess_b = Session(team_id=team.id, project_id=project.id, title="B", created_by=user.id)
            s.add_all([sess_a, sess_b])
            await s.flush()
            runs = []
            for sess in (sess_a, sess_a, sess_b):
                run = Run(team_id=team.id, project_id=project.id, session_id=sess.id,
                          triggered_by=user.id, run_type="modify", status="queued")
                s.add(run)
                await s.flush()
                runs.append(run)
            a1, a2, b1 = (str(r.id) for r in runs)
            await s.commit()
        q = LiteQueue(session_factory)
        first = await q.claim()          # A 的第一条（created_at 最早）
        second = await q.claim()         # 应是 B 的那条，不能是 A 的第二条
        third = await q.claim()          # A 第一条跑完之前，A 的第二条仍领不到
        return a1, a2, b1, first, second, third

    a1, a2, b1, first, second, third = _run(_flow())
    assert first == a1, (first, a1)
    assert second == b1, (second, b1)     # 跨会话没有排队
    assert third is None, (third, a2)     # A 的第一条还占着会话，A 的第二条必须继续等


def test_tool_hard_timeout_returns_tool_error() -> None:
    """工具挂死（毫无进展）时，execute_tool_with_timeout 按时把它变成「工具错误」文案。"""
    async def _hang(args, t):  # noqa: ANN001
        await asyncio.sleep(30)

    async def _flow():
        ctx = ToolContext(run_ctx=None, workspace=Path("."), role="main", task_id="t1")
        return await tool_registry.execute_tool_with_timeout("hang_probe", {}, ctx, 0.1)

    old = dict(tool_registry.TOOL_IMPLS)
    tool_registry.TOOL_IMPLS["hang_probe"] = _hang
    try:
        out = _run(_flow())
    finally:
        tool_registry.TOOL_IMPLS.clear()
        tool_registry.TOOL_IMPLS.update(old)

    assert isinstance(out, str)
    assert out.startswith("工具"), out
    assert "没有任何进展" in out and "中止" in out


def test_tool_hard_timeout_passes_through_fast_tool() -> None:
    """没超时的工具正常返回，不被超时逻辑改写。"""

    async def _fast(args, t):  # noqa: ANN001
        return "ok-result"

    async def _flow():
        ctx = ToolContext(run_ctx=None, workspace=Path("."), role="main", task_id="t1")
        return await tool_registry.execute_tool_with_timeout("fast_probe", {}, ctx, 5)

    old = dict(tool_registry.TOOL_IMPLS)
    tool_registry.TOOL_IMPLS["fast_probe"] = _fast
    try:
        out = _run(_flow())
    finally:
        tool_registry.TOOL_IMPLS.clear()
        tool_registry.TOOL_IMPLS.update(old)
    assert out == "ok-result"


def test_tool_hard_timeout_resets_on_progress() -> None:
    """关键回归（2026-11-01）：批量出图「一张张正常出、总时长超过超时值」不能被误杀。

    模拟：每 0.1s 出一张（推进 progress_seq），总共跑 0.8s，而硬超时只给 0.3s。
    旧实现（总时长 0.3s 一刀切）会在中途中止；新实现（无进展 0.3s）应跑完全部并返回结果。
    """
    async def _batch(args, t):  # noqa: ANN001
        for _ in range(8):
            await asyncio.sleep(0.1)
            t.progress_seq += 1  # 每出一张就推进进展
        return "batch-done"

    async def _flow():
        ctx = ToolContext(run_ctx=None, workspace=Path("."), role="main", task_id="t1")
        return await tool_registry.execute_tool_with_timeout("batch_probe", {}, ctx, 0.3)

    old = dict(tool_registry.TOOL_IMPLS)
    tool_registry.TOOL_IMPLS["batch_probe"] = _batch
    try:
        out = _run(_flow(), timeout=10)
    finally:
        tool_registry.TOOL_IMPLS.clear()
        tool_registry.TOOL_IMPLS.update(old)
    assert out == "batch-done", out


def test_tool_hard_timeout_still_kills_stalled_tool() -> None:
    """没有进展的工具即使「总时长没到上限」也按无进展中止（progress_seq 一直不变）。"""
    async def _stall(args, t):  # noqa: ANN001
        await asyncio.sleep(30)  # 永不推进 progress_seq

    async def _flow():
        ctx = ToolContext(run_ctx=None, workspace=Path("."), role="main", task_id="t1")
        return await tool_registry.execute_tool_with_timeout("stall_probe", {}, ctx, 0.3)

    old = dict(tool_registry.TOOL_IMPLS)
    tool_registry.TOOL_IMPLS["stall_probe"] = _stall
    try:
        out = _run(_flow(), timeout=10)
    finally:
        tool_registry.TOOL_IMPLS.clear()
        tool_registry.TOOL_IMPLS.update(old)
    assert isinstance(out, str) and out.startswith("工具"), out
    assert "没有任何进展" in out, out


def test_format_sources_dedupes_and_is_parseable() -> None:
    raw = [
        {"title": "A 页面 | 带竖线", "url": "https://a.example.com/x"},
        {"title": "重复", "url": "https://a.example.com/x"},     # 已出现，去重
        {"title": "B", "url": "https://b.example.com/y"},
        {"title": "无链接", "url": "not-a-url"},                  # 非 http(s)，跳过
    ]
    block = web_tools._format_sources(raw)
    assert "[Sources]" in block
    assert "https://a.example.com/x" in block and "https://b.example.com/y" in block
    assert block.count("https://a.example.com/x") == 1
    assert "not-a-url" not in block
    # 每条目标格式：`序号. 标题 | URL`
    lines = [ln for ln in block.splitlines() if ln[:1].isdigit()]
    assert lines and all(" | http" in ln for ln in lines)


def test_web_search_result_has_sources_block(monkeypatch) -> None:
    """web_search 返回末尾应带结构化来源清单，供前端渲染可点击来源。"""

    async def fake_bing(query: str, n: int):  # noqa: ANN001
        return [
            {"title": "Godot 文档", "url": "https://docs.godotengine.org/zh-cn/4.x/",
             "snippet": "官方文档"},
            {"title": "示例站", "url": "https://example.com/post", "snippet": "摘要"},
        ]

    monkeypatch.setattr(web_tools, "_bing_search", fake_bing)
    monkeypatch.setattr(web_tools, "SEARCH_PROVIDER", "bing")

    async def _flow():
        ctx = ToolContext(run_ctx=None, workspace=Path("."), role="main", task_id="t1")
        return await web_tools.web_search({"query": "godot 教程"}, ctx)

    out = _run(_flow())
    assert "[Sources]" in out
    # 正文里仍能看到结果条目与 URL
    assert "Godot 文档" in out and "https://example.com/post" in out
    src_part = out.split("[Sources]", 1)[1]
    assert "1." in src_part and "https://docs.godotengine.org" in src_part


def _parse_like_frontend(text: str) -> list[tuple[str, str]]:
    """镜像前端 parseSources 的解析规则，验证事件文本里来源块可被解析成芯片。"""
    import re
    idx = text.find("[Sources]")
    if idx < 0:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text[idx + len("[Sources]"):].split("\n"):
        m = re.match(r"^\s*\d+\.\s*(.*?)\s*\|\s*(https?://\S+)\s*$", line)
        if not m:
            continue
        url = m.group(2)
        if url in seen:
            continue
        seen.add(url)
        out.append(((m.group(1) or url).strip(), url))
    return out


def test_tail_with_sources_keeps_sources_when_body_truncated() -> None:
    """正文超 2000 字时，来源块在末尾也必须保留，否则前端「来源」芯片整块消失。"""
    from game_agent.agents.runtime import _tail_with_sources, _TOOL_RESULT_EVENT_CAP

    body = "结果正文。" * 900  # 远超 2000 字
    sources = "[Sources]\n" + "\n".join(
        f"{i}. 标题{i} | https://site{i}.example.com/article/{i}" for i in range(1, 11))
    raw = body + "\n\n" + sources
    assert len(raw) > _TOOL_RESULT_EVENT_CAP
    # 朴素从头截断会丢掉来源（复现缺陷）
    assert "[Sources]" not in raw[:_TOOL_RESULT_EVENT_CAP]

    cut = _tail_with_sources(raw)
    assert "[Sources]" in cut
    chips = _parse_like_frontend(cut)
    assert len(chips) == 10, chips
    # 正文仍被限制（没有把整段原文原样带回）
    assert len(cut) < len(raw)


def test_tail_with_sources_short_result_passthrough() -> None:
    """不超限的结果原样返回，不被改写。"""
    from game_agent.agents.runtime import _tail_with_sources

    text = "短结果\n\n[Sources]\n1. A | https://a.example.com/x"
    assert _tail_with_sources(text) == text


def test_tail_with_sources_plain_result_truncates_normally() -> None:
    """无来源块的普通长结果：仍按 2000 字截断，行为不变。"""
    from game_agent.agents.runtime import _tail_with_sources, _TOOL_RESULT_EVENT_CAP

    text = "x" * 5000
    assert _tail_with_sources(text) == text[:_TOOL_RESULT_EVENT_CAP]
