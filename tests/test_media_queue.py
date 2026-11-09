"""媒体生成全局串行队列：跨会话/跨类型排队的最小单测（不碰真实生成服务）。"""
from __future__ import annotations

import asyncio

from game_agent.assets import media_queue


def _run(coro, timeout: float = 30):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(_wrapped())


def test_slots_are_globally_serialized_across_kinds() -> None:
    """图片与语音共用一条队列：同一时刻只有一个生成在跑。"""
    order: list[str] = []
    running = {"now": 0, "max": 0}

    async def job(name: str, kind: str) -> None:
        async with media_queue.media_slot(kind, name):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            order.append(f"start:{name}")
            await asyncio.sleep(0.05)
            order.append(f"end:{name}")
            running["now"] -= 1

    async def _flow():
        await asyncio.gather(job("A", "image"), job("B", "voice"), job("C", "voice"))

    _run(_flow())
    assert running["max"] == 1, order
    assert len(order) == 6


def test_same_task_reentrant_no_deadlock() -> None:
    """同一任务嵌套占用（工具入口 + 底层函数都包了一层）只加深度，不会自锁。"""
    async def _flow():
        async with media_queue.media_slot("voice", "x"):
            async with media_queue.media_slot("voice", "x"):
                assert media_queue.busy()
        assert not media_queue.busy()

    _run(_flow())


def test_waiter_count_and_status() -> None:
    async def _flow():
        gate = asyncio.Event()

        async def hold() -> None:
            async with media_queue.media_slot("image", "holder"):
                await gate.wait()

        task = asyncio.create_task(hold())
        await asyncio.sleep(0.02)
        assert media_queue.busy()
        gate.set()
        await task
        assert not media_queue.busy()
        assert media_queue.status()["waiting"] == 0

    _run(_flow())
