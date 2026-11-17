import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { SubagentItem, SubagentList } from "../types";
import { SubAgentModal, subGroupFromEvents, type SubGroup } from "./RunTraceInline";

/* 子agent 清单：本会话所有跑过的只读调研子agent（spawn_agents 派出的）。

   用户 2026-11-16 要求：「子和主的统计分开，这里只统计主的，子的在子里面看，
   而且运行面板里要有单独 tab 看到这个会话所有运行过的子agent」。
   所以顶栏的 token/上下文/速度全都不含子agent，它们的账只在这里看。
   同一天追加要求：「子agent 要能点击，并弹窗查看对话详情」——每条点开就是它
   自己的完整执行轨迹（思考 + 每次工具调用 + 收尾结论），与聊天流里那张
   子agent 卡片用的是同一个弹窗组件，两处不会各说各话。

   token 只能到「run 粒度」：llm_calls 没有 task_id，同一 run 里并行派了 N 个子agent 时，
   端点上报的用量是这 N 个合在一起的。接口如实返回 run_usage，这里也如实标注
   「本 run 子agent 合计」，不假造每个子agent 的逐条 token。 */

const STATUS_STYLE: Record<string, string> = {
  running: "bg-sky-100 text-sky-700",
  done: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
  cancelled: "bg-slate-100 text-slate-500",
  pending: "bg-slate-100 text-slate-500",
};
const STATUS_TEXT: Record<string, string> = {
  running: "执行中",
  done: "完成",
  failed: "失败",
  cancelled: "已停止",
  pending: "排队中",
};

function tok(n: number | null | undefined): string {
  return Math.max(0, Math.round(n || 0)).toLocaleString();
}

/** 窄栏里用的短数字：436465 → 436.5k（右边那一列只有几十 px，写全数字会被裁掉）。 */
function tokShort(n: number | null | undefined): string {
  const v = Math.max(0, Math.round(n || 0));
  if (v < 10_000) return v.toLocaleString();
  return `${(v / 1000).toFixed(v >= 100_000 ? 0 : 1)}k`;
}

function secs(ms: number): string {
  if (!ms || ms < 0) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** 本项目里「还在跑」的子agent 每 3 秒补拉一次清单（换个 tab 回来也能看到进度）；
 *  全部收工后自动停，不白烧查询。 */
const POLL_MS = 3000;

export default function SubagentsPanel({ sessionId, runId, refreshKey = 0 }: {
  sessionId: string;
  /** 当前打开的 run：只用于把属于它的子agent 标出来（清单仍是整个会话的） */
  runId?: string;
  /** 变化时重拉（run 状态/用量变化） */
  refreshKey?: string | number;
}) {
  const [data, setData] = useState<SubagentList | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  /** 展开中的那一条（内联的元数据；对话详情走弹窗） */
  const [open, setOpen] = useState<string | null>(null);
  /** 弹窗里正在看的那一个子agent */
  const [detail, setDetail] = useState<SubagentItem | null>(null);

  const load = useCallback((sync?: () => boolean) => {
    if (!sessionId) return;
    api
      .sessionSubagents(sessionId)
      .then((r: any) => {
        if (sync && !sync()) return;
        setData(r);
        setErr(null);
      })
      .catch((e: any) => {
        if (sync && !sync()) return;
        setErr(String(e?.message || e));
      })
      .finally(() => setLoading(false));
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    setLoading(true);
    load(() => alive);
    return () => {
      alive = false;
    };
  }, [sessionId, refreshKey, load]);

  // 有子agent 还在跑 → 定时补拉，否则一条请求都不发
  const running = Number(data?.totals?.running || 0);
  useEffect(() => {
    if (!sessionId || !running) return;
    const t = window.setInterval(() => load(), POLL_MS);
    return () => clearInterval(t);
  }, [sessionId, running, load]);

  if (!sessionId) {
    return <div className="p-3 text-sm text-slate-400">还没有运行记录，无法统计子agent。</div>;
  }
  if (loading && !data) {
    return <div className="p-3 text-sm text-slate-400">读取子agent 记录…</div>;
  }
  if (err) {
    return <div className="p-3 text-sm text-red-600">读取子agent 失败：{err}</div>;
  }
  const items = data?.items || [];
  const t = data?.totals;
  // 同一 run 里并行派出的子agent 共用一份 run 级 token：如果每行都写一遍那个大数字
  // （截图里 6 行全是同一个 436,465 tok），会让人以为「每个子agent 各花了 43 万」。
  // 所以只在每个 run 的第一条上写「本 run 合计」，其余写「同 run 合计」，并在展开区注明口径。
  const perRunCount = new Map<string, number>();
  for (const it of items) perRunCount.set(it.run_id, (perRunCount.get(it.run_id) || 0) + 1);
  const seenRun = new Set<string>();
  if (!items.length) {
    return (
      <div className="p-3 text-sm text-slate-400">
        本会话还没派过子agent。
        <div className="mt-1 text-[11px] text-slate-400">
          主对话用 spawn_agents 并行派只读调研时，每个子agent 都会在这里出现一条
          （点开可看它自己的完整对话详情：思考、每次工具调用、收尾结论）。
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-auto">
      {/* 汇总条：子agent 自己的账（与顶栏那份主对话统计互不混算） */}
      <div className="px-3 py-2 border-b border-slate-200 bg-slate-50/70 text-[11px] text-slate-500 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-slate-700">子agent 合计</span>
        <span>{t?.count ?? items.length} 个</span>
        {!!t?.running && <span className="text-sky-600">{t.running} 个执行中</span>}
        {!!t?.failed && <span className="text-red-600">{t.failed} 个失败</span>}
        <span>{tok(t?.calls)} 次调用</span>
        <span>{tok(t?.total_tokens)} tok</span>
        <span className="text-slate-400">
          （输入 {tok(t?.prompt_tokens)} + 输出 {tok(t?.completion_tokens)}）
        </span>
        <span className="text-slate-400">
          这些数不计入顶栏的上下文/token/速度，顶栏只统计主对话
        </span>
      </div>

      <ul className="p-2 space-y-1.5">
        {items.map((it: SubagentItem) => {
          const isCur = runId && it.run_id === runId;
          const expanded = open === it.task_id;
          // 同一 run 的第 2 条起不再重复那个 run 级合计（避免 6 行顶着同一个 43 万 tok）
          const firstOfRun = !seenRun.has(it.run_id);
          if (firstOfRun) seenRun.add(it.run_id);
          const sharedRun = (perRunCount.get(it.run_id) || 0) > 1;
          const tokText = !it.run_usage
            ? "—"
            : firstOfRun
            ? `${tokShort(it.run_usage.total_tokens)} tok${sharedRun ? " 合计" : ""}`
            : "同 run 合计";
          return (
            <li
              key={it.task_id}
              className={`rounded border bg-white ${isCur ? "border-brand-300" : "border-slate-200"}`}
            >
              <div className="w-full px-2 py-1.5 flex items-start gap-2">
                <button
                  className="flex-1 min-w-0 text-left flex items-start gap-2 group"
                  onClick={() => setDetail(it)}
                  title={`查看这个子agent 的对话详情（思考 / 工具调用 / 结论）\n${it.question || it.name}`}
                >
                  <span
                    className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] ${
                      STATUS_STYLE[it.status] || "bg-slate-100 text-slate-500"
                    }`}
                  >
                    {STATUS_TEXT[it.status] || it.status}
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="text-xs font-medium text-slate-700 truncate block group-hover:text-brand-700">
                      {it.name}
                      <span className="ml-1.5 text-[10px] font-normal text-slate-400 group-hover:text-brand-600">
                        查看详情 ›
                      </span>
                    </span>
                    <span className="text-[11px] text-slate-400 truncate block">
                      {it.question || "（没有记录问题描述）"}
                    </span>
                  </span>
                  <span className="shrink-0 text-[10px] text-slate-400 mono text-right">
                    <span className="block">{secs(it.elapsed_ms)}</span>
                    <span className="block">{tokText}</span>
                  </span>
                </button>
                <button
                  className="shrink-0 mt-0.5 rounded p-0.5 text-slate-300 hover:bg-slate-100 hover:text-slate-500"
                  onClick={() => setOpen(expanded ? null : it.task_id)}
                  title={expanded ? "收起记录信息" : "展开运行记录（起止时间 / token 口径）"}
                  aria-label={expanded ? "收起记录信息" : "展开运行记录"}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                    className={expanded ? "rotate-90" : ""}>
                    <path d="m9 18 6-6-6-6" />
                  </svg>
                </button>
              </div>
              {expanded && (
                <div className="px-2 pb-2 border-t border-slate-100 pt-1.5 text-[11px] text-slate-500 space-y-0.5">
                  <div>
                    所属 run：
                    <span className="mono text-slate-600">{it.run_id.slice(0, 8)}</span>
                    {isCur && <span className="ml-1 text-brand-600">（当前打开这条）</span>}
                  </div>
                  <div>开始：{it.started_at || it.created_at}</div>
                  {it.finished_at && <div>结束：{it.finished_at}</div>}
                  <div>
                    本 run 子agent 合计：
                    {it.run_usage
                      ? `${tok(it.run_usage.calls)} 次调用 · ${tok(it.run_usage.total_tokens)} tok`
                        + `（输入 ${tok(it.run_usage.prompt_tokens)} + 输出 ${tok(it.run_usage.completion_tokens)}）`
                        + ` · ${secs(it.run_usage.latency_ms || 0)}`
                      : "没有记录"}
                  </div>
                  {it.run_usage && (
                    <div className="text-slate-400">
                      token 是「这个 run 里所有子agent 的合计」：llm_calls 没有 task_id，
                      并行派出的几条无法逐条拆分，这里不编造单条数字。
                    </div>
                  )}
                  {it.error && <div className="text-red-600">错误：{it.error}</div>}
                  {it.cancel_requested && <div className="text-slate-400">（用户单独停止过这个子agent）</div>}
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {detail && (
        <SubagentDetailModal
          item={detail}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  );
}

/** 子agent 对话详情弹窗：按需只拉这一个 task 的事件（不是整条 run 的 1.5MB 流），
 *  再交给 RunTraceInline 的 SubAgentModal 统一渲染——和聊天流里点开子agent 卡片看到的
 *  完全一样：思考过程 / 每次工具调用（可再点开看参数与结果）/ 收尾结论。
 *  还在跑的子agent 每 3 秒补拉一次，用户能看着它一步步往下走。 */
function SubagentDetailModal({ item, onClose }: { item: SubagentItem; onClose: () => void }) {
  const [group, setGroup] = useState<SubGroup | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const aliveRef = useRef(true);

  const running = item.status === "running" || item.status === "pending";
  const usageText = useMemo(() => {
    const u = item.run_usage;
    const base = `所属 run ${item.run_id.slice(0, 8)} · 耗时 ${secs(item.elapsed_ms)}`;
    return u
      ? `${base} · 本 run 子agent 合计 ${tok(u.calls)} 次调用 ${tok(u.total_tokens)} tok`
        + `（输入 ${tok(u.prompt_tokens)} + 输出 ${tok(u.completion_tokens)}；`
        + `llm_calls 无 task_id，只能到 run 粒度）`
      : base;
  }, [item]);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    api
      .events(item.run_id, 0, item.task_id)
      .then((raw: any) => {
        if (cancelled || !aliveRef.current) return;
        setGroup(subGroupFromEvents(item.task_id, raw, {
          title: item.name,
          question: item.question,
          status: item.status,
          error: item.error,
          // 结论由事件流里的 task_done.summary 带出来；老 run 没带，就退回清单里的
          // task.result_json.text（后端已回）。都没有才是真的没留结论。
          summary: item.summary || null,
          startedAt: item.started_at || item.created_at,
          finishedAt: item.finished_at,
        }));
      })
      .catch((e: any) => {
        if (cancelled || !aliveRef.current) return;
        setErr(String(e?.message || e));
      });
    return () => {
      cancelled = true;
    };
  }, [item, tick]);

  useEffect(() => {
    // 只在「确实还在跑」时补拉：拉回来的组一旦收口（有 status、不再 open）就停，
    // 免得弹窗一直开着白刷请求。
    const stillOpen = !group || group.open;
    if (!running || !stillOpen) return;
    const t = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => clearInterval(t);
  }, [running, group]);

  if (err) {
    return (
      <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6" onClick={onClose}>
        <div className="bg-white rounded-lg border border-slate-200 w-[520px] max-w-[92vw] p-4"
          onClick={(e) => e.stopPropagation()}>
          <div className="text-sm text-red-600">读取这个子agent 的对话详情失败：{err}</div>
          <button className="mt-2 text-xs text-slate-500 hover:text-slate-700" onClick={onClose}>关闭</button>
        </div>
      </div>
    );
  }
  if (!group) {
    // 加载态：用和 SubAgentModal 同样的尺寸/位置，避免"小框闪一下再变大框"的跳变
    return (
      <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6" onClick={onClose}>
        <div className="bg-white rounded-lg border border-slate-200 w-[720px] max-w-[92vw] max-h-[85vh] flex flex-col"
          onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center gap-2.5 px-4 py-2.5 border-b border-slate-100">
            <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">{item.name}</span>
            <button className="text-slate-400 hover:text-slate-600 text-lg leading-none shrink-0"
              onClick={onClose}>×</button>
          </div>
          {item.question && (
            <div className="px-4 py-2 text-[12px] text-slate-500 border-b border-slate-100 whitespace-pre-wrap break-words">
              {item.question}
            </div>
          )}
          <div className="p-4 text-sm text-slate-500">读取这个子agent 的对话详情…</div>
        </div>
      </div>
    );
  }
  return (
    <SubAgentModal
      group={group}
      runId={item.run_id}
      onClose={onClose}
      usageText={usageText}
    />
  );
}
