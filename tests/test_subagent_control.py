"""子agent 控制面：单条停止 / 整轮停止连带 / 错误透出 / 插话分派 的单元测试。

覆盖用户 2026-11-11 提的四条要求里**可在纯单测层验证**的部分：
1) 子agent 可单独停止（task 级 cancel 位 → 只回一段说明，不打断整轮）；
2) 整轮停止时兜底把所有 running 子 task 收口为 cancelled（不留在「运行中」）；
3) 子agent 报错要把 error 透出到 task_done（前端才能显示「失败 + 原因」而不是永远运行中）；
4) 插话分派用模型判定目标子agent（解析 + 兜底不派）。

不拉起真实 Orchestrator / LLM：用假对象替换 run_agent 里的依赖点太重，这里直接测
可独立测试的纯函数与 _close_open_tasks 的落库行为。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from game_agent.orchestration import interject, steering


# ---------------- 插话分派：解析 + 兜底 ----------------

def test_parse_targets_extracts_valid_indexes() -> None:
    raw = '```json\n{"targets":[0,2,2,9],"reason":"两个相关"}\n```'
    assert interject._parse_targets(raw, {0, 1, 2}) == [0, 2]


def test_parse_targets_rejects_garbage() -> None:
    assert interject._parse_targets("not json", {0}) is None
    assert interject._parse_targets('{"targets":"x"}', {0}) is None
    assert interject._parse_targets("", {0}) is None


def test_decide_targets_empty_inputs_returns_empty() -> None:
    assert asyncio.run(interject.decide_targets("", [{"index": 0}])) == []
    assert asyncio.run(interject.decide_targets("看看别的", [])) == []


def test_decide_targets_llm_failure_falls_back_to_no_dispatch(monkeypatch) -> None:
    """模型不可用 → 不派（安全兜底），绝不能抛异常影响插话投递。"""
    class _BoomLLM:
        async def chat(self, *a, **k):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(interject, "build_llm", lambda *a, **k: _BoomLLM())
    subs = [{"index": 0, "name": "A", "question": "查 a"}]
    assert asyncio.run(interject.decide_targets("补充一点", subs)) == []


def test_decide_targets_reads_model_targets(monkeypatch) -> None:
    class _LLM:
        async def chat(self, *a, **k):
            class _R:
                text = '{"targets":[1],"reason":"只跟 B 相关"}'
            return _R()

    monkeypatch.setattr(interject, "build_llm", lambda *a, **k: _LLM())
    subs = [{"index": 0, "name": "A", "question": "查 a"},
            {"index": 1, "name": "B", "question": "查 b"}]
    assert asyncio.run(interject.decide_targets("顺便看 b 的旧版本", subs)) == [1]


# ---------------- 子任务信箱 ----------------

def test_task_mailbox_isolated_per_task() -> None:
    steering.push_to_task("task-A", "补充一")
    steering.push_to_task("task-B", "补充二")
    assert steering.drain_task("task-A") == [{"text": "补充一"}]
    # 取走即清空；B 不受影响
    assert steering.drain_task("task-A") == []
    assert steering.drain_task("task-B") == [{"text": "补充二"}]


def test_task_mailbox_ignores_blank() -> None:
    steering.push_to_task("task-C", "   ")
    assert steering.drain_task("task-C") == []


# ---------------- _close_open_tasks 兜底收口 ----------------

class _FakeRepo:
    def __init__(self, tasks):
        self._tasks = tasks
        self.updated: list[tuple[str, dict]] = []

    async def list_tasks(self, run_id):
        return self._tasks

    async def update_task(self, task_id, **fields):
        self.updated.append((task_id, fields))


class _FakeBus:
    def __init__(self):
        self.events = []

    async def emit(self, env):
        self.events.append(env)


def test_close_open_tasks_cancels_only_running(monkeypatch) -> None:
    from game_agent.orchestration import orchestrator as orch_mod

    repo = _FakeRepo([
        type("T", (), {"id": "t1", "status": "running", "agent_key": "sub"})(),
        type("T", (), {"id": "t2", "status": "done", "agent_key": "sub"})(),
        type("T", (), {"id": "t3", "status": "running", "agent_key": "main"})(),
    ])
    bus = _FakeBus()
    svcs = type("S", (), {"repo": repo, "bus": bus})()
    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o.svcs = svcs

    rc = type("RC", (), {"team_id": "team", "project_id": "proj", "run_id": "run"})()
    asyncio.run(o._close_open_tasks(rc, "run"))

    by_id = {tid: f for tid, f in repo.updated}
    assert by_id["t1"]["status"] == "cancelled"
    assert by_id["t3"]["status"] == "cancelled"
    assert "t2" not in by_id, "已收口的子任务不应被重复改动"
    # 每条被收口的 task 都要补一条 task_done，前端才能停掉「运行中」
    done_ids = {getattr(e, "payload", {}).get("task_id") for e in bus.events}
    assert done_ids == {"t1", "t3"}


# ---------------- 子agent 继承主agent 档位（修 403 SUBSCRIPTION_NOT_FOUND） ----------------

def test_subagent_inherits_main_profile_when_unbound() -> None:
    """sub 无自己的绑定 → 回落到主agent 的档位，绝不能让子agent 掉到 .env 全局端点。

    这就是实测 2026-11-11「6 个子agent 全部 403、主agent 正常」的根因：sub 无绑定就会
    _load_profile(None) → build_llm 用 .env 里过期的 gonode.si 订阅。
    """
    from game_agent.orchestration import orchestrator as orch_mod

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)

    async def fake_agent_profile_key(team_id, role):
        return {"main": "profile-main", "sub": None}.get(role)

    o._agent_profile_key = fake_agent_profile_key  # type: ignore[assignment]

    # sub 无绑定、未在输入框选档 → 继承 main
    assert asyncio.run(o._resolve_profile_key("team", "sub", {}, own_key=None)) == "profile-main"
    # 输入框显式选择优先于继承
    assert asyncio.run(o._resolve_profile_key(
        "team", "sub", {"model_profile_key": "user-pick"}, own_key=None)) == "user-pick"
    # sub 自己有绑定时用自己的
    assert asyncio.run(o._resolve_profile_key(
        "team", "sub", {}, own_key="profile-sub")) == "profile-sub"
    # main 没有继承逻辑：无绑定就返回 None（走全局兜底）
    assert asyncio.run(o._resolve_profile_key("team", "main", {}, own_key=None)) is None



# ---------------- 供应商模型目录窗口补齐（修「配了 256k/1M 不生效」） ----------------

def test_merge_model_catalog_params_catalog_wins() -> None:
    """目录里按模型配的窗口/视觉**优先**于档位 params（2026-11-12 修正）。

    实测：用户把目录里 Qwen3.8-Flash-AI 的窗口改成 128k 后，档位 params 残留的
    256k 仍按旧规则压着目录值 → 顶栏继续显示 256k（用户：「目录明明是 128k」）。
    窗口是按模型的属性，目录有配置就以目录为准；目录缺该模型时才用档位 params 兜底。
    """
    from game_agent.llm.context_window import merge_model_catalog_params, resolve_window

    # 档位没窗口，目录有 256000 → 补齐
    p = merge_model_catalog_params(
        {"vision": True},
        [{"name": "deepseek-v4.1-flash", "context_window": 256000}],
        "deepseek-v4.1-flash")
    assert resolve_window(p) == (256000, True)

    # 档位残留旧值 1M、目录显式配了 256k → 目录赢（按模型的配置优先）
    p = merge_model_catalog_params(
        {"context_window": 1000000},
        [{"name": "x", "context_window": 256000}], "x")
    assert resolve_window(p) == (256000, True)

    # 目录里没有这条模型 → 档位 params 原样兜底
    p = merge_model_catalog_params({"context_window": 1000000},
                                   [{"name": "other", "context_window": 1}],
                                   "deepseek-v4.1-flash")
    assert resolve_window(p) == (1000000, True)

    # 目录有该模型但没填窗口 → 也不动档位 params
    p = merge_model_catalog_params({"context_window": 1000000},
                                   [{"name": "x"}], "x")
    assert resolve_window(p) == (1000000, True)

    # 目录没该模型、档位也没配 → 原样返回，仍按默认猜（默认窗口 256k）
    p = merge_model_catalog_params({}, [{"name": "other", "context_window": 1}],
                                   "deepseek-v4.1-flash")
    assert resolve_window(p) == (256000, False)

    # 1M 简写值（1048576）也照常
    p = merge_model_catalog_params({}, [{"name": "deepseek-v4-pro",
                                         "context_window": 1048576}],
                                   "deepseek-v4-pro")
    assert resolve_window(p) == (1048576, True)
