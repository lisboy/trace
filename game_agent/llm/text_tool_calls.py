"""正文里的「文本协议工具调用」解析（兜底路径）。

部分中转站在某些档位上不走结构化 tool_calls，而是把调用按聊天模板的分隔符包起来
直接吐进 content（DSML 风格：尖括号 + 竖线 + DSML + 竖线 + invoke name="read_file"，
参数用 parameter name="path" string="true" 包住值，再各自闭合）。各家前缀不同，
还有 tool_call 命名空间与 function 模板两类写法。

这类 token 不解析的话，本轮 tool_calls 为空，runtime 当成「模型说完了」→ run 直接
收尾，界面上就是模型把调用协议当回复吐出来然后停住（实测：deepseek-v4.1-flash
经 gonode 中转时稳定复现）。

正则一律用字符串拼接构造，避免本文件里出现完整的模板分隔符字面量。
"""
from __future__ import annotations

import json
import re
from typing import Any

from game_agent.llm.base import ToolCall

# 分隔符：竖线（半角/全角）、空白、斜杠可以任意顺序出现在尖括号与关键词之间
_SEP = r"[|｜\s/]*"
# 命名空间段：各家模板在关键词前带自己的标记（DSML / tool_call / tool_calls）
_NS = r"(?:DSML|tool_calls?|functions?)?" + _SEP
# 开始标签前缀
_OPEN = r"<" + _SEP + _NS
# 闭合标签前缀：斜杠可出现在竖线之后或尖括号之后（两种写法都见过）
_CLOSE = r"<" + r"[|｜\s]*" + r"/" + _SEP + _NS
_KW = r"(?:invoke|call|tool_call)"
# 关键词后面紧跟竖线 = 它其实是命名空间段（如 tool_call|parameter），不是标签名
_KW_T = _KW + r"\b(?![|｜])"
# name 取值有两种写法：带引号（DSML/function 属性式）与不带引号（等号式）
_NAME_ALT = r"\bname\s*=\s*(?:[\"']([^\"']+)[\"']|([^\s\"'>]+))"

_INV_RE = re.compile(_OPEN + _KW_T + r"[^>]*?" + _NAME_ALT, re.I)
_CLOSE_RE = re.compile(_CLOSE + _KW_T + r"\s*>", re.I)
_PARAM_RE = re.compile(
    _OPEN + r"parameter\b[^>]*?" + _NAME_ALT + r"(?P<rest>[^>]*)>"
    r"(?P<val>.*?)" + _CLOSE + r"parameter\s*>", re.I | re.S)
_STR_TRUE_RE = re.compile(r"\bstring\s*=\s*[\"']?true", re.I)
# 残留的模板标签（外层 calls 包裹之类）：尖括号里含竖线的整段剥掉
_LEFTOVER_RE = re.compile(r"<[^>\n]*[|｜][^>\n]*>")

# function 模板：开始标签的取值写法有两种（等号式与 name 属性式），参数标签不带 string 声明
_FUNC_RE = re.compile(
    r"<" + _SEP + r"function\s*(?:=\s*[\"']?|\s+name\s*=\s*[\"'])([^\"'>\s]+)[\"']?\s*>", re.I)
_FUNC_CLOSE_RE = re.compile(r"<" + r"[|｜\s]*" + r"/" + _SEP + r"function\s*>", re.I)
# function 模板的参数标签有两种写法：属性式（name 加引号）与等号式（等号后直接跟参数名）
_PNAME = r"(?:" + _NAME_ALT + r"|=\s*[\"']?([^\"'\s>]+)[\"']?)"
_FUNC_ARG_RE = re.compile(
    r"<" + _SEP + r"parameter\b[^>]*?" + _PNAME + r"\s*>(?P<val>.*?)"
    r"<" + r"[|｜\s]*" + r"/" + _SEP + r"parameter\s*>", re.I | re.S)


def _coerce_arg(raw: str, declared_str: bool) -> Any:
    """文本协议里的参数值 → Python：声明成字符串的按字符串，其余先试 JSON。"""
    v = (raw or "").strip()
    if declared_str:
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v


def looks_like_text_calls(text: str) -> bool:
    """粗筛：正文里是否可能出现模板式调用（避免对每条普通回复跑全套正则）。"""
    if not text:
        return False
    return ("｜" in text or "|" in text) or ("function" in text.lower() and "<" in text)


def contains_unparsed_calls(text: str) -> bool:
    """解析后仍残留模板式调用（工具名不在白名单、或参数块畸形）→ 这条回复没真正作答。"""
    if not looks_like_text_calls(text or ""):
        return False
    return bool(_INV_RE.search(text) or _FUNC_RE.search(text))


def parse_text_calls(text: str,
                     known: set[str] | None = None) -> tuple[str, list[ToolCall]]:
    """把正文里的文本协议调用解析成 ToolCall，返回 (剥掉调用块后的正文, 调用列表)。

    只认白名单内的工具名：模型写出模板 token 不代表它调的是合法工具，名字不在白名单
    就整块不认，留给上层按「没有工具调用」处理。一个都没解析出来时原样返回正文。
    """
    if not looks_like_text_calls(text):
        return text, []
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []

    invs = list(_INV_RE.finditer(text))
    for i, m in enumerate(invs):
        name = (m.group(1) or m.group(2) or "").strip()
        if known is not None and name not in known:
            continue
        stop = invs[i + 1].start() if i + 1 < len(invs) else len(text)
        cl = _CLOSE_RE.search(text, m.end(), stop)
        if cl:
            stop = cl.start()
            spans.append((m.start(), cl.end()))
        else:
            spans.append((m.start(), stop))
        args: dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(text, m.end(), stop):
            is_str = bool(_STR_TRUE_RE.search(pm.group("rest") or ""))
            key = (pm.group(1) or pm.group(2) or "").strip()
            if key:
                args[key] = _coerce_arg(pm.group("val"), is_str)
        calls.append(ToolCall(id=f"txt_{i + 1}", name=name, arguments=args))

    for i, m in enumerate(_FUNC_RE.finditer(text)):
        name = m.group(1).strip()
        if known is not None and name not in known:
            continue
        cl = _FUNC_CLOSE_RE.search(text, m.end())
        stop = cl.start() if cl else len(text)
        spans.append((m.start(), cl.end() if cl else stop))
        calls.append(ToolCall(
            id=f"fn_{i + 1}", name=name,
            arguments={(am.group(1) or am.group(2) or am.group(3) or "").strip():
                       _coerce_arg(am.group("val"), False)
                       for am in _FUNC_ARG_RE.finditer(text, m.end(), stop)}))

    if not calls:
        return text, []
    stripped = text
    for a, b in sorted(spans, reverse=True):
        stripped = stripped[:a] + stripped[b:]
    # 外层包裹标签（calls / invokes 之类）不在上面的 span 里，单独按「含竖线的标签」剥掉
    return _LEFTOVER_RE.sub("", stripped).strip(), calls
