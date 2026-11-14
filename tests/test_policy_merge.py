"""指导项（guidance）的新内置项合并逻辑。

背景：guidance 一旦在 Settings 里存过，load_policy 就以存值为准。于是后来往
DEFAULT_GUIDANCE 加的新条目（如 spawn-when-parallel）对老安装永远不可见——
实测某库 50117 次工具调用、0 次 spawn_agents，而库里存的 guidance 恰好保存在
spawn-when-parallel 加入之前。修法是 load_policy 按 payload.builtin_ids 区分
「老安装没见过的新内置项（补）」与「用户故意删掉的内置项（尊重删除）」。
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from game_agent.db.models import Settings
from game_agent.db.session import SessionLocal
from game_agent.orchestration import policy as policy_mod


def _run(coro, timeout: float = 30) -> Any:
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(_wrapped())


async def _clear_settings():
    async with SessionLocal() as s:
        row = await s.get(Settings, policy_mod.POLICY_KEY)
        if row is not None:
            await s.delete(row)
            await s.commit()


def setup_function(_fn):
    _run(_clear_settings())


def teardown_function(_fn):
    _run(_clear_settings())


def test_unstored_uses_defaults():
    pol = _run(policy_mod.load_policy())
    ids = {g["id"] for g in pol["guidance"]}
    assert "spawn-when-parallel" in ids
    assert ids == {g["id"] for g in policy_mod.DEFAULT_GUIDANCE}


def test_new_builtin_merged_into_old_stored_payload():
    # 模拟「spawn-when-parallel 加入之前」的旧 payload：没有 builtin_ids 字段。
    old_ids = [g["id"] for g in policy_mod.DEFAULT_GUIDANCE
               if g["id"] != "spawn-when-parallel"]

    async def _seed():
        async with SessionLocal() as s:
            s.add(Settings(key=policy_mod.POLICY_KEY, value_json={
                "permission_scope": "workspace",
                "guidance": [{"id": i, "title": i, "text": i, "enabled": True}
                             for i in old_ids],
                # 旧 payload 没有 builtin_ids
            }))
            await s.commit()

    _run(_seed())
    pol = _run(policy_mod.load_policy())
    ids = {g["id"] for g in pol["guidance"]}
    # 新内置项补进来了
    assert "spawn-when-parallel" in ids
    # 旧条目原样保留
    for i in old_ids:
        assert i in ids


def test_user_deleted_builtin_not_resurrected():
    # 通过 save_policy 保存（会记录 builtin_ids）：用户删掉 spawn-when-parallel 后，
    # load_policy 必须尊重删除，而不是每次读都补回来。
    kept = [g for g in policy_mod.DEFAULT_GUIDANCE
            if g["id"] != "spawn-when-parallel"]
    _run(policy_mod.save_policy("workspace", kept))
    pol = _run(policy_mod.load_policy())
    ids = {g["id"] for g in pol["guidance"]}
    assert "spawn-when-parallel" not in ids
    assert len(ids) == len(kept)


def test_user_custom_guidance_preserved_and_new_builtin_added():
    # 用户自己写过一条自定义 + 存过旧 payload（无 builtin_ids）：
    # 自定义保留，新内置项也补进来。
    async def _seed():
        async with SessionLocal() as s:
            s.add(Settings(key=policy_mod.POLICY_KEY, value_json={
                "permission_scope": "workspace",
                "guidance": [{"id": "my-rule", "title": "我的规则",
                              "text": "遇到 X 先做 Y", "enabled": True}],
            }))
            await s.commit()

    _run(_seed())
    pol = _run(policy_mod.load_policy())
    ids = {g["id"] for g in pol["guidance"]}
    assert "my-rule" in ids
    assert "spawn-when-parallel" in ids


def test_save_records_builtin_ids():
    _run(policy_mod.save_policy("workspace",
                                [dict(g) for g in policy_mod.DEFAULT_GUIDANCE]))

    async def _check():
        async with SessionLocal() as s:
            row = await s.get(Settings, policy_mod.POLICY_KEY)
            assert row is not None
            return row.value_json or {}

    val = _run(_check())
    assert isinstance(val.get("builtin_ids"), list)
    assert "spawn-when-parallel" in val["builtin_ids"]
