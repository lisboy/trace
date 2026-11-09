"""上下文窗口管理（设计 §7.1 增强）：token 估算 + 全局摘要压缩。

不同模型窗口不同（ModelProfile.params_json.context_window），发送前统一压到预算内：
- 系统提示词全保留；较早的历史**合并成一条要点摘要**，而不是整段丢掉；
- 最近的若干轮保留原文（模型要看得见「我刚才做了什么」）；
- tool 消息与其所属 assistant(tool_calls) 绑定为组，绝不拆散；
- 工具定义每轮都随请求发出去，必须先扣掉它占的 token，再谈历史预算。
token 估算用启发式（CJK≈1 token/字，其余≈4 字符/token），不依赖 tiktoken。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("llm.context_window")

# 单张图片按 OpenAI low detail 估 ~900 token（不解析 base64 长度）
IMAGE_TOKENS = 900
# 未配置窗口时的默认假设：当前主流模型基本都在 128k 起，按 32k 猜会让长 run 白白
# 丢掉几十轮历史（实测：策划角色每轮都被顶到窗口外，压缩一直在做无用功）。
# 真实窗口更小怎么办？超限时端点回 400，适配层会按半预算重压一次再发（openai_compat）。
DEFAULT_CONTEXT_WINDOW = 128_000
# 发送预算 = 窗口 × 该比例（给回复留空间）
BUDGET_RATIO = 0.75
# 压缩的「装回目标」= 预算 × 该比例（滞回）。触发在预算线、压到预算的 8 成：
# 否则压完的占用仍贴着触发线（历史一直在长，est 永远停在预算附近），
# 用户看到的占用%压缩前后几乎不动，像「压缩没生效」（2026-10-29 实测反馈）。
# 压到更低还有个副产品：距离下一次触发更远，增量摘要的调用也更稀疏。
COMPRESS_TARGET_RATIO = 0.80

# 摘要消息的开头标记：runtime、统计页、下一次压缩都靠它认出「这是摘要」
DIGEST_MARK = "[上下文压缩]"
# 保留原文的最近历史最多占可用预算的比例，其余留给摘要与系统提示
TAIL_RATIO = 0.55
# 摘要目标长度 = 被合并内容 × 该系数（再夹在下面两个上下限之间）——
# 压 8k tokens 和压 40k tokens 的历史，摘要不该一样长，所以不是固定字数
SUMMARY_RATE = 0.18
SUMMARY_MIN_TOKENS = 250
SUMMARY_MAX_RATIO = 0.30
# 序列化给摘要模型看时，单条消息的字符上限与「单次摘要调用」的输入上限。
# 以前只有后一个上限、且超出部分直接丢弃：实测 52,778 tokens 的历史只有 29,145 字符
# （12,822 tokens）进了摘要器，其余 76% 只留下一句「因长度上限省略」——最早的目标、
# 决策、路径全丢。现在按上限分块、旧→新逐块并入上一版摘要（map-reduce），覆盖率 100%。
SERIAL_CHARS_PER_MSG = 900
SERIAL_CHARS_CHUNK = 22_000
# 一次压缩最多调几次摘要模型（再多就把最早的整块丢掉并在摘要里注明，避免压一次卡 5 分钟）
SUMMARY_MAX_CHUNKS = 4
# 摘要骨架：模型必须按这五段写。缺 3 段以上就判定「答成了任务」，重问一次。
DIGEST_SECTIONS = ("目标", "已完成", "关键事实", "未完成", "坑与禁令")
# 骨架合格的最短长度：按骨架写出来的《很紧凑但信息完整》的摘要可以比 250 短
#（实测 113 tokens 的骨架摘要比 5,271 tokens 的要点堆更可用），但太短就不像话了。
DIGEST_MIN_TOKENS = 100
# 被合并内容太少就别调模型（截断标注足够，省一次往返）
SUMMARY_MIN_SOURCE_TOKENS = 800

MAX_IMAGES_PER_REQUEST = 9
# token 预算无法限制 base64 的字节数，图片另留体积上限。
MEDIA_BYTES_BUDGET = 500_000
MAX_SHEET_DATA_URL_CHARS = 300_000
VIEWBACK_MARK = "【视觉回灌】"
MEDIA_DROPPED_NOTE = ("[图片像素已因历史过期或请求体积限制省略。需要再看请重新调用 "
                      "view_image（单张）或 view_images（多张合并成一张）打开对应文件]")


def encode_vision_image(image: Any, max_edge: int = 1400,
                        max_chars: int = MAX_SHEET_DATA_URL_CHARS) -> str:
    import base64
    import io

    from PIL import Image

    rgba = image.convert("RGBA")
    rgb = Image.new("RGB", rgba.size, "white")
    rgb.paste(rgba, mask=rgba.getchannel("A"))
    rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    while True:
        for quality in (82, 72, 62, 52):
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            if len(url) <= max_chars:
                return url
        rgb.thumbnail((max(1, rgb.width * 3 // 4), max(1, rgb.height * 3 // 4)),
                      Image.Resampling.LANCZOS)


def _image_url(part: dict) -> str:
    return str((part.get("image_url") or {}).get("url") or "")


def media_stats(messages: list[dict]) -> dict:
    """当前请求里的图片张数与 dataURL 合计字节（字节才是 413 的口径）。"""
    n = b = 0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for part in c:
            if isinstance(part, dict) and part.get("type") == "image_url":
                n += 1
                b += len(_image_url(part))
    return {"images": n, "bytes": b}


def _drop_image_parts(m: dict, idxs: set[int]) -> dict:
    """把指定位置的 image part 摘掉，并在末尾补一句「为什么看不见了」。"""
    c = m.get("content")
    parts = [p for j, p in enumerate(c or []) if not (j in idxs and isinstance(p, dict)
             and p.get("type") == "image_url")]
    if not any(isinstance(p, dict) and MEDIA_DROPPED_NOTE in str(p.get("text") or "")
               for p in parts):
        parts = parts + [{"type": "text", "text": MEDIA_DROPPED_NOTE}]
    out = {k: v for k, v in m.items() if k != "content"}
    out["content"] = parts
    return out


def _is_viewback(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(isinstance(p, dict)
                                       and str(p.get("text") or "").startswith(VIEWBACK_MARK)
                                       for p in c)


def expire_viewback(messages: list[dict]) -> tuple[list[dict], int]:
    """模型回复后移除已看的像素，但**最新一张回灌图一直保留**。

    以前连最新那条也会在下一轮过期：模型看到图的那一轮把判断只能写进 reasoning，
    下一轮图没了、判断也没了，只能重新调用 view_image 再看一遍。实测 kpgame 立绘任务
    （2026-10-29）：51 次看图调用 / 178 次文件级查看，6 张图每张看 8–9 遍，光看图就吃掉
    11.5 分钟。留一张的成本是每轮多 ~200KB base64 与 900 估 token（请求前缀命中缓存，
    实测 96%），换来的是「同一批图不必重新打开」——字节上限仍由 fit_media 兜底。
    出现更新的回灌图时，旧的照常过期。
    """
    hits = [i for i, m in enumerate(messages) if _is_viewback(m)]
    if not hits:
        return messages, 0
    expired = hits[:-1]
    if not expired:
        return messages, 0
    out = list(messages)
    dropped = 0
    for i in expired:
        idxs = {j for j, p in enumerate(out[i]["content"])
                if isinstance(p, dict) and p.get("type") == "image_url"}
        if not idxs:
            continue
        out[i] = _drop_image_parts(out[i], idxs)
        dropped += len(idxs)
    return out, dropped


def fit_media(messages: list[dict], max_bytes: int = MEDIA_BYTES_BUDGET,
              max_images: int = MAX_IMAGES_PER_REQUEST) -> tuple[list[dict], int]:
    """按真实字节把图片压进预算：从最新往前保留，装不下的换成文字说明。

    单张就超预算的老图（历史消息里存的 4MB PNG）会被整张摘掉 —— 端点收不下它，
    留着只会让整轮白跑；摘掉后正文里仍有路径，模型想看可以自己再打开。
    """
    spots: list[tuple[int, int, int]] = []  # (消息下标, part 下标, 字节) 新→旧
    for i in range(len(messages) - 1, -1, -1):
        c = messages[i].get("content")
        if not isinstance(c, list):
            continue
        for j, part in enumerate(c):
            if isinstance(part, dict) and part.get("type") == "image_url":
                spots.append((i, j, len(_image_url(part))))
    if not spots or (len(spots) <= max_images
                     and sum(b for _, _, b in spots) <= max_bytes):
        return messages, 0
    keep: set[tuple[int, int]] = set()
    used = 0
    for i, j, b in spots:
        if len(keep) >= max_images or used + b > max_bytes:
            continue
        keep.add((i, j))
        used += b
    by_msg: dict[int, set[int]] = {}
    dropped = 0
    for i, j, _b in spots:
        if (i, j) in keep:
            continue
        by_msg.setdefault(i, set()).add(j)
        dropped += 1
    out = list(messages)
    for i, idxs in by_msg.items():
        out[i] = _drop_image_parts(out[i], idxs)
    return out, dropped


def apply_media_budget(messages: list[dict]) -> tuple[list[dict], dict]:
    """两根闸一起过：先让回灌图过期，再按字节预算裁剪剩下的。"""
    msgs, expired = expire_viewback(messages)
    msgs, dropped = fit_media(msgs)
    return msgs, {"media_expired": expired, "media_dropped": dropped,
                  **media_stats(msgs)}



def resolve_window(params: dict | None) -> tuple[int, bool]:
    """档位 params_json.context_window → (窗口 tokens, 是否显式配置)。

    没配时回退 DEFAULT_CONTEXT_WINDOW（128k）：这是猜的，所以调用方需要知道这个
    值是不是配置里的——猜大了端点会 400（适配层会重压重试），猜小了白丢历史。
    """
    raw = (params or {}).get("context_window")
    try:
        window = int(str(raw).strip().replace("_", "")) if raw else 0
    except (TypeError, ValueError):
        window = 0
    return (window, True) if window > 0 else (DEFAULT_CONTEXT_WINDOW, False)


def budget_for(window: int, ratio: float = BUDGET_RATIO) -> int:
    """该窗口的发送预算（超过就压缩），与 compress_messages 用的是同一个公式。"""
    return int((window or DEFAULT_CONTEXT_WINDOW) * ratio)


def estimate_tokens(content: Any) -> int:
    """估算一条消息内容的 token 数。content 可为 str 或 OpenAI content parts 列表。"""
    if content is None:
        return 0
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "image_url":
                    total += IMAGE_TOKENS
                else:
                    total += estimate_tokens(part.get("text", ""))
        return total
    text = str(content)
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def message_tokens(m: dict) -> int:
    """一条消息的估算 token。必须把 assistant 的 tool_calls 入参算进去：
    write_file/edit_file 的 arguments 里是整个文件正文，是真实 prompt 的大头，
    只统计 content 会低估一个数量级 → 该压缩时不压缩，直接把超长请求发给端点。"""
    total = 4 + estimate_tokens(m.get("content"))
    for tc in m.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        total += 8 + estimate_tokens(fn.get("name"))
        args = fn.get("arguments")
        total += estimate_tokens(args if isinstance(args, str)
                                 else _dumps_args(args))
    return total


def tools_tokens(tools: Any) -> int:
    """工具定义（ToolSpec 列表或已转好的 OpenAI dict）的估算 token。

    这份开销每轮都原样发一遍，却不在 messages 里——不扣它就会「压完还是超长」：
    实测本项目策划角色 12 个工具 ≈3.1k tokens，正好是把请求顶出窗口的差额。
    """
    if not tools:
        return 0
    first = tools[0] if isinstance(tools, list) else None
    if isinstance(first, dict):
        return estimate_tokens(_dumps_args(tools))
    # ToolSpec（dataclass / 对象）：按 OpenAI 的序列化形状估
    raw = [{"type": "function", "function": {
        "name": getattr(t, "name", ""), "description": getattr(t, "description", ""),
        "parameters": getattr(t, "parameters", {})}} for t in tools]
    return estimate_tokens(_dumps_args(raw))


def truncate_tokens(text: str, limit: int) -> str:
    """按估算 token 截断（保头保尾，中间标注省略了多少）。"""
    if limit <= 0 or estimate_tokens(text) <= limit:
        return text
    est = estimate_tokens(text)
    # 估算→字符的换算按当前文本的密度反推，中文和英文都能大致对上
    keep = max(1, int(len(text) * limit / est))
    if keep < 40:
        return text[:keep] + "…"
    head = text[: keep * 3 // 4]
    tail = text[-(keep // 4):]
    return f"{head}\n…（中间省略 {len(text) - len(head) - len(tail)} 字）…\n{tail}"


def _dumps_args(args: object) -> str:
    import json
    try:
        return json.dumps(args, ensure_ascii=False) if args else ""
    except (TypeError, ValueError):
        return str(args)


def _compact_digest(m: dict, limit: int = 120) -> str:
    """一条消息的一行概览（给摘要模型看，也给用户要点清单用）。

    工具调用的**入参**要带上：本平台的动作全在参数里（写哪个文件、跑什么命令、
    派了哪张图），只记工具名等于把「改了什么」压没了。
    """
    c = m.get("content")
    if isinstance(c, list):
        texts = [p.get("text", "") for p in c if isinstance(p, dict)]
        imgs = sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
        c = " ".join(t for t in texts if t) + (f" [图片×{imgs}]" if imgs else "")
    c = str(c or "").replace("\n", " ")
    calls = []
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "?")
        args = truncate_tokens(_dumps_args(fn.get("arguments")), max(60, limit // 3))
        calls.append(f"{name}({args})" if args != "{}" else name)
    if calls:
        c = f"{c} [调用 {'；'.join(calls)}]".strip()
    return c[:limit] + ("…" if len(c) > limit else "")


def summary_lines(msgs: list[dict]) -> list[str]:
    """逐条一行概览（分块前先建好，避免按块重新序列化导致边界漂移）。"""
    return [f"({m.get('role')}) {_compact_digest(m, SERIAL_CHARS_PER_MSG)}"
            for m in msgs]


def _split_chunks(lines: list[str], limit: int) -> list[str]:
    """把逐条概览按字符上限切成块（不拆行，尽量均匀）。"""
    chunks: list[str] = []
    cur: list[str] = []
    used = 0
    for ln in lines:
        if cur and used + len(ln) + 1 > limit:
            chunks.append("\n".join(cur))
            cur, used = [], 0
        cur.append(ln)
        used += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def digest_has_sections(text: str) -> bool:
    """摘要是否按骨架写（至少命中 3 段）。

    实测（kpgame 2026-10-29 run 3292305e）：摘要模型把「压缩这段历史」理解成
    「把这些事做完」，返回了一张 656 字的验收结论表，被当成摘要写回——压缩后模型
    接着又重看了 6 张图。只校验长度（≥250 tokens）拦不住这种答非所问。
    """
    return sum(1 for s in DIGEST_SECTIONS if s in (text or "")) >= 3


def _units(rest: list[dict]) -> list[list[dict]]:
    """把历史切成「不可拆的单元」：assistant(tool_calls) 与它后面跟随的 tool 结果同组。"""
    units: list[list[dict]] = []
    cur: list[dict] = []
    for m in reversed(rest):
        cur.insert(0, m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            units.insert(0, cur)
            cur = []
        elif m.get("role") not in ("tool",):
            units.insert(0, cur)
            cur = []
    if cur:
        units.insert(0, cur)
    return units


async def compress_messages(messages: list[dict], context_window: int,
                            summarizer: Any = None, report: dict | None = None,
                            tools: Any = None, budget_ratio: float = BUDGET_RATIO,
                            facts_provider: Any = None) -> list[dict]:
    """全局摘要压缩：把整条对话（含会话历史锚点）压进模型窗口。

    做法不是「丢掉最旧的 N 条」，而是：
    1) 最近若干轮保留原文（上限 = 预算 × TAIL_RATIO）；最后一条用户文本（插话/补充）
       与首条需求同等重要，也保留原文；
    2) 其余全部（包括首条需求里带进来的会话历史）**分块、旧→新** 交给 summarizer，
       每块把上一版的摘要作为 prior 带进去，保证被压历史的每一段都进过摘要器，
       且上一轮的摘要不会在这一轮蒸发；
    3) 摘要必须按骨架写（DIGEST_SECTIONS），答非所问会重问一次；
    4) 压完复核：还是超预算就先削摘要正文，再退最旧的保留轮，
       保证发出去的请求真的在窗口内（超预算直接 400，等于整轮白跑）。

    summarizer(text, target_tokens, prior="", strict=False)->str 由 runtime 提供。
    facts_provider() -> str：压缩真的发生时才会被调用，返回要固定在摘要前的硬事实
    （当前未完成清单）；不压缩就一次 DB 都不查。
    report（out-param）回写压缩现场，给上下文统计与「已压缩上下文」事件用。
    """
    window = context_window or DEFAULT_CONTEXT_WINDOW
    budget = budget_for(window, budget_ratio)
    # 装回目标比触发线更低一档（滞回）：压完占用肉眼可见地降下来，
    # 而不是停在触发线附近让人以为压缩没做事
    fit = max(2000, int(budget * COMPRESS_TARGET_RATIO))
    t_tok = tools_tokens(tools)
    # 可用预算先扣掉工具定义；扣完不剩多少时兜个底，至少别让历史为 0
    room = max(2000, fit - t_tok)

    # 图片先过字节闸（与 token 预算独立）：这一步会真的改写历史，像素过期后不再回来
    messages, media_rep = apply_media_budget(messages)

    systems: list[dict] = []
    old_digests: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.get("role") != "system" or rest:
            rest.append(m)
            continue
        c = m.get("content")
        # 上一轮的摘要不留在原位：这次要把它并进新摘要，否则摘要一轮叠一轮越长越大
        if isinstance(c, str) and c.startswith(DIGEST_MARK):
            old_digests.append(c)
        else:
            systems.append(m)

    # 用户原始需求必须原样保留：它是本轮唯一不可再生的事实来源（尤其是截图说明 /
    # 复现步骤）。之前它跟着「较早历史」一起被压掉——实测 run 27a8a2bf 第 108 轮压缩后，
    # 模型连「用户到底要修什么」都丢了，只能拿 search_history 一遍遍考古。
    anchor: dict | None = None
    if rest and rest[0].get("role") == "user":
        anchor = rest[0]
        rest = rest[1:]

    total = sum(message_tokens(m) for m in messages) + t_tok
    if report is not None:
        report.update({"compressed": False, "dropped": 0, "window": window,
                       "budget": budget, "tools_tokens": t_tok, "est_tokens": total,
                       **media_rep})
    if total <= budget:
        return messages

    units = _units(rest)
    # 1. 从最新往回收集保留原文的尾部（最新那一轮无条件保留，否则模型丢的就是
    #    「我刚才做了什么」，会重复读同一批文件原地打转）
    anchor_tokens = message_tokens(anchor) if anchor else 0
    tail: list[list[dict]] = []
    tail_cap = max(1, int(room * TAIL_RATIO))
    tail_tokens = sum(message_tokens(m) for m in systems) + anchor_tokens
    split = 0
    for i in range(len(units) - 1, -1, -1):
        u_tokens = sum(message_tokens(m) for m in units[i])
        if tail and tail_tokens + u_tokens > tail_cap:
            split = i + 1
            break
        tail.insert(0, units[i])
        tail_tokens += u_tokens

    head_units = units[:split]
    # 最后一条「用户说的话」（插话 / 补充需求 / 系统刹车都是 user 角色）不能跟着历史一起
    # 被摘要掉：它和首条需求一样是用户给的约束，丢了模型就会按旧理解继续做。
    pinned: list[dict] = []
    for i in range(len(head_units) - 1, -1, -1):
        u = head_units[i]
        if any(m.get("role") == "user" and not _is_viewback(m) for m in u):
            pinned = head_units.pop(i)
            break

    head = [m for u in head_units for m in u]
    head_tokens = sum(message_tokens(m) for m in head)
    old_digest_tokens = sum(estimate_tokens(d) for d in old_digests)
    if report is not None:
        report.update({"compressed": True, "dropped": len(head),
                       "dropped_tokens": head_tokens, "est_before": total,
                       "pinned": len(pinned)})
    if not head and not old_digests:
        # 只有「最新一轮 + 系统提示」本身就超预算：没有可合并的历史，只能硬截断兜底
        keep = ([anchor] if anchor else []) + pinned + [m for u in tail for m in u]
        return _force_fit(systems, keep, t_tok, fit)

    # 压缩真的发生了才去读当前清单：不压缩的轮次一次 DB 都不查
    facts = ""
    if facts_provider is not None:
        try:
            facts = str(await facts_provider() or "").strip()
        except Exception:  # noqa: BLE001 - 拿不到清单不影响压缩
            log.warning("读取当前清单失败（压缩时附带的硬事实）", exc_info=True)

    # 2. 摘要目标长度按比例给（不是固定字数）：被压得越多，摘要可以越长，
    #    但也绝不允许摘要自己变成新的上下文负担
    target = int((head_tokens + old_digest_tokens) * SUMMARY_RATE)
    target = max(SUMMARY_MIN_TOKENS, min(target, int(room * SUMMARY_MAX_RATIO)))

    digest = ""
    digest_ok = False
    chunks: list[str] = []
    chunks_dropped = 0
    if head and head_tokens >= SUMMARY_MIN_SOURCE_TOKENS and summarizer is not None:
        # 分块（旧→新）+ 逐块并入上一版摘要：每一条被压历史都会进摘要器一次，
        # 且上一轮的摘要是下一轮的 prior，不再依赖「文本前缀不变」那种不会成立的增量判断。
        chunks = _split_chunks(summary_lines(head), SERIAL_CHARS_CHUNK)
        if len(chunks) > SUMMARY_MAX_CHUNKS:
            chunks_dropped = len(chunks) - SUMMARY_MAX_CHUNKS
            chunks = chunks[-SUMMARY_MAX_CHUNKS:]
        prior = "\n\n".join(d for d in old_digests if d)
        prev_prior = prior
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            prev_prior = prior
            t_i = target if last else max(300, min(target, 1200))
            try:
                got = str(await summarizer(chunk, t_i, prior=prior, strict=False) or "").strip()
            except Exception:  # noqa: BLE001 - 单块失败保留上一版摘要，不能把 run 带崩
                log.warning("上下文分块摘要失败（第 %s/%s 块）", i + 1, len(chunks),
                            exc_info=True)
                got = ""
            if got:
                prior = got
        digest = prior
        digest_ok = digest_has_sections(digest)
        if digest and not digest_ok:
            # 答非所问（把「压缩这段历史」理解成「把历史里的事做完」）：带骨架再问一次
            try:
                strict_out = str(await summarizer(chunks[-1], target, prior=prev_prior,
                                                  strict=True) or "").strip()
            except Exception:  # noqa: BLE001
                strict_out = ""
            if digest_has_sections(strict_out):
                digest, digest_ok = strict_out, True
            else:
                log.warning("上下文摘要连续两次不符合骨架，退化为要点节选")
        digest = truncate_tokens(digest, target) if digest else ""
    # 摘要质量兜底：实测 summarizer 会返回「<assistant>[调用 run_command(...)]」这种
    # 复读工具调用的垃圾（2026-10-28 run 27a8a2bf：42k tokens 压成 101 tokens），
    # 结果等于历史被静默丢掉。短得离谱就当失败，退回「逐条要点」——宁可啰嗦。
    # 但**按骨架写成的**摘要可以更短：113 tokens 的五段摘要也远胜 5k tokens 的要点堆。
    if digest and head_tokens >= SUMMARY_MIN_SOURCE_TOKENS:
        dtok = estimate_tokens(digest)
        floor = DIGEST_MIN_TOKENS if digest_ok else SUMMARY_MIN_TOKENS
        if dtok < floor:
            log.warning("上下文摘要过短（%s tokens < %s，骨架%s），退化为要点节选",
                        dtok, floor, "合格" if digest_ok else "不合格")
            digest = ""
            digest_ok = False
    if not digest:
        # 没调成摘要（体量太小 / 无摘要器 / 端点失败）：旧摘要原样带下去，
        # 新挤出去的历史退化成一行行要点——宁可啰嗦，也不能静默丢掉
        parts = [d for d in old_digests if d]
        points = [_compact_digest(m, 200) for m in head]
        dropped_pts = max(0, len(points) - 60)
        points = points[-60:]
        if points:
            lead = f"（另有 {dropped_pts} 条更早消息未列出）\n" if dropped_pts else ""
            parts.append(lead + "（以下较早消息未自动摘要，按原序列出要点）\n- "
                         + "\n- ".join(points))
        digest = "\n\n".join(parts) or "（本段历史已合并，暂无可用摘要）"

    unsummarized = 0

    def _mk_note(body: str) -> dict:
        header = (f"{DIGEST_MARK} 为适配模型窗口（{window} tokens，发送预算 {budget}），"
                  f"已把较早的 {len(head)} 条历史（约 {head_tokens} tokens）合并成下面的要点摘要，"
                  f"最近 {len(tail)} 个回合保留原文。最近一条用户需求原文保留在摘要之后，"
                  f"以它为准。工具调用轨迹不在 search_history 里，需要现场就重新 read_file/grep。")
        if pinned:
            header += f" 期间用户补充/插话的 {len(pinned)} 条消息也保留原文。"
        if len(chunks) > 1:
            header += f" 摘要由 {len(chunks)} 段历史分块合并而成。"
        if chunks_dropped:
            header += f" 最早的 {chunks_dropped} 段历史未能纳入本次摘要。"
        if unsummarized:
            header += (f" 另有 {unsummarized} 条更早消息连摘要都放不下，已省略。")
        full = (f"## 当前状态（系统直读，优先于摘要）\n{facts}\n\n## 历史摘要\n{body}"
                if facts else body)
        return {"role": "system", "content": f"{header}\n\n{full}"}

    note_msg = _mk_note(digest)
    # 原始需求固定放在摘要之后、保留的最近回合之前
    anchor_list = [anchor] if anchor else []

    # 3. 复核：超预算先削摘要，再退最旧的保留**单元**
    #    （必须整组退：assistant(tool_calls) 和它的 tool 结果拆开会使请求直接 400）
    for _ in range(24):
        kept_flat = [m for u in tail for m in u]
        out = systems + [note_msg] + anchor_list + pinned + kept_flat
        est = sum(message_tokens(m) for m in out) + t_tok
        if est <= fit:
            break
        over = est - fit
        body_tokens = estimate_tokens(digest)
        if body_tokens > SUMMARY_MIN_TOKENS:
            digest = truncate_tokens(digest, max(SUMMARY_MIN_TOKENS, body_tokens - over - 32))
            note_msg = _mk_note(digest)
            continue
        if len(tail) > 1:
            unsummarized += len(tail.pop(0))
            note_msg = _mk_note(digest)
            continue
        return _force_fit(systems, [note_msg] + anchor_list + pinned + kept_flat, t_tok, fit)
    if report is not None:
        report.update({"dropped": len(head), "dropped_tokens": head_tokens,
                       "digest_tokens": estimate_tokens(digest),
                       "target_tokens": target,
                       "digest_ok": bool(digest_ok),
                       "chunks": len(chunks),
                       "chunks_dropped": chunks_dropped,
                       "summarized_chars": sum(len(c) for c in chunks),
                       "est_tokens": sum(message_tokens(m) for m in out) + t_tok})
    return out


def _truncate_calls(m: dict, keep: int) -> dict:
    """削 assistant 工具调用的入参（write_file 的整个文件正文就在这儿）。

    只削 arguments 的文本，不动 id/name：调用与结果必须还配得上对，
    否则端点直接 400（「tool_call_id 找不到对应调用」）。
    """
    out = dict(m)
    calls = []
    for tc in out.get("tool_calls") or []:
        if not isinstance(tc, dict):
            calls.append(tc)
            continue
        nested = tc.get("function") if isinstance(tc.get("function"), dict) else None
        src = nested or tc
        args = src.get("arguments")
        text = args if isinstance(args, str) else _dumps_args(args)
        cut = dict(tc)
        if nested is not None:
            cut["function"] = {**nested, "arguments": truncate_tokens(text, keep)}
        else:
            cut["arguments"] = truncate_tokens(text, keep)
        calls.append(cut)
    out["tool_calls"] = calls
    return out


def _force_fit(systems: list[dict], rest: list[dict], t_tok: int,
               budget: int) -> list[dict]:
    """最后兜底：连摘要都救不了（单条巨型消息），逐条硬截断到预算内。

    发给端点一个超长请求只会换来 400，整轮白跑；截断至少还能让模型继续干活。
    """
    out = list(systems) + list(rest)
    for _ in range(32):
        est = sum(message_tokens(m) for m in out) + t_tok
        if est <= budget:
            break
        over = est - budget
        # 从最贵的非系统消息开刀（入参和正文都要算，不然永远找不到大头）
        idx, big = -1, 0
        for i, m in enumerate(out):
            if m.get("role") == "system":
                continue
            t = message_tokens(m)
            if t > big:
                idx, big = i, t
        if idx < 0 or big <= 0:
            break
        m = out[idx]
        keep = max(200, big - over - 32)
        arg_tokens = sum(message_tokens({"tool_calls": [tc]}) - 4 for tc in (m.get("tool_calls") or []))
        if arg_tokens > estimate_tokens(m.get("content")):
            out[idx] = _truncate_calls(m, max(120, keep - 32))
        else:
            c = m.get("content")
            if isinstance(c, list):  # 图片消息：把图换成文字说明，正文留头尾
                c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
            m2 = {k: v for k, v in m.items() if k != "content"}
            m2["content"] = truncate_tokens(str(c or ""), keep) + "\n[因窗口限制已截断]"
            out[idx] = m2
    return out
