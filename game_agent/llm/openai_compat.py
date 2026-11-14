"""OpenAI 兼容 LLM 客户端（设计 §2.4：OpenAI / Anthropic / Google / Ollama 统一适配层）。

支持 tool calling、视觉消息（image_url parts）、思考过程（reasoning_content / reasoning）。
发送前按模型档位的 context_window 压缩历史（见 context_window.py）。
content 做兼容归一化：只有「user + 视觉档位 + 真带图」才发 parts 数组，其余一律字符串；
端点仍以「content 必须是 string」400 拒绝时自动压成纯文本重试（见 _ContentPartsUnsupported）。
非流式（流式令牌事件由编排器单独处理，见 orchestration）。
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx

from game_agent.config import (
    LLM_CONNECT_TIMEOUT_S, LLM_MAX_CONCURRENCY, LLM_MODEL, LLM_PARALLEL_TOOL_CALLS,
    LLM_RETRY_BASE_S, LLM_USER_AGENT, OPENAI_API_KEY, OPENAI_BASE_URL,
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
# 端点只收纯文本 content（严格网关会 400）：图片无法作为 parts 发送时的说明。
# 与 NO_VISION_NOTE 分开，事后能从上下文判断是「档位没开视觉」还是「连接不兼容 parts」。
PARTS_OFF_NOTE = ("[图片已省略：当前模型连接不接受多段 content（image_url），本轮无法看图。"
                  "请基于文字与文件路径继续，并在结论里说明未目视确认；不要声称已看过图片。"
                  "需要真正看图时请改用支持视觉的模型档位]")

# 可重试：限流 / 服务端瞬断 / 网络与超时错误
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _parts_to_text(parts: Any, *, image_note: str = "") -> str:
    """content parts → 纯字符串；图片按 image_note 替换（空串 = 直接省略）。

    只有「user + 视觉档位 + 端点接受 parts + 真带图」才保留数组，其余一律走这里。
    严格端点（如 SGLang 的 ChatCompletionMessageGenericParam）把 content 定成 str，
    连纯文本 parts 都会 400；所以数组能不发就不发。
    """
    if parts is None:
        return ""
    if not isinstance(parts, list):
        if isinstance(parts, dict):
            # 单块 {type:text,text:...} / 思考块 / 其它结构，都尽量取可读文本
            if parts.get("type") in ("thinking", "reasoning"):
                return ""
            return str(parts.get("text") or parts.get("content") or "")
        return str(parts)
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            if p.get("type") == "image_url":
                if image_note and image_note not in out:
                    out.append(image_note)
                continue
            if p.get("type") in ("thinking", "reasoning"):
                continue  # 思考块不回传：部分端点只允许 assistant 带 thinking part
            txt = p.get("text")
            if txt:
                out.append(str(txt))
        elif p is not None:
            out.append(str(p))
    return " ".join(out).strip()


class _RetryableLLMError(RuntimeError):
    """可重试错误（429/5xx/超时/网络中断）：由调用方统一退避重试。"""

    def __init__(self, msg: str, retry_after: str | None = None,
                 status: int | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after
        # HTTP 状态码（连接层失败时为 None）：429 要用来收并发闸门（见 _AdaptiveGate）
        self.status = status


class _ProxyRefused(_RetryableLLMError):
    """**经由代理**连接失败（代理端口没人监听/握手不通）。

    单独分一类，是因为它的正确处置不是「退避重试同一个失败」，而是**当次立刻换直连**：
    系统代理多半是本地代理/VPN 客户端设的，客户端一退出端口就失效，此时直连通常没问题。
    继承 _RetryableLLMError，所以在别处被当成可重试错误处理也不会出错。"""


class _WafBlocked(_RetryableLLMError):
    """被站点前置的 Cloudflare 人机验证拦下（HTTP 403 + HTML 挑战页，不是 Key 问题）。

    走代理时最常见的根因是**代理出口 IP 声誉差**（同一台机器直连往往 200），所以正确
    处置与 _ProxyRefused 相同：当次立刻换直连；直连仍被拦则按可重试错误退避。
    继承 _RetryableLLMError，兜底路径也不会出错。"""


def _is_waf_response(status: int, headers, raw: str) -> bool:
    """HTTP 403/429/503 + HTML 挑战页 = 人机验证，不是鉴权失败。"""
    if status not in (403, 429, 503):
        return False
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    ct = (headers.get("content-type") or "").lower()
    if "html" not in ct:
        return False
    low = (raw or "")[:800].lower()
    return ("just a moment" in low or "cf-chl" in low
            or "challenge-platform" in low
            or "<!doctype html" in low or "<html" in low)


class _ContentPartsUnsupported(RuntimeError):
    """端点不接受 content parts 数组（只收纯文本 str）。

    不是请求本身有错，而是**格式不兼容**：把 messages 压成纯字符串后重发即可，
    不需要退避、也不应该计一次失败。典型来源：SGLang 的 ChatCompletionMessageGenericParam
    （``'content', 'str'`` / ``Input should be a valid string``），常见于中转站后面的
    Qwen/GLM 等推理后端。"""


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
                    timeout: Any = None, follow_redirects: bool = False) -> httpx.AsyncClient:
    """模型请求专用的 httpx 客户端。

    `proxy` 为 None → 直连：显式 trust_env=False，**不读系统代理**。这是默认，
    因为系统代理常指向已退出的本地代理软件（127.0.0.1:xxxxx），让它生效会让每条请求
    都撞死端口、表现为满屏 `ConnectError: All connection attempts failed`。
    `proxy` 非 None → 显式使用该代理，仍 trust_env=False（只认我们指定的这一个）。
    需要走代理的联网类工具（web 搜索/抓取）另有自己的客户端，不受影响。

    超时口径（2026-11-14 改）：**建连 8s、读（相邻数据块间隔）300s**。
    以前统一 300s，等于把「Cloudflare 某个 A 记录黑洞」的代价放大成硬等 5 分钟；
    真正防挂死的是外层 `asyncio.wait_for(LLM_CALL_TIMEOUT_S)`，不靠建连超时。
    `limits` 打开长连接复用：一个 run 几十上百轮往返，每轮重新握手要白扔 0.6~0.9s。
    """
    kwargs: dict = {
        "timeout": timeout if timeout is not None else httpx.Timeout(300, connect=LLM_CONNECT_TIMEOUT_S),
        "trust_env": False,
        "follow_redirects": follow_redirects,
        "limits": httpx.Limits(max_connections=64, max_keepalive_connections=32,
                               keepalive_expiry=90),
    }
    if proxy:
        kwargs["proxy"] = proxy
    if user_agent:
        kwargs["headers"] = {"User-Agent": user_agent}
    return httpx.AsyncClient(**kwargs)


def _proxy_env_disabled() -> bool:
    """用户显式要求「不用系统代理」时返回 True（GA_LLM_NO_PROXY=1/true/yes）。"""
    v = os.environ.get("GA_LLM_NO_PROXY", "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------- 连接复用
# 每次 LLM 往返都新建 httpx.AsyncClient 的代价，在跨境端点上非常实在：
# TCP 255~410ms + TLS 330~680ms ≈ 0.6~0.9s（2026-11-14 对 gonode.si 实测），
# 一个 100 轮往返的 run 光握手就白扔一分多钟；更糟的是每建一次连接就重掷一次
# DNS/选路骰子（Cloudflare 两个 A 记录里那个会黑洞的，命中就是十几秒到几分钟）。
# 所以改成**进程级共享客户端**：只在「直连/走代理」切换、UA 变化或被关掉时重建，
# 其余时间全部复用同一条 keep-alive 连接（limits 已打开连接池）。
_CLIENTS: dict[tuple[int, str, str], tuple[httpx.AsyncClient, Any]] = {}


def _prune_dead_loops() -> None:
    """丢掉「事件循环已经关掉」的客户端。

    httpx 的连接池绑在创建它的 loop 上，跨 loop 复用会报 `Event loop is closed`。
    服务进程只有一个 loop，所以这条只在测试/CLI 反复 `asyncio.run()` 时才起作用
    ——不清理的话，旧 loop 留下的死客户端会被新 loop 选中。
    """
    for k, (_c, loop) in list(_CLIENTS.items()):
        try:
            dead = loop is None or loop.is_closed()
        except Exception:  # noqa: BLE001
            dead = True
        if dead:
            _CLIENTS.pop(k, None)


def _shared_client(proxy: str | None, user_agent: str | None) -> httpx.AsyncClient:
    """按 (当前事件循环, 代理, UA) 取长连接客户端；同 key 全程复用，不随单次请求关闭。"""
    loop = asyncio.get_running_loop()
    key = (id(loop), proxy or "", user_agent or "")
    hit = _CLIENTS.get(key)
    if hit is not None:
        c, lp = hit
        # id(loop) 会被回收复用：命中同 id 但其实是另一个 loop 对象时，旧客户端绑在已关闭
        # 的 loop 上，复用它只会拿到「Event loop is closed」或用错 transport（测试里反复
        # asyncio.run 时最容易踩）。必须核对是**同一个** loop 对象才复用。
        if lp is loop and not c.is_closed:
            return c
        _CLIENTS.pop(key, None)
    _prune_dead_loops()
    c = _new_llm_client(proxy, user_agent)
    _CLIENTS[key] = (c, loop)
    return c


async def aclose_shared_clients() -> None:
    """进程退出/测试收尾用：关掉并清空所有共享客户端。"""
    for c, _loop in list(_CLIENTS.values()):
        try:
            await c.aclose()
        except Exception:  # noqa: BLE001 - 关不掉也不影响退出
            pass
    _CLIENTS.clear()


_PROXY_TTL_S = 60.0
_proxy_cache: tuple[Any, str | None, float] | None = None


def _system_proxy_cached() -> str | None:
    """`_system_proxy()` 的 60s 缓存版。

    读系统代理要过 Windows Internet 设置（注册表），而它每个 LLM 往返都会被问一次，
    还要额外 `asyncio.to_thread` 开一条线程；代理配置几分钟才可能变一次，缓存足够。
    缓存按**函数对象**识别：测试或运行期把 `_system_proxy` 换掉时会自动失效。
    """
    global _proxy_cache
    now = time.monotonic()
    if (_proxy_cache is not None and _proxy_cache[0] is _system_proxy
            and now - _proxy_cache[2] < _PROXY_TTL_S):
        return _proxy_cache[1]
    val = _system_proxy()
    _proxy_cache = (_system_proxy, val, now)
    return val


_PROXY_COOLDOWN_S = 120.0
_dead_proxies: dict[str, float] = {}


def _poison_proxy(proxy: str | None) -> None:
    """记下「这条代理连不上」，120s 内不再优先尝试它。

    代理多半是本地代理/VPN 客户端设的，客户端一退端口就没了。以前每条 LLM 请求都要
    先撞一次死端口（ConnectError）、再降级直连，等于每轮白付一次失败往返。
    """
    if proxy:
        _dead_proxies[proxy] = time.monotonic() + _PROXY_COOLDOWN_S


def _proxy_is_dead(proxy: str | None) -> bool:
    if not proxy:
        return False
    exp = _dead_proxies.get(proxy)
    if exp is None:
        return False
    if time.monotonic() >= exp:
        _dead_proxies.pop(proxy, None)
        return False
    return True


# ------------------------------------------------------------ 会话级「就用直连」
# 一条会话（一次对话）里只要确认过「走代理不通 / 被 Cloudflare 人机验证拦住」并成功降级到
# 直连，这条会话后面所有往返就一律直连：不再每条请求先白撞一次代理。
# 上面的 _PROXY_COOLDOWN_S 只是 120s 的进程级冷却，一次长 run 会反复过期、每 2 分钟就再撞
# 一次代理（用户看到的就是「明明早就切直连了，怎么又坑一下」）。会话记忆按会话 id 记
# （runtime 注入到适配器实例的 session_key），进程内有效。
_SESSION_DIRECT_TTL_S = 12 * 3600.0
_direct_sessions: dict[str, float] = {}


def mark_session_direct(session_key: str | None) -> None:
    """记住「这条会话已经改用直连」：后续往返不再尝试代理。"""
    if not session_key:
        return
    now = time.monotonic()
    if len(_direct_sessions) > 512:  # 兜底：长期运行的进程别无限攒
        for k, exp in list(_direct_sessions.items()):
            if now >= exp:
                _direct_sessions.pop(k, None)
    _direct_sessions[str(session_key)] = now + _SESSION_DIRECT_TTL_S


def session_is_direct(session_key: str | None) -> bool:
    """这条会话是否已经改成直连（真值；过期即忘）。"""
    if not session_key:
        return False
    key = str(session_key)
    exp = _direct_sessions.get(key)
    if exp is None:
        return False
    if time.monotonic() >= exp:
        _direct_sessions.pop(key, None)
        return False
    return True


def clear_session_direct(session_key: str | None = None) -> None:
    """忘掉直连记忆（不传=全清）：换了网络、用户重开代理客户端后想再给代理一次机会时用。"""
    if session_key is None:
        _direct_sessions.clear()
    else:
        _direct_sessions.pop(str(session_key), None)


# ---------------------------------------------------------------- 出站并发闸门
_GATE_COOLDOWN_S = 10.0     # 429 之后多少秒内不再放开并发
_GATE_GROW_STREAK = 12      # 连续多少次成功才 +1


class _AdaptiveGate:
    """进程级 LLM 出站闸门：限制并发，并按 429 自适应收放。

    为什么要它：主agent 与子agent 共用同一把 key，同一时刻可能有十几个请求在飞；
    中转站的配额是按 key 算的，打满了只会换来 429 + 指数退避（实测 412 次 429 全部
    出现在长 run 里，26.8% 的调用被拖到 10s 以上，吃掉 64.6% 的总时长）。
    收放策略是 AIMD 的简化版：
    - 收到 429 → 并发立刻腰斩（下限 1），冷却 10s；
    - 连续成功 12 次 → +1，逐步吃回带宽（上限 = LLM_MAX_CONCURRENCY）。

    实现刻意不用 `asyncio.Condition`/`Semaphore`：`release()` 必须是**同步**的，
    否则任务被取消时 finally 里再 await 锁会二次抛 CancelledError，把占用数泄漏掉
    （泄漏几次并发就永久降到 1，表现为「越来越慢」）。这里用 Future 队列 + 同步收尾。
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit or 1))
        self._in_flight = 0
        self._waiters: list[asyncio.Future] = []
        self._ok_streak = 0
        self._grow_after = time.monotonic() + _GATE_COOLDOWN_S

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self) -> None:
        # while 而不是 if：被唤醒时上限可能已经被 429 降下来了，得重新检查
        while self._in_flight >= self._limit:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)
        self._in_flight += 1

    def release(self) -> None:
        """归还一个并发位（同步，取消上下文里也能安全调用）。"""
        self._in_flight = max(0, self._in_flight - 1)
        self._wake()

    def note_rate_limited(self) -> None:
        """被限流了：并发腰斩并冷却（再撞到 429 就继续腰斩，直到 1）。"""
        self._limit = max(1, self._limit // 2)
        self._ok_streak = 0
        self._grow_after = time.monotonic() + _GATE_COOLDOWN_S

    def note_ok(self) -> None:
        """一次成功往返：连续成功够多就慢慢把并发加回去（封顶 LLM_MAX_CONCURRENCY）。"""
        self._ok_streak += 1
        if (self._ok_streak >= _GATE_GROW_STREAK
                and self._limit < _gate_ceiling()
                and time.monotonic() >= self._grow_after):
            self._limit += 1
            self._ok_streak = 0

    def _wake(self) -> None:
        """把所有等待者叫醒（它们会各自重新检查上限，不用精确唤醒一个）。"""
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)


def _gate_ceiling() -> int:
    """并发上限（LLM_MAX_CONCURRENCY，0/负数 = 视为不限）。"""
    try:
        n = int(LLM_MAX_CONCURRENCY)
    except (TypeError, ValueError):
        n = 8
    return n if n > 0 else 64


_GATE: _AdaptiveGate | None = None


def _gate() -> _AdaptiveGate:
    """取进程级闸门（延迟创建：只在真正发请求时读配置）。"""
    global _GATE
    if _GATE is None:
        _GATE = _AdaptiveGate(_gate_ceiling())
    return _GATE


def _gated(fn):
    """把「占用一个出站并发位」包在单次网络往返外面（成功/异常/取消都会释放）。

    只包真正的 HTTP 往返，不包退避 sleep：退避等待时不该继续占着并发位，
    否则一次 429 会让所有 agent 一起排队干等。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        gate = _gate()
        await gate.acquire()
        try:
            return await fn(*args, **kwargs)
        finally:
            gate.release()
    return wrapper


@asynccontextmanager
async def _client_scope(proxy: str | None, user_agent: str | None):
    """在 `async with` 里用共享长连接客户端，退出时**不关闭**它。

    保留 `async with` 只是为了不动 `_request` 的缩进与异常结构；客户端实际活到进程结束
    （或代理/UA 切换时重建），于是每个往返都能命中已建好的 TCP+TLS 连接。
    """
    yield _shared_client(proxy, user_agent)


# 端点「请求太长」的说法各家不同，命中任何一条都按上下文超限处理（值得重压后重试一次）
_OVERFLOW_PAT = re.compile(
    r"context.{0,24}(length|window|limit)|too many tokens|maximum.{0,16}context"
    r"|exceed(s|ed)?.{0,24}(context|token)|input is too long|超出.{0,8}(上下文|长度|token)"
    r"|超过.{0,8}(上下文|长度|最大)", re.I)

# 端点以 400 拒绝 content parts（只收字符串）。命中后把 messages 压成纯文本重发：
# 这是格式协商，不是请求内容有错。常见措辞是 pydantic 校验：
#   'loc': (..., 'content', 'str'), 'msg': 'Input should be a valid string'
# 或网关转述的 ChatCompletionMessageGenericParam。用 DOTALL 兜住正文里的换行。
_PARTS_REJECT_PAT = re.compile(
    r"content.{0,200}(?:Input should be a valid string|string_type|str type expected"
    r"|must be (?:a )?string|should be (?:a )?string|expected (?:a )?string"
    r"|got (?:a )?(?:list|array))"
    r"|(?:Input should be a valid string|string_type|str type expected"
    r"|expected (?:a )?string).{0,200}content"
    r"|ChatCompletionMessage(?:Generic|User)Param.{0,400}content",
    re.I | re.S)


class _UnknownFieldRejected(RuntimeError):
    """端点以 400/422 拒绝某个**可选请求字段**（我们不认识它，它不认识我们）。

    典型：中转/推理后端没有 ``parallel_tool_calls``，某些自建网关不认
    ``reasoning_effort`` / ``stream_options``。这不是请求内容有错，去掉字段重发即可，
    所以单独一类、且不消费重试次数。
    """

    def __init__(self, msg: str, field: str) -> None:
        super().__init__(msg)
        self.field = field


# 端点可能不认识的可选字段：只在它自己 400 提到时才丢，不主动猜
_DROPPABLE_FIELDS = ("parallel_tool_calls", "reasoning_effort", "stream_options",
                     "tool_choice", "temperature")


def _classify_http_error(status: int, raw: str,
                         retry_after: str | None = None, hint: str = "") -> RuntimeError:
    """把非 200 响应分成「鉴权 / 上下文超限 / 可重试 / 其他参数错误」。

    ``hint``：拼在消息后面的一小段端点/模型信息，让 403 这类错误能直接看出
    「是哪个端点、哪个模型、哪把 key」——多档位/多供应商并存时这是最快的定位依据。
    """
    h = f"（{hint}）" if hint else ""
    if status in (401, 403):
        return LlmAuthError(
            f"LLM 端点拒绝访问（HTTP {status}）：请检查 API Key 与账户额度{h}。{raw[:200]}")
    if status in (413, 414):
        # 前置网关按字节数拒收（token 预算管不住它：一张图 900 token 却是几 MB）
        return LlmPayloadTooLarge(f"HTTP {status}: {raw[:200]}")
    if status == 400 and _OVERFLOW_PAT.search(raw):
        return LlmContextOverflow(f"HTTP 400: {raw[:300]}")
    if status in (400, 422) and _PARTS_REJECT_PAT.search(raw):
        return _ContentPartsUnsupported(f"HTTP {status}: {raw[:300]}")
    if status in (400, 422):
        low = raw.lower()
        for f in _DROPPABLE_FIELDS:
            # 只有端点明确点名这个字段（unknown/unexpected/extra/not permitted + 字段名），
            # 才认定「它不认这个字段」；否则还是当普通参数错误。
            if f in low and any(k in low for k in ("unknown", "unexpected", "extra",
                                                   "not permitted", "unrecognized",
                                                   "additional propert", "invalid")):
                return _UnknownFieldRejected(f"HTTP {status}: {raw[:300]}", f)
    if status in _RETRYABLE_STATUS:
        return _RetryableLLMError(f"HTTP {status}: {raw[:200]}", retry_after, status=status)
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
                 reasoning_effort: str = "",
                 max_tokens: int = 0) -> None:
        self.role = role
        self.model = model or LLM_MODEL
        self.api_key = api_key or OPENAI_API_KEY
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.context_window = context_window
        self.vision = vision
        # 推理强度（模型档位 params_json.reasoning_effort）：""=不传走端点默认；
        # none/minimal/low/medium/high 原样透传，端点不认该字段时会自己忽略或报错
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        # 单次回复输出上限：0 = 不传（走端点默认）。推理模型的思考 token 也计入此上限，
        # 设太小会截断思考/工具调用，所以默认 0，由档位或 GA_LLM_MAX_TOKENS 显式开启。
        self.max_tokens = max(0, int(max_tokens or 0))
        # 供应商可单独指定 UA（前置 WAF 按 UA 拦脚本），未指定则用全局 GA_LLM_USER_AGENT
        self.user_agent = user_agent or LLM_USER_AGENT
        # 本轮可见工具名：文本协议兜底解析时用来挡掉白名单外的调用
        self.tool_names: set[str] = set()
        self.media_budget = MEDIA_BYTES_BUDGET
        # 端点是否接受 content parts 数组。遇到「content 必须是字符串」的 400 后
        # 本轮 run 内永久降级为纯文本（图片用文字说明代替），避免每轮都撞一次。
        self._allow_content_parts = True
        # 端点明确表示「不认识」的可选字段（parallel_tool_calls / reasoning_effort …）：
        # 记下来，后续轮次不再带，省掉每轮一次 400。
        self._dropped_fields: set[str] = set()
        # 当前 httpx 客户端是否正在走代理（连接层失败时据此决定「换直连」还是「普通重试」）
        self._using_proxy = False
        # 本会话 id（runtime 注入）：用来记住「这条会话已经改成直连了」，见 mark_session_direct
        self.session_key = ""
        # 本会话已确认代理不可用（不通 / 被人机验证拦住）：后续往返一律直连
        self._direct_only = False

    # ---------- 消息转换 ----------
    def _content(self, role: str, m: dict) -> Any:
        """content 归一化：**只有** user + 视觉档位 + 端点接受 parts + 真带图才发数组。

        其余一律发纯字符串。OpenAI 文档允许 parts 的地方，部分兼容端点并不允许
        （SGLang 把 system/assistant/tool 的 content 定成 str，纯文本 parts 也 400；
        实测 2026-11-12 用户反馈的 ``[{'type': 'text'}]`` 校验错误即此）。
        """
        c = m.get("content")
        if not isinstance(c, list):
            return c if c is not None else ""
        has_image = any(
            isinstance(p, dict) and p.get("type") == "image_url"
            and str((p.get("image_url") or {}).get("url") or "").strip()
            for p in c)
        if role == "user" and self.vision and self._allow_content_parts and has_image:
            return c
        note = ""
        if has_image:
            note = NO_VISION_NOTE if not self.vision else PARTS_OFF_NOTE
        return _parts_to_text(c, image_note=note)

    def _system_part_text(self, m: dict) -> str:
        """system 消息的纯文本（把多条 system 合并成一条时用）。"""
        return _parts_to_text(m.get("content"))

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
                    "content": _parts_to_text(m.get("content")),
                })
            elif role == "assistant" and m.get("tool_calls"):
                oa_msgs.append({
                    "role": "assistant",
                    "content": _parts_to_text(m.get("content")),
                    "tool_calls": [
                        {
                            "id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                        }
                        for tc in m["tool_calls"]
                    ],
                })
            else:
                oa_msgs.append({"role": role, "content": self._content(role, m)})
        if sys_parts:
            oa_msgs.insert(0, {"role": "system", "content": "\n\n".join(sys_parts)})
        oa_tools = [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters,
            }}
            for t in tools
        ] if tools else []
        return {"messages": oa_msgs, "tools": oa_tools}, oa_tools

    def _flatten_oai_messages(self, msgs: list[dict]) -> list[dict]:
        """已转好的 OpenAI messages → 所有 content 都压成字符串（一次性兼容降级）。"""
        out = []
        for m in msgs:
            c = m.get("content")
            if isinstance(c, list):
                m = {**m, "content": _parts_to_text(c, image_note=PARTS_OFF_NOTE)}
            out.append(m)
        return out

    @_gated
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
        if _is_waf_response(resp.status_code, resp.headers, resp.text):
            raise _WafBlocked(f"被 Cloudflare 人机验证拦下（HTTP {resp.status_code}，"
                              f"不是 Key 问题）；入口={self.base_url}")
        raise _classify_http_error(resp.status_code, resp.text,
                                   resp.headers.get("retry-after"),
                                   hint=f"端点={self.base_url} 模型={self.model}")

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
        compressed = await compress_messages(messages, self.context_window,
                                             summarizer, report, tools=tools)
        # 压缩/媒体裁剪结果原地写回调用方的历史：runtime 的 _compact 通常已经压过并写回，
        # 这里再压只发生在两边预算口径不一致或直连调用时——写回让后续轮次从压缩后的
        # 历史继续，而不是每轮从全量重压一遍。
        messages[:] = compressed
        if report.get("compressed"):
            self._notify_compressed(ctx, report, "pre_send")
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
        # 推理强度：本轮 override 优先（runtime 长 run 后段降档用），否则用档位配置。
        eff = (str(getattr(ctx, "reasoning_effort_override", "") or "").strip().lower()
               or self.reasoning_effort)
        if eff and eff != "default":
            body["reasoning_effort"] = eff
        # 输出上限：仅显式配置时下发（0/未设 = 走端点默认，避免截断推理模型的思考）
        if self.max_tokens > 0:
            body["max_tokens"] = self.max_tokens
        if payload["tools"]:
            body["tools"] = payload["tools"]
            body["tool_choice"] = "auto"
            # 显式声明「一轮可以给多个调用」：部分中转/推理后端默认只返还一个，
            # 模型明明能一次发 3 个互不依赖的 grep，也会被拆成 3 轮，每轮都付一次
            # 完整往返（实测历史平均只有 1.5 个工具/轮，是 run 时长最大的单一来源）。
            # 端点不认这个字段时（400 提到 parallel_tool_calls），_request 内部会
            # 去掉它重发一次，不消费重试次数。
            if LLM_PARALLEL_TOOL_CALLS:
                body["parallel_tool_calls"] = True
        # 端点之前明确表示不认识的字段，本轮起不再带（省掉每轮一次 400）
        for f in self._dropped_fields & set(body):
            body.pop(f, None)
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
            retry_report: dict[str, Any] = {"source": "endpoint_overflow",
                                            "overflow_error": str(e)[:300]}
            compressed = await compress_messages(
                messages, self.context_window, summarizer, retry_report,
                tools=tools, budget_ratio=BUDGET_RATIO * 0.5)
            # 原地写回：端点的实际上限比配置窗口小，不写回的话每轮都会撞一次 400、
            # 每轮都重压一遍（用户看到的就是反复「上下文压缩处理中」）。
            messages[:] = compressed
            report.update({k: v for k, v in retry_report.items() if k != "est_before"})
            self._notify_compressed(ctx, retry_report, "endpoint_overflow")
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
        # 记录**实际发出去**的 messages：端点拒绝 parts 时，降级发生在 _request 内部，
        # 这里若还记 payload 里的旧数组，排查时就会看到一份从未真正发出去的请求。
        await self._report(ctx, body["messages"], oa_tools, resp, report)
        return resp

    def _notify_compressed(self, ctx: Ctx, report: dict, source: str) -> None:
        """适配层压缩了历史时通知 runtime（同步钩子，失败不影响主流程）。

        端点拒收（endpoint_overflow）触发的压缩以前完全静默：用户只看到
        「上下文压缩处理中」，不知道是端点的实际上限比配置的窗口小。
        """
        hook = getattr(ctx, "on_llm_compressed", None)
        if hook is None:
            return
        try:
            hook(dict(report, source=source), source)
        except Exception:  # noqa: BLE001 - 通知失败不影响请求
            log.exception("on_llm_compressed hook failed")

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
        proxy: str | None = None
        if self._direct_only or session_is_direct(self.session_key):
            # 会话级记忆：这条会话早前已经从代理降级成直连（代理不通 / 被 Cloudflare 拦下），
            # 后面所有往返一律直连——不再每条请求先撞一次代理。撞一次 = 一次连接失败或一页
            # 人机验证，白白多等一个往返，日志里还全是无关的 WARNING。
            if not self._direct_only:
                self._direct_only = True
                log.info("本会话已改用直连：后续 LLM 请求不再尝试代理（%s）", self.base_url)
        elif not _proxy_env_disabled():
            proxy = await asyncio.to_thread(_system_proxy_cached)
            if proxy and _proxy_is_dead(proxy):
                # 这条代理最近刚连不上（本地代理/VPN 客户端退了）：冷却期内别再撞，
                # 直接直连。否则每条 LLM 往返都要先白付一次失败的连接尝试。
                log.info("系统代理 %s 最近连接失败，本轮直接直连", proxy)
                proxy = None
        self._using_proxy = bool(proxy)
        gate = _gate()
        async with _client_scope(proxy, self.user_agent) as client:
            downgraded = False
            attempt = 0
            while attempt < max_attempts:
                attempt += 1
                retry_after: str | None = None
                try:
                    if use_stream:
                        try:
                            resp = await asyncio.wait_for(
                                self._chat_stream(client, body, headers, on_delta, t0),
                                timeout=call_timeout)
                        except (LlmAuthError, LlmContextOverflow, LlmPayloadTooLarge,
                                _ContentPartsUnsupported, _UnknownFieldRejected):
                            raise
                        except _ProxyRefused:
                            # 代理连不上要交给外层「换直连」，别当成「流式不支持」降级
                            raise
                        except _WafBlocked:
                            # 人机验证页同上：交给外层换直连/退避，别降级成非流式
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
                    # 流式失败时上面只把 use_stream 置 False（不是 return），所以这里用
                    # `if not` 而不是 `else`：降级后的非流式请求必须真的发出去。
                    if not use_stream:
                        http_resp = await asyncio.wait_for(
                            self._post_once(client, body, headers), timeout=call_timeout)
                        resp = self._parse_completion(http_resp.json(), t0)
                    # 走到这里说明这一往返成功了：让并发闸门慢慢把带宽加回去
                    gate.note_ok()
                    return resp
                except LlmAuthError:
                    raise
                except _ContentPartsUnsupported as e:
                    # 格式协商，不是请求内容有错：压成纯文本重发，且**不消费**重试次数。
                    # 之后的轮次 _to_oai 也会走纯文本，避免每轮先撞一次 400。
                    if self._allow_content_parts:
                        self._allow_content_parts = False
                        attempt -= 1
                        last_err = "端点不接受多段 content（parts），已改为纯文本发送"
                        log.warning("端点拒绝 content parts，降级为纯文本后重试: %s", e)
                        body["messages"] = self._flatten_oai_messages(
                            body.get("messages") or [])
                        if on_retry:
                            try:
                                on_retry(attempt + 1, max_attempts, 0.0, last_err)
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    last_err = str(e)
                except asyncio.TimeoutError:
                    last_err = f"单次调用超过 {call_timeout}s 硬上限"
                except _ProxyRefused as e:
                    # 走代理连不上：当次立刻切直连，且**不消费**重试次数（真正的问题还没试到）
                    if proxy and not downgraded:
                        downgraded = True
                        attempt -= 1
                        last_err = f"代理不通（{proxy}：{e}），已自动改直连重试"
                        log.warning("经代理连接失败，自动降级直连: %s", e)
                        # 这条代理短期内别再用了（每条请求都先撞一次失败太亏），
                        # 换共享的直连客户端（同样复用长连接，不关闭旧的）
                        _poison_proxy(proxy)
                        client = _shared_client(None, self.user_agent)
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
                    # 走代理被拦：多半是代理出口 IP 被人机验证盯上，换直连通常就 200（不消费重试次数）
                    if proxy and not downgraded:
                        downgraded = True
                        attempt -= 1
                        last_err = f"经代理被 Cloudflare 人机验证拦截（{proxy}），已自动改直连重试"
                        log.warning("经代理被 WAF 拦截，自动降级直连: %s", e)
                        _poison_proxy(proxy)
                        client = _shared_client(None, self.user_agent)
                        self._using_proxy = False
                        # 被 Cloudflare 拦下不是偶发抖动，本会话别再拿代理试了
                        self._direct_only = True
                        mark_session_direct(self.session_key)
                        if on_retry:
                            try:
                                on_retry(attempt, max_attempts, 0.0, last_err)
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    last_err = str(e)  # 直连仍被拦：按可重试错误退避（下一轮再撞运气）
                except _UnknownFieldRejected as e:
                    # 端点不认某个可选字段（如 parallel_tool_calls / reasoning_effort）：
                    # 去掉该字段重发，不消费重试次数；记进实例黑名单，后续轮次不再带。
                    if e.field in body:
                        body.pop(e.field, None)
                        self._dropped_fields.add(e.field)
                        attempt -= 1
                        last_err = f"端点不认识请求字段 {e.field}，已去掉后重发"
                        log.warning("%s（端点=%s）：%s", last_err, self.base_url, e)
                        continue
                    last_err = str(e)
                except _RetryableLLMError as e:
                    last_err = str(e)
                    retry_after = e.retry_after
                    if getattr(e, "status", None) == 429:
                        # 限流：收窄并发（AIMD），否则只会换来更多 429 + 指数退避
                        gate.note_rate_limited()
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
        # 少数端点（多模态/中转网管）把 content 返回成 parts 数组；原样塞进历史会在
        # 下一轮被当成 assistant 的多段 content 发回去，严格端点直接 400。这里统一取文本。
        text = _parts_to_text(choice.get("content"))
        # 思考过程：OpenAI o 系列 reasoning_content；部分兼容网关用 reasoning
        reasoning = _parts_to_text(
            choice.get("reasoning_content") or choice.get("reasoning"))
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

    @_gated
    async def _chat_stream(self, client: httpx.AsyncClient, body: dict, headers: dict,
                           on_delta, t0: float) -> LLMResponse:
        """SSE 流式：逐段回调 on_delta(reasoning_delta, text_delta)，返回聚合结果。

        注意 `@_gated`：整条流（从建连到最后一个 chunk）都占一个出站并发位——
        流没读完就等于这条连接还在被端点服务，不能提前放行别的请求去抢配额。
        """
        stream_body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        text_parts: list[str] = []
        reason_parts: list[str] = []
        tcs: dict[int, dict] = {}
        usage: dict = {}
        ttft_ms = 0  # 首 token 耗时（见 LLMResponse.ttft_ms）
        async with client.stream("POST", f"{self.base_url}/chat/completions",
                                 json=stream_body, headers=headers) as resp:
            if resp.status_code in (401, 403, 429, 503):
                # 与非流式 _post_once 保持一致：把服务端返回的真实原因带出来，
                # 否则排查时只能看到光秃秃的 "HTTP 403"，定位不到 INVALID_API_KEY / 额度等根因。
                raw = (await resp.aread())[:2000].decode("utf-8", "ignore")
                if _is_waf_response(resp.status_code, resp.headers, raw):
                    raise _WafBlocked(f"被 Cloudflare 人机验证拦下（HTTP {resp.status_code}，"
                                      f"不是 Key 问题）；入口={self.base_url}")
                raise _classify_http_error(
                    resp.status_code, raw, resp.headers.get("retry-after"),
                    hint=f"端点={self.base_url} 模型={self.model}")
            if resp.status_code != 200:
                # 2000 而不是 200：pydantic 校验详情（content 类型错误）常常在 200 字符之后，
                # 截太短会让「content parts 不兼容」识别不到，只能当普通 400 失败。
                raw = (await resp.aread())[:2000].decode("utf-8", "ignore")
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
                        rd = _parts_to_text(
                            delta.get("reasoning_content") or delta.get("reasoning"))
                        td = _parts_to_text(delta.get("content"))
                        if rd:
                            reason_parts.append(rd)
                        if td:
                            text_parts.append(td)
                        if rd or td:
                            # 首字耗时：第一个内容增量到达的时刻（思考增量也算——
                            # 用户看到「正在思考…」已经在响应了）。只记第一次。
                            if not ttft_ms:
                                ttft_ms = max(1, int((time.monotonic() - t0) * 1000))
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
                           tool_calls=parsed_tcs, usage=usage_out, latency_ms=latency_ms,
                           ttft_ms=ttft_ms)
