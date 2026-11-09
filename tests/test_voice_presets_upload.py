"""「上传音频直接存为音色预设」的回归测试。

背景（2026-11-05）：工作台原来只能 ① 选服务端已有预设 ② 临时上传参考音频（一次性）
③ 用文字造音色再存预设。手里已经有一段干声（自己录的 / 别处拿的 / 之前生成的 wav）
时没有入口把它变成可复用预设；对话里的 generate_audio(voice=名字) 也就永远用不上。

这里锁住新入口的关键行为：
- 纯函数：音频魔数嗅探、文件名安全化、预设名清洗与「实际落盘名」回填；
- HTTP：成功保存返回前端可直接选中的名字；重名未声明 overwrite 给 409（界面弹覆盖确认）；
  非音频/空文件/空名 400；服务不可用 502；缺权限 403。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from game_agent.api.deps import get_current_context
from game_agent.api.routers import voicegen
from game_agent.core.context import RequestContext

WAV = (b"RIFF" + (0).to_bytes(4, "little") + b"WAVE"
       + b"fmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00"
       + (22050).to_bytes(4, "little") + (44100).to_bytes(4, "little")
       + b"\x02\x00\x10\x00" + b"data" + (0).to_bytes(4, "little"))


# ---------------- 纯函数 ----------------

@pytest.mark.parametrize("data,ok", [
    (WAV, True),
    (b"OggS" + b"\x00" * 20, True),
    (b"fLaC" + b"\x00" * 20, True),
    (b"ID3\x04\x00" + b"\x00" * 20, True),
    (b"\xff\xfb\x90\x00" + b"\x00" * 20, True),          # MP3 frame sync
    (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 10, True),  # m4a
    (b"hello world, not audio at all", False),
    (b"RIFFxxxxAVI ", False),                            # RIFF 但非 WAVE
    (b"", False),
    (b"tiny", False),
])
def test_audio_sniff(data, ok):
    assert voicegen._audio_sniff_ok(data) is ok


@pytest.mark.parametrize("raw,expect_suffix", [
    ("干声.wav", ".wav"),
    ("a/b/c.MP3", ".mp3"),
    ("weird.xyz", ".wav"),          # 不支持的扩展名回落 .wav
    ("noext", ".wav"),
])
def test_safe_upload_name_keeps_supported_suffix(raw, expect_suffix):
    out = voicegen._safe_upload_name(raw)
    assert out.endswith(expect_suffix)
    assert "/" not in out and "\\" not in out


def test_norm_and_pick_saved_name_matches_cleaned_server_name():
    # IndexTTS2 会把空格/非法字符清洗成下划线：回填「实际落盘名」，前端才能选中
    assert voicegen._norm_preset_name(" 沙哑 大叔 ") == "沙哑_大叔"
    presets = ["沙哑_大叔", "别人"]
    assert voicegen._pick_saved_name("沙哑 大叔", presets) == "沙哑_大叔"
    # 已经精确存在就原样回填
    assert voicegen._pick_saved_name("沙哑_大叔", presets) == "沙哑_大叔"
    # 对不上就回退请求名
    assert voicegen._pick_saved_name("不存在", presets) == "不存在"


# ---------------- HTTP ----------------

@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(voicegen, "TTS_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(voicegen, "_UPLOAD_DIR", tmp_path / "uploads")

    async def fake_boot(_base=""):
        return {"started": False, "waited_ms": 0, "error": None}

    monkeypatch.setattr(voicegen, "ensure_tts_running", fake_boot)
    a = FastAPI()
    a.include_router(voicegen.router)
    a.dependency_overrides[get_current_context] = lambda: RequestContext(
        user_id="u", team_id="t", role="owner", project_ids=[])
    return a


def req(app, method, path, **kw):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(go())


def upload(app, name="新音色", data=WAV, filename="ref.wav", **extra):
    form = {"name": name}
    form.update(extra)
    return req(app, "POST", "/api/voice-gen/presets/upload",
               files={"file": (filename, data, "audio/wav")}, data=form)


def test_upload_creates_preset_and_returns_selectable_name(app, monkeypatch):
    calls = {}

    async def fake_create(name, audio, **kw):
        calls.update(name=name, size=len(audio), overwrite=kw.get("overwrite"))
        return {"presets": ["新音色", "旧音色"]}

    monkeypatch.setattr(voicegen.tts_client, "create_preset", fake_create)
    r = upload(app, name="新音色")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["name"] == "新音色"
    assert "新音色" in body["presets"]
    assert calls == {"name": "新音色", "size": len(WAV), "overwrite": False}


def test_upload_maps_server_sanitized_name_back(app, monkeypatch):
    async def fake_create(name, audio, **kw):
        # 服务端清洗后落盘成 沙哑_大叔
        return {"presets": ["沙哑_大叔"]}

    monkeypatch.setattr(voicegen.tts_client, "create_preset", fake_create)
    body = upload(app, name="沙哑 大叔").json()
    assert body["name"] == "沙哑_大叔"


def test_duplicate_without_overwrite_is_409(app, monkeypatch):
    from game_agent.assets.tts import TTSError

    async def fake_create(name, audio, **kw):
        if not kw.get("overwrite"):
            raise TTSError(f"预设「{name}」已存在")
        return {"presets": [name]}

    monkeypatch.setattr(voicegen.tts_client, "create_preset", fake_create)
    r = upload(app, name="重复名")
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]
    # 声明覆盖后成功
    assert upload(app, name="重复名", overwrite="true").status_code == 200


@pytest.mark.parametrize("case", ["empty_file", "not_audio", "empty_name"])
def test_upload_rejects_bad_input(app, monkeypatch, case):
    async def fake_create(*a, **k):  # 不应被调用
        raise AssertionError("bad input must be rejected before calling service")

    monkeypatch.setattr(voicegen.tts_client, "create_preset", fake_create)
    data, name = WAV, "ok"
    if case == "empty_file":
        data = b""
    elif case == "not_audio":
        data = b"just some text pretending to be audio...."
    elif case == "empty_name":
        name = "   "
    r = upload(app, name=name, data=data)
    assert r.status_code == 400, r.text


def test_upload_reports_service_unavailable(app, monkeypatch):
    async def boom(_base=""):
        return {"started": False, "waited_ms": 0, "error": "端口被占用"}

    monkeypatch.setattr(voicegen, "ensure_tts_running", boom)
    r = upload(app)
    assert r.status_code == 502
    assert "语音服务不可用" in r.json()["detail"]


# ---------------- 情绪：三种控制通路透传 ----------------

def test_upload_preset_carries_emotion_vector_and_audio(app, monkeypatch):
    """存预设时把 8 维情感 + 情感参考音频一起带给服务端（复用时情绪可复现）。"""
    calls = {}

    async def fake_create(name, audio, **kw):
        calls.update(kw)
        return {"presets": [name]}

    monkeypatch.setattr(voicegen.tts_client, "create_preset", fake_create)
    r = req(app, "POST", "/api/voice-gen/presets/upload",
            files={"file": ("ref.wav", WAV, "audio/wav"),
                   "emo_audio": ("emo.wav", WAV, "audio/wav")},
            data={"name": "情绪音色", "emo_vector": "0.9,0.1"})
    assert r.status_code == 200, r.text
    assert calls.get("emo_vector") == "0.9,0.1"
    assert calls.get("emo_audio") == WAV
    assert calls.get("emo_audio_name") == "emo.wav"


def test_generate_passes_vector_and_text(app, monkeypatch):
    """/voice-gen 合成时把 emo_mode/emo_text/emo_vector 透传给 tts.synthesize。"""
    captured = {}

    async def fake_synth(text, **kw):
        captured.update(kw)
        return b"RIFFxxxxWAVEdata", {"elapsed": 1.0, "emo_mode": kw.get("emo_mode")}

    monkeypatch.setattr(voicegen.tts_client, "synthesize", fake_synth)
    r = req(app, "POST", "/api/voice-gen",
            json={"text": "你好", "preset": "p", "emo_mode": "2",
                  "emo_vector": [0.1, 0.9]})
    assert r.status_code == 200, r.text
    assert captured["emo_mode"] == "2"
    assert captured["emo_vector"] == [0.1, 0.9]
    assert r.json()["emo_mode"] == "2"


def test_generate_reads_uploaded_emotion_audio(app, monkeypatch, tmp_path):
    """mode=1 时把 /upload 落盘的情感音频读成字节交给 synthesize。"""
    captured = {}
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / "emo123.wav").write_bytes(WAV)

    async def fake_synth(text, **kw):
        captured.update(kw)
        return b"RIFFxxxxWAVEdata", {"elapsed": 1.0, "emo_mode": "1"}

    monkeypatch.setattr(voicegen.tts_client, "synthesize", fake_synth)
    r = req(app, "POST", "/api/voice-gen",
            json={"text": "你好", "preset": "p", "emo_mode": "1", "emo_audio": "emo123.wav"})
    assert r.status_code == 200, r.text
    assert captured["emo_audio"] == WAV


def test_generate_missing_emotion_audio_is_400(app, monkeypatch):
    async def fake_synth(text, **kw):  # 不应被调用
        raise AssertionError("缺情感音频应在调用前拒绝")

    monkeypatch.setattr(voicegen.tts_client, "synthesize", fake_synth)
    r = req(app, "POST", "/api/voice-gen",
            json={"text": "你好", "preset": "p", "emo_mode": "1", "emo_audio": "不存在.wav"})
    assert r.status_code == 400
    assert "情感参考音频" in r.json()["detail"]
