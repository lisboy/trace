"""LLM 代理自动模式测试。

规则：系统配了代理就先走代理、没配就直连、**代理连不上自动改直连**。
这里用本地 mock 的 OpenAI 兼容服务充当「模型端点」，再把 `_system_proxy` 打桩成一个
必定连不上的代理，验证 adapter 会自己降级到直连并正常拿到结果（且流式仍然是流式）。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

import game_agent.llm.openai_compat as oa
from game_agent.llm.base import Ctx


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        if body.get("stream"):
            async def gen():
                yield 'data: {"choices":[{"delta":{"content":"直"}}]}\n\n'
                yield 'data: {"choices":[{"delta":{"content":"连"}}]}\n\n'
                yield 'data: {"choices":[{"delta":{"content":"成功"}}]}\n\n'
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {
            "choices": [{"message": {"role": "assistant", "content": "直连成功"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    return app


def _start_server():
    import uvicorn
    app = _make_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    return srv, f"http://127.0.0.1:{port}"


DEAD_PROXY = "http://127.0.0.1:1"  # 必定 ConnectionRefused


def test_system_proxy_reader_and_switch(monkeypatch):
    monkeypatch.delenv("GA_LLM_NO_PROXY", raising=False)
    assert oa._proxy_env_disabled() is False
    monkeypatch.setenv("GA_LLM_NO_PROXY", "1")
    assert oa._proxy_env_disabled() is True
    # 直连客户端：trust_env=False 且没有代理
    c = oa._new_llm_client(None)
    try:
        assert c.trust_env is False
        assert not c._mounts  # 没有挂 proxy transport
    finally:
        asyncio.run(c.aclose())
    # 代理客户端：挂了 http/https 的 proxy transport
    c2 = oa._new_llm_client(DEAD_PROXY)
    try:
        assert c2.trust_env is False
        assert c2._mounts  # 有 proxy mount
    finally:
        asyncio.run(c2.aclose())


def _chat_once(base: str, *, stream: bool):
    llm = oa.OpenAICompatibleLLM("main", model="m", api_key="k",
                                 base_url=base.rstrip("/") + "/v1")
    deltas: list[tuple[str, str]] = []
    cb = (lambda r, t: deltas.append((r, t))) if stream else None
    ctx = Ctx(role="main")
    resp = asyncio.run(llm.chat([{"role": "user", "content": "hi"}], [], ctx,
                                on_delta=cb, timeout_s=15, max_attempts=3))
    return resp, deltas


def test_no_proxy_direct_works(monkeypatch):
    srv, base = _start_server()
    monkeypatch.setattr(oa, "_system_proxy", lambda: None)
    monkeypatch.delenv("GA_LLM_NO_PROXY", raising=False)
    try:
        resp, _ = _chat_once(base, stream=False)
        assert resp.text == "直连成功"
    finally:
        srv.should_exit = True


def test_dead_proxy_auto_downgrades_to_direct(monkeypatch):
    srv, base = _start_server()
    monkeypatch.setattr(oa, "_system_proxy", lambda: DEAD_PROXY)
    monkeypatch.delenv("GA_LLM_NO_PROXY", raising=False)
    try:
        resp, _ = _chat_once(base, stream=False)
        assert resp.text == "直连成功"  # 代理连不上 → 自动改直连 → 仍成功
    finally:
        srv.should_exit = True


def test_dead_proxy_downgrade_keeps_streaming(monkeypatch):
    """关键回归：代理阶段是流式连接失败，降级直连后必须**仍然是流式**，
    不能误判成「端点不支持流式」而退化成非流式。"""
    srv, base = _start_server()
    monkeypatch.setattr(oa, "_system_proxy", lambda: DEAD_PROXY)
    monkeypatch.delenv("GA_LLM_NO_PROXY", raising=False)
    try:
        resp, deltas = _chat_once(base, stream=True)
        assert resp.text == "直连成功"
        assert deltas, "降级直连后应仍然通过 on_delta 逐段回调"
        assert any(t for _, t in deltas)
    finally:
        srv.should_exit = True


def test_no_proxy_env_forces_direct_even_if_system_proxy_set(monkeypatch):
    srv, base = _start_server()
    monkeypatch.setattr(oa, "_system_proxy", lambda: DEAD_PROXY)
    monkeypatch.setenv("GA_LLM_NO_PROXY", "1")
    try:
        resp, _ = _chat_once(base, stream=False)
        assert resp.text == "直连成功"  # 用户强制直连：压根不碰那个死代理
    finally:
        srv.should_exit = True
