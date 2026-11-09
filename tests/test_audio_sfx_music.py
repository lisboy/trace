"""音频「没声音」回归测试（2026-10-31）。

事故：用户在卡牌项目里让 agent 做音效/BGM，generate_audio 连续 3 次报
「缺 text（要念的台词）」；即使绕过参数问题，generate_sfx / generate_music
也永远写全零静音 wav——用户听到的就是「音频没有声音」。

本文件锁住：
1. sfx / music 不需要 text（prompt 即可），voice 仍必须 text；
2. 内置合成器输出非静音波形（峰值/RMS 阈值）；
3. generate_sfx / generate_music 落盘的是真实可听音频，provider=synth，
   is_placeholder=False；
4. _tts_with_meta 遇到全零 wav 会拒绝交付（不能把静音当真实配音）；
5. registry schema 把 kind/prompt/duration_s 暴露给模型。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.collaboration import _audio_jobs, generate_audio
from game_agent.agents.tools.registry import TOOL_SCHEMAS
from game_agent.agents.tools.specialized import generate_music, generate_sfx, _tts_with_meta
from game_agent.assets import synth
from game_agent.core.context import RunContext


class _Repo:
    def __init__(self) -> None:
        self.artifacts: list[dict] = []
        self.placeholder_runs: list[bool] = []

    async def create_artifact(self, **kw):
        self.artifacts.append(kw)
        return kw

    async def update_run(self, run_id, **kw):
        if "has_placeholder" in kw:
            self.placeholder_runs.append(kw["has_placeholder"])


class _Bus:
    async def emit(self, env):
        pass


class _Orch:
    async def resolve_media_profile(self, team_id, kind):
        return {"provider": "indextts2", "model": "", "base_url": "http://127.0.0.1:8860",
                "api_key": None, "params": {"preset": "顾霜心"}}

    async def _emit(self, *a, **k):
        pass


def _ctx(tmp_path: Path, request: str = "做音效") -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=_Repo(), bus=_Bus(), locks=None, store=None,  # type: ignore[arg-type]
                    user_request=request)
    t = ToolContext(run_ctx=rc, workspace=tmp_path, role="main", task_id="t")
    t.orchestrator = _Orch()  # type: ignore[assignment]
    return t


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1. 参数链路
def test_audio_jobs_sfx_music_do_not_require_text() -> None:
    """sfx/music 不需要 text；以前 all(...) 空迭代器恒真导致「缺 text」误拒。"""
    jobs = _audio_jobs({"kind": "sfx", "slug": "click", "prompt": "清脆的按钮点击声"})
    assert len(jobs) == 1 and jobs[0]["kind"] == "sfx"
    jobs = _audio_jobs({"kind": "music", "slug": "home", "prompt": "温柔的主城背景音乐",
                        "duration_s": 8})
    assert len(jobs) == 1 and jobs[0]["duration_s"] == 8
    # voice 仍然必须 text
    assert _audio_jobs({"kind": "voice", "slug": "x"}) == []


def test_generate_audio_routes_sfx_without_text(tmp_path: Path) -> None:
    """端到端：generate_audio(kind=sfx, prompt=...) 不再报「缺 text」。"""
    t = _ctx(tmp_path)
    out = _run(generate_audio({"kind": "sfx", "slug": "click", "prompt": "清脆的按钮点击声",
                               "out": "web/audio/sfx/click.wav"}, t))
    assert "缺 text" not in out
    p = tmp_path / "web/audio/sfx/click.wav"
    assert p.is_file() and p.stat().st_size > 1000
    assert not synth.analyze_wav(p.read_bytes())["silent"]


# ---------------------------------------------------------------- 2. 合成器质量
def test_synth_sfx_is_audible_for_common_profiles() -> None:
    from game_agent.assets.synth import analyze_wav, synthesize_sfx
    for prompt in ("清脆的按钮点击声", "金币掉落", "重击命中", "胜利号角", "魔法施法"):
        data, info = synthesize_sfx(prompt, "t")
        probe = analyze_wav(data)
        assert not probe["silent"], f"{prompt} 合成结果静音"
        assert probe["peak"] > 0.05 and probe["duration_s"] > 0.03
        assert info["profile"]


def test_synth_music_is_audible_and_bounded() -> None:
    from game_agent.assets.synth import analyze_wav, synthesize_music
    data, info = synthesize_music("温柔的主城背景音乐", 5, "home")
    probe = analyze_wav(data)
    assert not probe["silent"]
    assert 4.9 <= probe["duration_s"] <= 5.1
    assert probe["peak"] > 0.05


def test_wav_tool_roundtrip_and_silent_detection() -> None:
    from game_agent.assets.placeholder import generate_silent_wav
    from game_agent.assets.synth import analyze_wav
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        p = f.name
    try:
        generate_silent_wav(p, "测试")
        probe = analyze_wav(Path(p).read_bytes())
        assert probe["silent"] is True
    finally:
        Path(p).unlink(missing_ok=True)


# ---------------------------------------------------------------- 3. 落盘与元数据
def test_generate_sfx_writes_real_audible_artifact(tmp_path: Path) -> None:
    t = _ctx(tmp_path)
    out = _run(generate_sfx({"slug": "coin", "prompt": "金币掉落叮当声",
                             "out": "web/audio/sfx/coin.wav"}, t))
    assert "占位" not in out
    p = tmp_path / "web/audio/sfx/coin.wav"
    assert p.is_file()
    assert not synth.analyze_wav(p.read_bytes())["silent"]
    art = t.run_ctx.repo.artifacts[-1]
    assert art["provider"] == "synth" and art["is_placeholder"] is False


def test_generate_music_writes_real_audible_artifact(tmp_path: Path) -> None:
    t = _ctx(tmp_path)
    out = _run(generate_music({"slug": "home", "prompt": "温柔的主城背景音乐",
                               "duration_s": 6, "out": "web/audio/bgm/home.wav"}, t))
    assert "占位" not in out
    p = tmp_path / "web/audio/bgm/home.wav"
    assert p.is_file()
    probe = synth.analyze_wav(p.read_bytes())
    assert not probe["silent"] and 5.9 <= probe["duration_s"] <= 6.1
    art = t.run_ctx.repo.artifacts[-1]
    assert art["provider"] == "synth" and art["is_placeholder"] is False


# ---------------------------------------------------------------- 4. 静音防线
def test_tts_rejects_silent_wav_from_provider(tmp_path: Path, monkeypatch) -> None:
    """TTS 服务返回全零 wav 时必须报错/回退占位，不能当「真实配音」交付。"""
    from game_agent.agents.tools import specialized

    async def _silent_generate(text, prof, args, workspace=None, on_done=None):
        import io
        import wave
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 22050)
        return bio.getvalue(), "wav"

    monkeypatch.setattr(specialized, "_indextts_generate", _silent_generate)
    t = _ctx(tmp_path)
    text, meta = _run(_tts_with_meta({"text": "测试", "slug": "silent", "voice": "顾霜心"}, t))
    assert meta["placeholder"] is True
    assert "静音" in meta["error"] or "无声音" in meta["error"]
    p = tmp_path / "assets/audio/voice/silent.wav"
    assert p.is_file()  # 回退的占位文件确实存在
    assert synth.analyze_wav(p.read_bytes())["silent"] is True


# ---------------------------------------------------------------- 5. schema
def test_generate_audio_schema_has_prompt_and_kind() -> None:
    props = TOOL_SCHEMAS["generate_audio"]["parameters"]["properties"]
    for k in ("text", "prompt", "kind", "duration_s", "items"):
        assert k in props, f"generate_audio 缺少 {k}"
    desc = TOOL_SCHEMAS["generate_audio"]["description"]
    assert "sfx/music" in desc and "prompt" in desc
    assert "静音" in desc
    # required 不能包含 text：宿主层先按 schema 校验，sfx/music 只传 prompt 会被拦。
    required = TOOL_SCHEMAS["generate_audio"]["parameters"].get("required") or []
    assert "text" not in required, "sfx/music 不需要 text，schema 不能强制 required=['text']"


# ---------------------------------------------------------------- 6. 目录式 out
def test_dir_out_resolves_per_slug(tmp_path: Path) -> None:
    """out 写成目录（web/audio/cv/me/）时，每条按 slug 落成独立文件，不能再互相覆盖。

    事故（2026-10-31）：24 条台词 out 都传同一个目录，旧实现把它当成无扩展名文件
    `web/audio/cv/me`，全部互相覆盖，最后只剩一个无后缀文件——用户「生成完却取不到」。
    """
    from game_agent.agents.tools.specialized import _out_file_rel, _safe_out_path

    d = _safe_out_path(tmp_path, "web/audio/cv/me/")
    assert d == "web/audio/cv/me/", d
    a = _out_file_rel(d, "story-prologue-2", "wav")
    b = _out_file_rel(d, "story-prologue-4", "wav")
    assert a == "web/audio/cv/me/story-prologue-2.wav"
    assert b == "web/audio/cv/me/story-prologue-4.wav"
    assert a != b
    # 明确给文件名时原样返回
    assert _out_file_rel(_safe_out_path(tmp_path, "web/audio/cv/me/x.mp3"), "ignored", "wav") \
        == "web/audio/cv/me/x.mp3"


def test_audio_jobs_batch_dir_out_maps_to_distinct_files(tmp_path: Path) -> None:
    """批量 + 顶层 out=目录：_audio_jobs 展开出的每条都是独立文件路径。"""
    from game_agent.agents.tools.specialized import _out_file_rel, _safe_out_path

    jobs = _audio_jobs({"out": "web/audio/cv/me/", "items": [
        {"slug": "a", "text": "一"}, {"slug": "b", "text": "二"}, {"slug": "c", "text": "三"},
    ]})
    assert len(jobs) == 3
    rels = [_out_file_rel(_safe_out_path(tmp_path, str(j["out"])), j["slug"], "wav") for j in jobs]
    assert len(set(rels)) == 3, rels


# ---------------------------------------------------------------- 7. 批量造音色去重
def test_design_preset_dedup_batch(monkeypatch) -> None:
    """顶层 design_desc 广播到整批时，同一预设名只造一次音色。

    事故（2026-10-31）：24 条台词每条都触发一次几十秒的音色设计，整批像卡死，
    永远轮不到真正的台词合成。
    """
    from game_agent.agents.tools import collaboration

    calls = {"design": 0, "save": 0}

    async def _fake_design(desc, say, n=1, base_url=""):
        calls["design"] += 1
        return {"ok": True, "files": [{"name": f"cand_{calls['design']}.wav", "seconds": 1.0}]}

    async def _fake_save(name, file, base_url="", overwrite=False):
        calls["save"] += 1
        return {"ok": True}

    async def _fake_ensure(base=""):
        return {"error": None, "started": False}

    monkeypatch.setattr("game_agent.assets.tts.design_voice", _fake_design)
    monkeypatch.setattr("game_agent.assets.tts.design_save_preset", _fake_save)
    monkeypatch.setattr("game_agent.assets.tts.ensure_tts_running", _fake_ensure)

    jobs = _audio_jobs({
        "design_desc": "青年男性主角，干净阳光",
        "preset_name": "拾缘人·男主",
        "items": [{"slug": f"s{i}", "text": f"台词{i}"} for i in range(6)],
    })
    note, made = _run(collaboration._design_preset_if_needed(jobs, _ctx(Path("."))))
    assert calls["design"] == 1, f"应只造一次音色，实际 {calls['design']}"
    assert calls["save"] == 1
    assert len(made) == 1
    assert all(j.get("voice") == "拾缘人·男主" for j in jobs)


# ---------------------------------------------------------------- 8. 成品捡回
def test_recover_local_wav_reads_server_output(tmp_path: Path, monkeypatch) -> None:
    """客户端超时/断开时，能从服务端已落盘的成品 wav 捡回字节。"""
    import wave
    import io
    from game_agent.agents.tools.specialized import _recover_local_wav

    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    wav = out_dir / "line_20261031_190000.wav"
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x00\x10" * 22050)
    wav.write_bytes(bio.getvalue())

    got = _recover_local_wav(str(wav))
    assert got and got[:4] == b"RIFF"
    assert _recover_local_wav("") is None
    assert _recover_local_wav(str(out_dir / "nope.wav")) is None


# ---------------------------------------------------------------- 9. 端到端：目录式 out 真实落盘
def test_generate_audio_batch_dir_out_writes_distinct_files(tmp_path: Path, monkeypatch) -> None:
    """批量 + 顶层 out=目录，走完整 _audio_batch，真的落成 3 个独立可听文件。"""
    import io
    import wave
    from game_agent.agents.tools import specialized

    def _good_wav() -> bytes:
        buf = [0.0] * 2205
        for i in range(2205):
            buf[i] = 0.4 * __import__("math").sin(2 * 3.14159 * 440 * i / 22050)
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"".join(
                int(max(-1.0, min(1.0, v)) * 32767).to_bytes(2, "little", signed=True)
                for v in buf))
        return bio.getvalue()

    async def _fake_indextts(text, prof, args, workspace=None, on_done=None):
        return _good_wav(), "wav"

    async def _fake_ensure(base=""):
        return {"error": None, "started": False}

    monkeypatch.setattr(specialized, "_indextts_generate", _fake_indextts)
    monkeypatch.setattr("game_agent.assets.tts.ensure_tts_running", _fake_ensure)

    t = _ctx(tmp_path)
    out = _run(generate_audio({
        "out": "web/audio/cv/me/",
        "items": [{"slug": "a", "text": "第一句台词"},
                  {"slug": "b", "text": "第二句台词"},
                  {"slug": "c", "text": "第三句台词"}],
    }, t))
    import json as _json
    res = _json.loads(out)
    assert res["real"] == 3 and res["placeholder"] == 0 and len(res["failed"]) == 0, res
    for slug in ("a", "b", "c"):
        p = tmp_path / f"web/audio/cv/me/{slug}.wav"
        assert p.is_file(), f"缺少 {p}"
        assert synth.analyze_wav(p.read_bytes())["silent"] is False
    # 不能再出现那个无扩展名的覆盖文件
    assert not (tmp_path / "web/audio/cv/me").is_file()
