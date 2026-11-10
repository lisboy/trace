"""spawn_agents：主 agent 把互不依赖的调研/核对拆成多个只读子 agent 并行执行。

设计取舍（见 docs/DESIGN-OPTIMIZATION.md「子 agent 并行」节）：
- 子 agent 只读：sub 角色白名单只有读工具 + report_to_user，没有写/跑/生成，
  天然没有写锁冲突，可以在同一 run 内并发核对多条线索。
- 复用 Orchestrator.run_agent：子 agent 走同一套 LLM 调用 / 取消看门狗 / task 记账，
  只是创建为 role="sub" 的嵌套 task（parent_task_id = 主 task）。
- 递归禁止：sub 白名单里没有 spawn_agents，派不出孙 agent。
- 并发上限：与运行时工具并行度一致（最多 6），且受模型自身调用习惯约束。
"""
from __future__ import annotations

import asyncio

from game_agent.agents.tools import ToolContext, ToolError

MAX_SUBAGENTS = 6  # 一次 spawn_agents 最多并行派多少个；再多拆批
MAX_INSTRUCTION_CHARS = 2000


async def spawn_agents(args: dict, t: ToolContext) -> str:
    """并行派发只读子 agent 调研/核对，汇总结论返回给主 agent。

    入参 items：[{name, question, note?}]，每个 item 是一个独立问题。
    子 agent 只读（read/grep/glob/web/memory），不写文件不跑命令；
    适合「并行核对 N 个文件的某个点」「并行查 N 个资料源」这类互不依赖的查询。
    """
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ToolError("spawn_agents 需要 items（非空数组），每项 {name, question}")
    items = [it for it in items if isinstance(it, dict)][:MAX_SUBAGENTS]
    if not items:
        raise ToolError("spawn_agents 的 items 里没有合法项")

    async def _one(idx: int, it: dict) -> str:
        name = str(it.get("name") or f"sub-{idx + 1}").strip()[:40]
        question = str(it.get("question") or "").strip()
        if not question:
            return f"- **{name}**：没有给 question，跳过"
        question = question[:MAX_INSTRUCTION_CHARS]
        note = str(it.get("note") or "").strip()[:200]
        inst = question + (f"\n（备注：{note}）" if note else "")
        try:
            result = await t.orchestrator.run_agent(
                role="sub", parent_task_id=t.task_id, instruction=inst,
                run_ctx=t.run_ctx)
            text = (result.text or "").strip() or "（子agent 没有返回正文）"
            return f"## {name}\n{text}"
        except Exception as e:  # noqa: BLE001 - 单个子agent失败不影响其他并行任务
            return f"## {name}\n执行出错：{type(e).__name__}: {e}"

    results = await asyncio.gather(*(_one(i, it) for i, it in enumerate(items)))
    header = (f"已并行派出 {len(results)} 个只读子agent 核对，全部回收：\n\n")
    return header + "\n\n".join(results)
