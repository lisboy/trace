"""OpenAI 兼容 LLM 客户端（设计 §2.4：OpenAI / Anthropic / Google / Ollama 统一适配层）。

支持 tool calling、视觉消息（image_url parts）、思考过程（reasoning_content / reasoning）。
发送前按模型档位的 context_window 压缩历史（见 context_window.py）。
非流式（流式令牌事件由编排器单独处理，见 orchestration）。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import uuid
from typing import Any

import httpx

from game_agent.config import (
    LLM_MODEL, LLM_RETRY_BASE_S, LLM_USER_AGENT, OPENAI_API_KEY, OPENAI_BASE_URL,
)
from game_agent.llm.base import (
    Ctx, LLM, LLMResponse, LlmAuthError, LlmContextOverflow, LlmPayloadTooLarge,
    ToolCall, ToolSpec, parse_tool_arguments,
)
from game_agent.llm.context_window import (
    BUDGET_RATIO, MEDIA_BYTES_BUDGET, compress_messages, fit_media, media_stats,
)
from game_agent.llm.text_tool_calls import parse_text_calls
from game_agent.llm.transcript import build_record
from game_agent.logging_setup import get_logger

log = get_logger("llm.openai")

NO_VISION_NOTE = "[图片已忽略：当前模型档位未开启视觉（vision）]"

# 可重试：限流 / 服务端瞬断 / 网络与超时错误
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class _RetryableLLMError(RuntimeError):
    """可重试错误（429/5xx/超时/网络中断）：由调用方统一退避重试。"""

    def __init__(self, msg: str, retry_after: str | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class _ProxyRefused(_RetryableLLMError):
    """**经由代理**连接失败（代理端口没人监听/握手不通）。

    单独分一类，是因为它的正确处置不是「退避重试同一个失败」，而是**当次立刻换直连**：
    系统代理多半是本地代理/VPN 客户端设的，客户端一退出端口就失效，此时直连通常没问题。
    继承 _RetryableLLMError，所以在别处被当成可重试错误处理也不会出错。"""


def _system_proxy() -> str | None:
    """本机「系统代理」地址（Windows 注册表 / 环境变量里配的），没配则 None。

    用 urllib 读（Windows 上正是读 Internet 设置），不走 httpx 的 trust_env 全量探测，
    这样只拿到一个确切的代理 URL，且能被 asyncio.to_thread 安全调用。
    注意：只代表「系统里配了代理」，不代表它此刻可用——可用性在真正连接时才知道。"""
    try:
        import urllib.request
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        return None
    p = proxies.get("https") or proxies.get("http") or proxies.get("all")
    if not p:
        return None
    p = p.strip()
    if "://" not in p:
        p = "http://" + p
    return p or None


def _new_llm_client(proxy: str | None, user_agent: str | None = None, *,
                    timeout: float = 300, follow_redirects: bool = False) -> httpx.AsyncClient:
    """模型请求专用的 httpx 客户端。

    `proxy` 为 None → 直连：显式 trust_env=False，**不读系统代理**。这是默认，
    因为系统代理常指向已退出的本地代理软件（127.0.0.1:xxxxx），让它生效会让每条请求
    都撞死端口、表现为满屏 `ConnectError: All connection attempts failed`。
    `proxy` 非 None → 显式使用该代理，仍 trust_env=False（只认我们指定的这一个）。
    需要走代理的联网类工具（web 搜索/抓取）另有自己的客户端，不受影响。"""
    kwargs: dict = {"timeout": timeout, "trust_env": False,
                    "follow_redirects": follow_redirects}
    if proxy:
        kwargs["proxy"] = proxy
    if user_agent:
        kwargs["headers"] = {"User-Agent": user_agent}
    return httpx.AsyncClient(**kwargs)


def _proxy_env_disabled() -> bool:
    """用户显式要求「不用系统代理」时返回 True（GA_LLM_NO_PROXY=1/true/yes）。"""
    v = os.environ.get("GA_LLM_NO_PROXY", "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


# 端点「请求太长」的说法各家不同，命中任何一条都按上下文超限处理（值得重压后重试一次）
_OVERFLOW_PAT = re.compile(
    r"context.{0,24}(length|window|limit)|too many tokens|maximum.{0,16}context"
    r"|exceed(s|ed)?.{0,24}(context|token)|input is too long|超出.{0,8}(上下文|长度|token)"
    r"|超过.{0,8}(上下文|长度|最大)", re.I)


def _classify_http_error(status: int, raw: str,
                         retry_after: str | None = None) -> RuntimeError:
    """把非 200 响应分成「鉴权 / 上下文超限 / 可重试 / 其他参数错误」。"""
    if status in (401, 403):
        return LlmAuthError(
            f"LLM 端点拒绝访问（HTTP {status}）：请检查 API Key 与账户额度。{raw[:200]}")
    if status in (413, 414):
        # 前置网关按字节数拒收（token 预算管不住它：一张图 900 token 却是几 MB）
        return LlmPayloadTooLarge(f"HTTP {status}: {raw[:200]}")
    if status == 400 and _OVERFLOW_PAT.search(raw):
        return LlmContextOverflow(f"HTTP 400: {raw[:300]}")
    if status in _RETRYABLE_STATUS:
        return _RetryableLLMError(f"HTTP {status}: {raw[:200]}", retry_after)
    return RuntimeError(f"LLM HTTP {status}: {raw[:500]}")


def _normalize_usage(usage: dict) -> dict:
    """把各家 usage 归一化成统一字段，供用量统计与缓存命中率计算。

    - DeepSeek：``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``（也透传）
    - OpenAI：``prompt_tokens_details.cached_tokens``
    - 统一输出 ``cached_tokens``（命中缓存的 prompt token 数），无该信息记为 0。
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = 0
    reported = False
    if usage.get("prompt_cache_hit_tokens") is not None:
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
        reported = True
    details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cached = max(cached, int(details.get("cached_tokens") or 0))
        reported = True
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": min(cached, prompt),
        # 端点是否真的上报了缓存信息：没上报时命中率应为"未知"，不能显示成 0%
        "cache_reported": reported,
    }
    for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        if k in usage:
            out[k] = int(usage[k] or 0)
    return out


class OpenAICompatibleLLM(LLM):
    def __init__(self, role: str, model: str | None = None,
                 api_key: str | None = None, base_url: str | None = None,
                 context_window: int = 0, vision: bool = False,
                 user_agent: str | None = None,
                 reasoning_effort: str = "") -> None:
        self.role = role
        self.model = model or LLM_MODEL
        self.api_key = api_key or OPENAI_API_KEY
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.context_window = context_window
        self.vision = vision
        # 推理强度（模型档位 params_json.reasoning_effort）：""=不传走端点默认；
        # none/minimal/low/medium/high 原样透传，端点不认该字段时会自己忽略或报错
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        # 供应商可单独指定 UA（前置 WAF 按 UA 拦脚本），未指定则用全局 GA_LLM_USER_AGENT
        self.user_agent = user_agent or LLM_USER_AGENT
        # 本轮可见工具名：文本协议兜底解析时用来挡掉白名单外的调用
        self.tool_names: set[str] = set()
        self.media_budget = MEDIA_BYTES_BUDGET
        # 当前 httpx 客户端是否正在走代理（连接层失败时据此决定「换直连」还是「普通重试」）
        self._using_proxy = False

    # ---------- 消息转换 ----------
    def _content(self, m: dict) -> Any:
        """str / content parts 透传；非视觉模型把图片替换为提示文本。"""
        c = m.get("content")
        if not isinstance(c, list):
            return c if c is not None else ""
        if self.vision:
            return c
        return " ".join(
            NO_VISION_NOTE if isinstance(p, dict) and p.get("type") == "image_url"
            else str(p.get("text", "")) if isinstance(p, dict) else str(p)
            for p in c
        ).strip()

    def _system_part_text(self, m: dict) -> str:
        """system 消息的纯文本（把多条 system 合并成一条时用）。"""
        c = m.get("content")
        if isinstance(c, list):
            return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
        return str(c) if c is not None else ""

    def _to_oai(self, messages: list[dict], tools: list[ToolSpec]) -> tuple[dict, list[dict]]:
        oa_msgs = []
        sys_parts: list[str] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                # 严格端点（DeepSeek 等）只接受「唯一且在最前」的 system。历史上既有初始
                # system 提示、又有压缩后生成的摘要 system，两条并排就会被拒：
                # 400 "System message must be at the beginning."。这里按原顺序合并成**一条**
                # 顶层 system（内容空行分隔，与 Anthropic 适配器同一套思路）；哪怕 system 出现在
                # 非 system 之后也并到最前面，保证发出去的 system 永远只有最前那一条。
                txt = self._system_part_text(m)
                if txt.strip():
                    sys_parts.append(txt)
                continue
            if role == "tool":
                oa_msgs.append({
                    "role": "tool", "tool_call_id": m["tool_call_id"],
                    "content": m.get("content", ""),
                })
            elif role == "assistant" and m.get("tool_calls"):
                oa_msgs.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {
                            "id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                        }
                        for tc in m["tool_calls"]
                    ],
                })
            else:
                oa_msgs.append({"role": role, "content": self._content(m)})
        if sys_parts:
            oa_msgs.insert(0, {"role": "system", "content": "\n\n".join(sys_parts)})
        oa_tools = [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters,
            }}
            for t in tools
        ] if tools else []
        return {"messages": oa_msgs, "tools": oa_tools}, oa_tools

    async def _post_once(self, client: httpx.AsyncClient, body: dict,
                         headers: dict) -> httpx.Response:
        """单次非流式请求；把结果分成「可重试 / 不可重试」两类抛出。"""
        try:
            resp = await client.post(f"{self.base_url}/chat/completions",
                                     json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise self._net_error(e) from e
        if resp.status_code == 200:
            return resp
        raise _classify_http_error(resp.status_code, resp.text,
                                   resp.headers.get("retry-after"))

    def _net_error(self, e: Exception) -> Exception:
        """把连接层异常归类：**正在走代理且是连不上代理** → _ProxyRefused（当次换直连）；
        其它情况保持普通可重试。不把「读超时」也算代理不通——那是端点/网关慢，换直连也白搭，
        退避重试即可。"""
        if getattr(self, "_using_proxy", False) and isinstance(
                e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)):
            return _ProxyRefused(f"{type(e).__name__}: {e}")
        return _RetryableLLMError(f"{type(e).__name__}: {e}")

    async def chat(self, messages: list[dict], tools: list[ToolSpec], ctx: Ctx,
                   on_delta=None, summarizer=None, *,
                   timeout_s: int | None = None,
                   max_attempts: int | None = None) -> LLMResponse:
        # timeout_s / max_attempts：调用方可按用途收紧预算（摘要压缩用更短的超时与更少
        # 的重试——它不是正文产出，卡住不该按 5×360s 把整个 run 拖死）。默认仍用全局配置。
        # 超长历史按当前模型窗口做全局摘要压缩（见 context_window.compress_messages）：
        # 预算里必须先扣掉每轮都重发的工具定义，否则「压完了」只是错觉。
        # runtime 会先把压缩结果写回它自己的历史，这里的调用多数只是复核一遍。
        report: dict[str, Any] = {}
        messages = await compress_messages(messages, self.context_window,
                                           summarizer, report, tools=tools)
        messages, media_dropped = fit_media(messages, max_bytes=self.media_budget)
        if media_dropped:
            report["media_dropped"] = int(report.get("media_dropped") or 0) + media_dropped
        self.tool_names = {t.name for t in tools}
        payload, oa_tools = self._to_oai(messages, tools)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": payload["messages"],
            "temperature": 0.2,
        }
        if self.reasoning_effort and self.reasoning_effort != "default":
            body["reasoning_effort"] = self.reasoning_effort
        if payload["tools"]:
            body["tools"] = payload["tools"]
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key or ''}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        retry_hook = getattr(ctx, "on_llm_retry", None)
        try:
            resp = await self._request(body, headers, on_delta, t0, on_retry=retry_hook,
                                        timeout_s=timeout_s, max_attempts=max_attempts)
        except LlmContextOverflow as e:
            # 端点说「太长」：说明窗口比配的/猜的小。按半预算重压一次再发，
            # 而不是让整个 run 直接失败（下次再撞就是这次压完的规模了）。
            log.warning("端点报上下文超限，按半预算重压后重试: %s", e)
            retry_report: dict[str, Any] = {}
            messages = await compress_messages(
                messages, self.context_window, summarizer, retry_report,
                tools=tools, budget_ratio=BUDGET_RATIO * 0.5)
            report.update({k: v for k, v in retry_report.items() if k != "est_before"})
            payload, oa_tools = self._to_oai(messages, tools)
            body["messages"] = payload["messages"]
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
            payload, oa_tools = self._to_oai(messages, tools)
            body["messages"] = payload["messages"]
            t0 = time.monotonic()
            resp = await self._request(body, headers, on_delta, t0, on_retry=retry_hook,
                                        timeout_s=timeout_s, max_attempts=max_attempts)
        resp.trace_id = uuid.uuid4().hex
        await self._report(ctx, payload["messages"], oa_tools, resp, report)
        return resp

    async def _report(self, ctx: Ctx, sent_msgs: list[dict], oa_tools: list[dict],
                      resp: LLMResponse, report: dict) -> None:
        """把这一往返（发给模型的原文 + 模型返回）交给 runtime 注入的回调落库。
        观测性不能反过来拖垮主流程：任何异常只记日志。"""
        hook = getattr(ctx, "on_llm_request", None)
        if hook is None:
            return
        try:
            # 保留天数 0 = 关掉现场记录（只留 llm_calls 计数），此时不再组装大字段
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
                # 工具定义每轮都相同且最占体积，只在 run 第一轮存一份
                include_tools=(rec_round == 0))
            rec["call_id"] = resp.trace_id
            await hook(rec)
        except Exception:  # noqa: BLE001
            log.exception("LLM 往返现场记录失败（不影响主流程）")

    async def _request(self, body: dict, headers: dict, on_delta, t0: float,
                       on_retry=None, timeout_s: int | None = None,
                       max_attempts: int | None = None) -> LLMResponse:
        # 单次 LLM 调用硬上限：httpx 的 timeout=300 是「相邻数据块间隔」，
        # 网关持续吐心跳/空行时永不触发——必须外层 wait_for 兜底，
        # 否则中转站挂起会让 run 卡几个小时（watchdog 65min 才兜底，体验差）
        from game_agent.config import LLM_CALL_TIMEOUT_S, LLM_MAX_ATTEMPTS
        # 最多 LLM_MAX_ATTEMPTS（默认 5）次尝试：429/5xx/超时/网络错误→指数退避重试；
        # 401/403（鉴权/额度）与其他 4xx 参数错误立即失败，不浪费尝试次数。
        # 流式与非流式共用同一份尝试预算：首轮优先流式，流式不支持则本轮退化为
        # 非流式，后续轮次不再尝试流式。
        # timeout_s / max_attempts 由调用方按用途收紧（摘要压缩：短超时 + 少重试）。
        call_timeout = int(timeout_s or LLM_CALL_TIMEOUT_S)
        max_attempts = max(1, int(max_attempts or LLM_MAX_ATTEMPTS))
        use_stream = on_delta is not None
        last_err = ""
        # 代理自动模式：系统里配了代理就先试「走代理」，没配就直接「直连」。走代理的
        # 每一次尝试只要**连接层失败**（端口没人监听/握手不通），当次立刻换直连再试同一轮
        # ——代理多半是本地客户端设的、客户端一退就失效，而直连通常没问题。这样「有代理走
        # 代理、没代理直连、代理不通自动降级直连」都覆盖到，既不会因代理开着但客户端没开
        # 而「怎么对话都用不了」，重开代理客户端时又能正常用回。
        proxy = None if _proxy_env_disabled() else await asyncio.to_thread(_system_proxy)
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
                        except (LlmAuthError, LlmContextOverflow, LlmPayloadTooLarge):
                            raise
                        except _ProxyRefused:
                            # 代理连不上要交给外层「换直连」，别当成「流式不支持」降级
                            raise
                        except asyncio.TimeoutError:
                            # 流式硬超时：端点已经挂住，不再本轮降级，直接计一次失败退避重试
                            raise
                        except (httpx.TimeoutException, httpx.TransportError) as e:
                            # 连接层失败（最常见：代理端口没人监听）。交给外层按「代理不通→
                            # 改直连」处理，且**保持流式**——别误当成「端点不支持流式」降级。
                            raise self._net_error(e) from e
                        except Exception as e:  # noqa: BLE001 - 流式不支持，本轮降级
                            log.warning("流式请求失败（第 %s/%s 次），退化为非流式: %s",
                                        attempt, max_attempts, e)
                            use_stream = False
                    resp = await asyncio.wait_for(self._post_once(client, body, headers),
                                                  timeout=call_timeout)
                    return self._parse_completion(resp.json(), t0)
                except LlmAuthError:
                    raise
                except asyncio.TimeoutError:
                    last_err = f"单次调用超过 {call_timeout}s 硬上限"
                except _ProxyRefused as e:
                    # 走代理连不上：当次立刻切直连，且**不消费**重试次数（真正的问题还没试到）
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
                        # 503/限流这类会自己恢复的失败，同步给运行轨迹一条内联提醒；
                        # 钩子出错不能影响重试本身
                        try:
                            on_retry(attempt, max_attempts, delay, last_err)
                        except Exception:  # noqa: BLE001
                            pass
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"LLM 请求已尝试 {max_attempts} 次均失败，放弃: {last_err}")

    def _parse_completion(self, data: dict, t0: float) -> LLMResponse:
        """非流式响应体 → LLMResponse（流式聚合后也复用字段解析）。"""
        latency_ms = int((time.monotonic() - t0) * 1000)
        choice = data["choices"][0]["message"]
        text = choice.get("content") or ""
        # 思考过程：OpenAI o 系列 reasoning_content；部分兼容网关用 reasoning
        reasoning = choice.get("reasoning_content") or choice.get("reasoning") or ""
        tcs: list[ToolCall] = []
        for tc in choice.get("tool_calls") or []:
            fn = tc["function"]
            args, arg_err = parse_tool_arguments(fn.get("arguments") or "")
            tcs.append(ToolCall(id=tc["id"], name=fn["name"], arguments=args,
                                arguments_error=arg_err))
        if not tcs and text:
            # 端点不走结构化 tool_calls：把正文里的文本协议调用解析出来（见 text_tool_calls）
            text, tcs = parse_text_calls(text, self.tool_names)
        usage = data.get("usage", {}) or {}
        return LLMResponse(text=text, reasoning=str(reasoning), tool_calls=tcs,
                           usage=_normalize_usage(usage), latency_ms=latency_ms)

    async def _chat_stream(self, client: httpx.AsyncClient, body: dict, headers: dict,
                           on_delta, t0: float) -> LLMResponse:
        """SSE 流式：逐段回调 on_delta(reasoning_delta, text_delta)，返回聚合结果。"""
        stream_body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        text_parts: list[str] = []
        reason_parts: list[str] = []
        tcs: dict[int, dict] = {}
        usage: dict = {}
        async with client.stream("POST", f"{self.base_url}/chat/completions",
                                 json=stream_body, headers=headers) as resp:
            if resp.status_code in (401, 403):
                # 与非流式 _post_once 保持一致：把服务端返回的真实原因带出来，
                # 否则排查时只能看到光秃秃的 "HTTP 403"，定位不到 INVALID_API_KEY / 额度等根因。
                raw = (await resp.aread())[:200].decode("utf-8", "ignore")
                raise LlmAuthError(
                    f"LLM 端点拒绝访问（HTTP {resp.status_code}）："
                    f"请检查 API Key 与账户额度。{raw}")
            if resp.status_code != 200:
                raw = (await resp.aread())[:200].decode("utf-8", "ignore")
                raise _classify_http_error(resp.status_code, raw,
                                           resp.headers.get("retry-after"))
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        d = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if d.get("usage"):
                        usage = d["usage"]
                    for ch in d.get("choices") or []:
                        delta = ch.get("delta") or {}
                        rd = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        td = delta.get("content") or ""
                        if rd:
                            reason_parts.append(rd)
                        if td:
                            text_parts.append(td)
                        if rd or td:
                            try:
                                on_delta(rd, td)
                            except Exception:  # noqa: BLE001 - 回调失败不影响流式收集
                                pass
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index", 0))
                            slot = tcs.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # 流中途断开：归类为可重试，交由 chat() 的尝试预算处理
                raise self._net_error(e) from e
        latency_ms = int((time.monotonic() - t0) * 1000)
        parsed_tcs: list[ToolCall] = []
        for slot in [tcs[k] for k in sorted(tcs)]:
            args, arg_err = parse_tool_arguments(slot["arguments"])
            parsed_tcs.append(ToolCall(id=slot["id"], name=slot["name"],
                                       arguments=args, arguments_error=arg_err))
        usage_out = _normalize_usage(usage)
        text_out = "".join(text_parts)
        if not parsed_tcs and text_out:
            # 流式同理：整段攒完再兜底解析（模板 token 可能被拆在多个 chunk 里）
            text_out, parsed_tcs = parse_text_calls(text_out, self.tool_names)
        return LLMResponse(text=text_out, reasoning="".join(reason_parts),
                           tool_calls=parsed_tcs, usage=usage_out, latency_ms=latency_ms)
