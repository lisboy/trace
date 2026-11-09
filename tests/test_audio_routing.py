"""配音路由回归测试（2026-10-28 事故 run 865be9fd）。

用户说「涂山月见的配音重新生成……不要之前的预设」，模型却：
load 项目技能 → 照技能里的 python 管线自己 curl 8860 / start_service 起 IndexTTS2 /
写 target/_tushan_new_preset.py 自己合成，**全程没调用配音工具**。

平台规则（v3 直连架构）：配音一律直接调 generate_audio 工具；
只有用户明确要求「按之前 python 脚本的方式」才允许自己起服务/跑脚本。

这里锁住四层防线：
1. audio_python_allowed 的口径（默认 False，明确要求才 True，否定句不算）；
2. start_service 起 8860 / IndexTTS2 被硬拦；
3. run_command curl 8860、跑 cv 脚本、跑「内部调语音接口的自写脚本」都被硬拦；
4. generate_audio schema 暴露了 design_desc / preset_name（造新音色不用绕去脚本）。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.audio_guard import (
    audio_python_allowed,
    is_audio_script_command,
    is_audio_server_command,
    script_calls_audio,
)
from game_agent.agents.tools.native import run_command
from game_agent.agents.tools.registry import TOOL_SCHEMAS
from game_agent.agents.tools.service import start_service
from game_agent.core.context import RunContext


def _ctx(workspace: Path, user_request: str) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request=user_request)
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1. 判定口径
def test_audio_python_allowed_defaults_to_false() -> None:
    """默认一律走 generate_audio——包括「不要之前的预设」「重启语音服务」这类容易误判的句子。"""
    assert not audio_python_allowed(
        "涂山月见的配音重新生成，先重构他的语音文字，再重新配音，不要之前的预设，"
        "这是一个九尾狐少女，需要妩媚，诱人，勾魂的声线")
    assert not audio_python_allowed("把配音重新生成")
    assert not audio_python_allowed("不要用之前的方式，直接生成配音")
    # 明确要求才放行
    assert audio_python_allowed("按之前 python 脚本的方式生成配音")
    assert audio_python_allowed("用 gen-cv-indextts.py 重新生成")
    assert audio_python_allowed("重启一下语音服务")


def test_audio_command_classifiers() -> None:
    assert is_audio_server_command(
        r"D:\AI\IndexTTS2.0_v3\runtime\python.exe -u D:\AI\IndexTTS2.0_v3\server.py --port 8860")
    assert is_audio_server_command("python server.py", port=8860)
    assert not is_audio_server_command("python -m uvicorn app:app --port 8000")
    assert is_audio_script_command("curl -s http://127.0.0.1:8860/api/v1/presets")
    assert is_audio_script_command("python tools/gen-cv-indextts.py --id ur-yx-01")
    assert not is_audio_script_command("node tools/build.mjs")
    assert not is_audio_script_command("python -m pytest -q")


# ---------------------------------------------------------------- 2. start_service
def test_start_service_blocks_tts_server(tmp_path: Path) -> None:
    t = _ctx(tmp_path, "涂山月见的配音重新生成，换一个妩媚的声线")
    try:
        _run(start_service({"command": r"D:\AI\IndexTTS2.0_v3\runtime\python.exe server.py --port 8860",
                            "port": 8860}, t))
    except ToolError as e:
        assert "generate_audio" in str(e)
    else:  # pragma: no cover - 必须被拦
        raise AssertionError("start_service 没有被配音路由拦下")

    # 用户明确要求运维语音服务时放行（这里只验证判定不抛路由错误；服务本身不真起）
    t2 = _ctx(tmp_path, "重启一下语音服务")
    assert is_audio_server_command("python server.py --port 8860", 8860)


# ---------------------------------------------------------------- 3. run_command
def test_run_command_blocks_curl_tts(tmp_path: Path) -> None:
    t = _ctx(tmp_path, "把涂山月见的配音重新生成")
    try:
        _run(run_command({"command": "curl -s http://127.0.0.1:8860/api/v1/presets"}, t))
    except ToolError as e:
        assert "generate_audio" in str(e)
    else:  # pragma: no cover
        raise AssertionError("curl 8860 没有被拦")


def test_run_command_blocks_self_written_audio_script(tmp_path: Path) -> None:
    """模型被拦后写探针脚本绕行：脚本里 requests.post('http://127.0.0.1:8860')。"""
    script = tmp_path / "target" / "_probe_voice.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import requests\n"
        "requests.post('http://127.0.0.1:8860/api/v1/voice-design', data={'desc': 'x'})\n",
        encoding="utf-8")
    t = _ctx(tmp_path, "把涂山月见的配音重新生成")
    assert script_calls_audio("python target/_probe_voice.py", tmp_path)
    try:
        _run(run_command({"command": "python target/_probe_voice.py"}, t))
    except ToolError as e:
        assert "generate_audio" in str(e)
    else:  # pragma: no cover
        raise AssertionError("自写语音脚本没有被拦")


def test_script_scan_ignores_file_ops_and_outside_workspace(tmp_path: Path) -> None:
    """误杀回归（2026-10-28）：copy/读文件不许被配音路由拦，工作区外脚本不扫。

    事故：模型把平台源码 collaboration.py（文档字符串里写了 IndexTTS2）copy 进
    .trace/tmp 想读，被 script_calls_audio 当成「执行配音探针」拦下——
    copy 不是执行，平台源码也不在工作区。
    """
    # ① 命令没有执行动词（copy）→ 直接放行，哪怕目标文件正文全是 8860
    probe = tmp_path / "target" / "_probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("requests.post('http://127.0.0.1:8860/api')", encoding="utf-8")
    assert not script_calls_audio(f'copy "{probe}" .trace/tmp/x.py && echo OK', tmp_path)
    # ② 有执行动词、但脚本是工作区外的（平台源码）→ 不扫，放行
    assert not script_calls_audio(
        "python D:/DevelopSoftware/carGameAgent/game_agent/agents/tools/collaboration.py",
        tmp_path)
    # ③ 真在执行工作区内的探针脚本 → 仍然拦
    assert script_calls_audio("python target/_probe.py", tmp_path)


def test_pytest_file_argument_is_not_treated_as_executed_script(tmp_path: Path) -> None:
    """误杀回归（2026-10-30）：`python -m pytest tests/xxx.py` 里的 .py 是测试参数，
    不是被执行的脚本；测试正文里提到 indextts/8860 不等于在调语音接口。

    实测：跑 `pytest tests/test_audio_routing.py` 时被配音路由当成「自写语音脚本」拦下，
    整条测试命令直接失败。真正被执行的是 pytest 模块（-m），不是那个 .py 参数。
    """
    test_file = tmp_path / "tests" / "test_voice_words.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_a():\n"
        "    assert 'indextts' in 'x' and '8860' in 'x'\n",
        encoding="utf-8")
    assert not script_calls_audio("pytest tests/test_voice_words.py", tmp_path)
    assert not script_calls_audio("python -m pytest tests/test_voice_words.py", tmp_path)
    # 真正的探针脚本（python 直接执行）仍然要拦
    probe = tmp_path / "target" / "_probe_voice.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("x = '8860 indextts'", encoding="utf-8")
    assert script_calls_audio("python target/_probe_voice.py", tmp_path)


def test_readonly_preset_listing_allowed() -> None:
    """误杀回归（2026-10-28）：dir 看 indextts2 预设目录有哪些音色 ≠ 起服务。"""
    cmd = ('if exist "D:/AI_Small/indextts2_v3/presets" '
           '(dir /b "D:/AI_Small/indextts2_v3/presets/*") else echo NO_PRESETS_DIR')
    assert not is_audio_server_command(cmd)
    assert not is_audio_script_command(cmd)
    assert not script_calls_audio(cmd, tmp_path := Path("."))
    # 真正拉起服务/执行入口脚本的仍然拦
    assert is_audio_server_command("start D:/AI_Small/indextts2_v3/app.py --port 8860")
    assert is_audio_server_command(
        "D:/AI_Small/indextts2_v3/.venv/Scripts/python.exe main.py")
    assert is_audio_server_command("conda activate indextts2 && python app.py")


def test_run_command_allows_normal_build(tmp_path: Path) -> None:
    """普通构建/测试不能被配音路由误伤（这里只验证不被拦，命令本身会因缺文件失败）。"""
    t = _ctx(tmp_path, "把涂山月见的配音重新生成")
    assert not is_audio_script_command("node tools/build.mjs")
    assert not script_calls_audio("node tools/build.mjs", tmp_path)


# ---------------------------------------------------------------- 4. 工具 schema 与直连路由
def test_generate_audio_single_goes_direct(monkeypatch) -> None:
    """单条配音必须**直连**生成器（不再有「配音角色」可派）。

    v3 改版前，单条派发走 run_agent(role='voice') 的嵌套 LLM 循环；改版后
    generate_audio 直接调 _tts_with_meta。这里锁住直连路径，防止改回嵌套。
    """
    from game_agent.agents.tools import collaboration

    calls: list[dict] = []

    async def _fake_tts(args, ctx):
        calls.append(args)
        return "配音完成：assets/audio/voice/x.wav", {"placeholder": False}

    class _FakeOrch:  # 直连入口要求编排器在位（用于档位解析），这里给个空壳
        pass

    monkeypatch.setattr(collaboration, "_tts_with_meta", _fake_tts)
    t = _ctx(Path("."), "把涂山月见的配音重新生成")
    t.orchestrator = _FakeOrch()  # type: ignore[assignment]
    out = _run(collaboration.generate_audio(
        {"text": "大人，今晚的月色真好看。", "voice": "缘·涂山月见", "slug": "tushan-demo"}, t))
    assert calls, "单条配音没有直连 _tts_with_meta"
    assert "配音完成" in out


def test_generate_audio_batch_goes_serial(monkeypatch) -> None:
    """批量（items>1）必须走串行合成器，一条失败不拖垮整批。"""
    from game_agent.agents.tools import collaboration

    seq = {"n": 0}

    async def _fake_tts(args, ctx):
        seq["n"] += 1
        if seq["n"] == 1:
            raise RuntimeError("tts boom")
        return f"配音完成：assets/audio/voice/x{seq['n']}.wav", {"placeholder": False}

    async def _fake_progress(**kw):
        pass

    monkeypatch.setattr(collaboration, "_tts_with_meta", _fake_tts)

    class _FakeOrch:
        async def _emit(self, *a, **k):  # pragma: no cover - 测试里不校验事件负载
            pass

    t = _ctx(Path("."), "给角色们配音")
    t.orchestrator = _FakeOrch()  # type: ignore[assignment]
    out = _run(collaboration.generate_audio({"items": [
        {"slug": "a", "text": "第一句"},
        {"slug": "b", "text": "第二句"},
    ]}, t))
    assert '"real": 1' in out and "tts boom" in out  # 一条失败、一条成功


def test_intent_hint_forbids_model_starting_tts() -> None:
    """前置硬提示必须写明：不许自己起语音服务/跑脚本，唯一入口 generate_audio。"""
    from game_agent.orchestration.orchestrator import Orchestrator
    hint = Orchestrator._intent_hint("涂山月见的配音重新生成，换一个妩媚的声线")
    assert "generate_audio" in hint
    assert "start_service" in hint and "8860" in hint
    assert "design_desc" in hint and "preset_name" in hint


def test_generate_audio_exposes_voice_design_params() -> None:
    props = TOOL_SCHEMAS["generate_audio"]["parameters"]["properties"]
    for k in ("design_desc", "preset_name", "design_say", "design_n", "items"):
        assert k in props, f"generate_audio 缺少 {k}"
    desc = TOOL_SCHEMAS["generate_audio"]["description"]
    # 描述里必须把「唯一入口 + 不许自己起服务/跑脚本」写死，模型才会照做
    assert "唯一允许的配音入口" in desc
    assert "start_service" in desc and "8860" in desc
    # 助手手里必须有 generate_audio（配音是直连工具，没有独立「配音角色」了）
    from game_agent.agents.roles import ROLE_TOOLS
    assert "generate_audio" in ROLE_TOOLS["main"]
    assert set(ROLE_TOOLS) == {"main"}
