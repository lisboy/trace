"""LLM 往返现场记录（上下文可观测性的数据源）。

把「压缩后真正发给模型的 messages + 工具定义」和「模型返回的正文/思考/工具调用」
原样存下来，并把 prompt 拆成几类占比，用于回答这类问题：
- 这轮到底发了什么，为什么模型答非所问；
- 上下文被谁吃掉了（系统提示 / 工具定义 / 历史 / 工具结果）；
- 压缩有没有开始丢历史。

图片 dataURL 一律换成占位描述（单张 base64 可达 2MB，全存下来数据库会被撑爆），
整体超过 MAX_STORE_BYTES 时按条截断并打 truncated 标记。
"""
from __future__ import annotations

import json
from typing import Any

from game_agent.llm.context_window import IMAGE_TOKENS, estimate_tokens

# 单轮请求原文的存储上限（字符）：正常一轮几十 KB，超限说明有巨型附件/工具结果，
# 截断保平安，避免 LONGTEXT 行无限膨胀。
MAX_STORE_BYTES = 400_000
# 图片 dataURL 的替换标记里保留 MIME 与体积，够定位问题即可
_IMAGE_PREFIX_LEN = 24


def _redact_content(content: Any) -> tuple[Any, int]:
    """content parts 里的 image_url → 占位文本。返回 (处理后内容, 图片数)。"""
    if not isinstance(content, list):
        return content, 0
    n_img = 0
    out = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            n_img += 1
            url = str((part.get("image_url") or {}).get("url") or "")
            mime = url[5:url.index(";")] if url.startswith("data:") and ";" in url else "image"
            out.append({"type": "image_ref", "mime": mime, "bytes": len(url),
                        "text": f"[图片 dataURL 已省略：{mime}，{len(url) // 1024}KB]"})
        else:
            out.append(part)
    return out, n_img


def _msg_tokens(m: dict) -> int:
    # content 可能是 str，也可能是 OpenAI content parts 列表；图片（真实 image_url 或落库时
    # 已被替换成 image_ref 占位）都要按 IMAGE_TOKENS 计，否则「各消息之和」会比
    # est_prompt_tokens 少掉每张图那 900——历史记录（图片早已替换成占位文本）尤其明显。
    content = m.get("content")
    if isinstance(content, list):
        body_tok = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image_url", "image_ref"):
                body_tok += IMAGE_TOKENS
            elif isinstance(part, dict):
                body_tok += estimate_tokens(part.get("text", ""))
            else:
                body_tok += estimate_tokens(part)
        t = 4 + body_tok
    else:
        t = 4 + estimate_tokens(content)
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        if isinstance(fn, dict):
            t += 8 + estimate_tokens(fn.get("name")) + estimate_tokens(fn.get("arguments"))
    return t


def categorize(messages: list[dict], tools: list[dict]) -> dict:
    """把一次请求的 prompt 按来源拆开（估算 token + 字符数），供统计视图画占比。"""
    cats: dict[str, dict] = {}
    # 逐条消息的估算 token（与 messages 同下标）：落库后前端直接用，不再另算一份
    # 估算去凑「各消息之和 = 头部 est_prompt_tokens」——两份估算永远差一点，用户
    # 看到的就是「7 条消息加起来才 8k，头部却写 16.2k」。
    per_msg: list[int] = []

    def add(key: str, text_tokens: int, chars: int) -> None:
        c = cats.setdefault(key, {"tokens": 0, "chars": 0})
        c["tokens"] += text_tokens
        c["chars"] += chars

    for m in messages:
        role = str(m.get("role") or "")
        body = m.get("content")
        size = len(json.dumps(body, ensure_ascii=False)) if isinstance(body, list) \
            else len(str(body or ""))
        size += sum(len(json.dumps(tc.get("function", tc), ensure_ascii=False))
                    for tc in (m.get("tool_calls") or []))
        tok = _msg_tokens(m)
        per_msg.append(tok)
        if role == "system":
            # 压缩说明是运行时注入的，和角色系统提示词不是一回事，单独归类
            add("注入内容" if str(body or "").startswith("[上下文压缩]") else "系统提示词", tok, size)
        elif role == "user":
            add("用户消息", tok, size)
        elif role == "assistant":
            add("助手消息", tok, size)
        elif role == "tool":
            add("工具结果", tok, size)
        else:
            add("其他", tok, size)
    tool_tok = 0
    if tools:
        raw = json.dumps(tools, ensure_ascii=False)
        tool_tok = estimate_tokens(raw)
        add("工具定义", tool_tok, len(raw))
    total = sum(c["tokens"] for c in cats.values())
    return {"categories": cats, "est_prompt_tokens": total,
            "messages": len(messages), "tools": len(tools),
            "msg_tokens": per_msg, "tools_tokens": tool_tok}


def build_record(*, role: str, model: str, round_no: int,
                 messages: list[dict], tools: list[dict],
                 resp_text: str, resp_reasoning: str, resp_tool_calls: list[dict],
                 usage: dict, latency_ms: int, compressed: bool,
                 dropped_msgs: int, include_tools: bool = True) -> dict:
    """组装一条待落库记录（字段名对齐 llm_transcripts 表）。

    include_tools=False：工具定义每轮都一样且最占体积（几十 KB），只在 run 的第一轮
    存一份，后续轮次记个数量即可（统计里的 token 占比照常算）。
    """
    redacted: list[dict] = []
    n_images = 0
    for m in messages:
        body, n = _redact_content(m.get("content"))
        n_images += n
        item = {k: v for k, v in m.items() if k != "content"}
        item["content"] = body
        redacted.append(item)

    stats = categorize(messages, tools)
    stats["images"] = n_images
    # 本轮模型发起的工具调用（按名字计数）：统计页用它算「工具分布」，
    # 不用回头解析 request/response 大字段。
    by_name: dict[str, int] = {}
    for tc in resp_tool_calls:
        by_name[str((tc or {}).get("name") or "?")] = by_name.get(str((tc or {}).get("name") or "?"), 0) + 1
    stats["tool_calls"] = by_name
    stats["completion_tokens"] = int(usage.get("completion_tokens") or 0)
    stats["cached_tokens"] = int(usage.get("cached_tokens") or 0)
    stats["cache_reported"] = 1 if usage.get("cache_reported") else 0
    stats["latency_ms"] = latency_ms

    req_obj: dict[str, Any] = {"messages": redacted}
    req_obj.update({"tools": tools} if include_tools and tools
                   else {"tools_omitted": len(tools)})
    req_raw = json.dumps(req_obj, ensure_ascii=False)
    resp_obj = {"text": resp_text, "reasoning": resp_reasoning,
                "tool_calls": resp_tool_calls, "usage": usage}
    resp_raw = json.dumps(resp_obj, ensure_ascii=False)

    truncated = 0
    if len(req_raw) > MAX_STORE_BYTES:
        # 优先保住「最近的几轮」——排查问题看的就是刚发出去的那部分
        keep = req_obj["messages"][-12:]
        tail = {k: v for k, v in req_obj.items() if k != "messages"}
        req_raw = json.dumps({"messages": keep, **tail,
                              "truncated_note": f"原请求 {len(req_raw)} 字符，仅存最后 12 条消息"},
                             ensure_ascii=False)[:MAX_STORE_BYTES]
        truncated = 1
    if len(resp_raw) > MAX_STORE_BYTES:
        resp_raw = resp_raw[:MAX_STORE_BYTES]
        truncated = 1

    first_sys = next((str(m.get("content") or "") for m in messages
                      if m.get("role") == "system"), "")
    purpose = "summarize" if first_sys.startswith("你是对话压缩器") else "agent"
    return {
        "agent_key": role, "model": model, "round_no": round_no, "purpose": purpose,
        "compressed": 1 if compressed else 0, "dropped_msgs": dropped_msgs,
        "req_bytes": len(req_raw), "resp_bytes": len(resp_raw),
        "stats_json": stats, "request_json": req_raw, "response_json": resp_raw,
        "truncated": truncated,
    }
