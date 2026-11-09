"""按进程名全杀浏览器的拦截回归测试。

2026-11-05 用户截图复现：组长每次跑 CDP 自测前都先来一条
``taskkill /IM chrome.exe /F >nul 2>&1 & node target\\_cdp_xxx.mjs ...``
想「清场再起无头浏览器」，结果 chrome.exe 是全局同名进程，把用户正开着的
循迹界面一起关掉了。

这里锁两件事：
1. ``_browser_global_kill`` 认得出各种按名全杀的写法（taskkill /IM、/FI IMAGENAME、
   killall、pkill、kill $(pidof)、Stop-Process -Name、Get-Process | Stop-Process、
   wmic ... delete），不因大小写/引号/别名漏网；
2. 正常命令不误伤（按 PID 精确杀、带独立 profile 起无头 Chrome、netstat/tasklist 查询、
   杀别的进程名），且 run_command / start_service 这两个入口都真的会拦。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.native import _browser_global_kill, run_command
from game_agent.agents.tools.service import start_service
from game_agent.core.context import RunContext


def _ctx(workspace: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request="改完自测一下")
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


@pytest.mark.parametrize("cmd", [
    r"taskkill /IM chrome.exe /F > nul 2>&1 & node target\_cdp_fate_stars.mjs 5174 54642",
    "taskkill /F /IM chrome.exe",
    "taskkill /im msedge.exe /f",
    "TASKKILL /IM Chrome.exe /F",
    'taskkill /F /IM "chrome.exe"',
    'taskkill /FI "IMAGENAME eq chrome.exe" /F',
    'taskkill /f /fi "imagename eq msedge.exe"',
    "killall chrome",
    "killall -9 chrome",
    "pkill -f chrome",
    "pkill chrome",
    "kill $(pidof chrome)",
    "kill -9 $(pgrep -f chrome)",
    'powershell -c "Stop-Process -Name chrome -Force"',
    "Stop-Process -Name msedge",
    'powershell -Command "Get-Process chrome | Stop-Process -Force"',
    """wmic process where "name='chrome.exe'" delete""",
])
def test_blocks_global_browser_kill(cmd: str) -> None:
    assert _browser_global_kill(cmd), f"应识别为按名全杀：{cmd}"


@pytest.mark.parametrize("cmd", [
    "netstat -ano | findstr :54642",
    "taskkill /F /PID 12345",
    "taskkill /F /T /PID 4321",
    # 带引号的 Chrome 路径 + 独立 profile 起无头实例：这是被鼓励的做法，不能误拦
    r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --headless '
    r"--remote-debugging-port=9333 --user-data-dir=.trace/cdp-prof --screenshot=out.png "
    r"http://127.0.0.1:8000/",
    r"node target\_cdp_fate_stars.mjs 5174 54642",
    "python scripts/stop_all.py",
    "kill 12345",
    "kill -9 999",
    "tasklist | findstr node",
    "wmic process where \"name='node.exe'\" get processid,commandline",
    "wmic process get processid,commandline",
    "Get-Process python | Select-Object Id",
    "taskkill /F /PID $(netstat -ano | findstr :9333)",
])
def test_allows_targeted_or_unrelated_commands(cmd: str) -> None:
    assert not _browser_global_kill(cmd), f"不该误拦：{cmd}"


def test_run_command_blocks_global_browser_kill(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as ei:
        asyncio.run(run_command({"command": "taskkill /IM chrome.exe /F"}, _ctx(tmp_path)))
    msg = str(ei.value)
    assert "按「进程名」" in msg and "screenshot_page" in msg


def test_start_service_blocks_global_browser_kill(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as ei:
        asyncio.run(start_service(
            {"command": "taskkill /IM chrome.exe /F & node target/cdp.mjs 9222"}, _ctx(tmp_path)))
    msg = str(ei.value)
    assert "按「进程名」" in msg and "screenshot_page" in msg
