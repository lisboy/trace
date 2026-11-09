"""ask_user：组长拿不准就问用户（不要自己拍板）。

设计要点：
- 工具不在「主流程」里等：落一条 run_questions + 在会话里发一条带问题卡片的消息，
  然后抛 ``AskUserStop`` 中止本轮 agent 循环，run 落 ``blocked``。
- 用户回答后（前端调 /api/questions/{id}/answer）由后端把答案作为一条 user 消息写进同一会话
  并重新入队一个 run —— 会话历史会被重新注入，上下文不丢。
  这样不会有一个 worker 被"等人回答"长时间占住（worker 池只有 3 个）。
"""
from __future__ import annotations

from typing import Any

from game_agent.agents.tools import ToolContext, ToolError

MAX_OPTIONS = 6


class AskUserStop(Exception):
    """中止本轮 agent 循环：已把问题抛给用户，等回答后再起新 run 继续。"""

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
    record = await orchestrator.ask_user(
        t.run_ctx, question=question, options=options,
        allow_text=bool(allow_text), agent_key=t.role,
    )
    # 中止循环，由 orchestrator 把 run 落 blocked 并推给前端
    raise AskUserStop(record)
