# -*- coding: utf-8 -*-
"""纯汇报项不该把 run 卡在「还有 1 项没打勾」，也不该凭空催一轮自动续跑。

真实事故（2026-11-16 读库统计，307 份清单）：
  · 97 份清单把「汇报」列成待办的一项；
  · 其中 34 个 run 收尾时就卡在这一项上 → 被判「活儿没做完」；
  · 于是收尾流程自动排一条续跑，续跑说明要求它做的正是「把这次汇报做完」，
    新一轮同样打不上勾（打勾只有 update_plan 一条路，而它做完就 report_to_user 了），
    直到 AUTO_CONTINUE_MAX 烧完。

本文件钉死两件事：
  1. is_report_only_item 只认「纯汇报」——混了实际工作的项（「起服务截图验收并汇报」）
     必须仍然算未完成，否则就等于把没做的活偷偷勾掉（那是老 bug 的另一个方向）。
  2. Orchestrator._close_out_plan 收尾时把纯汇报项标 completed 并写清原因，
     返回的未完成项里不再包含它。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Coroutine

import pytest

from game_agent.agents.tools.plan import is_report_only_item


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 120) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


@pytest.mark.parametrize("content", [
    "汇报结果",
    "向用户汇报完成情况",
    "汇报",
    "汇报结果与风险",
    "标出风险点/待定决策项，向用户汇报",
    "汇报六张真实路径与动作差异",
    "汇报文档位置与使用方式",
])
def test_pure_report_items_are_recognized(content: str) -> None:
    assert is_report_only_item({"content": content, "status": "pending"}), content


@pytest.mark.parametrize("content", [
    "起服务截图验收 + 回归测试并汇报",
    "同步 release 产物并汇报验证证据",
    "验收音频（文件、时长、可播放）并汇报台词与路径",
    "汇报结论：选模型是否会被使用、修了什么、怎么验证的",
    "汇报清理结果与 B 档详细分析",
    "修复侧栏项目行点击无法收缩",
    "生成 6 角色头像（avatar）+ 卡面（card）共 12 张",
    "梳理卡牌详情弹窗要做哪些功能点，分几步实现",
])
def test_items_with_real_work_are_not_report_only(content: str) -> None:
    """含汇报字样但带实际工作的项**不能**被收口——否则就是偷偷把没做的活勾掉。"""
    assert not is_report_only_item({"content": content, "status": "pending"}), content


def test_report_only_accepts_string_and_empty() -> None:
    assert is_report_only_item("汇报结果")
    assert not is_report_only_item("")
    assert not is_report_only_item(None)
    assert not is_report_only_item({})
    assert not is_report_only_item({"content": ""})


@pytest.mark.slow
def test_close_out_plan_closes_report_item_only(session_factory) -> None:
    """收尾：纯汇报项被标 completed 且不进未完成项；真活未完成项照旧保留。"""
    from game_agent.core.context import RunContext
    from game_agent.core.lite.container import build_lite_services
    from game_agent.db.models import Project, Run, Session, Team, User
    from game_agent.orchestration.orchestrator import Orchestrator

    async def _flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"report-item-{suffix}"
        async with session_factory() as s:
            user = User(email=f"ri-{suffix}@example.com", display_name="汇报项")
            team = Team(name="汇报项团队", slug=f"ri-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="汇报项项目", slug=repo_path,
                              repo_path=repo_path, project_type="python",
                              created_by=user.id)
            s.add(project)
            await s.flush()
            session = Session(team_id=team.id, project_id=project.id, title="汇报项会话",
                              created_by=user.id)
            s.add(session)
            await s.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                      triggered_by=user.id, run_type="modify", status="running",
                      plan_json=[
                          {"id": "p1", "content": "改好侧栏收缩", "status": "completed"},
                          {"id": "p2", "content": "汇报结果", "status": "pending"},
                          {"id": "p3", "content": "起服务截图验收并汇报", "status": "pending"},
                      ])
            s.add(run)
            await s.commit()
            ids = {"team_id": str(team.id), "project_id": str(project.id),
                   "session_id": str(session.id), "run_id": str(run.id)}

        svcs = await build_lite_services(session_factory, "ri-worker")
        orch = Orchestrator(svcs, "ri-worker", session_factory)
        ctx = RunContext(
            run_id=ids["run_id"], team_id=ids["team_id"], project_id=ids["project_id"],
            session_id=ids["session_id"], triggered_by="", run_type="modify",
            worker_id="ri-worker", repo=svcs.repo, bus=svcs.bus,
            locks=svcs.locks, store=svcs.store,
        )
        items, open_items = await orch._close_out_plan(ctx)
        run_after = await svcs.repo.get_run(ids["run_id"])
        return items, open_items, run_after

    items, open_items, run_after = _run(_flow())

    by_content = {i["content"]: i for i in items}
    # 纯汇报项：收口为 completed，且写明原因
    assert by_content["汇报结果"]["status"] == "completed"
    assert "已完成" in by_content["汇报结果"]["note"]
    # 带实际工作的项：仍然是未完成，note 说明未完成
    assert by_content["起服务截图验收并汇报"]["status"] != "completed"
    assert "未完成" in by_content["起服务截图验收并汇报"]["note"]
    # 返回的未完成项只含真活
    assert [i["content"] for i in open_items] == ["起服务截图验收并汇报"]
    # 落库的 plan_json 同样是收口后的
    stored = {i["content"]: i["status"] for i in (run_after.plan_json or [])}
    assert stored["汇报结果"] == "completed"
    assert stored["起服务截图验收并汇报"] != "completed"
