"""编排器冒烟测试：import 装配 / 纯函数守卫 / lite 引擎 / 一次完整 run。

分层意图：
1. **wiring**：三角色工具表、编排器可构造——重构改坏名字会立刻红；
2. **纯函数**：``_needs_action`` / ``_cmd_is_readonly`` / ``_intent_hint`` 这类
   「运行时硬提示」的判定逻辑，是"组长只勘察不动手"和"要图却去翻仓库"两个
   实测故障的修复点，必须锁住行为；
3. **lite 引擎**：临时 SQLite 上装配 CoreServices，跑一次锁的申请/释放；
4. **真 run**：MockBrain 驱动一整轮 modify（读工程 → 写代码 → 派美术出占位图 →
   跑 pytest → 汇报），断言 run 落到 succeeded 且回复消息真的落库。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Coroutine

import pytest

from game_agent.agents.roles import ROLE_TOOLS
from game_agent.config import PROJECTS_ROOT
from game_agent.core.lite.container import build_lite_services
from game_agent.db.models import Message, Project, Run, Session, Team, User
from game_agent.orchestration.orchestrator import Orchestrator

RUN_TIMEOUT_S = 180


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 60) -> Any:
    """同步测试里跑协程（不引入 pytest-asyncio 依赖）。"""

    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


# ---------------------------------------------------------------- 造数据
async def _seed(session_factory, requirement: str) -> dict[str, str]:
    """建一套最小可跑数据：用户 / 团队 / 项目 / 会话 / 用户消息 / queued run。"""
    suffix = uuid.uuid4().hex[:8]
    repo_path = f"smoke-{suffix}"
    async with session_factory() as s:
        user = User(email=f"smoke-{suffix}@example.com", display_name="冒烟用户")
        team = Team(name="冒烟团队", slug=f"smoke-{suffix}")
        s.add_all([user, team])
        await s.flush()
        project = Project(
            team_id=team.id, name="冒烟项目", slug=repo_path, repo_path=repo_path,
            project_type="python", created_by=user.id,
        )
        s.add(project)
        await s.flush()
        session = Session(team_id=team.id, project_id=project.id, title="冒烟会话",
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
        ids = {
            "team_id": str(team.id), "project_id": str(project.id),
            "session_id": str(session.id), "message_id": str(message.id),
            "run_id": str(run.id), "user_id": str(user.id), "repo_path": repo_path,
        }
    workspace = PROJECTS_ROOT / repo_path
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("# 冒烟项目\n", encoding="utf-8")
    return ids


# ---------------------------------------------------------------- 1. wiring
def test_role_tools_wiring() -> None:
    """main 角色在（动手工具齐全）；sub 是 spawn_agents 派出的只读子agent（无写/跑/生成权限）。"""
    assert set(ROLE_TOOLS) == {"main", "sub"}
    main = set(ROLE_TOOLS["main"])
    for tool in ("write_file", "edit_file", "run_command", "start_service",
                 "generate_image", "generate_audio"):
        assert tool in main, f"助手缺少工具 {tool}"
    # 子agent 只读，不能写/跑/生成，也不能递归派发
    sub = set(ROLE_TOOLS["sub"])
    assert not (sub & {"write_file", "edit_file", "run_command", "start_service",
                       "generate_image", "generate_audio", "spawn_agents"})


def test_orchestrator_constructs(session_factory) -> None:
    """CoreServices + 编排器能在临时库上装配起来（不改代码就能构造）。"""

    async def _build():
        svcs = await build_lite_services(session_factory, "smoke-worker")
        return Orchestrator(svcs, "smoke-worker", session_factory)

    orch = _run(_build())
    assert orch.worker_id == "smoke-worker"
    assert orch.svcs.repo is not None


# ---------------------------------------------------------------- 2. 纯函数守卫
@pytest.mark.parametrize("text,expected", [
    ("给主界面加一个卡牌详情弹窗", True),
    ("把这张卡换成稀有度边框", True),
    ("帮我修一下排序 bug", True),
    ("出战提示操作失败", True),        # 只描述现象的 bug 报告也要触发守卫
    ("点全部翻开卡死了", True),
    ("这个项目是做什么的？", False),
    ("这个报错怎么回事", False),      # 纯提问，不是动手指令
    ("帮我看看为什么白屏", True),      # 「帮我」= 请求处理，不是只要解释
    ("只分析一下项目结构，不要改代码", False),
    ("先别动，我看看", False),
    ("跑一遍单测", False),             # 交付物就是测试结果，不要求改代码
    ("", False),
])
def test_needs_action_guard(text: str, expected: bool) -> None:
    """「只勘察不动手」守卫的判定：要求动手 → True；纯问答/明确只读 → False。"""
    assert Orchestrator._needs_action(text) is expected


@pytest.mark.parametrize("cmd,readonly", [
    ("ls -la", True),
    ("git status", True),
    ("pytest -q", False),          # pytest 前有没有 --version 是分水岭
    ("python -m pytest -q", False),
    ("rm -rf build", False),
    ("", True),
])
def test_cmd_is_readonly(cmd: str, readonly: bool) -> None:
    assert Orchestrator._cmd_is_readonly(cmd) is readonly


def test_intent_hint_routes_media_requests() -> None:
    """出图/配音需求必须被前置成 generate_image / generate_audio 硬指令。"""
    assert "generate_image" in Orchestrator._intent_hint("给主界面生成一张背景图")
    assert "generate_audio" in Orchestrator._intent_hint("给这个角色配一句台词")
    assert Orchestrator._intent_hint("把排序改成按稀有度") == ""


def test_intent_hint_known_gap_plain_wording() -> None:
    """记录一个已知缺口（不修生产代码，先把它钉成断言）：

    ``_IMAGE_WORDS`` 只认「生成一张 / 场景图 / 出图…」这类词，用户很自然的说法
    「给主界面出一张背景图」不含任何关键词 → 预判为空，模型又回到自己翻仓库找脚本的老路。
    修的时候应当往 ``_IMAGE_WORDS`` 补「出一张 / 背景图 / 配图」，这条断言会随之变红，
    提醒同步更新。
    """
    assert Orchestrator._intent_hint("给主界面出一张背景图") == ""


def test_plan_and_ask_hints() -> None:
    """多步需求要求先列计划；用户把决定权丢回来时要求先 ask_user。"""
    assert "update_plan" in Orchestrator._plan_hint("新增一个卡牌筛选功能，分几步实现")
    assert "ask_user" in Orchestrator._ask_hint(requirement_text="稀有度怎么算我拿不准，你看着办")
    assert Orchestrator._ask_hint(requirement_text="短") == ""


# ---------------------------------------------------------------- 3. lite 引擎
def test_lite_lock_roundtrip(session_factory) -> None:
    """项目写锁：申请拿 token → current 能看到 → 释放后清空。"""

    async def _flow():
        ids = await _seed(session_factory, "冒烟：锁往返")
        svcs = await build_lite_services(session_factory, "smoke-worker")
        token = await svcs.locks.acquire(ids["project_id"], ids["run_id"], mode="exclusive")
        info = await svcs.locks.current(ids["project_id"])
        await svcs.locks.release(ids["project_id"], ids["run_id"], token)
        after = await svcs.locks.current(ids["project_id"])
        return token, info, after

    token, info, after = _run(_flow())
    assert isinstance(token, int) and token >= 0
    assert info is not None
    assert after is None


# ---------------------------------------------------------------- 4. 真 run
@pytest.mark.slow
def test_execute_run_end_to_end(session_factory) -> None:
    """MockBrain 跑完一整轮 modify：run → succeeded，产物与回复都落库。"""

    async def _flow():
        ids = await _seed(session_factory, "给主界面加一个卡牌详情弹窗")
        svcs = await build_lite_services(session_factory, "smoke-worker")
        orch = Orchestrator(svcs, "smoke-worker", session_factory)
        await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        messages = await svcs.repo.list_messages(ids["session_id"])
        artifacts = await svcs.repo.list_artifacts(ids["run_id"])
        events = await svcs.repo.get_events_since(ids["run_id"], 0)
        return ids, run, messages, artifacts, events

    ids, run, messages, artifacts, events = _run(_flow(), timeout=RUN_TIMEOUT_S)

    assert run is not None
    assert run.status == "succeeded", f"run 未成功：status={run.status} error={run.error}"
    assert (run.final_summary or "").strip(), "收尾摘要为空"
    # runs.usage_json 要被写上（前端 RunPanel 的「N 次调用 · X tok」依赖它）
    assert isinstance(run.usage_json, dict) and "calls" in run.usage_json, run.usage_json

    replies = [m for m in messages if m.author_type == "agent"]
    assert replies, "助手没有把回复落成消息（聊天流会是空的）"

    kinds = {a.asset_kind for a in artifacts}
    assert "image" in kinds, f"美术链没产出图片制品，实际：{kinds}"

    tool_calls = [e for e in events if e.type == "tool_call"]
    called = {str((e.payload_json or {}).get("tool")) for e in tool_calls}
    assert {"write_file", "generate_image", "report_to_user"} <= called, called

    workspace = PROJECTS_ROOT / ids["repo_path"]
    assert (workspace / "feature_agent.py").exists(), "模型没把代码真的写进工作区"


# ---------------------------------------------------------------- 5. 会话挂载技能
@pytest.mark.slow
def test_execute_run_with_attached_skills(session_factory) -> None:
    """会话挂载技能后 run 不得崩，技能清单块也要真的注入。

    回归点：``_attached_skills_block`` 曾从 core.skills 导入不存在的
    ``_SKILL_SCOPE_LABELS``，一挂技能 execute_run 就 ImportError；
    ``get_extra_roots`` 也漏过 db 导入（NameError 被守卫吞掉，清单静默变空）。
    这条测试用真实技能文件锁住两条路径。
    """

    async def _flow():
        ids = await _seed(session_factory, "给主界面加一个卡牌详情弹窗")
        workspace = PROJECTS_ROOT / ids["repo_path"]
        skill_dir = workspace / ".trace" / "skills"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "smoke-skill.md").write_text(
            "---\nname: smoke-skill\ndescription: 冒烟技能：验证挂载注入\n---\n\n# 用法\n照常干活。\n",
            encoding="utf-8",
        )
        async with session_factory() as s:
            sess = await s.get(Session, uuid.UUID(ids["session_id"]))
            sess.skills_json = ["smoke-skill"]
            await s.commit()
        svcs = await build_lite_services(session_factory, "smoke-worker")
        orch = Orchestrator(svcs, "smoke-worker", session_factory)
        block = await orch._attached_skills_block(workspace, ["smoke-skill"])
        await orch.execute_run(ids["run_id"])
        run = await svcs.repo.get_run(ids["run_id"])
        return run, block

    run, block = _run(_flow(), timeout=RUN_TIMEOUT_S)

    assert "smoke-skill" in block, f"技能清单块没注入挂载技能：{block!r}"
    assert "项目" in block, f"来源标签缺失：{block!r}"
    assert run is not None
    assert run.status == "succeeded", f"挂技能后 run 失败：status={run.status} error={run.error}"


def test_attachment_context_and_continuation(session_factory) -> None:
    async def flow():
        from datetime import datetime, timedelta, timezone

        ids = await _seed(session_factory, "读取附件")
        workspace = PROJECTS_ROOT / ids["repo_path"]
        upload = workspace / ".trace" / "uploads" / "table.xlsx"
        upload.parent.mkdir(parents=True)
        upload.write_bytes(b"fixture")
        attachment = {"path": ".trace/uploads/table.xlsx", "name": "数据表.xlsx", "size": 7}
        async with session_factory() as s:
            original = await s.get(Message, uuid.UUID(ids["message_id"]))
            original.meta_json = {"files": [attachment], "images": ["data:image/png;base64,AA=="]}
            original.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
            followup = Message(session_id=original.session_id, author_type="user", content="继续",
                               created_at=datetime.now(timezone.utc))
            s.add(followup)
            await s.commit()
            followup_id = str(followup.id)
        orch = Orchestrator(None, "context-test", session_factory)
        first, images = await orch._load_context(ids["team_id"], ids["message_id"], {}, workspace=workspace)
        assert attachment["path"] in first and "parse_document" in first
        assert "数据表.xlsx" in first and "已不存在" not in first
        assert images == ["data:image/png;base64,AA=="]
        # 注入侧（改动四 L0/L2）：环境事实全文给出，经验只给标题索引、正文要 agent 自己读
        from game_agent.orchestration import envfacts, project_notes
        envfacts.record(workspace, {"shell": "cmd", "unavailable": ["grep"]})
        project_notes.append(workspace, [{"category": "pitfall", "title": "整库扫描会卡住",
                                          "detail": "- 递归扫描必须限定子目录"}])
        with_facts, _ = await orch._load_context(ids["team_id"], ids["message_id"], {},
                                                 workspace=workspace)
        assert "环境事实" in with_facts and "grep" in with_facts, with_facts[:400]
        assert "项目改动经验索引" in with_facts and "整库扫描会卡住" in with_facts
        assert "递归扫描必须限定子目录" not in with_facts
        continued, inherited_images = await orch._load_context(ids["team_id"], followup_id, {}, workspace=workspace)
        assert "沿用上一条需求附带的 1 个文件" in continued
        assert attachment["path"] in continued and inherited_images == images
        upload.unlink()
        missing, _ = await orch._load_context(ids["team_id"], followup_id, {}, workspace=workspace)
        assert "该文件已不存在" in missing
        async with session_factory() as s:
            followup = await s.get(Message, uuid.UUID(followup_id))
            followup.meta_json = {"files": [{"path": ".trace/uploads/second.pdf", "name": "second.pdf"}]}
            await s.commit()
        replaced, _ = await orch._load_context(ids["team_id"], followup_id, {}, workspace=workspace)
        assert "[附件×1：.trace/uploads/table.xlsx]" in replaced
        assert ".trace/uploads/second.pdf" in replaced

    _run(flow())


def test_builtin_skills_available_by_default(session_factory, tmp_path, monkeypatch) -> None:
    from game_agent.core import skills

    monkeypatch.setattr(skills, "GLOBAL_SKILLS_DIR", tmp_path / "global-skills")
    skills.seed_builtin_skills()
    orch = Orchestrator(None, "context-test", session_factory)
    block = _run(orch._structure_block(tmp_path))
    assert all(f"- {name}:" in block for name in ("excel", "word", "pdf"))
    disabled = _run(orch._structure_block(tmp_path, session_skills=[]))
    assert "- excel:" not in disabled and "- word:" not in disabled and "- pdf:" not in disabled
