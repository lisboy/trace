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
    # 8860 被系统拒绝；让 18960 可用 → 应改绑到 18960
    _bindable_set(clean, ports={18960})
    out = tts._pick_alt_base("http://127.0.0.1:8860")
    assert out == "http://127.0.0.1:18960"


def test_rebind_preserves_scheme_and_host(clean):
    _bindable_set(clean, ports={7160})
    out = tts._pick_alt_base("https://localhost:8860")
    assert out == "https://localhost:7160"


def test_skip_port_blocked_list(clean):
    # 近邻候选即使可 bind，只要在 _PORT_BLOCKED_SKIP 里（如 7860）也要跳过
    _bindable_set(clean, ports={7860, 18961})
    out = tts._pick_alt_base("http://127.0.0.1:7760")
    assert out == "http://127.0.0.1:18961"


def test_missing_port_returns_none(clean):
    assert tts._pick_alt_base("http://127.0.0.1") is None


def test_base_remaps_stale_config_port_to_actual(clean, monkeypatch):
    """改绑后，caller 仍把旧配置端口（8860）传下来时，_base 要映射到新端口。"""
    monkeypatch.setattr(tts, "_ACTUAL_BASE", "http://127.0.0.1:18960", raising=False)
    monkeypatch.setattr(tts, "TTS_BASE_URL", "http://127.0.0.1:8860", raising=False)
    assert tts._base("http://127.0.0.1:8860") == "http://127.0.0.1:18960"
    # 不传或传新地址都走新端口
    assert tts._base("") == "http://127.0.0.1:18960"
    assert tts._base("http://127.0.0.1:18960") == "http://127.0.0.1:18960"


def test_actual_base_prefers_rebound(clean, monkeypatch):
    monkeypatch.setattr(tts, "TTS_BASE_URL", "http://127.0.0.1:8860", raising=False)
    assert tts.actual_base() == "http://127.0.0.1:8860"
    tts._ACTUAL_BASE = "http://127.0.0.1:18960"
    assert tts.actual_base() == "http://127.0.0.1:18960"
