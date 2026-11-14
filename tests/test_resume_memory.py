"""docs/DESIGN-RESUME-MEMORY.md 的回归测试（含文档里「不在本期范围」那条登录态固化）。

对着 2026-10-28「首页为啥还有滚动条」那次会话的浪费逐个钉死：
1. edit/write 落盘后自己刷新指纹 —— 同一 run 内连改两次不再自撞 stale；
2. 视觉回灌瘦身 —— 1024/q78，同图短间隔重看改成一句节流提示；
3. 中断恢复摘要 —— continue 时把上次 tool_result 里的测量结果带回去；
4. 记忆加层 —— env.json 记环境事实、codemap 第 5 类、notes 由全文注入改标题索引；
5. 入口事实固化 —— 测试账号/起服务命令一次确认就写死，下个 run 直接照用（那次光登录就烧了 6 分钟）。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolResult
from game_agent.agents.tools.native import edit_file, read_file, write_file
from game_agent.agents.tools.registry import execute_tool
from game_agent.agents.tools.service import _to_data_url
from game_agent.core.context import RunContext
from game_agent.db.models import Event, Message, Project, Run, Session, Team, User
from game_agent.orchestration import envfacts, project_notes


def _run(coro, timeout: float = 30):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


def _ctx(workspace: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None)  # type: ignore[arg-type]
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


# ---------------------------------------------------------------- 改动一：stale 自愈
def test_second_edit_in_same_run_is_not_stale(tmp_path: Path) -> None:
    """read → edit → edit：第二次必须过（自己刚写的等于自己刚读的）。"""
    (tmp_path / "style.css").write_text(".a{color:red}\n.b{color:blue}\n", encoding="utf-8")
    t = _ctx(tmp_path)

    async def _flow():
        await read_file({"path": "style.css"}, t)
        first = await edit_file({"path": "style.css", "old_string": ".a{color:red}",
                                 "new_string": ".a{color:#f00}"}, t)
        second = await edit_file({"path": "style.css", "old_string": ".b{color:blue}",
                                  "new_string": ".b{color:#00f}"}, t)
        return first, second

    first, second = _run(_flow())
    assert "已修改" in first
    assert "已修改" in second, second
    assert "已经被改过" not in second
    assert (tmp_path / "style.css").read_text(encoding="utf-8") == ".a{color:#f00}\n.b{color:#00f}\n"


def test_external_change_is_still_safe_via_unique_match(tmp_path: Path) -> None:
    """run 外改过文件照样安全，但靠的是「old_string 唯一命中」而不是 stale 检查。

    口径见 native.py:621-629：edit_file 刻意不做「读过之后是否被改过」的检查（同轮并行
    多个 edit 会互相误报、run_command 改文件后必误报）。安全闸是精确替换本身：
    old_string 不命中或不唯一都会被拦下。本用例锁这个行为，防止有人把 stale 检查加回来。
    """
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\n", encoding="utf-8")
    t = _ctx(tmp_path)

    async def _flow():
        await read_file({"path": "a.txt"}, t)
        f.write_text("one\ntwo\nthree\n", encoding="utf-8")  # 外部改动：大小变了
        ok = await execute_tool("edit_file",
                                {"path": "a.txt", "old_string": "one", "new_string": "1"}, t)
        # 外部改动把内容改得 old_string 不唯一了 → 必须拦下，不能瞎改
        f.write_text("1\ntwo\n1\n", encoding="utf-8")
        bad = await execute_tool("edit_file",
                                 {"path": "a.txt", "old_string": "1", "new_string": "x"}, t)
        return ok, bad

    ok, bad = _run(_flow())
    assert "已修改" in ok, ok                       # 精确替换，外部改动不影响这一处
    assert "工具错误" in bad and "不唯一" in bad or "多处" in bad, bad


def test_write_then_edit_needs_no_reread(tmp_path: Path) -> None:
    """write_file 新建的文件，紧接着 edit_file 不该再要「未读不许写」。"""
    t = _ctx(tmp_path)

    async def _flow():
        await write_file({"path": "new.js", "content": "var a=1;\n"}, t)
        return await edit_file({"path": "new.js", "old_string": "var a=1;",
                               "new_string": "var a=2;"}, t)

    out = _run(_flow())
    assert "已修改" in out, out


# ---------------------------------------------------------------- 改动二：视觉回灌
def test_data_url_is_1024_wide_jpeg(tmp_path: Path) -> None:
    """回灌图收瘦到 1024 宽 JPEG，原图不动。"""
    from PIL import Image
    png = tmp_path / "shot.png"
    Image.new("RGB", (2400, 1400), (200, 30, 30)).save(png)
    url = _to_data_url(png)
    assert url.startswith("data:image/jpeg;base64,")
    import base64
    import io
    back = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert back.width == 1024
    assert png.stat().st_size > 0  # 原图仍在磁盘上


def test_repeat_view_is_throttled(tmp_path: Path) -> None:
    """同一张图在几次工具调用内重看 → 提示改用数字指标，而不是再塞一句「目视核对」。"""
    from PIL import Image
    png = tmp_path / "x.png"
    Image.new("RGB", (300, 200), (10, 20, 30)).save(png)
    t = _ctx(tmp_path)

    async def _flow():
        first = await execute_tool("view_image", {"path": "x.png"}, t)
        await execute_tool("list_dir", {"path": "."}, t)   # 间隔 1 次调用
        second = await execute_tool("view_image", {"path": "x.png"}, t)
        for _ in range(6):                                  # 拉开间隔
            await execute_tool("list_dir", {"path": "."}, t)
        third = await execute_tool("view_image", {"path": "x.png"}, t)
        return first, second, third

    first, second, third = _run(_flow())
    assert isinstance(first, ToolResult) and first.images
    assert "刚看过" not in first.text
    assert "刚看过" in second.text and "scrollHeight" in second.text, second.text
    assert "刚看过" not in third.text, third.text


# ---------------------------------------------------------------- 改动三：中断恢复摘要
async def _seed_run(session_factory, note: str, with_events: bool = True) -> str:
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as s:
        user = User(email=f"rs-{suffix}@example.com", display_name="续跑测试")
        team = Team(name="续跑团队", slug=f"rs-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(team_id=team.id, name="续跑项目", slug=f"rs-{suffix}",
                          repo_path=f"rs-{suffix}", created_by=user.id)
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="续跑会话",
                          created_by=user.id)
        s.add(session)
        await s.flush()
        run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                  triggered_by=user.id, run_type="modify", status="failed", error=note)
        s.add(run)
        await s.flush()
        base = int(uuid.uuid4().int % 10 ** 12)
        for i, (etype, payload) in enumerate([
            ("tool_result", {"tool": "run_command",
                             "result": "[exit=0] scrollHeight=1029 clientHeight=911 —— "
                                       "首页内容比视口高 118px，所以一直有滚动条"}),
            ("tool_result", {"tool": "grep", "result": "(无匹配)"}),        # 过短：跳过
            ("tool_result", {"tool": "read_file",
                             "result": "home-grid 这一段在 style.css 第 394 行起，"
                                       "hero-stage 用了固定高度 620px，这就是溢出的来源"}),
            ("agent_thinking", {"reasoning": "根因基本确定：hero-stage 固定高度撑破了视口，"
                                             "把固定高度改成 min-height 就应该不再溢出。"}),
        ]):
            if not with_events:
                break
            s.add(Event(team_id=team.id, project_id=project.id, run_id=run.id,
                        seq=base + i, type=etype, payload_json=payload))
        await s.commit()
        return str(run.id)


def test_findings_brief_pulls_measurements_and_reasoning(session_factory) -> None:
    from game_agent.api.routers.runs import _findings_brief

    async def _flow():
        run_id = await _seed_run(session_factory, "服务重启，run 被中断")
        async with session_factory() as s:
            return await _findings_brief(s, run_id)

    block = _run(_flow())
    assert "1029" in block and "394" in block          # 测量结果与定位都回来了
    assert "(无匹配)" not in block                      # 过短结果不进摘要
    assert "阶段性推理" in block and "未必正确" in block
    assert block.index("1029") < block.index("394")    # 时间正序：旧的在前


def test_findings_brief_empty_for_run_without_events(session_factory) -> None:
    from game_agent.api.routers.runs import _findings_brief

    async def _flow():
        run_id = await _seed_run(session_factory, "x", with_events=False)
        async with session_factory() as s:
            return await _findings_brief(s, run_id)

    assert _run(_flow()) == ""


# ------------------------------------------------ 改动七：续跑时告诉模型「哪些文件已经读过」
def test_touched_files_brief_lists_read_and_written_without_duplicates(session_factory) -> None:
    """现场（用户截图）：点「继续」后助手又「读取了 26 个文件」，把上一轮已查清的代码从头读一遍。

    续跑说明里原来只有「最后 8 个动作」且不带路径。这里把整条 run 的读/写轨迹按文件去重，
    读过的列进「不要整篇重读」，改过的列进「不要重写」；同一文件先读后改只算改过。
    """
    from game_agent.api.routers.runs import _touched_files_brief

    async def _flow():
        run_id = await _seed_run(session_factory, "服务重启，run 被中断")
        async with session_factory() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            base = int(uuid.uuid4().int % 10 ** 12)
            for i, payload in enumerate([
                {"tool": "read_file", "args": {"path": "web/js/screens-main.js"}},
                {"tool": "grep", "args": {"path": "web/js/core.js"}},
                {"tool": "read_file", "args": {"path": "web/js/screens-main.js"}},   # 重复读：去重
                {"tool": "edit_file", "args": {"path": "web/js/core.js"}},          # 先读后改
                {"tool": "read_file", "args": {"path": "web/style.css"}},
            ]):
                s.add(Event(team_id=run.team_id, project_id=run.project_id, run_id=run.id,
                            seq=base + i, type="tool_call", payload_json=payload))
            await s.commit()
            return await _touched_files_brief(s, run_id)

    block = _run(_flow())
    assert "上次已经读过的文件" in block and "上次已经改过的文件" in block
    assert block.count("web/js/screens-main.js") == 1        # 重复读只列一次
    assert "web/style.css" in block
    read_part, _, write_part = block.partition("上次已经改过的文件")
    assert "web/js/core.js" not in read_part                  # 改过的文件不再列进「已读」
    assert "web/js/core.js" in write_part


def test_touched_files_brief_empty_for_run_without_events(session_factory) -> None:
    from game_agent.api.routers.runs import _touched_files_brief

    async def _flow():
        run_id = await _seed_run(session_factory, "x", with_events=False)
        async with session_factory() as s:
            return await _touched_files_brief(s, run_id)

    assert _run(_flow()) == ""


# ------------------------------------------------ 改动六：续跑说明不再糊在聊天里
def test_continue_message_is_short_but_model_gets_full_brief(session_factory) -> None:
    """点「继续」：聊天里只落一句短话，完整续跑说明进 meta["brief"] 交给模型。

    现场（用户原话）：「每次异常中断点继续，就发送了一堆东西过去，没必要，
    就是要继续完成任务目标几个字就行了」。所以正文=短句，模型侧靠 meta["brief"]。
    """
    from game_agent.api.routers.runs import CONTINUE_SHORT
    from game_agent.orchestration.orchestrator import Orchestrator

    async def _flow():
        run_id = await _seed_run(session_factory, "服务重启，run 被中断")
        async with session_factory() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            orig = Message(session_id=run.session_id, author_type="user",
                           content="给首页 hero 区加个滚动条修复")
            s.add(orig)
            await s.flush()
            run.trigger_msg_id = orig.id
            # 模拟 continue_run 落库的那条消息：正文短句 + 完整 brief 在 meta 里
            cont = Message(session_id=run.session_id, author_type="user",
                           content=CONTINUE_SHORT,
                           meta_json={"continue_from": run_id,
                                      "brief": "【继续上次被中断的任务】\n\n上次请求原文：\n"
                                               "给首页 hero 区加个滚动条修复"})
            s.add(cont)
            await s.commit()
            cont_id = str(cont.id)
            sid = str(run.session_id)
        orch = Orchestrator(None, "ctx-test", session_factory)
        ctx, images = await orch._load_context(sid, cont_id, {})
        return ctx, images

    ctx, images = _run(_flow())
    # 模型拿到的是完整 brief，而不是光秃秃的「继续完成任务目标」
    assert "上次请求原文" in ctx and "给首页 hero 区加个滚动条修复" in ctx
    assert images == []


def test_resolve_user_request_walks_continue_chain(session_factory) -> None:
    """「用户原话」要顺着 continue_from 链回溯，而不是取短句正文。"""
    from game_agent.orchestration.orchestrator import Orchestrator

    async def _flow():
        run_id = await _seed_run(session_factory, "中断")
        async with session_factory() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            orig = Message(session_id=run.session_id, author_type="user",
                           content="把首页 hero 固定高度改成 min-height 修掉溢出")
            s.add(orig)
            await s.flush()
            run.trigger_msg_id = orig.id
            cont = Message(session_id=run.session_id, author_type="user",
                           content="继续完成任务目标",
                           meta_json={"continue_from": run_id})
            s.add(cont)
            await s.commit()
            cont_id = str(cont.id)
        orch = Orchestrator(None, "ctx-test", session_factory)
        async with session_factory() as s:
            m = await s.get(Message, uuid.UUID(cont_id))
            return await orch._resolve_user_request(s, m)

    text = _run(_flow())
    assert text == "把首页 hero 固定高度改成 min-height 修掉溢出", text


def test_continue_run_creates_run_with_brief_in_options_and_no_message(session_factory, monkeypatch) -> None:
    """端到端锁「点继续」的新落库形态：不再写任何消息（trigger_msg_id 为空、会话消息数不变），
    续跑说明/上下文放进 run.options_json["brief"]，模型侧照样能读到。"""
    from types import SimpleNamespace

    from sqlalchemy import func, select

    from game_agent.api import state as state_mod
    from game_agent.api.routers.runs import continue_run
    from game_agent.core.context import RequestContext
    from game_agent.orchestration.orchestrator import Orchestrator

    class _Queue:
        def __init__(self):
            self.enqueued = []

        async def enqueue(self, run_id, priority=0):
            self.enqueued.append(run_id)

    async def _flow():
        run_id = await _seed_run(session_factory, "服务重启，run 被中断")
        async with session_factory() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            orig = Message(session_id=run.session_id, author_type="user",
                           content="把首页 hero 固定高度改成 min-height 修掉溢出")
            s.add(orig)
            await s.flush()
            run.trigger_msg_id = orig.id
            await s.commit()
            team_id, user_id, sid = str(run.team_id), str(run.triggered_by), str(run.session_id)
            before = (await s.execute(select(func.count()).select_from(Message)
                                      .where(Message.session_id == run.session_id))).scalar()

        q = _Queue()
        monkeypatch.setattr(state_mod.state, "session_factory", session_factory)
        monkeypatch.setattr(state_mod.state, "services",
                            SimpleNamespace(queue=q))
        ctx = RequestContext(user_id=user_id, team_id=team_id, role="owner")
        res = await continue_run(run_id, None, ctx)

        async with session_factory() as s:
            new_run = await s.get(Run, uuid.UUID(res["run_id"]))
            after = (await s.execute(select(func.count()).select_from(Message)
                                     .where(Message.session_id == run.session_id))).scalar()
            return new_run, before, after, res, q, sid

    new_run, before, after, res, q, sid = _run(_flow())
    # 关键：继续不再写任何消息，对话里零新增，也没有触发消息
    assert new_run.trigger_msg_id is None
    assert after == before
    opts = dict(new_run.options_json or {})
    assert opts.get("continue_from") == res["continued_from"]
    assert "上次请求原文" in (opts.get("brief") or "")
    assert "把首页 hero 固定高度改成 min-height 修掉溢出" in (opts.get("brief") or "")
    assert q.enqueued == [res["run_id"]]

    # 模型侧仍能读到完整 brief（不写消息也不丢上下文）
    async def _model_ctx():
        orch = Orchestrator(None, "ctx-test", session_factory)
        return await orch._load_context(sid, None, {}, override_req=opts.get("brief"))
    ctx_text, _ = _run(_model_ctx())
    assert "上次请求原文" in ctx_text


# ---------------------------------------------------------------- 改动四 L0：环境事实
def test_sniff_cmd_missing_command(tmp_path: Path) -> None:
    out = "'grep' 不是内部或外部命令，也不是可运行的程序或批处理文件。"
    facts = envfacts.sniff("grep -rn x .", out, 1)
    assert facts["shell"] == "cmd"
    assert facts["unavailable"] == ["grep"]


def test_sniff_powershell_and_git(tmp_path: Path) -> None:
    ps = ("The term 'Get-NetTCPConnection' is not recognized as the name of a cmdlet, "
          "function, script file, or operable program.")
    f1 = envfacts.sniff("Get-NetTCPConnection -LocalPort 5174", ps, 1)
    assert f1["shell"] == "powershell" and f1["unavailable"] == ["get-nettcpconnection"]
    f2 = envfacts.sniff("git status", "fatal: not a git repository (or any of the parents): .git", 128)
    assert f2["git"] is False
    # 认不准的一律不升级成机器事实
    assert envfacts.sniff("python -m pytest", "2 failed, 10 passed", 1) == {}


def test_env_record_merges_and_prompt_block(tmp_path: Path) -> None:
    assert envfacts.record(tmp_path, {"shell": "cmd", "unavailable": ["grep"]})
    # 已有同键不覆盖（第一条事实最可信）、列表里没有的新项才并集进来
    assert envfacts.record(tmp_path, {"shell": "powershell", "unavailable": ["grep"]}) is False
    assert envfacts.record(tmp_path, {"git": False, "unavailable": ["sed"]})
    data = envfacts.load(tmp_path)
    assert data["shell"] == "cmd"
    assert data["unavailable"] == ["grep", "sed"]
    assert data["git"] is False
    block = envfacts.prompt_block(tmp_path)
    assert "环境事实" in block and "grep" in block and "cmd" in block
    assert envfacts.prompt_block(tmp_path / "empty") == ""


def test_envfacts_record_is_idempotent(tmp_path: Path) -> None:
    envfacts.record(tmp_path, {"shell": "cmd"})
    assert envfacts.record(tmp_path, {"shell": "cmd"}) is False  # 没有新东西不落盘


# ---------------------------------------------------------------- 改动四 L1/L2：codemap + 索引
def test_codemap_category_roundtrip(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    touched = project_notes.append(ws, [{
        "category": "codemap", "title": "首页布局骨架",
        "detail": "- 布局骨架在 web/css/style.css 约 200-244 行\n- home-grid 约 394 行起",
        "files": ["web/css/style.css"]}], run_id="deadbeef0000")
    assert touched == [".trace/notes/codemap.md"]
    assert (ws / ".trace" / "notes" / "codemap.md").is_file()
    entries = project_notes.list_entries(ws)
    assert entries[0]["category"] == "codemap" and entries[0]["category_cn"] == "代码地图"
    assert "首页布局骨架" in project_notes.index_titles(ws)[0]
    # 解析回来的 detail 不被「- 」前缀污染
    assert "200-244" in entries[0]["detail"]
    # judge 文案里必须带上第 5 类，否则模型不会往 codemap 写
    prompt = project_notes.judge_prompt("修滚动条", "改成 min-height", ["style.css"], [])
    assert "codemap" in prompt
    assert project_notes.parse_judge(
        '{"worth": true, "reason": "摸清结构", "entries": [{"category": "codemap",'
        '"title": "战斗入口", "detail": "- 在 battle.js 约 80 行"}]}')["entries"][0][
            "category"] == "codemap"
    assert project_notes.delete_entry(ws, "codemap", entries[0]["key"]) is True
    assert project_notes.list_entries(ws) == []


# ---------------------------------------------------------------- 改动八：生命周期（钉住 / 失效 / 公平预算）
def test_trim_keeps_pinned_and_drops_stale_first(tmp_path: Path) -> None:
    """三级淘汰：钉住的永不丢、失效的最先丢、其余满了丢最旧。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    project_notes.append(ws, [{"category": "convention", "title": "迁移先复制再删旧",
                               "detail": "- 铁律"}])
    project_notes.append(ws, [{"category": "convention", "title": "过程性临时条目",
                               "detail": "- 一次性"}])
    entries = {e["title"]: e["key"] for e in project_notes.list_entries(ws)}
    assert project_notes.set_flag(ws, "convention", entries["迁移先复制再删旧"], keep=True) is True
    assert project_notes.set_flag(ws, "convention", entries["过程性临时条目"], stale=True) is True
    for i in range(45):
        project_notes.append(ws, [{"category": "convention", "title": f"普通条目 {i}",
                                   "detail": "- x"}])
    listed = project_notes.list_entries(ws)
    titles = [e["title"] for e in listed]
    assert "迁移先复制再删旧" in titles, "钉住的被挤掉了"
    assert "过程性临时条目" not in titles, "失效的没被优先淘汰"
    assert len(listed) <= 40
    assert any(e["keep"] for e in listed if e["title"] == "迁移先复制再删旧")


def test_stale_entry_not_injected(tmp_path: Path) -> None:
    """失效条目不再注入上下文（否则旧口径会一直误导后续 run）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    project_notes.append(ws, [{"category": "pitfall", "title": "旧口径已过时", "detail": "- x"}])
    key = project_notes.list_entries(ws)[0]["key"]
    assert project_notes.set_flag(ws, "pitfall", key, stale=True) is True
    assert "旧口径已过时" not in project_notes.index_block(ws)
    assert project_notes.list_entries(ws)[0]["stale"] is True
    # 恢复后重新注入
    assert project_notes.set_flag(ws, "pitfall", key, stale=False) is True
    assert "旧口径已过时" in project_notes.index_block(ws)


def test_index_block_fair_budget_across_categories(tmp_path: Path) -> None:
    """五类都要拿到预算：曾出现 playbook/codemap 整类被饿死（一条都没注入）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    for cat in ("decision", "pitfall", "convention", "playbook", "codemap"):
        for i in range(30):
            project_notes.append(ws, [{"category": cat, "title": f"{cat} 条目 {i}",
                                       "detail": "- x"}])
    block = project_notes.index_block(ws, budget_tokens=600)
    for cn in ("决策与取舍", "踩坑与规避", "项目约定", "可复用做法", "代码地图"):
        assert f"**{cn}**" in block, f"{cn} 没拿到任何预算"


def test_supersede_marks_old_entry_stale(tmp_path: Path) -> None:
    """judge 让模型写「取代：<旧标题>」，新条目落盘时旧条要自动标失效。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    project_notes.append(ws, [{"category": "decision", "title": "收尾汇报按情况选块",
                               "detail": "- 旧口径"}])
    project_notes.append(ws, [{"category": "decision", "title": "收尾汇报挑 3~5 块",
                               "detail": "- 取代：收尾汇报按情况选块\n- 新口径"}])
    by_title = {e["title"]: e for e in project_notes.list_entries(ws)}
    assert by_title["收尾汇报按情况选块"]["stale"] is True
    assert by_title["收尾汇报挑 3~5 块"]["stale"] is False


def test_index_block_lists_titles_not_bodies(tmp_path: Path) -> None:
    """注入的是标题索引 + 文件路径，正文要 agent 自己读（省 token 的关键）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    project_notes.append(ws, [{"category": "pitfall", "title": "整库扫描会卡住",
                               "detail": "- 递归扫描请限定子目录，这条正文很长"
                                         "，故意写得超过索引预算，用来验证正文没被整段塞进上下文。"}])
    project_notes.append(ws, [{"category": "codemap", "title": "首页布局骨架",
                               "detail": "- style.css 约 200-244 行"}])
    block = project_notes.index_block(ws)
    assert "项目改动经验索引" in block
    assert "整库扫描会卡住" in block and "首页布局骨架" in block
    assert "故意写得超过索引预算" not in block          # 正文不注入
    assert ".trace/notes/pitfalls.md" in block          # 给路径才能按需读
    assert "read_file" in block
    assert project_notes.index_block(tmp_path / "none") == ""


# ---------------------------------------------------------------- 改动七：入口事实固化
def test_save_env_fact_records_and_refuses_silent_override(tmp_path: Path) -> None:
    """登录账号这类事实一次写死；已有值不带 force 不覆盖（防手滑改掉对的）。"""
    async def _flow():
        steps = []
        for args in ({"key": "test_account", "value": "测试号 admin / 密码 a123456"},
                     {"key": "test_account", "value": "换了个号 root"},        # 没 force → 拒绝
                     {"key": "test_account", "value": "换了个号 root", "force": True},
                     {"key": "test_account", "value": "换了个号 root"}):        # 同值 → 不落盘
            out = await execute_tool("save_env_fact", args, _ctx(tmp_path))
            steps.append((out, envfacts.load(tmp_path).get("test_account")))
        return steps

    steps = _run(_flow())
    assert "已记入" in steps[0][0] and steps[0][1] == "测试号 admin / 密码 a123456"
    assert "没覆盖" in steps[1][0] and "force=true" in steps[1][0]
    assert steps[1][1] == "测试号 admin / 密码 a123456"   # 拒绝时旧值必须还在
    assert "已记入" in steps[2][0] and steps[2][1] == "换了个号 root"
    assert "本来就是" in steps[3][0] and steps[3][1] == "换了个号 root"
    # 落盘后必须能被下一个 run 读到
    block = envfacts.prompt_block(tmp_path)
    assert "test_account" in block and "换了个号 root" in block


def test_save_env_fact_rejects_bad_shape(tmp_path: Path) -> None:
    """key 要有界、value 不能写成长文（长步骤属于 notes 经验，不是环境事实）。"""
    from game_agent.agents.tools import ToolError
    from game_agent.agents.tools.native import save_env_fact

    async def _call(args: dict):
        return await save_env_fact(args, _ctx(tmp_path))

    bad = ({"key": "", "value": "x"}, {"key": "启动命令", "value": "x"},
           {"key": "test_account"}, {"key": "test_account", "value": "   "},
           {"key": "login_entry", "value": "第一步" * 200})
    for args in bad:
        try:
            _run(_call(args))
        except ToolError:
            continue
        raise AssertionError(f"应当拒绝：{args}")
    # 没工作区时也不能默默写到一个奇怪的地方
    ctx = _ctx(tmp_path)
    ctx.workspace = None
    try:
        _run(save_env_fact({"key": "test_account", "value": "x"}, ctx))
        raise AssertionError("workspace=None 应当报错")
    except ToolError:
        pass


def test_start_service_records_entry_command(tmp_path: Path) -> None:
    """起服务成功 = 这条命令在这个项目里真的能跑，直接沉成 service_start / service_url。"""
    from game_agent.agents.tools.service import record_start_entry
    fresh = record_start_entry(tmp_path, "python -m http.server 8123", "http://127.0.0.1:8123/")
    assert fresh == {"service_start": "python -m http.server 8123",
                     "service_url": "http://127.0.0.1:8123/"}
    # 再跑一次不覆盖、也不重复落盘
    assert record_start_entry(tmp_path, "npm run dev", "http://127.0.0.1:5173/") == {}
    assert envfacts.load(tmp_path)["service_start"] == "python -m http.server 8123"
    assert record_start_entry(None, "whatever", "") == {}


def test_save_env_fact_is_wired_for_main() -> None:
    """工具得真的在白名单 + schema 里，否则模型看不见等于没做。"""
    from game_agent.agents.roles import ROLE_TOOLS
    from game_agent.agents.tools.registry import TOOL_IMPLS, TOOL_SCHEMAS, build_tool_specs
    assert "save_env_fact" in ROLE_TOOLS["main"]
    assert "save_env_fact" in TOOL_IMPLS and "save_env_fact" in TOOL_SCHEMAS
    specs = {s.name: s for s in build_tool_specs(ROLE_TOOLS["main"])}
    assert "test_account" in specs["save_env_fact"].description
