"""文本协议工具调用解析的回归测试（text_tool_calls.py）。

真实事故（2026-11-10 山西可研会话，用户截图）：模型输出的闭合标签把 `/` 写成全角 `／`
（`<／｜DSML｜｜parameter>`），闭合正则只认半角斜杠 → 两段 grep 调用整块解析失败 →
tool_calls 为空 → runtime 按残留调用协议重问，而原文已先 emit 成 agent_message ——
DSML 原文直接漏进聊天流。这里钉死：全角斜杠的闭合标签必须能解析。
"""
from __future__ import annotations

from game_agent.llm.text_tool_calls import (
    contains_unparsed_calls,
    parse_text_calls,
)

KNOWN = {"grep", "read_file"}

# 半角斜杠（原有行为，回归保护）
_HALF = (
    '<｜DSML｜｜tool_calls>\n'
    '<｜DSML｜｜invoke name="grep">\n'
    '<｜DSML｜｜parameter name="pattern" string="true">foo</｜DSML｜｜parameter>\n'
    '<｜DSML｜｜parameter name="path" string="true">src</｜DSML｜｜parameter>\n'
    '</｜DSML｜｜invoke>\n'
    '</｜DSML｜｜tool_calls>'
)

# 全角斜杠闭合（2026-11-10 实测漏网写法）
_FULLWIDTH = (
    '<｜DSML｜｜tool_calls>\n'
    '<｜DSML｜｜invoke name="grep">\n'
    '<｜DSML｜｜parameter name="pattern" string="true">foo</／｜DSML｜｜parameter>\n'
    '<｜DSML｜｜parameter name="path" string="true">src</／｜DSML｜｜parameter>\n'
    '</／｜DSML｜｜invoke>\n'
    '</／｜DSML｜｜tool_calls>'
)


def test_halfwidth_close_parses() -> None:
    text, calls = parse_text_calls(_HALF, KNOWN)
    assert len(calls) == 2 or len(calls) == 1  # 外层 tool_calls 不算，invoke 算
    assert calls[0].name == "grep"
    assert calls[0].arguments.get("pattern") == "foo"
    assert "DSML" not in text


def test_fullwidth_slash_close_parses() -> None:
    """闭合标签用全角 ／ 也要能解析——否则 DSML 原文漏进聊天流（2026-11-10 实测）。

    注意 contains_unparsed_calls 只是「没解析出 tool_calls 时」的残留信号，解析成功
    （tool_calls 非空）后 runtime 根本不看它，这里只钉解析结果本身。
    """
    text, calls = parse_text_calls(_FULLWIDTH, KNOWN)
    assert len(calls) >= 1
    assert calls[0].name == "grep"
    assert calls[0].arguments.get("pattern") == "foo"
    assert calls[0].arguments.get("path") == "src"
    assert "DSML" not in text, "调用块应整体剥掉，不留在正文里"


def test_plain_text_untouched() -> None:
    text, calls = parse_text_calls("这只是普通回复，没有调用。", KNOWN)
    assert calls == []
    assert "普通回复" in text
