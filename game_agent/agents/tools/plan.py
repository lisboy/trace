"""计划机制（todo/plan）：组长维护本轮任务的待办清单，完成后逐项打勾。

设计要点：
- 单一写者：只有组长（planner）白名单里有 update_plan，避免多方并发覆盖。
- 整体覆盖 + 稳定 id：每次调用传完整清单（顺序即展示顺序），服务端归一化后落
  ``runs.plan_json`` 并广播 ``plan_update`` 事件；前端据此实时渲染勾选进度。
- 定位：这是给用户看的执行进度（"现在到哪一步了"），面向复杂度 ≥3 步的提问；
  不再有工单（write_work_order 已随三角色架构移除）。
"""
from __future__ import annotations

from game_agent.agents.tools import ToolContext, ToolError

# 对外暴露的三态；模型偶尔会写中文/别名，这里统一归一
PLAN_STATUSES = ("pending", "in_progress", "completed")
_STATUS_ALIASES = {
    "pending": "pending", "todo": "pending", "wait": "pending", "waiting": "pending",
    "未开始": "pending", "待办": "pending", "待处理": "pending", "": "pending",
    "in_progress": "in_progress", "in-progress": "in_progress", "doing": "in_progress",
    "running": "in_progress", "active": "in_progress", "started": "in_progress",
    "进行中": "in_progress", "处理中": "in_progress", "执行中": "in_progress",
    "completed": "completed", "complete": "completed", "done": "completed",
    "finished": "completed", "ok": "completed", "success": "completed",
    "已完成": "completed", "完成": "completed", "已勾选": "completed", "✅": "completed",
}
_STATUS_MARK = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
_STATUS_LABEL = {"completed": "已完成", "in_progress": "进行中", "pending": "未开始"}

MAX_ITEMS = 20
MAX_CONTENT = 200
MAX_NOTE = 300


def norm_status(raw: object) -> str:
    """任意写法 → pending|in_progress|completed（未知值退回 pending）。"""
    key = str(raw or "").strip().lower()
    return _STATUS_ALIASES.get(key, _STATUS_ALIASES.get(str(raw or "").strip(), "pending"))


def normalize_plan_items(raw: object) -> list[dict]:
    """把模型给的 items 归一化成 [{id, content, status, note}]。

    容错：允许字符串项（当作 pending 待办）、未知 status、缺 note、空内容（丢弃）。
    """
    if not isinstance(raw, list):
        raise ToolError("items 必须是数组，形如 [{content: '…', status: 'pending'}]")
    out: list[dict] = []
    for it in raw:
        if isinstance(it, str):
            content, status, note = it, "pending", ""
        elif isinstance(it, dict):
            content = str(it.get("content") or it.get("title") or it.get("text") or "").strip()
            status = norm_status(it.get("status") or it.get("state") or "")
            note = str(it.get("note") or it.get("detail") or "").strip()
        else:
            continue
        if not content:
            continue
        out.append({
            "id": f"p{len(out) + 1}",
            "content": content[:MAX_CONTENT],
            "status": status,
            "note": note[:MAX_NOTE],
        })
        if len(out) >= MAX_ITEMS:
            break
    return out


def plan_progress(items: list[dict]) -> dict:
    total = len(items)
    completed = sum(1 for i in items if i.get("status") == "completed")
    in_progress = sum(1 for i in items if i.get("status") == "in_progress")
    return {"total": total, "completed": completed,
            "in_progress": in_progress, "pending": total - completed - in_progress}


def render_plan(items: list[dict]) -> str:
    """给模型回读的纯文本清单（带勾选标记）——模型据此知道下一步该干哪项。"""
    if not items:
        return "（清单已清空）"
    lines = []
    for i, it in enumerate(items, 1):
        mark = _STATUS_MARK.get(it.get("status", "pending"), "⬜")
        line = f"{mark} {i}. {it.get('content', '')}（{_STATUS_LABEL.get(it.get('status', ''), it.get('status', ''))}）"
        if it.get("note"):
            line += f" —— {it['note']}"
        lines.append(line)
    p = plan_progress(items)
    lines.append(f"\n进度：{p['completed']}/{p['total']} 已完成"
                 + (f"，{p['in_progress']} 项进行中" if p["in_progress"] else ""))
    return "\n".join(lines)


async def update_plan(args: dict, t: ToolContext) -> str:
    """组长：写入/更新本轮待办清单（整体覆盖）。"""
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    items = normalize_plan_items(args.get("items"))
    summary = str(args.get("summary") or "").strip()[:200]
    items = await t.orchestrator.update_plan(t.run_ctx, items, summary=summary,
                                             agent_key=t.role)
    return ("清单已更新并同步到界面（用户可见）。\n" + render_plan(items)
            + "\n\n提示：每完成一步就立刻把对应项改为 completed 再调用一次 update_plan，"
              "不要等全部做完才更新；未开始的项保持 pending。")
