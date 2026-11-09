"""AI 联网工具：web_search（搜索）+ web_fetch（抓网页正文）。

Provider（环境变量 GA_SEARCH_PROVIDER）：
- bing（默认，无 key，国内可达）：抓取 bing 的 RSS 结果流解析
- ddg：html.duckduckgo.com HTML 版（无 key，部分网络不可达）
- tavily：Tavily API（需 GA_TAVILY_KEY，质量最好）

仅只读查询，analyze 模式同样可用。
"""
from __future__ import annotations

import html as _html
import re

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.config import LLM_USER_AGENT, SEARCH_PROVIDER, TAVILY_API_KEY, WEB_SEARCH_TIMEOUT_S

_BROWSER_UA = LLM_USER_AGENT or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_WS_RE = re.compile(r"[ \t\r\f\v]+")

# 结果末尾附一段结构化来源清单（前端据此渲染可点击的「来源」芯片，模型据此写引用）。
# 用固定 ASCII 标记，避免中文编码/换行在不同端上错位；每条 `序号. 标题 | URL`。
_SOURCES_MARK = "[Sources]"


def _format_sources(items: list[dict]) -> str:
    """把搜索结果压成一段可解析的来源清单；无有效 URL 时返回空串。"""
    seen: set[str] = set()
    out: list[str] = [_SOURCES_MARK]
    for it in items:
        url = str(it.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        title = _html.unescape(str(it.get("title") or "").strip())
        title = _WS_RE.sub(" ", title).replace("|", "｜").replace("\n", " ")[:120]
        out.append(f"{len(seen)}. {title or url} | {url}")
    return "\n".join(out) if len(out) > 1 else ""


def _strip_tags(fragment: str) -> str:
    text = _TAG_RE.sub("", fragment)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _clean_page(raw: str) -> str:
    """HTML → 纯文本：去脚本/样式/注释/标签，压空白。"""
    text = _COMMENT_RE.sub("", raw)
    text = _SCRIPT_RE.sub("", text)
    # 块级标签换成换行，保留基本可读结构
    text = re.sub(r"</?(p|div|br|li|tr|h[1-6]|section|article|blockquote|pre)[^>]*>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


async def _bing_search(query: str, n: int) -> list[dict]:
    import httpx
    from urllib.parse import quote
    last_err = ""
    for host in ("www.bing.com", "cn.bing.com"):
        try:
            async with httpx.AsyncClient(timeout=WEB_SEARCH_TIMEOUT_S, follow_redirects=True,
                                         headers=_HEADERS) as client:
                # 用 RSS 输出而非 HTML 结果页：无 cookie/JS 的会话里 HTML 版会退化成
                # 只匹配第一个词甚至推热门（实测 godot 查询返回过 Burberry），RSS 相关性好。
                # 别加 mkt/setlang：加了反而退化成热门流（实测）
                r = await client.get(f"https://{host}/search?q={quote(query)}"
                                     f"&format=rss&count={n + 3}")
            if r.status_code != 200:
                last_err = f"{host} HTTP {r.status_code}"
                continue
            results = _parse_bing_rss(r.text, n)
            if results:
                return results
            last_err = f"{host} RSS 无结果"
        except Exception as e:  # noqa: BLE001
            last_err = f"{host} {type(e).__name__}: {e}"
    raise ToolError(f"Bing 搜索失败（{last_err}）；可在 .env 配 GA_SEARCH_PROVIDER=tavily + GA_TAVILY_KEY 换用 Tavily")


def _parse_bing_rss(page: str, n: int) -> list[dict]:
    out: list[dict] = []
    for block in re.findall(r"<item>(.*?)</item>", page, re.S)[:n]:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
        d = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", block, re.S)
        if not (t and l):
            continue
        out.append({"title": _strip_tags(t.group(1)), "url": _strip_tags(l.group(1)),
                    "snippet": _strip_tags(d.group(1))[:300] if d else ""})
    return out


async def _ddg_search(query: str, n: int) -> list[dict]:
    import httpx
    from urllib.parse import quote
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    async with httpx.AsyncClient(timeout=WEB_SEARCH_TIMEOUT_S, follow_redirects=True,
                                 headers=_HEADERS) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise ToolError(f"DuckDuckGo HTTP {r.status_code}")
    results: list[dict] = []
    for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        href = _html.unescape(m.group(1))
        # ddg 的跳转链接 //duckduckgo.com/l/?uddg=<encoded>
        um = re.search(r"[?&]uddg=([^&]+)", href)
        if um:
            from urllib.parse import unquote
            href = unquote(um.group(1))
        results.append({"title": _strip_tags(m.group(2)), "url": href, "snippet": ""})
        if len(results) >= n:
            break
    if not results:
        raise ToolError("DuckDuckGo 未解析到结果（可能被风控）")
    snips = re.findall(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', r.text, re.S)
    for i, s in enumerate(snips[:len(results)]):
        results[i]["snippet"] = _strip_tags(s)[:300]
    return results


async def _tavily_search(query: str, n: int) -> list[dict]:
    import httpx
    if not TAVILY_API_KEY:
        raise ToolError("未配置 Tavily：.env 里设 GA_TAVILY_KEY=tvly-xxx（或改 GA_SEARCH_PROVIDER=bing）")
    async with httpx.AsyncClient(timeout=WEB_SEARCH_TIMEOUT_S + 15) as client:
        r = await client.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY, "query": query,
            "max_results": n, "search_depth": "basic", "include_answer": False})
    if r.status_code != 200:
        raise ToolError(f"Tavily HTTP {r.status_code}：{r.text[:200]}")
    data = r.json()
    return [{"title": it.get("title") or "", "url": it.get("url") or "",
             "snippet": _strip_tags(str(it.get("content") or ""))[:300]}
            for it in (data.get("results") or [])[:n]]


async def web_search(args: dict, t: ToolContext) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("缺少 query")
    try:
        n = max(1, min(int(args.get("max_results", 6) or 6), 10))
    except (TypeError, ValueError):
        n = 6
    provider = (SEARCH_PROVIDER or "bing").lower()
    if provider == "tavily" and TAVILY_API_KEY:
        results = await _tavily_search(query, n)
    elif provider == "ddg":
        results = await _ddg_search(query, n)
    else:
        results = await _bing_search(query, n)
    lines = [f"「{query}」搜索结果（{provider}，{len(results)} 条，用 web_fetch(url) 读全文）：", ""]
    for i, it in enumerate(results, 1):
        lines.append(f"{i}. {it['title'] or '（无标题）'}")
        lines.append(f"   {it['url']}")
        if it.get("snippet"):
            lines.append(f"   {it['snippet']}")
    # 末尾结构化来源清单：前端渲染成可点击来源，模型答复时可直接按序号引用。
    src = _format_sources(results)
    if src:
        lines.append("")
        lines.append(src)
    lines.append("")
    lines.append("（回答里引用这些资料时，用 [标题](URL) 或 [序号] 注明出处，不要整段照抄。）")
    return "\n".join(lines)


async def web_fetch(args: dict, t: ToolContext) -> str:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("url 必须以 http(s):// 开头")
    try:
        max_chars = max(500, min(int(args.get("max_chars", 6000) or 6000), 20000))
    except (TypeError, ValueError):
        max_chars = 6000
    import httpx
    try:
        async with httpx.AsyncClient(timeout=WEB_SEARCH_TIMEOUT_S + 10, follow_redirects=True,
                                     headers=_HEADERS) as client:
            r = await client.get(url)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"抓取失败：{type(e).__name__}: {e}")
    if r.status_code != 200:
        raise ToolError(f"HTTP {r.status_code}（{url[:120]}）")
    ctype = (r.headers.get("content-type") or "").lower()
    final_url = str(r.url)
    if "json" in ctype:
        body = r.text[:max_chars]
    elif ctype.startswith("image/") or "octet-stream" in ctype or "pdf" in ctype:
        return f"[二进制内容 {ctype or 'unknown'}，{len(r.content)} 字节，无法转文本] {final_url}"
    else:
        body = _clean_page(r.text)
        if not body:
            return f"[页面无可提取正文（可能是纯 JS 渲染）] {final_url}"
    head = f"[{final_url}]\n\n" if final_url.rstrip("/") != url.rstrip("/") else ""
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n...[截断，共 {len(body)} 字符；可用 max_chars 参数调大（上限 20000）]"
    # 单页也附一行来源，便于模型/前端把这页当出处引用（抓取失败/二进制不走这里）
    src = f"\n\n{_SOURCES_MARK}\n1. {url} | {final_url}"
    return head + body + src
