/** 计划机制：组长维护的待办清单（todo list）。
 *
 * 数据源：后端 runs.plan_json（DB 权威源）。实时更新走 WS 事件 `plan_update`
 * （Chat 页把 run 事件转发成 window 上的 ga-run-ev-<runId>）；刷新/重连后回落 REST。
 * 勾选状态：completed=✅（划线）、in_progress=🔄（进行中）、pending=⬜。
 */
import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export interface PlanItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | string;
  note?: string;
}

const MARK: Record<string, string> = { completed: "✅", in_progress: "🔄", pending: "⬜" };

export default function PlanChecklist({ runId }: { runId: string }) {
  const [items, setItems] = useState<PlanItem[]>([]);
  const [summary, setSummary] = useState("");
  // 默认收起：标题行已经带进度条和「N 项进行中」，展开与否交给用户。
  // 以前进会话默认整块展开，几行清单把聊天区往上顶，进来先看到的是一堆待办。
  const [open, setOpen] = useState(false);
  const latest = useRef<PlanItem[]>([]);
  useEffect(() => {
    latest.current = items;
  }, [items]);

  // 初次加载（历史 run / 刷新页面）——换 run 时必须先清空再取：
  // 以前只在「取回非空清单」时才 setItems，于是切到一条没有计划的 run（例如配音任务）
  // 时，上一轮/别的会话的清单会一直挂在输入框上方，看着就像「计划串会话了」。
  useEffect(() => {
    let alive = true;
    setItems([]);
    setSummary("");
    api
      .plan(runId)
      .then((r: any) => {
        if (!alive) return;
        setItems(Array.isArray(r?.items) ? r.items : []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId]);

  // 实时事件：plan_update 直接替换整份清单（后端整体覆盖语义）
  useEffect(() => {
    function onEv(e: Event) {
      const ev = (e as CustomEvent).detail as { type?: string; payload?: any };
      if (ev?.type !== "plan_update") return;
      const list = ev.payload?.items;
      if (Array.isArray(list)) {
        setItems(list);
        setSummary(String(ev.payload?.summary || ""));
      }
    }
    window.addEventListener(`ga-run-ev-${runId}`, onEv);
    return () => window.removeEventListener(`ga-run-ev-${runId}`, onEv);
  }, [runId]);

  const total = items.length;
  const done = items.filter((i) => i.status === "completed").length;
  const active = items.filter((i) => i.status === "in_progress").length;
  const allDone = total > 0 && done === total;
  const pct = total ? Math.round((done / total) * 100) : 0;

  if (!total) return null;

  return (
    <div className="mb-2 rounded-lg border border-slate-200 bg-white overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-2.5 py-1.5 text-xs hover:bg-slate-50"
        onClick={() => setOpen(!open)}
        title={summary || "组长维护的待办清单"}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" className="text-slate-400 shrink-0">
          <path d="M9 11l3 3L22 4" />
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
        </svg>
        <span className="font-medium text-slate-600">待办清单</span>
        <span className={allDone ? "text-emerald-600" : "text-slate-500"}>
          {done}/{total} 已完成
        </span>
        {active > 0 && !allDone && <span className="text-amber-600">· {active} 项进行中</span>}
        <span className="ml-auto flex items-center gap-2">
          <span className="h-1.5 w-16 rounded-full bg-slate-200 overflow-hidden">
            <span
              className={`block h-full rounded-full transition-all ${allDone ? "bg-emerald-500" : "bg-brand-500"}`}
              style={{ width: `${pct}%` }}
            />
          </span>
          <span className="text-slate-300">{open ? "▲" : "▼"}</span>
        </span>
      </button>
      {open && (
        <div className="px-2.5 pb-2 space-y-1">
          {items.map((it, i) => {
            const cls =
              it.status === "completed"
                ? "text-slate-400 line-through"
                : it.status === "in_progress"
                ? "text-slate-800"
                : "text-slate-600";
            return (
              <div key={it.id || i} className="flex items-start gap-1.5 text-xs leading-5">
                <span className="shrink-0 select-none">{MARK[it.status] || "⬜"}</span>
                <span className={`${cls} font-medium`}>{i + 1}.</span>
                <span className={cls}>
                  {it.content}
                  {it.note && (
                    <span className="ml-1 text-[11px] text-slate-400 no-underline">— {it.note}</span>
                  )}
                </span>
              </div>
            );
          })}
          {summary && <div className="text-[11px] text-slate-400 pt-0.5">最新更新：{summary}</div>}
        </div>
      )}
    </div>
  );
}
