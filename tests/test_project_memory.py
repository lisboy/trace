"""项目记忆 v2：结构化融合 + 渐进式披露 + memory_read 工具。

覆盖 2026-10-29 用户反馈的三点：
1. 由模型判断把多个对话的记忆融合成一组条目（重复合并、冲突以新为准、上限 12 条）；
2. 对话上下文只注入标题索引，正文按需用 memory_read 取（不再整段堆上下文）；
3. 手动约定不参与改写，注入时保留全文。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from game_agent.agents.tools import ToolContext
from game_agent.agents.tools.native import memory_read
from game_agent.core.context import RunContext
from game_agent.db.models import Project, ProjectMemory, Team
from game_agent.db.session import SessionLocal
from game_agent.orchestration import project_memory as pm


def _run(coro, timeout: float = 20):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


def _ctx_for(project_id: str, tmp_path: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=project_id, session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None)  # type: ignore[arg-type]
    return ToolContext(run_ctx=rc, workspace=tmp_path, role="main", task_id="t")


class _FakeLLM:
    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None):
        self.prompts.append(messages[-1]["content"])

        class R:
            pass

        r = R()
        r.text = self.text
        return r


async def _new_project() -> str:
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as s:
        team = Team(name="t", slug=f"t-{tag}")
        s.add(team)
        await s.flush()
        proj = Project(team_id=team.id, name="p", slug=f"p-{tag}", repo_path=f"p-{tag}")
        s.add(proj)
        await s.commit()
        return str(proj.id)


async def _seed(project_id: str) -> None:
    async with SessionLocal() as s:
        old = ProjectMemory(project_id=pm._u(project_id), kind="auto", title="角色定位",
                            content="角色定位用 label（武人语）+ position（通用名）。\n细节：角色卡与技能模板都显示两套名字，UI 不得只显示 label；TAIL-MARK 只应出现在正文。",
                            tags="角色,命名", session_id="s-old")
        manual = ProjectMemory(project_id=pm._u(project_id), kind="manual",
                               content="美术风格：像素质感，深蓝主色。")
        s.add_all([old, manual])
        await s.commit()


def test_consolidate_merges_and_keeps_manual(monkeypatch, tmp_path: Path) -> None:
    project_id = _run(_new_project())
    _run(_seed(project_id))
    fake = _FakeLLM("""```json
{"memories":[
 {"id":"KEEP-OLD","title":"角色定位口径","body":"label 保留武人语；position 用通用定位名。","tags":["角色","命名"]},
 {"title":"素材规格","body":"立绘 1024×1024，透明底；卡面 512×720。","tags":["素材","规格"]}
]}
```""")
    # 让旧条目的 id 前缀命中（模拟模型沿用已有 id）
    monkeypatch.setattr(pm, "build_llm", lambda role, ctx=None: fake)

    # 第一次：模型给的是占位 id，会新建一条，旧的被删（模型没保留它）
    res = _run(pm.consolidate(project_id, new_text="新增：立绘 1024，透明底", force=True))
    assert res["used_ai"] is True
    assert res["after"] == 2

    entries = _run(pm.load_entries(project_id))
    manual = [e for e in entries if e["kind"] == "manual"]
    auto = [e for e in entries if e["kind"] != "manual"]
    assert len(manual) == 1 and "像素" in manual[0]["body"], "手动约定不能被改写"
    assert len(auto) == 2
    assert {e["title"] for e in auto} == {"角色定位口径", "素材规格"}
    assert any("1024" in e["body"] for e in auto)

    # 第二次：模型沿用已有 id → 原地更新，不新增
    keep_id = next(e["id"] for e in auto if e["title"] == "角色定位口径")
    fake.text = ('{"memories":[{"id":"%s","title":"角色定位口径","body":"label 武人语；position 通用名；'
                 '两个都要展示。","tags":["角色"]}]}' % keep_id[:8])
    res2 = _run(pm.consolidate(project_id, new_text="补充：两个都要展示", force=True))
    assert res2["after"] == 1
    entries2 = _run(pm.load_entries(project_id))
    auto2 = [e for e in entries2 if e["kind"] != "manual"]
    assert len(auto2) == 1 and auto2[0]["id"] == keep_id
    assert "两个都要展示" in auto2[0]["body"]


def test_index_is_progressive_and_read_returns_body(tmp_path: Path) -> None:
    project_id = _run(_new_project())
    _run(_seed(project_id))
    entries = _run(pm.load_entries(project_id))
    block = pm.index_block(entries)
    assert "项目记忆索引" in block
    assert "角色定位" in block
    # 索引里只有一份截断概括，正文尾部的细节不注入
    assert "TAIL-MARK" not in block
    # 手动约定给全文
    assert "像素质感" in block
    assert "memory_read" in block

    t = _ctx_for(project_id, tmp_path)
    out = _run(memory_read({"query": "美术风格"}, t))
    assert "像素质感" in out
    hid = entries[0]["id"][:8]
    out2 = _run(memory_read({"ids": [hid]}, t))
    assert "【" + hid + "】" in out2


def test_fallback_keeps_old_memory(monkeypatch) -> None:
    project_id = _run(_new_project())
    _run(_seed(project_id))

    def _boom(role, ctx=None):
        raise RuntimeError("no key")

    monkeypatch.setattr(pm, "build_llm", _boom)
    res = _run(pm.consolidate(project_id, new_text="新增：图标 256×256", force=True))
    assert res["used_ai"] is False
    entries = _run(pm.load_entries(project_id))
    assert any("角色定位" in e["body"] for e in entries), "模型失败不能丢旧记忆"
    assert any("图标 256" in e["body"] for e in entries)


def test_manual_merge_all_into_one(monkeypatch) -> None:
    """手动点「AI 融合」（没有新的 run 结果）必须把所有自动记忆重写成 1 份「项目现状」。"""
    project_id = _run(_new_project())
    _run(_seed(project_id))
    fake = _FakeLLM('"bad json"')  # 模型输出不可解析 → 走本地兜底合并
    monkeypatch.setattr(pm, "build_llm", lambda role, ctx=None: fake)

    res = _run(pm.consolidate(project_id, force=True))
    assert res["before"] == 1 and res["after"] == 1
    auto = [e for e in _run(pm.load_entries(project_id)) if e["kind"] != "manual"]
    assert len(auto) == 1 and auto[0]["title"] == "项目现状"
    assert "角色定位" in auto[0]["body"]
