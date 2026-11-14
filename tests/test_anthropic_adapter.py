"""Anthropic 适配器联调：本地起一个 mock /v1/messages 服务，用真实 adapter 跑通
对话 + 工具调用 + 流式，校验请求翻译与响应解析。

不依赖外网，也不碰真实供应商（内网地址在本机解析不到，无法直连验证）。
"""
from __future__ import annotations

import asyncio
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from game_agent.llm.anthropic_compat import AnthropicMessagesLLM
from game_agent.llm.base import Ctx, ToolSpec


def _make_app(captured: dict) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        captured["body"] = body
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        if body.get("stream"):
            async def gen():
                yield 'data: {"type":"message_start","message":{"usage":{"input_tokens":30,"output_tokens":1}}}\n\n'
                yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n\n'
                yield 'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file"}}\n\n'
                yield 'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\": \\"a.py\\"}"}}\n\n'
                yield 'data: {"type":"message_delta","usage":{"output_tokens":12}}\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [
                {"type": "text", "text": "我来看看文件。"},
                {"type": "tool_use", "id": "toolu_abc", "name": "read_file",
                 "input": {"path": "a.py"}},
            ],
            "usage": {"input_tokens": 42, "output_tokens": 7,
                      "cache_read_input_tokens": 10},
        }

    return app


def _start_server():
    import uvicorn
    captured: dict = {}
    app = _make_app(captured)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    return srv, f"http://127.0.0.1:{port}", captured


def _tools() -> list[ToolSpec]:
    return [ToolSpec(name="read_file", description="读文件",
                     parameters={"type": "object", "properties": {"path": {"type": "string"}}})]


def test_non_stream_translation():
    srv, base, captured = _start_server()
    try:
        llm = AnthropicMessagesLLM("main", model="claude-x", api_key="sk-test",
                                   base_url=base, vision=True)
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "读 a.py"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "toolu_abc", "name": "read_file", "arguments": {"path": "a.py"}}]},
            {"role": "tool", "tool_call_id": "toolu_abc", "content": "print(1)"},
        ]
        resp = asyncio.run(llm.chat(messages, _tools(), Ctx(role="main")))

        body = captured["body"]
        assert body["system"] == "你是助手"
        assert all(m["role"] != "system" for m in body["messages"])
        assert body["tools"][0]["input_schema"]["type"] == "object"
        assert body["tools"][0]["name"] == "read_file"
        asst = [m for m in body["messages"] if m["role"] == "assistant"][0]
        assert any(b.get("type") == "tool_use" and b.get("name") == "read_file" for b in asst["content"])
        last = body["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
        assert last["content"][0]["tool_use_id"] == "toolu_abc"
        assert captured["headers"].get("x-api-key") == "sk-test"
        assert captured["headers"].get("anthropic-version")

        assert resp.text == "我来看看文件。"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == {"path": "a.py"}
        assert resp.usage["prompt_tokens"] == 42
        assert resp.usage["completion_tokens"] == 7
        assert resp.usage["cached_tokens"] == 10
    finally:
        srv.should_exit = True


def test_stream_translation():
    srv, base, captured = _start_server()
    try:
        llm = AnthropicMessagesLLM("main", model="claude-x", api_key="sk-test", base_url=base)
        deltas: list[tuple[str, str]] = []

        async def run():
            return await llm.chat([{"role": "user", "content": "hi"}], _tools(),
                                  Ctx(role="main"),
                                  on_delta=lambda r, t: deltas.append((r, t)))

        resp = asyncio.run(run())
        assert resp.text == "你好"
        assert len(resp.tool_calls) == 1 and resp.tool_calls[0].arguments == {"path": "a.py"}
        assert any(t == "你好" for _, t in deltas)
    finally:
        srv.should_exit = True


def _body_has_image_block(body: dict) -> bool:
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, list) and any(
                isinstance(p, dict) and p.get("type") == "image" for p in c):
            return True
    return False


def _start_reject_image_server():
    """模拟「anthropic 协议」中转站背后是 OpenAI/SGLang 内核：图片块会被内核以
    「content 必须是字符串」400 拒绝（即线上真实故障）。纯文本则正常。"""
    import uvicorn
    from fastapi.responses import JSONResponse
    captured: dict = {"reqs": []}
    app = FastAPI()

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        captured["reqs"].append(body)
        if _body_has_image_block(body):
            msg = ("23 validation errors: {'type':'string_type','loc':('body','messages',1,"
                   "'function-after[validate_thinking_parts_role(), ChatCompletionMessageGenericParam]',"
                   "'content','str'),'msg':'Input should be a valid string','input':[{'type':")
            return JSONResponse({"error": {"type": "bad_response_status_code",
                                           "message": msg}}, status_code=400)
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


def test_image_block_reject_degrades_to_text():
    """带图被 400 时不应把 run 打崩：自动把图片降成文字说明后重发成功。"""
    srv, base, captured = _start_reject_image_server()
    try:
        llm = AnthropicMessagesLLM("main", model="claude-x", api_key="sk-test",
                                   base_url=base, vision=True)
        img = {"type": "image_url",
               "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="}}
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "看图"}, img]},
        ]
        resp = asyncio.run(llm.chat(messages, [], Ctx(role="main")))
        assert resp.text == "好的"
        assert len(captured["reqs"]) == 2
        assert _body_has_image_block(captured["reqs"][0])       # 第一次带图块 → 400
        assert not _body_has_image_block(captured["reqs"][1])    # 降级后不再有图块
    finally:
        srv.should_exit = True
