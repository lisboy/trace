"""「只收字符串 content」的 anthropic 中转站：降级后必须能跑通，不能每轮撞 400。

线上真实故障（2026-11-16）：标称 anthropic 协议的端点背后是 OpenAI/SGLang 内核，
它把 messages[*].content 定成 str，收到块数组就 400：
    23 validation errors: {'type':'string_type','loc':('body','messages',103,
    'function-after[validate_thinking_parts_role(), ChatCompletionMessageGenericParam]',
    'content','str'),'msg':'Input should be a valid string',...}
原实现只关图片块、content 仍是数组 → 重发同一个 400 → 整个 run 崩。
本测试锁住：纯文本块数组也被拒时，整体压成字符串并成功重发，且第二轮之后不再发数组。
"""
from __future__ import annotations

import asyncio
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from game_agent.llm.anthropic_compat import AnthropicMessagesLLM
from game_agent.llm.base import Ctx, ToolSpec

_REJECT_MSG = ("23 validation errors: {'type':'string_type','loc':('body','messages',103,"
               "'function-after[validate_thinking_parts_role(), ChatCompletionMessageGenericParam]',"
               "'content','str'),'msg':'Input should be a valid string','input':[{'type':")


def _all_content_are_str(body: dict) -> bool:
    for m in body.get("messages") or []:
        if not isinstance(m.get("content"), str):
            return False
    return True


def _start_str_only_server():
    """严格内核：messages[*].content 只要不是 str 就 400。"""
    import uvicorn
    captured: dict = {"reqs": []}
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        captured["reqs"].append(body)
        if not _all_content_are_str(body):
            return JSONResponse({"error": {"type": "bad_response_status_code",
                                           "message": _REJECT_MSG}}, status_code=400)
        if body.get("stream"):
            async def gen():
                yield ('data: {"type":"message_start","message":{"usage":'
                       '{"input_tokens":5,"output_tokens":1}}}\n\n')
                yield ('data: {"type":"content_block_start","index":0,'
                       '"content_block":{"type":"text","text":""}}\n\n')
                yield ('data: {"type":"content_block_delta","index":0,'
                       '"delta":{"type":"text_delta","text":"好的"}}\n\n')
                yield 'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {"id": "m", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "好的"}],
                "usage": {"input_tokens": 5, "output_tokens": 2}}

    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    return srv, f"http://127.0.0.1:{port}", captured


def _tools() -> list[ToolSpec]:
    return [ToolSpec(name="read_file", description="读文件",
                     parameters={"type": "object", "properties": {"path": {"type": "string"}}})]


def _history() -> list[dict]:
    """一段带工具往返的历史：正好覆盖 tool_use / tool_result / user 文本三种块。"""
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": [{"type": "text", "text": "读 a.py"}]},
        {"role": "assistant", "content": "我来读",
         "tool_calls": [{"id": "toolu_abc", "name": "read_file", "arguments": {"path": "a.py"}}]},
        {"role": "tool", "tool_call_id": "toolu_abc", "content": "print(1)"},
    ]


def test_str_only_endpoint_degrades_and_recovers():
    """块数组被拒 → 自动压成字符串重发成功；后续轮不再发数组。"""
    srv, base, captured = _start_str_only_server()
    try:
        llm = AnthropicMessagesLLM("main", model="claude-x", api_key="sk-test", base_url=base)
        resp = asyncio.run(llm.chat(_history(), _tools(), Ctx(role="main")))

        assert resp.text == "好的"
        assert len(captured["reqs"]) == 2
        assert not _all_content_are_str(captured["reqs"][0])   # 第一次是块数组 → 400
        second = captured["reqs"][1]
        assert _all_content_are_str(second)                     # 降级后全是字符串
        # 工具往返不能丢信息：调用与结果都要有痕迹
        flat = "\n".join(m["content"] for m in second["messages"])
        assert "read_file" in flat and "a.py" in flat and "print(1)" in flat

        # 第二轮：同一实例不再先撞 400（降级状态在 run 内保留）
        asyncio.run(llm.chat(_history(), _tools(), Ctx(role="main")))
        assert len(captured["reqs"]) == 3
        assert _all_content_are_str(captured["reqs"][2])
    finally:
        srv.should_exit = True


def test_text_only_body_keeps_system_and_tools():
    """纯文本模式下 system 仍走顶层字段、tools 仍按 anthropic schema 发送。"""
    llm = AnthropicMessagesLLM("main", model="claude-x", api_key="sk-test",
                               base_url="http://127.0.0.1:9", vision=True)
    llm._text_only = True
    body = llm._to_anthropic(_history(), _tools())
    assert body["system"] == "你是助手"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert _all_content_are_str(body)
    # 降级后不应出现 Anthropic 特有的块结构
    for m in body["messages"]:
        assert isinstance(m["content"], str)


def test_stream_path_recognizes_str_only_reject():
    """流式路径也要认出这个 400（正文较长，别被截断成「流式不支持」白降级）。"""
    srv, base, captured = _start_str_only_server()
    try:
        llm = AnthropicMessagesLLM("main", model="claude-x", api_key="sk-test", base_url=base)

        async def run():
            return await llm.chat(_history(), _tools(), Ctx(role="main"),
                                  on_delta=lambda r, t: None)

        resp = asyncio.run(run())
        assert resp.text == "好的"
        assert _all_content_are_str(captured["reqs"][-1])
        # 第一次是流式请求（带 stream=True）且被 400 拒在 content 上
        assert captured["reqs"][0].get("stream") is True
        assert not _all_content_are_str(captured["reqs"][0])
    finally:
        srv.should_exit = True
