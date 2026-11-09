"""IndexTTS2 三种情绪控制（情感音频 / 八维向量 / 文本描述）的回归测试。

背景（2026-11-05）：此前项目只接 emo_mode=0/3 两条路，且"只给音色参考音频"时
IndexTTS2 会用平静语气念（听感就是"三句一个调、没感情"）。服务端其实支持四条：
0=同参考 1=情感参考音频 2=八维情感向量 3=文本描述。这里锁住客户端把三种素材
正确翻译成 /api/v1/tts 的表单参数（emo_mode / vec1..vec8 / emo_audio / emo_text）。

不真起服务：把 tts 内部的 HTTP 客户端换成假对象，断言它收到的 payload/files。
"""
from __future__ import annotations

import asyncio

import pytest

from game_agent.assets import tts


class _FakeResp:
    def __init__(self, status_code=200, content=b"RIFFxxxxWAVEdata"):
        self.status_code = status_code
        self.content = content
        self.text = ""

    def json(self):
        return {"presets": ["A"]}

    async def aread(self):
        return self.content


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeClient:
    """记住 stream POST 收到的 data/files，并按 done 事件 + 成品下载返回。"""

    def __init__(self, *a, **k):
        _FakeClient.last = self
        self.posted_data = None
        self.posted_files = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, data=None, files=None):
        self.posted_data = data
        self.posted_files = files
        lines = [
            'data: {"type":"done","wav":"/out/x.wav","elapsed":1.0}',
        ]
        return _FakeStream(lines)

    async def post(self, url, data=None, files=None):
        self.posted_data = data
        self.posted_files = files
        return _FakeResp(content='{"presets":["A"]}'.encode())

    async def get(self, url):
        return _FakeResp()


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(tts.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(tts, "TTS_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(tts, "_ACTUAL_BASE", None)
    # 关键：绕开真实取预设参考音频（会去打网络）
    async def _fake_preset_audio(preset, base_url=""):
        return b"REFWAV"
    monkeypatch.setattr(tts, "preset_audio", _fake_preset_audio)
    return monkeypatch


def _run(coro):
    return asyncio.run(coro)


def _synth(text, **kw):
    """synthesize 被 @media_queue / @gpu_arbiter 两层装饰（会真抢卡、起服务），
    单测直接调最内层原函数，只验证它发给服务端的表单参数。"""
    fn = tts.synthesize
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn(text, **kw)


def test_parse_emo_vector_orders_and_pads():
    # 顺序固定：高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静
    assert tts.parse_emo_vector([0.9, 0, 0, 0, 0, 0, 0, 0]) == [0.9, 0, 0, 0, 0, 0, 0, 0]
    # 字符串、缺位补 0
    assert tts.parse_emo_vector("0.8,0.2") == [0.8, 0.2, 0, 0, 0, 0, 0, 0]
    # 全 0 / 空 = None（视同没给）
    assert tts.parse_emo_vector([0] * 8) is None
    assert tts.parse_emo_vector("") is None
    assert tts.parse_emo_vector(None) is None


def test_default_mode_is_calm(fake_http):
    # 只给音色参考音频、不给任何情绪 → emo_mode=0（正是"平"这一档）
    _run(_synth("你好", preset="p"))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "0"
    assert d["emo_text"] == ""
    assert "vec1" not in d
    assert "emo_audio" not in _FakeClient.last.posted_files


def test_text_mode_sends_emo_text(fake_http):
    _run(_synth("你好", preset="p", emo_mode="3", emo_text="愤怒地质问"))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "3"
    assert d["emo_text"] == "愤怒地质问"


def test_empty_text_mode_falls_back_to_calm(fake_http):
    # 选了 3 却没描述 → 不能白送空串让服务端拿主文本乱猜，回落 0
    _run(_synth("你好", preset="p", emo_mode="3", emo_text=""))
    assert _FakeClient.last.posted_data["emo_mode"] == "0"


def test_vector_mode_sends_eight_dims(fake_http):
    _run(_synth("你好", preset="p", emo_mode="2",
                        emo_vector=[0.9, 0, 0.1, 0, 0, 0, 0, 0]))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "2"
    assert d["vec1"] == "0.9"
    assert d["vec3"] == "0.1"
    assert d["vec8"] == "0.0"


def test_vector_wins_even_if_mode_left_default(fake_http):
    # 只传 emo_vector、不传 emo_mode → 自动按 2 走（不能因为默认 0 被丢弃）
    _run(_synth("你好", preset="p", emo_vector=[0.5]))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "2"
    assert d["vec1"] == "0.5"


def test_emo_audio_mode_sends_upload(fake_http):
    # 另给一段带情绪的干声 → mode=1 且作为 emo_audio 文件上传
    _run(_synth("你好", preset="p", emo_audio=b"EMOWAV"))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "1"
    assert "emo_audio" in _FakeClient.last.posted_files
    assert _FakeClient.last.posted_files["emo_audio"][1] == b"EMOWAV"
    # mode=1 时不应再带向量/文本
    assert "vec1" not in d
    assert d["emo_text"] == ""


def test_emo_auto_uses_text_when_nothing_given(fake_http):
    # 对话朗读默认：不给任何情绪素材 → 用台词本身做情感文本走 mode=3（不再平静腔）
    _run(_synth("你终于来了，我等了好久！", preset="p", emo_auto=True))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "3"
    assert d["emo_text"] == "你终于来了，我等了好久！"


def test_emo_auto_is_ignored_when_explicit_given(fake_http):
    # 显式选了平静 → auto 不生效，仍是 0（尊重用户显式选择）
    _run(_synth("你好", preset="p", emo_auto=True, emo_mode="0"))
    assert _FakeClient.last.posted_data["emo_mode"] == "0"


def test_emo_auto_is_ignored_when_vector_given(fake_http):
    # 显式给了向量 → auto 不生效，仍是 2
    _run(_synth("你好", preset="p", emo_auto=True, emo_vector=[0.9]))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "2"
    assert d["vec1"] == "0.9"


def test_emo_auto_text_is_truncated_to_server_limit(fake_http):
    # 服务端情感文本上限 500 字 → auto 取前 480 字，避免 413
    long_text = "啊" * 900
    _run(_synth(long_text, preset="p", emo_auto=True))
    d = _FakeClient.last.posted_data
    assert d["emo_mode"] == "3"
    assert len(d["emo_text"]) == 480
    # 实际念的文本不被截断（仍是全文）
    assert len(d["text"]) == 900


def test_create_preset_carries_emotion(fake_http):
    # 存预设时把 8 维情感 + 情感音频一起带上，之后复用该音色情绪也能复现
    async def scenario():
        return await tts.create_preset(
            "角色A", b"REFWAV", emo_vector=[0.0, 0.9], emo_audio=b"EMO", emo_audio_name="e.wav")
    _run(scenario())
    d = _FakeClient.last.posted_data
    files = _FakeClient.last.posted_files
    assert d["emo_control_method"] == "2"
    assert d["vec2"] == "0.9"
    assert "emo_audio" in files
    assert files["emo_audio"][1] == b"EMO"


def test_indextts_generate_translates_profile_params(fake_http, monkeypatch):
    """Agent 工具链路：档位 params 里的 emo_vector / emo_text 要传到 synthesize。"""
    from game_agent.agents.tools import specialized

    captured = {}

    async def fake_ensure(base=""):
        return {"error": None, "started": False, "waited_ms": 0}

    async def fake_synth(text, **kw):
        captured.update(kw)
        return b"RIFFxxxxWAVEdata", {"elapsed": 1.0, "emo_mode": kw.get("emo_mode")}

    from game_agent.assets import tts as local_tts
    import game_agent.agents.tools.specialized as sp
    # specialized 内部 from game_agent.assets import tts as local_tts（同一模块对象）
    monkeypatch.setattr(local_tts, "ensure_tts_running", fake_ensure)
    monkeypatch.setattr(local_tts, "synthesize", fake_synth)
    prof = {"provider": "indextts2", "base_url": "http://127.0.0.1:9999",
            "params": {"preset": "p", "emo_mode": "2", "emo_vector": [0.2, 0.8]}}
    _run(sp._indextts_generate("你好", prof, {}))
    assert captured["emo_mode"] == "2"
    assert captured["emo_vector"] == [0.2, 0.8]


def test_indextts_generate_item_overrides_profile(fake_http, monkeypatch):
    """条目入参 emo_text 覆盖档位默认 emo_mode=0（模型按台词给情绪）。"""
    from game_agent.agents.tools import specialized as sp
    from game_agent.assets import tts as local_tts

    captured = {}

    async def fake_ensure(base=""):
        return {"error": None, "started": False, "waited_ms": 0}

    async def fake_synth(text, **kw):
        captured.update(kw)
        return b"RIFFxxxxWAVEdata", {"elapsed": 1.0, "emo_mode": kw.get("emo_mode")}

    monkeypatch.setattr(local_tts, "ensure_tts_running", fake_ensure)
    monkeypatch.setattr(local_tts, "synthesize", fake_synth)
    prof = {"provider": "indextts2", "base_url": "http://127.0.0.1:9999",
            "params": {"preset": "p", "emo_mode": "0"}}
    _run(sp._indextts_generate("你好", prof, {"emo_text": "冷冷地嘲讽"}))
    assert captured["emo_mode"] == "3"
    assert captured["emo_text"] == "冷冷地嘲讽"
