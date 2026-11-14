"""spawn_agents：主 agent 把互不依赖的调研/核对拆成多个只读子 agent 并行执行。

设计取舍（2026-11-12 改为「派发即返回、结论后台回填」）：
- 以前 spawn_agents 一直 await 到所有子agent 结束才返回：那几十分钟里主agent 完全
  停摆，用户插话要等整批结束才被看见（用户实测：「主agent 还是没有去处理插话」）。
- 现在返回只要几毫秒：子agent 在后台并行跑，结论完成后由 AgentRuntime 在下一轮开头
  作为一条 user 消息注入对话，主agent 照常汇总。等待期间它依然能回应用户、处理插话。
- 子 agent 只读：sub 角色白名单只有读工具 + report_to_user，没有写/跑/生成，
  天然没有写锁冲突，可以在同一 run 内并发核对多条线索。
- 复用 Orchestrator.run_agent：子 agent 走同一套 LLM 调用 / 取消看门狗 / task 记账，
  只是创建为 role="sub" 的嵌套 task（parent_task_id = 主 task）。
- 递归禁止：sub 白名单里没有 spawn_agents，派不出孙 agent。
- 并发上限 MAX_SUBAGENTS，且受模型自身调用习惯约束。
- 最少 2 条：1 条自己读更快；2 条及以上互不依赖的核对就值得并行派。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from game_agent.agents.tools import ToolContext, ToolError

MAX_SUBAGENTS = 6  # 一次 spawn_agents 最多并行派多少个；再多拆批
MAX_INSTRUCTION_CHARS = 2000


@dataclass
class SpawnBatch:
    """一次 spawn_agents 派出的后台批次：任务句柄 + 回收汇总。"""

    names: list[str]
    tasks: list[asyncio.Task] = field(default_factory=list)
    _agg: str | None = None

    def ready(self) -> bool:
        """所有子agent 是否已收工（不论成败）。"""
        return bool(self.tasks) and all(t.done() for t in self.tasks)

    def aggregate(self) -> str:
        """各子agent 结论拼成交给主agent 的汇总（只在 ready() 之后调用）。"""
        if self._agg is None:
            parts: list[str] = []
            for name, task in zip(self.names, self.tasks):
                if task.cancelled():
                    parts.append(f"## {name}\n（已取消，没有结论）")
                    continue
                exc = task.exception()
                if exc is not None:
                    parts.append(f"## {name}\n执行出错：{type(exc).__name__}: {exc}")
                    continue
                parts.append(str(task.result()))
            self._agg = (f"已并行派出 {len(self.tasks)} 个只读子agent 核对，全部回收：\n\n"
                         + "\n\n".join(parts))
        return self._agg


async def spawn_agents(args: dict, t: ToolContext) -> str:
    """并行派发只读子 agent 调研/核对；立即返回，结论稍后自动送进对话。

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
    # 只有 1 条线索才自己直接读：就这点活建个 task、单独走一次 LLM，开销大于主agent 自己读。
    # 2 条及以上就值得并行派（实测 2026-11-11：两条较重的核对被旧门槛 3 逼成串行，主agent 反复
    # 试探 30s+；提示词/指导项里写的也是「两处以上」）。
    if len(items) < 2:
        raise ToolError(
            f"只有 {len(items)} 条线索，主agent 自己直接读更快，不必派子agent"
            "（2 条以上互不依赖的核对才值得用 spawn_agents）")

    names: list[str] = []
    questions: list[tuple[str, str, str]] = []  # (name, question, note)
    for idx, it in enumerate(items):
        name = str(it.get("name") or f"子任务 {idx + 1}").strip()[:40]
        question = str(it.get("question") or "").strip()[:MAX_INSTRUCTION_CHARS]
        note = str(it.get("note") or "").strip()[:200]
        names.append(name)
        questions.append((name, question, note))

    async def _one(name: str, question: str, note: str) -> str:
        if not question:
            return f"- **{name}**：没有给 question，跳过"
        inst = question + (f"\n（备注：{note}）" if note else "")
        try:
            # 任务名/问题随 task 落库并进 task_start 事件：前端把每个子agent 渲染成
            # 带名字的分组，用户能对上「哪个子agent 在查哪条线索」（而不是一串 sub）
            result = await t.orchestrator.run_agent(
                role="sub", parent_task_id=t.task_id, instruction=inst,
                run_ctx=t.run_ctx, payload={"question": question[:200]},
                task_title=f"子agent · {name}")
            text = (result.text or "").strip() or "（子agent 没有返回正文）"
            return f"## {name}\n{text}"
        except Exception as e:  # noqa: BLE001 - 单个子agent失败不影响其他并行任务
            return f"## {name}\n执行出错：{type(e).__name__}: {e}"

    tasks = [asyncio.create_task(_one(name, question, note))
             for name, question, note in questions]
    # 批次挂在 ToolContext 上：AgentRuntime 每轮开头检查 ready()，把结论注入对话；
    # 运行结束时 Orchestrator.run_agent 的 finally 会把没回收的批次 cancel 掉。
    t.spawn_batches.append(SpawnBatch(names=names, tasks=tasks))
    return (f"已派出 {len(tasks)} 个只读子agent 并行核对（{'、'.join(names)}）。\n"
            "它们还在后台跑，结论回来后会自动送进你的对话（不需要轮询/等待）。"
            "这期间你可以先回应用户插话、继续别的工作；收尾时会等结论回来再写总结。"
            "不要凭空猜测它们的发现。")
