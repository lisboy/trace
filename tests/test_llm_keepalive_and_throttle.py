"""LLM 出站长连接复用 / 并发闸门 / parallel_tool_calls 协商 的回归测试。

这三件事都是「同一个端点、同一个模型，为什么这个项目慢一倍」的直接对症改动
（2026-11-14 实测：每轮新建 TCP+TLS 要白扔 0.6~0.9s；412 次 429 全部来自并发打满配额；
历史平均只有 1.5 个工具/轮，因为端点没被告知可以一轮给多个调用）。所以每个行为都钉一条
测试，防止以后为了「少一个全局变量」又改回每轮新建客户端。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import game_agent.llm.openai_compat as oa
from game_agent.llm.base import Ctx, ToolSpec


# ---------------------------------------------------------------- 本地假端点
def _make_app() -> FastAPI:
    app = FastAPI()
    app.state.calls = 0
    app.state.bodies = []
    app.state.reject_parallel = False
    app.state.reject_stream = False

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        app.state.calls += 1
        app.state.bodies.append(body)
        if app.state.reject_stream and body.get("stream"):
            return JSONResponse(status_code=400,
                                content={"error": {"message": "stream is not supported"}})
        if app.state.reject_parallel and "parallel_tool_calls" in body:
            return JSONResponse(
                status_code=400,
                content={"error": {"message":
                                   "Unrecognized request argument supplied: parallel_tool_calls",
                                   "type": "invalid_request_error"}})
        if body.get("stream"):
            async def gen():
                yield 'data: {"choices":[{"delta":{"content":"流"}}]}\n\n'
                yield 'data: {"choices":[{"delta":{"content":"式"}}]}\n\n'
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")
        return {"choices": [{"message": {"role": "assistant", "content": "成功"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

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
    return srv, app, f"http://127.0.0.1:{port}/v1"


@pytest.fixture()
def server(monkeypatch):
    srv, app, base = _start_server()
    monkeypatch.setattr(oa, "_system_proxy", lambda: None)
    monkeypatch.delenv("GA_LLM_NO_PROXY", raising=False)
    oa._CLIENTS.clear()
    oa._dead_proxies.clear()
    try:
        yield app, base
    finally:
        srv.should_exit = True
        oa._CLIENTS.clear()


def _llm(base: str) -> oa.OpenAICompatibleLLM:
    return oa.OpenAICompatibleLLM("main", model="m", api_key="k", base_url=base)


TOOLS = [ToolSpec(name="grep", description="搜", parameters={"type": "object",
                                                             "properties": {}})]


# ---------------------------------------------------------------- 连接复用
def test_shared_client_is_reused_across_calls(server):
    """同一个 loop 内多次往返必须复用同一个客户端（= 同一条 keep-alive 连接）。"""
    app, base = server

    async def run():
        llm = _llm(base)
        ctx = Ctx(role="main")
        c1 = oa._shared_client(None, llm.user_agent)
        for _ in range(3):
            await llm.chat([{"role": "user", "content": "hi"}], [], ctx,
                           timeout_s=15, max_attempts=1)
        c2 = oa._shared_client(None, llm.user_agent)
        return c1, c2

    c1, c2 = asyncio.run(run())
    assert c1 is c2, "同 loop 同 key 必须命中同一个客户端"
    assert app.state.calls == 3


def test_shared_client_not_shared_across_event_loops():
    """跨事件循环不能复用（httpx 连接池绑 loop，复用会 Event loop is closed）。"""
    seen = []

    async def one():
        seen.append(oa._shared_client(None, "ua"))
        return seen[-1]

    a = asyncio.run(one())
    b = asyncio.run(one())
    assert a is not b, "跨 loop 必须重建客户端"


def test_client_connect_timeout_is_short():
    """建连超时必须远小于整体超时：300s 建连会把黑洞 IP 放大成五分钟白等。"""
    c = oa._new_llm_client(None)
    try:
        assert c.timeout.connect == oa.LLM_CONNECT_TIMEOUT_S
        assert c.timeout.connect <= 15
        assert c.timeout.read and c.timeout.read >= 60
    finally:
        asyncio.run(c.aclose())


def test_system_proxy_cached_but_invalidated_on_swap(monkeypatch):
    """代理读取要缓存（每轮 to_thread 读注册表太浪费），但换实现时必须立刻失效。"""
    calls = {"n": 0}

    def fake() -> str:
        calls["n"] += 1
        return "http://127.0.0.1:9"

    monkeypatch.setattr(oa, "_system_proxy", fake)
    oa._proxy_cache = None
    assert oa._system_proxy_cached() == "http://127.0.0.1:9"
    assert oa._system_proxy_cached() == "http://127.0.0.1:9"
    assert calls["n"] == 1, "60s 内应命中缓存"

    def other() -> str | None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(oa, "_system_proxy", other)
    assert oa._system_proxy_cached() is None, "换实现后必须重新读，不能吃旧缓存"
    assert calls["n"] == 2


def test_dead_proxy_is_poisoned_for_a_while(monkeypatch):
    """代理连不上过就冷却一段时间，别让每条往返都先撞一次死端口。"""
    oa._dead_proxies.clear()
    oa._poison_proxy("http://127.0.0.1:1")
    assert oa._proxy_is_dead("http://127.0.0.1:1") is True
    assert oa._proxy_is_dead("http://127.0.0.1:2") is False
    # 冷却到期自动解除
    oa._dead_proxies["http://127.0.0.1:1"] = time.monotonic() - 1
    assert oa._proxy_is_dead("http://127.0.0.1:1") is False


# ---------------------------------------------------------------- 并发闸门
def test_gate_limits_concurrency():
    gate = oa._AdaptiveGate(2)
    state = {"now": 0, "peak": 0}

    async def worker():
        await gate.acquire()
        try:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1
        finally:
            gate.release()

    async def run():
        await asyncio.gather(*[worker() for _ in range(8)])

    asyncio.run(run())
    assert state["peak"] == 2, f"并发上限应被限住，实际峰值 {state['peak']}"
    assert gate.in_flight == 0


def test_gate_halves_on_rate_limit_and_recovers():
    gate = oa._AdaptiveGate(8)
    gate.note_rate_limited()
    assert gate.limit == 4, "429 后并发必须腰斩"
    gate.note_rate_limited()
    assert gate.limit == 2
    gate.note_rate_limited()
    gate.note_rate_limited()
    assert gate.limit == 1, "再限流也不能降到 0（会永久卡死）"
    # 冷却期过后，连续成功应把并发加回来
    gate._grow_after = 0.0
    for _ in range(oa._GATE_GROW_STREAK):
        gate.note_ok()
    assert gate.limit == 2, "连续成功后应 +1"


def test_gate_release_is_safe_when_waiter_cancelled():
    """等并发位时被取消：不能泄漏占用数（泄漏几次并发就永久降到 1）。"""
    gate = oa._AdaptiveGate(1)

    async def run():
        await gate.acquire()
        waiter = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0.02)
        assert len(gate._waiters) == 1
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        gate.release()
        assert gate.in_flight == 0
        assert gate._waiters == []
        # 还能正常再拿一次
        await gate.acquire()
        gate.release()

    asyncio.run(run())


def test_gate_ceiling_from_config(monkeypatch):
    monkeypatch.setattr(oa, "LLM_MAX_CONCURRENCY", 3)
    assert oa._gate_ceiling() == 3
    monkeypatch.setattr(oa, "LLM_MAX_CONCURRENCY", 0)
    assert oa._gate_ceiling() > 3, "0 = 不限，应给一个很大的上限"


# ---------------------------------------------------------------- parallel_tool_calls
def test_parallel_tool_calls_sent_when_tools_present(server):
    app, base = server

    async def run():
        llm = _llm(base)
        return await llm.chat([{"role": "user", "content": "hi"}], TOOLS,
                              Ctx(role="main"), timeout_s=15, max_attempts=1)

    asyncio.run(run())
    body = app.state.bodies[-1]
    assert body.get("parallel_tool_calls") is True, \
        "带工具时必须显式声明允许一轮多个调用（否则模型会被拆成多轮）"


def test_no_parallel_field_without_tools(server):
    app, base = server

    async def run():
        llm = _llm(base)
        return await llm.chat([{"role": "user", "content": "hi"}], [],
                              Ctx(role="main"), timeout_s=15, max_attempts=1)

    asyncio.run(run())
    assert "parallel_tool_calls" not in app.state.bodies[-1]


def test_stream_rejection_falls_back_to_non_stream(server, monkeypatch):
    """流式被端点拒（非格式协商类错误）时，同一轮要退化为非流式并成功。

    这条也是回归：曾把「降级后的非流式请求」写成 `else` 分支，导致流式失败后
    什么都不发、直接 UnboundLocalError。
    """
    app, base = server
    app.state.reject_stream = True

    async def run():
        llm = _llm(base)
        deltas = []
        return await llm.chat([{"role": "user", "content": "hi"}], [],
                              Ctx(role="main"),
                              on_delta=lambda a, b: deltas.append(b),
                              timeout_s=15, max_attempts=1)

    resp = asyncio.run(run())
    assert resp.text == "成功", resp.text
    assert app.state.bodies[0].get("stream") is True, "首轮应优先尝试流式"
    assert not app.state.bodies[-1].get("stream"), "降级后必须是真的非流式请求"
    assert app.state.calls == 2


def test_gate_not_leaked_after_stream_failure(server):
    """流式失败+降级成功后，并发位要全部归还（否则并发会越跑越小）。"""
    app, base = server
    app.state.reject_stream = True

    async def run():
        llm = _llm(base)
        await llm.chat([{"role": "user", "content": "hi"}], [], Ctx(role="main"),
                       on_delta=lambda a, b: None, timeout_s=15, max_attempts=1)
        return oa._gate()

    g = asyncio.run(run())
    assert g.in_flight == 0


def test_unknown_field_is_dropped_without_consuming_attempts(server, monkeypatch):
    """端点不认 parallel_tool_calls：去掉重发即可，不能算一次失败（否则 5 次预算白白烧掉）。"""
    app, base = server
    app.state.reject_parallel = True
    monkeypatch.setattr(oa, "LLM_PARALLEL_TOOL_CALLS", True)

    async def run():
        llm = _llm(base)
        resp = await llm.chat([{"role": "user", "content": "hi"}], TOOLS,
                              Ctx(role="main"), timeout_s=15, max_attempts=1)
        return llm, resp

    llm, resp = asyncio.run(run())
    assert resp.text == "成功"
    assert "parallel_tool_calls" in llm._dropped_fields
    # 第一次带字段被 400，第二次去掉后成功：共 2 次请求、1 次尝试
    assert app.state.calls == 2
    assert "parallel_tool_calls" not in app.state.bodies[-1]


def test_classify_unknown_field_only_when_endpoint_names_it():
    """普通参数错误不能被误判成「字段不认识」，否则会把必填参数也删掉。"""
    e = oa._classify_http_error(
        400, '{"error":{"message":"messages[0].role is invalid, expected one of ..."}}')
    assert not isinstance(e, oa._UnknownFieldRejected)

    e2 = oa._classify_http_error(
        400, '{"error":{"message":"Unrecognized request argument supplied: reasoning_effort"}}')
    assert isinstance(e2, oa._UnknownFieldRejected)
    assert e2.field == "reasoning_effort"


def test_classify_429_carries_status_for_gate():
    e = oa._classify_http_error(429, "rate limited", "7")
    assert isinstance(e, oa._RetryableLLMError)
    assert e.status == 429
    assert e.retry_after == "7"
