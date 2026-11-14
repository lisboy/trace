"""插话的分派决策：主agent 收到用户插话时，用**大模型**判断要不要同步给正在跑的子agent。

背景（用户 2026-11-11 要求）：「我在插话的时候，主agent 还要能根据情况，如果存在子agent，
要把插话分派给各子agent，视情况而定，由大模型驱动，不然都无法停掉子agent」。

为什么必须由模型判定、而不是关键词：一条插话到底是「只跟主agent 说」（如改方向、补充一个
只对整体有效的要求）还是「某几个子agent 的核对范围变了」（如「顺便也看看另一个文件的 3.0
版本」），取决于插话语义与每个子agent 正在查什么——关键词表永远补不完中文表达，判定交给模型。

失败兜底：模型不可用/解析失败时**不派**（默认只给主agent）。理由：给子agent 多塞一条无关
要求会污染它们的只读核对结论，比漏派更难查；主agent 本来就会收到这条插话，能自己决定后续。
"""
from __future__ import annotations

import asyncio
import json

from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm
from game_agent.logging_setup import get_logger

log = get_logger("orchestration.interject")

DISPATCH_TIMEOUT_S = 15

_DISPATCH_PROMPT = """你在一个「主agent + 若干只读子agent」并行协作的场景里。用户在执行过程中插了一句话。
请判断这句话应该派给**哪些正在跑的子agent**（让它们把这句话纳入当前的只读核对），哪些不该派。

判据：
- 只有「会改变某个子agent 正在核对的内容/范围」的插话才派给它（如补充要看的角度、纠正查错的对象、
  追加同一主题的线索）。
- 只涉及整体方向、最终产物、或与某子agent 的核对主题无关的话，不派给它。
- 拿不准就不派——给子agent 塞无关要求会污染其只读结论。

用户插话：
<<<
{note}
>>>

正在跑的子agent 列表（每项有 index / 名字 / 它在核对的原始问题）：
{subs}

只输出一个 JSON 对象，不要多余文字：
{{"targets":[命中的 index 整数数组，可能为空],"reason":"一句话理由"}}"""


def _parse_targets(text: str, valid: set[int]) -> list[int] | None:
    t = (text or "").strip()
    if not t:
        return None
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.startswith("{") or p.lower().startswith("json"):
                t = p
                break
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
    except Exception:  # noqa: BLE001
        return None
    raw = obj.get("targets") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return None
    out: list[int] = []
    for x in raw:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n in valid and n not in out:
            out.append(n)
    return out


async def decide_targets(note: str, subs: list[dict], *,
                         profile: dict | None = None,
                         timeout_s: int = DISPATCH_TIMEOUT_S) -> list[int]:
    """用大模型判定这条插话要派给哪几个子agent。返回命中的 index 列表（可能为空）。

    ``subs``：``[{"index": int, "name": str, "question": str}]``。
    任何异常都安全兜底为「不派」（返回空列表）。
    """
    text = (note or "").strip()
    if not text or not subs:
        return []
    valid = {int(s.get("index")) for s in subs if s.get("index") is not None}
    if not valid:
        return []
    subs_txt = "\n".join(
        f"- index={s.get('index')} 名字={s.get('name') or '子agent'} "
        f"原始问题={str(s.get('question') or '')[:200]}"
        for s in subs)
    prompt = _DISPATCH_PROMPT.replace("{note}", text[:2000]).replace("{subs}", subs_txt)
    try:
        ctx = Ctx(role="main", run_type="dispatch", requirement=text,
                  model_profile=profile or {})
        llm = build_llm("main", ctx)
        resp = await asyncio.wait_for(
            llm.chat([{"role": "user", "content": prompt}], [], ctx),
            timeout=timeout_s)
        targets = _parse_targets(getattr(resp, "text", "") or "", valid)
        if targets is None:
            log.warning("插话分派返回无法解析，按不派兜底：%r",
                        (getattr(resp, "text", "") or "")[:120])
            return []
        log.info("插话分派：命中子agent %s", targets or "（无）")
        return targets
    except Exception as e:  # noqa: BLE001 - 分派失败绝不能影响主流程
        log.warning("插话分派失败，按不派兜底：%s: %s", type(e).__name__, str(e)[:160])
        return []
