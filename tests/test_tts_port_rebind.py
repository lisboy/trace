"""语音服务端口自动改绑的回归测试。

背景（2026-11-05）：`TTS_BASE_URL` 配的 8860 正好落在 Windows/Hyper-V 的「保留端口段」
8851-8950 里。这种端口**没有任何进程监听，内核也拒绝 bind**（WinError 10013），
于是 IndexTTS2 起来 ~1 秒就死、启动器日志只有一堆 `^C^C^C^C`，界面表现为
「服务本身是通的却又调不起音频」以及「generate_audio 一直运行中 + ReadTimeout 重试」。

修复：ensure_tts_running 发现配置端口 bind 不了时，自动改绑到一个可用端口，
并把结果记在 `_ACTUAL_BASE`，让之后所有健康检查/合成/列表都走新端口（不改库里的档位）。

这里只锁「决策逻辑」（纯函数），不真的起服务：
- 远端地址一律不动；
- 配置端口本来能 bind 就不改；
- 只有「本机 + 配置端口被系统拒绝」才挑近似/高位候选；
- `_base`/`actual_base` 在改绑后要一致地返回新端口。
"""
from __future__ import annotations

import pytest

from game_agent.assets import tts


@pytest.fixture
def clean(monkeypatch):
    """隔离模块级全局，测完还原。"""
    monkeypatch.setattr(tts, "_ACTUAL_BASE", None, raising=False)
    yield monkeypatch
    tts._ACTUAL_BASE = None


def _bindable_set(monkeypatch, ports):
    monkeypatch.setattr(tts, "_port_bindable", lambda p: p in set(ports))


def test_remote_address_is_never_rebound(clean):
    # 远端地址即使「bind 不了」（这里模拟全不可 bind）也不能动
    _bindable_set(clean, ports=set())
    assert tts._pick_alt_base("http://10.0.0.5:8860") is None


def test_configured_port_bindable_means_no_change(clean):
    # 配置端口本身可用 → 不改（保持 8860）
    _bindable_set(clean, ports={8860})
    assert tts._pick_alt_base("http://127.0.0.1:8860") is None


def test_reserved_port_rebinds_to_a_bindable_candidate(clean):
    # 8860 被系统拒绝；让 10188 可用 → 应改绑到 10188（候选均 >10000）
    _bindable_set(clean, ports={10188})
    out = tts._pick_alt_base("http://127.0.0.1:8860")
    assert out == "http://127.0.0.1:10188"


def test_rebind_preserves_scheme_and_host(clean):
    _bindable_set(clean, ports={11088})
    out = tts._pick_alt_base("https://localhost:8860")
    assert out == "https://localhost:11088"


def test_candidates_are_above_10000(clean):
    # 用户要求端口一律 >10000：即使低位端口（如 7860/7000）可 bind 也不能选
    _bindable_set(clean, ports={7860, 7160, 7000, 10288})
    out = tts._pick_alt_base("http://127.0.0.1:8860")
    assert out == "http://127.0.0.1:10288"


def test_no_candidate_above_floor_returns_none(clean):
    # 只有低位端口可用（全 <=10000）→ 拒绝改绑，返回 None（宁可报错也不降到低位）
    _bindable_set(clean, ports={7860, 7160, 7000})
    assert tts._pick_alt_base("http://127.0.0.1:8860") is None


def test_missing_port_returns_none(clean):
    assert tts._pick_alt_base("http://127.0.0.1") is None


def test_base_remaps_stale_config_port_to_actual(clean, monkeypatch):
    """改绑后，caller 仍把旧配置端口（8860）传下来时，_base 要映射到新端口。"""
    monkeypatch.setattr(tts, "_ACTUAL_BASE", "http://127.0.0.1:10188", raising=False)
    monkeypatch.setattr(tts, "TTS_BASE_URL", "http://127.0.0.1:8860", raising=False)
    assert tts._base("http://127.0.0.1:8860") == "http://127.0.0.1:10188"
    # 不传或传新地址都走新端口
    assert tts._base("") == "http://127.0.0.1:10188"
    assert tts._base("http://127.0.0.1:10188") == "http://127.0.0.1:10188"


def test_actual_base_prefers_rebound(clean, monkeypatch):
    monkeypatch.setattr(tts, "TTS_BASE_URL", "http://127.0.0.1:8860", raising=False)
    assert tts.actual_base() == "http://127.0.0.1:8860"
    tts._ACTUAL_BASE = "http://127.0.0.1:10188"
    assert tts.actual_base() == "http://127.0.0.1:10188"


# ------------------------------------------------ 改绑/跟随（_maybe_rebind / discover）
def test_maybe_rebind_skips_when_external_tts_holds_the_port(clean, monkeypatch):
    """配置端口 bind 不了，但正被「外部 IndexTTS 实例」占用（它在加载、暂探不通）→
    不能改绑，否则会另起一个实例双开抢卡。"""
    monkeypatch.setattr(tts, "_port_bindable", lambda p: False)
    monkeypatch.setattr(tts, "_find_starting_tts",
                        lambda: {"pid": 1234, "cmdline": "python server.py --port 8860", "port": 8860})
    assert tts._maybe_rebind("http://127.0.0.1:8860") == "http://127.0.0.1:8860"
    assert tts._ACTUAL_BASE is None


def test_maybe_rebind_rebinds_when_reserved_and_no_external(clean, monkeypatch):
    """端口被系统保留、且没有外部实例占用 → 改绑到 >10000 的可用端口并记住。"""
    monkeypatch.setattr(tts, "_port_bindable", lambda p: p == 10188)
    monkeypatch.setattr(tts, "_find_starting_tts", lambda: None)
    out = tts._maybe_rebind("http://127.0.0.1:8860")
    assert out == "http://127.0.0.1:10188"
    assert tts._ACTUAL_BASE == "http://127.0.0.1:10188"


def test_maybe_rebind_remote_untouched(clean, monkeypatch):
    monkeypatch.setattr(tts, "_port_bindable", lambda p: False)
    assert tts._maybe_rebind("http://10.0.0.5:8860") == "http://10.0.0.5:8860"
    assert tts._ACTUAL_BASE is None


def test_discover_external_follows_started_port(clean, monkeypatch):
    """配置端口探不通，但外部实例已绑在别的端口并能回话 → 跟随该端口。"""
    monkeypatch.setattr(tts, "TTS_BASE_URL", "http://127.0.0.1:10189", raising=False)
    monkeypatch.setattr(tts, "_find_starting_tts",
                        lambda: {"pid": 9, "cmdline": "python server.py --port 10289", "port": 10289})

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, *a, **k):
            return _Resp() if url.startswith("http://127.0.0.1:10289") else None

    monkeypatch.setattr(tts.httpx, "Client", _Client)
    out = tts.discover_external(probe=True)
    assert out == "http://127.0.0.1:10289"
    assert tts._ACTUAL_BASE == "http://127.0.0.1:10289"
