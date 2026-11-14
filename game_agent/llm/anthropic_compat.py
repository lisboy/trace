"""Anthropic Messages API 适配器（协议 anthropic，端点 /v1/messages）。

内部消息/工具格式与 OpenAICompatibleLLM 完全一致（system / user / assistant(tool_calls) /
tool；content 为 str 或 OpenAI content parts），本适配器负责翻译成 Anthropic 的：
  - 顶层 system 字段（Anthropic 不接受 messages 里的 system 角色）
  - content blocks：text / image(base64) / tool_use / tool_result
  - tools → {name, description, input_schema}
  - 响应 content blocks → text + ToolCall；SSE 流式聚合
  - usage：input_tokens/output_tokens → prompt/completion，cache_read → cached

复用 context_window 的压缩/图片预算与 openai_compat 的错误分类、重试预算语义，
所以两套协议在「上下文超限重压、网关字节超限、429/5xx 退避」上的行为一致。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from typing import Any

import httpx

from game_agent.config import LLM_MODEL, LLM_RETRY_BASE_S, LLM_USER_AGENT, OPENAI_API_KEY
from game_agent.llm.base import (
    Ctx, LLM, LLMResponse, LlmAuthError, LlmContextOverflow, LlmPayloadTooLarge,
    ToolCall, ToolSpec, parse_tool_arguments,
)
from game_agent.llm.context_window import (
    BUDGET_RATIO, MEDIA_BYTES_BUDGET, compress_messages, fit_media, media_stats,
)
from game_agent.llm.openai_compat import (
    NO_VISION_NOTE, _RetryableLLMError, _classify_http_error,
    _new_llm_client, _proxy_env_disabled, _system_proxy, _ProxyRefused,
    _ContentPartsUnsupported, _WafBlocked, _is_waf_response, PARTS_OFF_NOTE,
    mark_session_direct, session_is_direct,
)
from game_agent.llm.text_tool_calls import parse_text_calls
from game_agent.llm.transcript import build_record
from game_agent.logging_setup import get_logger

log = get_logger("llm.anthropic")

ANTHROPIC_VERSION = "2023-06-01"
# max_tokens 在 Anthropic 是必填项。默认给足（长文/大补丁），端点若有更小的上限会报错，用户可在档位里调。
DEFAULT_MAX_TOKENS = 8192


def _messages_url(base: str) -> str:
    b = (base or "").rstrip("/")
    return f"{b}/messages" if b.endswith("/v1") else f"{b}/v1/messages"


def _normalize_usage(usage: dict) -> dict:
    """Anthropic usage → 统一字段（与 openai_compat._normalize_usage 对齐）。"""
    usage = usage or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    reported = bool(cache_read or cache_write
                    or usage.get("cache_read_input_tokens") is not None
                    or usage.get("cache_creation_input_tokens") is not None)
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": min(cache_read, prompt),
        "cache_reported": reported,
    }
    if cache_write:
        out["cache_write_tokens"] = cache_write
    return out


def _data_url_to_image_part(url: str) -> dict | None:
    """OpenAI image_url(dataURL) → Anthropic image block（base64）。"""
    if not url.startswith("data:"):
        return None
    try:
        head, data = url.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
    except ValueError:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


class AnthropicMessagesLLM(LLM):
    def __init__(self, role: str, model: str | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 context_window: int = 0, vision: bool = False,
                 user_agent: str | None = None,
                 reasoning_effort: str = "",
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.role = role
        self.model = model or LLM_MODEL
        self.api_key = api_key or OPENAI_API_KEY
        self.base_url = (base_url or "").rstrip("/")
        self.context_window = context_window
        self.vision = vision
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self.user_agent = user_agent or LLM_USER_AGENT
        self.max_tokens = max_tokens or DEFAULT_MAX_TOKENS
        self.tool_names: set[str] = set()
        self.media_budget = MEDIA_BYTES_BUDGET
        # 端点是否接受图片块：某些「anthropic 协议」中转站背后其实是 OpenAI/SGLang 内核，
        # 图片块会被翻成 content 数组、被内核以「content 必须是字符串」400 拒绝。命中后
        # 本轮 run 内降级为「图片用文字说明代替」，避免每轮都撞一次。
        self._allow_images = True
        # 当前 httpx 客户端是否正在走代理（连接层失败时据此决定「换直连」还是「普通重试」）
        self._using_proxy = False
        # 本会话 id（runtime 注入）与「本会话已改用直连」标记：见 openai_compat.mark_session_direct
        self.session_key = ""
        self._direct_only = False

    # ---------- 请求体翻译 ----------
    def _content_blocks(self, content: Any) -> list[dict]:
        """把内部 content（str 或 OpenAI parts）转成 Anthropic content blocks。"""
        if isinstance(content, list):
            blocks: list[dict] = []
            for p in content:
                if not isinstance(p, dict):
                    blocks.append({"type": "text", "text": str(p)})
                elif p.get("type") == "image_url":
                    url = str((p.get("image_url") or {}).get("url") or "")
                    if self.vision and self._allow_images:
                        img = _data_url_to_image_part(url)
                        if img is not None:
                            blocks.append(img)
                    else:
                        blocks.append({"type": "text",
                                       "text": PARTS_OFF_NOTE if self.vision else NO_VISION_NOTE})
                else:
                    blocks.append({"type": "text", "text": str(p.get("text", ""))})
            return blocks
        return [{"type": "text", "text": "" if content is None else str(content)}]

    def _to_anthropic(self, messages: list[dict], tools: list[ToolSpec]) -> dict:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                c = m.get("content")
                if isinstance(c, list):
                    system_parts.extend(str(p.get("text", "")) for p in c if isinstance(p, dict))
                elif c:
                    system_parts.append(str(c))
                continue
            if role == "tool":
                # 连续的 tool 结果合并进同一条 user 消息（Anthropic 要求 tool_result 紧跟 tool_use）
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                         "content": str(m.get("content", ""))}
                if out and out[-1]["role"] == "user" \
                        and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and _is_tool_result(out[-1]["content"][0]):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue
            if role == "assistant":
                blocks: list[dict] = []
                c = m.get("content")
                if isinstance(c, list):
                    blocks.extend(self._content_blocks(c))
                elif c:
                    blocks.append({"type": "text", "text": str(c)})
                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name") or (tc.get("function") or {}).get("name")
                    args = tc.get("arguments")
                    if args is None:
                        args = (tc.get("function") or {}).get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                   "name": name, "input": args or {}})
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                out.append({"role": "assistant", "content": blocks})
                continue
            # user / 其他
            out.append({"role": "user", "content": self._content_blocks(m.get("content"))})

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": out,
        }
        if system_parts:
            body["system"] = "\n\n".join(s for s in system_parts if s)
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        body["temperature"] = 0.2
        return body

    # ---------- 响应解析 ----------
    def _parse_message(self, data: dict, t0: float) -> LLMResponse:
        latency_ms = int((time.monotonic() - t0) * 1000)
        text_parts: list[str] = []
        reason_parts: list[str] = []
        tcs: list[ToolCall] = []
        for b in data.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text") or "")
            elif bt == "thinking":
                reason_parts.append(b.get("thinking") or "")
            elif bt == "tool_use":
                tcs.append(ToolCall(id=b.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                    name=b.get("name") or "", arguments=b.get("input") or {}))
        text = "".join(text_parts)
        if not tcs and text:
            text, tcs = parse_text_calls(text, self.tool_names)
        return LLMResponse(text=text, reasoning="".join(reason_parts), tool_calls=tcs,
                           usage=_normalize_usage(data.get("usage") or {}), latency_ms=latency_ms)

    async def chat(self, messages: list[dict], tools: list[ToolSpec], ctx: Ctx,
                   on_delta=None, summarizer=None, *, timeout_s: int | None = None,
                   max_attempts: int | None = None) -> LLMResponse:
        # timeout_s / max_attempts：按用途收紧预算（摘要压缩用更短超时、更少重试）。
        # Anthropic 适配层目前只在 _request 里用全局上限；这里接住参数保证签名一致。
        report: dict[str, Any] = {}
        compressed = await compress_messages(messages, self.context_window,
                                             summarizer, report, tools=tools)
        # 压缩/媒体裁剪结果原地写回调用方的历史（与 openai_compat 同口径）
        messages[:] = compressed
        if report.get("compressed"):
            self._notify_compressed(ctx, report, "pre_send")
        messages, media_dropped = fit_media(messages, max_bytes=self.media_budget)
        if media_dropped:
            report["media_dropped"] = int(report.get("media_dropped") or 0) + media_dropped
        self.tool_names = {t.name for t in tools}
        body = self._to_anthropic(messages, tools)
        headers = {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        t0 = time.monotonic()
        retry_hook = getattr(ctx, "on_llm_retry", None)
        try:
            resp = await self._request(body, headers, on_delta, t0, on_retry=retry_hook,
                                       timeout_s=timeout_s, max_attempts=max_attempts)
        except _ContentPartsUnsupported as e:
            # 「anthropic 协议」中转站背后是 OpenAI/SGLang 内核时，图片块会被翻成
            # content 数组、被内核 400 拒绝（content 必须是字符串）。这不是请求内容错，
            # 是格式不兼容：把图片降级成文字说明、按纯文本重建后重发一次即可（与
            # openai_compat 的 parts 降级同口径），不能让它把整个 run 直接打崩。
            if not self._allow_images:
                raise
            self._allow_images = False
            log.warning("端点拒绝图片块（content 必须是字符串），降级为纯文本后重试: %s", e)
            body = self._to_anthropic(messages, tools)
            t0 = time.monotonic()
            resp = await self._request(body, headers, on_delta, t0, on_retry=retry_hook,
                                       timeout_s=timeout_s, max_attempts=max_attempts)
        except LlmContextOverflow as e:
            log.warning("端点报上下文超限，按半预算重压后重试: %s", e)
            retry_report: dict[str, Any] = {"source": "endpoint_overflow",
                                            "overflow_error": str(e)[:300]}
            compressed = await compress_messages(
                messages, self.context_window, summarizer, retry_report,
                tools=tools, budget_ratio=BUDGET_RATIO * 0.5)
            messages[:] = compressed
            report.update({k: v for k, v in retry_report.items() if k != "est_before"})
            self._notify_compressed(ctx, retry_report, "endpoint_overflow")
            body = self._to_anthropic(messages, tools)
            t0 = time.monotonic()
            resp = await self._request(body, headers, on_delta, t0, on_retry=retry_hook,
                                       timeout_s=timeout_s, max_attempts=max_attempts)
        except LlmPayloadTooLarge as e:
            used = media_stats(messages)["bytes"]
            if not used:
                raise
            self.media_budget = min(self.media_budget // 2, used // 2)
            log.warning("请求体被网关拒绝（%s），图片预算降到 %sKB 后重发一次",
                        e, self.media_budget // 1024)
            messages, dropped = fit_media(messages, max_bytes=self.media_budget)
            if dropped:
                report["media_dropped"] = int(report.get("media_dropped") or 0) + dropped
            body = self._to_anthropic(messages, tools)
            t0 = time.monotonic()
            resp = await self._request(body, headers, on_delta, t0, on_retry=retry_hook,
                                       timeout_s=timeout_s, max_attempts=max_attempts)
        resp.trace_id = uuid.uuid4().hex
        await self._report(ctx, body.get("messages") or [], body.get("tools") or [], resp, report)
        return resp

    def _notify_compressed(self, ctx: Ctx, report: dict, source: str) -> None:
        """适配层压缩了历史时通知 runtime（与 openai_compat 同口径，失败不影响主流程）。"""
        hook = getattr(ctx, "on_llm_compressed", None)
        if hook is None:
            return
        try:
            hook(dict(report, source=source), source)
        except Exception:  # noqa: BLE001
            log.exception("on_llm_compressed hook failed")

    async def _report(self, ctx: Ctx, sent_msgs: list[dict], oa_tools: list[dict],
                      resp: LLMResponse, report: dict) -> None:
        hook = getattr(ctx, "on_llm_request", None)
        if hook is None:
            return
        try:
            from game_agent.config import LLM_TRANSCRIPT_KEEP_DAYS
            if LLM_TRANSCRIPT_KEEP_DAYS <= 0:
                return
            rec_round = int(getattr(ctx, "llm_round", 0) or 0)
            rec = build_record(
                role=self.role, model=self.model, round_no=rec_round,
                messages=sent_msgs, tools=oa_tools,
                resp_text=resp.text, resp_reasoning=resp.reasoning,
                resp_tool_calls=[{"id": t.id, "name": t.name, "arguments": t.arguments,
                                  **({"arguments_error": t.arguments_error}
                                     if t.arguments_error else {})}
                                 for t in resp.tool_calls],
                usage=resp.usage, latency_ms=resp.latency_ms,
                compressed=bool(report.get("compressed")),
                dropped_msgs=int(report.get("dropped") or 0),
                include_tools=(rec_round == 0))
            rec["call_id"] = resp.trace_id
            await hook(rec)
        except Exception:  # noqa: BLE001
            log.exception("LLM 往返现场记录失败（不影响主流程）")

    async def _post_once(self, client: httpx.AsyncClient, body: dict,
                         headers: dict) -> httpx.Response:
        try:
            resp = await client.post(_messages_url(self.base_url), json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise self._net_error(e) from e
        if resp.status_code == 200:
            return resp
        if _is_waf_response(resp.status_code, resp.headers, resp.text):
            raise _WafBlocked(f"被 Cloudflare 人机验证拦下（HTTP {resp.status_code}，"
                              f"不是 Key 问题）；入口={self.base_url}")
        raise _classify_http_error(resp.status_code, resp.text,
                                   resp.headers.get("retry-after"))

    def _net_error(self, e: Exception) -> Exception:
        """连接层异常归类：走代理且连不上代理 → _ProxyRefused（当次换直连）；否则普通可重试。
        读超时不算代理不通（那是端点慢，换直连也白搭）。"""
        if getattr(self, "_using_proxy", False) and isinstance(
                e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)):
            return _ProxyRefused(f"{type(e).__name__}: {e}")
        return _RetryableLLMError(f"{type(e).__name__}: {e}")

    async def _request(self, body: dict, headers: dict, on_delta, t0: float,
                       on_retry=None, timeout_s: int | None = None,
                       max_attempts: int | None = None) -> LLMResponse:
        from game_agent.config import LLM_CALL_TIMEOUT_S, LLM_MAX_ATTEMPTS
        call_timeout = int(timeout_s or LLM_CALL_TIMEOUT_S)
        max_attempts = max(1, int(max_attempts or LLM_MAX_ATTEMPTS))
        use_stream = on_delta is not None
        last_err = ""
        # 代理自动模式：系统配了代理就先走代理，没配就直连；走代理连接失败当次立刻改直连。
        # 详见 openai_compat._request 的同名注释。
        proxy: str | None = None
        if self._direct_only or session_is_direct(self.session_key):
            # 会话级记忆：这条会话早前已从代理降级成直连（代理不通 / 被 Cloudflare 拦下），
            # 后面所有往返一律直连，不再每条请求先白撞一次代理。
            if not self._direct_only:
                self._direct_only = True
                log.info("本会话已改用直连：后续 LLM 请求不再尝试代理（%s）", self.base_url)
        elif not _proxy_env_disabled():
            proxy = await asyncio.to_thread(_system_proxy)
        self._using_proxy = bool(proxy)
        async with _new_llm_client(proxy, self.user_agent) as client:
            downgraded = False
            attempt = 0
            while attempt < max_attempts:
                attempt += 1
                retry_after: str | None = None
                try:
                    if use_stream:
                        try:
                            return await asyncio.wait_for(
                                self._chat_stream(client, body, headers, on_delta, t0),
                                timeout=call_timeout)
                        except (LlmAuthError, LlmContextOverflow, LlmPayloadTooLarge,
                                _ContentPartsUnsupported):
                            raise
                        except _ProxyRefused:
                            raise
                        except _WafBlocked:
                            raise
                        except asyncio.TimeoutError:
                            raise
                        except (httpx.TimeoutException, httpx.TransportError) as e:
                            # 连接层失败（最常见：代理端口没人监听）：交给外层「代理不通→改直连」，
                            # 并保持流式，别误判成「端点不支持流式」而白白降级。
                            raise self._net_error(e) from e
                        except Exception as e:  # noqa: BLE001 - 流式不支持，本轮降级
                            log.warning("流式请求失败（第 %s/%s 次），退化为非流式: %s",
                                        attempt, max_attempts, e)
                            use_stream = False
                    resp = await asyncio.wait_for(self._post_once(client, body, headers),
                                                  timeout=call_timeout)
                    return self._parse_message(resp.json(), t0)
                except LlmAuthError:
                    raise
                except asyncio.TimeoutError:
                    last_err = f"单次调用超过 {call_timeout}s 硬上限"
                except _ProxyRefused as e:
                    if proxy and not downgraded:
                        downgraded = True
                        attempt -= 1
                        last_err = f"代理不通（{proxy}：{e}），已自动改直连重试"
                        log.warning("经代理连接失败，自动降级直连: %s", e)
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            pass
                        client = _new_llm_client(None, self.user_agent)
                        self._using_proxy = False
                        # 本会话从此只走直连：后面的往返连「先撞一次代理」都省掉
                        self._direct_only = True
                        mark_session_direct(self.session_key)
                        if on_retry:
                            try:
                                on_retry(attempt, max_attempts, 0.0, last_err)
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    last_err = str(e)
                except _WafBlocked as e:
                    # 走代理被人机验证拦截：代理出口 IP 是根源，换直连通常就 200（不消费重试次数）
                    if proxy and not downgraded:
                        downgraded = True
                        attempt -= 1
                        last_err = f"经代理被 Cloudflare 人机验证拦截（{proxy}），已自动改直连重试"
                        log.warning("经代理被 WAF 拦截，自动降级直连: %s", e)
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            pass
                        client = _new_llm_client(None, self.user_agent)
                        self._using_proxy = False
                        # 被 Cloudflare 拦不是偶发抖动：本会话别再拿代理试了
                        self._direct_only = True
                        mark_session_direct(self.session_key)
                        if on_retry:
                            try:
                                on_retry(attempt, max_attempts, 0.0, last_err)
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    last_err = str(e)
                except _RetryableLLMError as e:
                    last_err = str(e)
                    retry_after = e.retry_after
                if attempt < max_attempts:
                    try:
                        delay = float(retry_after) if retry_after else (
                            LLM_RETRY_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5))
                    except ValueError:
                        delay = LLM_RETRY_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    log.warning("LLM 调用失败（第 %s/%s 次），%.1fs 后重试: %s",
                                attempt, max_attempts, delay, last_err)
                    if on_retry:
                        try:
                            on_retry(attempt, max_attempts, delay, last_err)
                        except Exception:  # noqa: BLE001
                            pass
                    await asyncio.sleep(delay)
        raise RuntimeError(f"LLM 请求已尝试 {max_attempts} 次均失败，放弃: {last_err}")

    async def _chat_stream(self, client: httpx.AsyncClient, body: dict, headers: dict,
                           on_delta, t0: float) -> LLMResponse:
        stream_body = {**body, "stream": True}
        text_parts: list[str] = []
        reason_parts: list[str] = []
        # content block index -> {"type","id","name","json"}
        blocks: dict[int, dict] = {}
        usage: dict = {}
        ttft_ms = 0  # 首 token 耗时（第一个文本/思考增量到达时刻）
        async with client.stream("POST", _messages_url(self.base_url),
                                 json=stream_body, headers=headers) as resp:
            if resp.status_code in (401, 403, 429, 503):
                raw = (await resp.aread())[:2000].decode("utf-8", "ignore")
                if _is_waf_response(resp.status_code, resp.headers, raw):
                    raise _WafBlocked(f"被 Cloudflare 人机验证拦下（HTTP {resp.status_code}，"
                                      f"不是 Key 问题）；入口={self.base_url}")
                if resp.status_code in (401, 403):
                    raise LlmAuthError(
                        f"LLM 端点拒绝访问（HTTP {resp.status_code}）：请检查 API Key 与账户额度。{raw[:200]}")
                raise _classify_http_error(resp.status_code, raw,
                                           resp.headers.get("retry-after"))
            if resp.status_code != 200:
                raw = (await resp.aread())[:200].decode("utf-8", "ignore")
                raise _classify_http_error(resp.status_code, raw,
                                           resp.headers.get("retry-after"))
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        d = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    et = d.get("type")
                    if et == "message_start":
                        u = (d.get("message") or {}).get("usage") or {}
                        if u:
                            usage.update(u)
                    elif et == "content_block_start":
                        idx = int(d.get("index", 0))
                        cb = d.get("content_block") or {}
                        blocks[idx] = {"type": cb.get("type"),
                                       "id": cb.get("id") or "",
                                       "name": cb.get("name") or "",
                                       "json": ""}
                        if cb.get("type") == "text" and cb.get("text"):
                            if not ttft_ms:
                                ttft_ms = max(1, int((time.monotonic() - t0) * 1000))
                            text_parts.append(cb["text"])
                            _safe_delta(on_delta, "", cb["text"])
                        elif cb.get("type") == "thinking" and cb.get("thinking"):
                            if not ttft_ms:
                                ttft_ms = max(1, int((time.monotonic() - t0) * 1000))
                            reason_parts.append(cb["thinking"])
                            _safe_delta(on_delta, cb["thinking"], "")
                    elif et == "content_block_delta":
                        idx = int(d.get("index", 0))
                        delta = d.get("delta") or {}
                        dt = delta.get("type")
                        if dt == "text_delta":
                            t = delta.get("text") or ""
                            if t:
                                if not ttft_ms:
                                    ttft_ms = max(1, int((time.monotonic() - t0) * 1000))
                                text_parts.append(t)
                                _safe_delta(on_delta, "", t)
                        elif dt == "thinking_delta":
                            t = delta.get("thinking") or ""
                            if t:
                                if not ttft_ms:
                                    ttft_ms = max(1, int((time.monotonic() - t0) * 1000))
                                reason_parts.append(t)
                                _safe_delta(on_delta, t, "")
                        elif dt == "input_json_delta":
                            slot = blocks.setdefault(idx, {"type": "tool_use", "id": "", "name": "", "json": ""})
                            slot["json"] += delta.get("partial_json") or ""
                    elif et == "message_delta":
                        u = d.get("usage") or {}
                        if u:
                            usage.update(u)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise self._net_error(e) from e
        latency_ms = int((time.monotonic() - t0) * 1000)
        tcs: list[ToolCall] = []
        for idx in sorted(blocks):
            slot = blocks[idx]
            if slot.get("type") != "tool_use":
                continue
            args, arg_err = parse_tool_arguments(slot.get("json") or "")
            tcs.append(ToolCall(id=slot.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                name=slot.get("name") or "", arguments=args,
                                arguments_error=arg_err))
        text_out = "".join(text_parts)
        if not tcs and text_out:
            text_out, tcs = parse_text_calls(text_out, self.tool_names)
        return LLMResponse(text=text_out, reasoning="".join(reason_parts),
                           tool_calls=tcs, usage=_normalize_usage(usage), latency_ms=latency_ms,
                           ttft_ms=ttft_ms)


def _is_tool_result(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_result"


def _safe_delta(on_delta, r: str, t: str) -> None:
    if on_delta is None:
        return
    try:
        on_delta(r, t)
    except Exception:  # noqa: BLE001 - 回调失败不影响流式收集
        pass
