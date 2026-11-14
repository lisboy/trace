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


# ---------------------------------------------------------------- 会话级直连记忆
def test_session_direct_memory_basics(monkeypatch):
    oa.clear_session_direct()
    assert oa.session_is_direct("s1") is False
    assert oa.session_is_direct("") is False
    oa.mark_session_direct(None)  # 没会话 id：不是错误，只是不记
    assert oa.session_is_direct("") is False
    oa.mark_session_direct("s1")
    assert oa.session_is_direct("s1") is True
    assert oa.session_is_direct("s2") is False, "按会话记，别串到别的会话上去"
    # 过期即忘（换网络 / 用户重开代理客户端后要能再给代理一次机会）
    monkeypatch.setattr(oa, "_SESSION_DIRECT_TTL_S", 0.0)
    oa.mark_session_direct("s1")
    assert oa.session_is_direct("s1") is False
    oa.clear_session_direct("s1")
    oa.mark_session_direct("s1")
    oa.clear_session_direct()
    assert oa.session_is_direct("s1") is False


def test_session_direct_sticks_after_downgrade(monkeypatch):
    """一次会话降级直连后，后续往返不再尝试代理（也不再报重试提醒）。

    以前只有 120s 的进程级冷却：一次长 run 过了冷却期就重新撞一次死代理，用户看到的是
    一阵阵无缘无故的卡顿 + 一条「已重试模型请求」。
    """
    srv, base = _start_server()
    endpoint = base.rstrip("/") + "/v1"
    hits: list[int] = []

    def fake_proxy():
        hits.append(1)
        return DEAD_PROXY

    monkeypatch.setattr(oa, "_system_proxy", fake_proxy)
    monkeypatch.delenv("GA_LLM_NO_PROXY", raising=False)
    oa.clear_session_direct()
    # 前面的用例会把这个死代理塞进 120s 冷却表：清掉，否则首轮压根不会去试代理
    oa._dead_proxies.clear()
    oa._proxy_cache = None
    try:
        first = oa.OpenAICompatibleLLM("main", model="m", api_key="k", base_url=endpoint)
        first.session_key = "sess-sticky"
        ctx = Ctx(role="main")
        notes: list[tuple] = []
        ctx.on_llm_retry = lambda *a: notes.append(a)
        resp = asyncio.run(first.chat([{"role": "user", "content": "hi"}], [], ctx,
                                      timeout_s=15, max_attempts=3))
        assert resp.text == "直连成功"
        assert hits, "首次应该真的去问了系统代理"
        assert notes, "首次是「代理不通 → 改直连」，这条内部提醒仍然会发（界面不再显示）"
        assert oa.session_is_direct("sess-sticky") is True

        # 新一轮 run（适配器重建）+ 冷却期已过 + 代理缓存清空：仍然不该碰代理
        oa._dead_proxies.clear()
        oa._proxy_cache = None
        hits.clear()
        second = oa.OpenAICompatibleLLM("main", model="m", api_key="k", base_url=endpoint)
        second.session_key = "sess-sticky"
        ctx2 = Ctx(role="main")
        notes2: list[tuple] = []
        ctx2.on_llm_retry = lambda *a: notes2.append(a)
        resp2 = asyncio.run(second.chat([{"role": "user", "content": "hi"}], [], ctx2,
                                        timeout_s=15, max_attempts=3))
        assert resp2.text == "直连成功"
        assert hits == [], "本会话已改直连：不该再去读系统代理"
        assert notes2 == [], "本会话已改直连：不该再出现代理降级的重试"
        # 别的会话不受影响：这条会话的直连决定不串台
        assert oa.session_is_direct("other-session") is False
    finally:
        oa.clear_session_direct()
        oa._dead_proxies.clear()
        srv.should_exit = True
