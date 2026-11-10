"""run_command 可取消 / 不再连坐 的回归测试（2026-11-10 卡死事故）。

事故现场：一次 run 里下发了 `npm build` + `pytest`，子进程挂住。旧实现用
`asyncio.to_thread(_run_shell_blocking, …)` 跑同步 `proc.communicate`——to_thread
**不可取消**，于是：
  1) 用户点「停止」时 task.cancel() 掀不翻它 → run 永远 running、界面「停止中」转圈；
  2) 旧实现还持有一把进程级 `_SHELL_LOCK`，一条挂死命令把**所有 run** 的 run_command
     全堵在锁上（64 worker 共用一个锁）→ 一起卡死。

这里锁三件事：
  A. 取消 `_run_shell_cmd` 会立刻抛出 CancelledError（不是等命令自己跑完）；
  B. run_command 对外层取消向上抛 CancelledError（不被吞成「启动命令失败」）；
  C. 一条长命令在跑时，另一条短命令能并行完成（没有全局串行锁）。

注：本项目测试不用 pytest-asyncio，统一用 sync 测试 + asyncio.run（见其它 tests）。
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

import pytest

from game_agent.agents.tools import ToolContext
from game_agent.agents.tools.native import _run_shell_cmd, run_command
from game_agent.core.context import RunContext


def _long_cmd(seconds: int) -> str:
    # 用 sys.executable，避免依赖 PATH 上的 python 是哪个
    return f'"{sys.executable}" -c "import time; time.sleep({seconds})"'


def _ctx(workspace: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request="跑一下测试")
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


def test_run_shell_cmd_is_cancellable(tmp_path: Path) -> None:
    """取消一个在跑的 30s 命令：应立刻抛 CancelledError（远早于 30s），不再卡线程。"""

    async def _run() -> None:
        task = asyncio.create_task(_run_shell_cmd(_long_cmd(30), str(tmp_path), 60))
        await asyncio.sleep(1.0)  # 让它真正起进程、进入等待
        assert not task.done()
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - t0
        # 若仍旧是「不可取消」，这里会等满 30s；正确实现应在数秒内结束
        assert elapsed < 8, f"取消后仍阻塞了 {elapsed:.1f}s，说明命令不可取消"

    asyncio.run(_run())


def test_run_command_propagates_cancelled_not_toolerror(tmp_path: Path) -> None:
    """run_command 被取消时必须向上抛 CancelledError，而不是吞成 ToolError。"""

    async def _run() -> None:
        ctx = _ctx(tmp_path)
        task = asyncio.create_task(run_command({"command": _long_cmd(30)}, ctx))
        await asyncio.sleep(1.0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_commands_are_not_globally_serialized(tmp_path: Path) -> None:
    """一条长命令在跑时，另一条短命令应能并行完成（旧全局锁下会被堵到长命令结束）。"""

    async def _run() -> None:
        long_task = asyncio.create_task(_run_shell_cmd(_long_cmd(8), str(tmp_path), 60))
        await asyncio.sleep(0.6)  # 确保长命令已经进入等待、占着一个并发槽
        try:
            t0 = time.monotonic()
            rc, out = await _run_shell_cmd("echo OK", str(tmp_path), 10)
            elapsed = time.monotonic() - t0
            assert rc == 0 and "OK" in out
            # 若被全局锁串行化，会等到长命令 8s 结束；并发实现应 < 3s
            assert elapsed < 3, f"短命令被长命令阻塞了 {elapsed:.1f}s，说明仍存在全局串行锁"
        finally:
            long_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await long_task

    asyncio.run(_run())
