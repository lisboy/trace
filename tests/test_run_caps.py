"""运行能力开关：默认全开、关掉即裁剪工具 + 提示不做什么。"""
from __future__ import annotations

from game_agent.orchestration.caps import (
    CAP_MEDIA_TOOLS, CAP_TEST_TOOLS, CAP_WEB_TOOLS,
    caps_hint, caps_tool_filter, normalize_caps,
)

TOOLS = [
    "read_file", "write_file", "run_command",
    "start_service", "stop_service", "service_logs", "screenshot_page",
    "web_search", "web_fetch",
    "generate_image", "generate_audio", "view_image",
]


def test_defaults_are_all_on() -> None:
    assert normalize_caps(None) == {"test": True, "web": True, "media": True}
    assert normalize_caps({}) == {"test": True, "web": True, "media": True}
    # 只给一个键：其余保持默认开
    assert normalize_caps({"test": False}) == {"test": False, "web": True, "media": True}
    # 字符串/数字也要能归一化成 bool
    assert normalize_caps({"web": 0, "media": "false"})["web"] is False


def test_filter_removes_only_disabled_tools() -> None:
    caps = normalize_caps({"test": False})
    kept = caps_tool_filter(TOOLS, caps)
    assert not (set(kept) & CAP_TEST_TOOLS)
    # 关测试不影响写码/读文件/构建
    assert {"read_file", "write_file", "run_command"} <= set(kept)
    # 联网/媒体仍开
    assert set(kept) & CAP_WEB_TOOLS and set(kept) & CAP_MEDIA_TOOLS

    caps = normalize_caps({"web": False, "media": False})
    kept = caps_tool_filter(TOOLS, caps)
    assert not (set(kept) & CAP_WEB_TOOLS)
    assert not (set(kept) & CAP_MEDIA_TOOLS)
    assert "view_image" in kept

    # 全开等于原样返回
    assert caps_tool_filter(TOOLS, normalize_caps(None)) == TOOLS


def test_hints_say_what_not_to_do() -> None:
    on = caps_hint(normalize_caps(None))
    assert "自动化测试已开启" in on
    off = caps_hint(normalize_caps({"test": False, "web": False, "media": False}))
    assert "关闭了自动化测试" in off and "screenshot_page" in off
    assert "关闭了联网" in off
    assert "关闭了出图与配音" in off
