"""start_service 启动前校验的回归测试。

2026-10-30 用户截图复现：用
`"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --headless=new ...`
起调试浏览器时，start_service 报「命令不在白名单中：program」——
原因是 ``_cmd_head`` 把带空格的引号路径按空白切碎，取到了 ``C:\\Program``。

这里锁住两件事：
1. 带空格的引号路径 / 绝对路径 / 普通命令都能解析出正确的命令头；
2. start_service 的默认策略与 run_command 一致：open 模式只拦危险动作，不再按白名单卡；
   strict 模式（GA_SHELL_POLICY=strict）才查白名单。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.native import _effective_whitelist, cmd_head
from game_agent.agents.tools import service as service_mod
from game_agent.agents.tools.service import _cmd_head, start_service
from game_agent.core.context import RunContext


def _ctx(workspace: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request="改完自测一下")
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1. 命令头解析
@pytest.mark.parametrize("cmd, want", [
    (r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new '
     r'--remote-debugging-port=9711 --user-data-dir="D:\GameSoftWare\target\_cdp-test"', "chrome"),
    (r'"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless', "msedge"),
    (r'"C:\Program Files\nodejs\npm.cmd" run dev', "npm"),
    (r'"D:\DevelopSoftware\carGameAgent\.venv\Scripts\python.exe" -m game_agent.main', "python"),
    (r"C:\Python313\python.exe -m http.server 8000", "python"),
    ("node tools/dev.mjs", "node"),
    ("python -m uvicorn game_agent.main:app --port 8000", "python"),
    ("powershell -NoProfile -Command \"Get-Process node\"", "powershell"),
])
def test_cmd_head_handles_quoted_paths(cmd: str, want: str) -> None:
    assert cmd_head(cmd) == want
    # service.py 里的旧调用点走同一份解析，不能再出现 program
    assert _cmd_head(cmd) == want


def test_cmd_head_rejects_old_bug() -> None:
    """旧实现把 `"C:\\Program Files\\...\\chrome.exe"` 解析成 program —— 这条必须保持不回归。"""
    quoted = r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new'
    assert cmd_head(quoted) != "program"


def test_allow_commands_supports_quoted_path(tmp_path: Path) -> None:
    """项目 .trace/allow_commands.txt 里写带空格的引号路径时，也要能正确变成命令头。"""
    agent_dir = tmp_path / ".trace"
    agent_dir.mkdir()
    (agent_dir / "allow_commands.txt").write_text(
        r'"C:\Program Files\Google\Chrome\Application\chrome.exe"' "\n"
        "node\n", encoding="utf-8")
    allowed = _effective_whitelist(tmp_path)
    assert "chrome" in allowed
    assert "node" in allowed


# ---------------------------------------------------------------- 2. start_service 策略
def test_start_service_open_policy_does_not_whitelist_block(tmp_path: Path, monkeypatch) -> None:
    """open 策略下，白名单外的正常命令不能被「命令不在白名单」拦掉（应走到真正启动）。"""
    monkeypatch.setattr(service_mod, "SHELL_POLICY", "open")

    class _Boom(RuntimeError):
        pass

    def _fake_popen(*a, **kw):  # noqa: ANN002, ANN003
        raise _Boom("popen-reached")

    monkeypatch.setattr(service_mod.subprocess, "Popen", _fake_popen)
    try:
        _run(start_service({
            "command": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
                       r'--headless=new --remote-debugging-port=9711',
            "port": 9711,
        }, _ctx(tmp_path)))
    except ToolError as e:
        assert "命令不在白名单" not in str(e), str(e)
        # 说明已越过白名单校验，走到真正启动那一步
        assert "启动失败" in str(e) and "popen-reached" in str(e), str(e)
    else:  # pragma: no cover
        raise AssertionError("fake Popen 应该抛错")


def test_start_service_strict_policy_still_blocks(tmp_path: Path, monkeypatch) -> None:
    """strict 模式保留白名单兜底（显式选择才启用）。"""
    monkeypatch.setattr(service_mod, "SHELL_POLICY", "strict")
    try:
        _run(start_service({
            "command": r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new',
            "port": 9711,
        }, _ctx(tmp_path)))
    except ToolError as e:
        assert "命令不在白名单" in str(e) and "chrome" in str(e), str(e)
    else:  # pragma: no cover
        raise AssertionError("strict 模式应该拦住白名单外的命令")


def test_start_service_blocks_dangerous_command(tmp_path: Path) -> None:
    """危险动作无论 open/strict 都要拦。"""
    try:
        _run(start_service({"command": "format c:", "port": 9999}, _ctx(tmp_path)))
    except ToolError as e:
        assert "格式化磁盘" in str(e), str(e)
    else:  # pragma: no cover
        raise AssertionError("危险命令应被拦")
