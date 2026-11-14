"""content parts 兼容性回归（2026-11-12）。

现场：某些 OpenAI 兼容连接（SGLang 系网关 / 中转站）把 message.content 定成纯字符串，
只要历史里出现 ``[{"type": "text", ...}]`` 这种多段 content 就整条拒绝：

    LLM HTTP 400: {'type': 'string_type', 'loc': (..., 'content', 'str'),
                   'msg': 'Input should be a valid string', 'input': [{'type': 'text'}]}

对策有两层，这里各钉一个测试：
1. 发请求前归一化：只有 user + 视觉档位 + 真带图才发 parts 数组，其余一律字符串；
2. 端点仍然报这种 400 时，自动压成纯文本重发（每轮不再重复撞）。
另外，少数端点把**模型回复**的 content 也返回成数组，必须立刻转成字符串再进历史，
否则下一轮就会把 assistant 的多段 content 原样发回去。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from game_agent.llm.base import Ctx
from game_agent.llm.openai_compat import (
    NO_VISION_NOTE, PARTS_OFF_NOTE, OpenAICompatibleLLM,
    _classify_http_error, _ContentPartsUnsupported,
)

IMG = "data:image/jpeg;base64,/9j/AAAA"

# 精简过的真实错误（LiteLLM 转述 SGLang 的 pydantic 校验）
PARTS_400 = (
    '{"error":{"type":"bad_response_status_code","message":"23 validation errors:\\n'
    " {'type': 'string_type', 'loc': ('body', 'messages', 0, "
    "'function-after[validate_thinking_parts_role(), ChatCompletionMessageGenericParam]', "
    "'content', 'str'), 'msg': 'Input should be a valid string', "
    "'input': [{'type': 'text'}]}}"
)


def _install(monkeypatch, respond):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(respond), **kw))


def _llm(vision: bool = True) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM("main", vision=vision, api_key="test",
                               base_url="https://llm.invalid/v1")


def _ok(text: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _sse(text: str = "ok") -> httpx.Response:
    chunk = {"choices": [{"delta": {"content": text}}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                          headers={"Content-Type": "text/event-stream"})


def test_text_only_parts_from_any_role_are_sent_as_strings(monkeypatch) -> None:
    sent: list[dict] = []

    def respond(request):
        sent.append(json.loads(request.content))
        return _ok()

    _install(monkeypatch, respond)
    msgs = [
        {"role": "system", "content": [{"type": "text", "text": "规则"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "上一步"}],
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "内容"}]},
        {"role": "user", "content": [{"type": "text", "text": "继续"}]},
    ]

    asyncio.run(_llm().chat(msgs, [], Ctx()))

    assert [m["role"] for m in sent[0]["messages"]] == ["system", "assistant", "tool", "user"]
    assert all(isinstance(m["content"], str) for m in sent[0]["messages"])


def test_vision_user_message_with_images_still_sends_parts(monkeypatch) -> None:
    sent: list[dict] = []

    def respond(request):
        sent.append(json.loads(request.content))
        return _ok()

    _install(monkeypatch, respond)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": IMG}},
    ]}]

    asyncio.run(_llm(vision=True).chat(msgs, [], Ctx()))

    content = sent[0]["messages"][0]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "image_url" for p in content)


def test_non_vision_image_is_replaced_with_note(monkeypatch) -> None:
    sent: list[dict] = []

    def respond(request):
        sent.append(json.loads(request.content))
        return _ok()

    _install(monkeypatch, respond)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": IMG}},
    ]}]

    asyncio.run(_llm(vision=False).chat(msgs, [], Ctx()))

    content = sent[0]["messages"][0]["content"]
    assert isinstance(content, str)
    assert NO_VISION_NOTE in content and "看这张图" in content


def test_sglang_content_string_400_is_classified_as_parts_unsupported() -> None:
    assert isinstance(_classify_http_error(400, PARTS_400), _ContentPartsUnsupported)
    # FastAPI 校验也有回 422 的实现
    assert isinstance(_classify_http_error(422, PARTS_400), _ContentPartsUnsupported)


@pytest.mark.parametrize("stream", [False, True])
def test_parts_rejection_retries_as_plain_text_and_stays_plain(monkeypatch, stream) -> None:
    sent: list[dict] = []

    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(400, text=PARTS_400)
        return _sse() if stream else _ok()

    _install(monkeypatch, respond)
    llm = _llm(vision=True)
    notes: list[tuple] = []
    ctx = Ctx()
    ctx.on_llm_retry = lambda *a: notes.append(a)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": IMG}},
    ]}]
    delta = (lambda rd, td: None) if stream else None

    resp = asyncio.run(llm.chat(msgs, [], ctx, on_delta=delta))

    assert resp.text == "ok"
    assert len(sent) == 2
    assert isinstance(sent[0]["messages"][0]["content"], list)   # 第一次确实带了图
    assert all(isinstance(m["content"], str) for m in sent[1]["messages"])
    assert PARTS_OFF_NOTE in sent[1]["messages"][0]["content"]  # 图换成可执行的说明
    assert any("纯文本" in str(a[-1]) for a in notes), "降级要同步到运行轨迹，不能静默"

    # 后续轮次直接发纯文本：不应再撞一次 400
    asyncio.run(llm.chat(msgs, [], Ctx(), on_delta=delta))
    assert len(sent) == 3
    assert all(isinstance(m["content"], str) for m in sent[2]["messages"])


def test_list_shaped_response_content_is_normalized_before_history(monkeypatch) -> None:
    sent: list[dict] = []

    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": [
                {"type": "text", "text": "第一段"},
                {"type": "text", "text": "第二段"},
            ]}}]})
        return _ok()

    _install(monkeypatch, respond)
    llm = _llm(vision=True)

    resp = asyncio.run(llm.chat([{"role": "user", "content": "开始"}], [], Ctx()))
    assert resp.text == "第一段 第二段"

    # runtime 会把 resp.text 原样写进 assistant 历史，下一轮必须是字符串
    asyncio.run(llm.chat([{"role": "assistant", "content": resp.text},
                          {"role": "user", "content": "继续"}], [], Ctx()))
    assert sent[1]["messages"][0]["content"] == "第一段 第二段"
