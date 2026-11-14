"""ask_user：组长拿不准就问用户（不要自己拍板）。

设计要点（2026-11-10 起改为「同一 run 内暂停」）：
- 工具在「主流程」里暂停：落一条 run_questions + 会话里发一条带问题卡片的消息，
  然后**挂起等用户回答**（同一 run、同一轨迹、同一份待办清单），回答后接着往下做。
  不再把提问当成「结束上一轮 + 起新 run」——那样计划、轨迹、对话上下文都会被切断。
- 只有拿不到进程内 future（服务重启过 / 多进程 server 模式）时才回退老逻辑：
  抛 ``AskUserStop`` 中止本轮，run 落 ``blocked``，用户作答后起新 run 续跑。
"""
from __future__ import annotations

from typing import Any

from game_agent.agents.tools import ToolContext, ToolError

MAX_OPTIONS = 6


class AskUserStop(Exception):
    """回退路径：已把问题抛给用户，本轮无法在进程内等待，等回答后再起新 run 继续。"""

    def __init__(self, question: dict[str, Any]) -> None:
        super().__init__("waiting for user answer")
        self.question = question


def _norm_options(raw: Any) -> list[dict]:
    out: list[dict] = []
    for i, o in enumerate(raw or []):
        if isinstance(o, str):
            label = o.strip()
            detail = ""
        elif isinstance(o, dict):
            label = str(o.get("label") or o.get("text") or "").strip()
            detail = str(o.get("detail") or o.get("note") or "").strip()
        else:
            continue
        if not label:
            continue
        out.append({"label": label, "detail": detail})
        if len(out) >= MAX_OPTIONS:
            break
    return out


async def ask_user(args: dict, t: ToolContext) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        raise ToolError("缺少 question（要向用户确认的问题）")
    options = _norm_options(args.get("options"))
    allow_text = args.get("allow_text", True)
    if not options and not allow_text:
        raise ToolError("至少要给 options（选项）或 allow_text=true（允许自由回答）")

    orchestrator = getattr(t, "orchestrator", None)
    if orchestrator is None:  # 理论上不会发生
        raise ToolError("当前上下文不支持向用户提问")
    # 推进「活进展」计数：等待回答期间工具层硬超时看门狗据此不把本次调用当成卡死。
    record = await orchestrator.ask_user(
        t.run_ctx, question=question, options=options,
        allow_text=bool(allow_text), agent_key=t.role,
        progress=lambda: setattr(t, "progress_seq", t.progress_seq + 1),
    )
    answer = record.get("answer")
    if answer is None:
        # 回退路径（拿不到进程内 future）：中止循环，由 orchestrator 落 blocked 并推给前端
        raise AskUserStop(record)
    # 同一 run 内拿到了答案：返回给模型，循环继续（不结束本轮）
    return f"用户已回答：{answer}\n请据此继续执行，不要再重复提问。"

