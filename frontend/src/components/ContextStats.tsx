import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { type ContextAgentLatest, type ContextStats as Stats } from "../types";

/* 上下文统计：这一轮/这一会话的上下文被谁占满、什么时候开始压缩、模型爱用哪些工具。
   所有数字来自 llm_transcripts.stats_json（发出去之前就地统计的占比）+ llm_calls（端点真实上报）。 */

const CAT_COLOR: Record<string, string> = {
  系统提示词: "#6366f1",
  注入内容: "#f59e0b",
  用户消息: "#10b981",
  助手消息: "#0ea5e9",
  工具结果: "#a855f7",
  工具定义: "#64748b",
  其他: "#cbd5e1",
};

function fmtTok(n: number): string {
  if (!n) return "0";
  if (n < 1000) return String(Math.round(n));
  if (n < 100000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n / 1000)}k`;
}

function catColor(name: string): string {
  return CAT_COLOR[name] || "#94a3b8";
}

/** 横向占比条：一段一段按 token 数分宽，配一张同色表 */
function ShareBar({ cats }: { cats: Record<string, { tokens: number }> }) {
  const entries = Object.entries(cats || {}).sort((a, b) => b[1].tokens - a[1].tokens);
  const total = entries.reduce((a, [, v]) => a + (v.tokens || 0), 0);
  if (!total) return <div className="text-[11px] text-slate-400">暂无数据。</div>;
  return (
    <div>
      <div className="flex h-2.5 rounded overflow-hidden bg-slate-100">
        {entries.map(([k, v]) => (
          <div
            key={k}
            title={`${k} ${fmtTok(v.tokens)} tok（${((v.tokens / total) * 100).toFixed(1)}%）`}
            style={{ width: `${(v.tokens / total) * 100}%`, background: catColor(k) }}
          />
        ))}
      </div>
      <table className="w-full text-[11px] mt-1.5">
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} className="text-slate-600">
              <td className="w-3">
                <span className="inline-block w-2 h-2 rounded-sm" style={{ background: catColor(k) }} />
              </td>
              <td>{k}</td>
              <td className="text-right mono">{fmtTok(v.tokens)}</td>
              <td className="text-right text-slate-400 w-12">
                {((v.tokens / total) * 100).toFixed(1)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 占用条：窗口是满格，预算线画在 75%，越线就开始丢历史 */
function OccupancyBar({ a }: { a: ContextAgentLatest }) {
  const pct = Math.min(100, a.used_pct);
  const budgetPct = Math.min(100, (a.budget_tokens / (a.context_window || 1)) * 100);
  return (
    <div className="relative h-3 rounded bg-slate-100 overflow-hidden">
      <div
        className={`absolute inset-y-0 left-0 ${a.will_compress ? "bg-orange-400" : "bg-emerald-400"}`}
        style={{ width: `${pct}%` }}
      />
      <div className="absolute inset-y-0 w-px bg-slate-500" style={{ left: `${budgetPct}%` }} />
    </div>
  );
}

/** 逐轮输入量折线：预算线画出来，压缩过的轮次点橙点 */
function TrendChart({ points, windowSize, budget }: {
  points: { y: number; compressed: boolean; agent: string; round: number; estOnly: boolean }[];
  windowSize: number;
  budget: number;
}) {
  const W = 620;
  const H = 120;
  const pad = 4;
  const maxY = Math.max(windowSize, ...points.map((p) => p.y), 1);
  const x = (i: number) => pad + (i / Math.max(1, points.length - 1)) * (W - pad * 2);
  const y = (v: number) => H - pad - (v / maxY) * (H - pad * 2);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${H - pad} L${x(0).toFixed(1)},${H - pad} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[120px]" preserveAspectRatio="none">
      <rect x="0" y={y(windowSize)} width={W} height={H} fill="#f8fafc" />
      <path d={area} fill="#0ea5e922" />
      <path d={line} fill="none" stroke="#0ea5e9" strokeWidth="1.5" />
      <line x1="0" y1={y(budget)} x2={W} y2={y(budget)} stroke="#f97316" strokeWidth="1" strokeDasharray="4 3" />
      {points.map((p, i) =>
        p.compressed ? (
          <circle key={i} cx={x(i)} cy={y(p.y)} r="2.4" fill="#f97316">
            <title>{`${p.agent} 第${p.round}轮 ${fmtTok(p.y)} tok（本轮触发过压缩）`}</title>
          </circle>
        ) : null,
      )}
      {points.map((p, i) => (
        <circle key={`h${i}`} cx={x(i)} cy={y(p.y)} r="3" fill="transparent">
          <title>{`${p.agent} 第${p.round}轮 ${fmtTok(p.y)} tok${p.estOnly ? "（估算）" : ""}`}</title>
        </circle>
      ))}
    </svg>
  );
}

export default function ContextStatsView({
  sessionId,
  runId,
  refreshKey = 0,
}: {
  sessionId: string;
  runId?: string | null;
  refreshKey?: number | string;
}) {
  const [scopeRun, setScopeRun] = useState<boolean>(!!runId);
  const [data, setData] = useState<Stats | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    setErr("");
    api
      .contextStats(sessionId, scopeRun ? runId || null : null)
      .then((r) => alive && setData(r))
      .catch((e: any) => alive && setErr(e?.message || String(e)));
    return () => {
      alive = false;
    };
  }, [sessionId, runId, scopeRun, refreshKey]);

  const latest = data?.latest || null;
  const toolRows = useMemo(
    () => Object.entries(data?.tool_calls || {}).sort((a, b) => b[1] - a[1]).slice(0, 12),
    [data],
  );
  const maxTool = toolRows.length ? toolRows[0][1] : 1;

  if (err) return <div className="p-3 text-xs text-red-600">统计载入失败：{err}</div>;
  if (!sessionId) return <div className="p-3 text-sm text-slate-400">等待运行信息…</div>;
  if (!data || !data.rounds) {
    return (
      <div className="p-3 text-sm text-slate-400">
        还没有上下文往返记录。后端启动并建表之后的运行才会记录现场。
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-auto p-3 space-y-3">
      <div className="flex items-center gap-1 text-[11px]">
        {runId && (
          <>
            <button
              className={`ga-btn ga-btn-sm ${scopeRun ? "ga-btn-primary" : "ga-btn-secondary"}`}
              onClick={() => setScopeRun(true)}
            >
              本次运行
            </button>
            <button
              className={`ga-btn ga-btn-sm ${!scopeRun ? "ga-btn-primary" : "ga-btn-secondary"}`}
              onClick={() => setScopeRun(false)}
            >
              整个会话
            </button>
          </>
        )}
        <span className="text-slate-400 ml-2">
          {data.rounds} 轮往返 · {data.compress_rounds} 轮触发压缩
          {data.summarize_rounds > 0 &&
            ` · ${data.summarize_rounds} 次摘要调用（约 ${fmtTok(data.summarize_est_tokens)} tok，不计入用量表）`}
          {data.history_capped && " · 仅统计最近 1000 轮"}
        </span>
      </div>

      {/* 当前占用 */}
      {latest && (
        <div className="bg-white rounded border border-slate-200 p-3">
          <div className="text-xs font-medium mb-2 flex items-center gap-2">
            上下文占用
            <span className="text-slate-400 font-normal">
              {latest.agent_label} · 第 {latest.round_no} 轮 · {latest.model}
            </span>
          </div>
          <div className="flex items-baseline gap-2 mb-1">
            <span className="text-lg mono text-slate-800">{fmtTok(latest.prompt_tokens)}</span>
            <span className="text-xs text-slate-400">
              / {fmtTok(latest.context_window)} tok 窗口（{latest.used_pct}%）
            </span>
            {latest.est_prompt_tokens > 0 && latest.est_prompt_tokens !== latest.prompt_tokens && (
              <span className="text-[11px] text-slate-400">本地估算 {fmtTok(latest.est_prompt_tokens)}</span>
            )}
          </div>
          <OccupancyBar a={latest} />
          <div className="text-[11px] text-slate-500 mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
            <span>
              预算线 {fmtTok(latest.budget_tokens)}（达此值即把较早历史合并成摘要）
            </span>
            <span className={latest.will_compress ? "text-orange-600" : "text-emerald-600"}>
              {latest.will_compress ? "压缩中：本轮把 " + latest.dropped_msgs + " 条较早历史并入了摘要" : "距压缩还有 " + fmtTok(Math.max(0, latest.budget_tokens - latest.prompt_tokens))}
            </span>
            <span>
              消息 {latest.messages} 条 · 工具 {latest.tools} 个 · 图片 {latest.images} 张
            </span>
          </div>
          {!latest.window_configured && (
            <div className="text-[11px] text-amber-600 mt-1">
              该档位未配置 context_window，按 {fmtTok(latest.context_window)} 估算（当前主流模型的常见量级）。
              真实窗口更小会在超限时自动按半预算重压；更大就在设置页填上，免得白压。
            </div>
          )}
          {data.by_agent.length > 1 && (
            <table className="w-full text-[11px] mt-2">
              <thead>
                <tr className="text-slate-400 text-left">
                  <th className="font-normal">角色</th>
                  <th className="font-normal text-right">输入 tok</th>
                  <th className="font-normal text-right">占用</th>
                  <th className="font-normal text-right">轮次</th>
                </tr>
              </thead>
              <tbody>
                {data.by_agent.map((a) => (
                  <tr key={a.agent_key} className="text-slate-600">
                    <td>{a.agent_label}</td>
                    <td className="text-right mono">{fmtTok(a.prompt_tokens)}</td>
                    <td className="text-right mono">{a.used_pct}%</td>
                    <td className="text-right mono">{a.round_no}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* 当前请求构成 */}
      <div className="bg-white rounded border border-slate-200 p-3">
        <div className="text-xs font-medium mb-2">
          最近一次请求构成
          <span className="text-slate-400 font-normal ml-1">发给模型的全部内容按来源拆开</span>
        </div>
        <ShareBar cats={data.categories_current} />
      </div>

      {/* 趋势 */}
      <div className="bg-white rounded border border-slate-200 p-3">
        <div className="text-xs font-medium mb-1">
          逐轮输入趋势
          <span className="text-slate-400 font-normal ml-1">
            虚线=预算线 · 橙点=该轮触发压缩 · 灰底=窗口上限
          </span>
        </div>
        <TrendChart
          windowSize={latest?.context_window || 32768}
          budget={latest?.budget_tokens || 24576}
          points={data.trend.map((t) => ({
            y: t.prompt_tokens || t.est_tokens,
            compressed: t.compressed,
            agent: t.agent_key,
            round: t.round_no,
            estOnly: t.est_only,
          }))}
        />
      </div>

      {/* 累计构成 */}
      <div className="bg-white rounded border border-slate-200 p-3">
        <div className="text-xs font-medium mb-2">
          跨轮累计消耗
          <span className="text-slate-400 font-normal ml-1">
            同一份历史每轮重发，这里看钱花在哪儿（估算值）
          </span>
        </div>
        <ShareBar
          cats={Object.fromEntries(
            Object.entries(data.categories_cumulative).map(([k, v]) => [k, { tokens: v.tokens }]),
          )}
        />
      </div>

      {/* 工具调用分布 */}
      <div className="bg-white rounded border border-slate-200 p-3">
        <div className="text-xs font-medium mb-2">
          工具调用分布
          <span className="text-slate-400 font-normal ml-1">模型在这些轮次里发起了什么</span>
        </div>
        {toolRows.length === 0 && <div className="text-[11px] text-slate-400">本轮范围没有工具调用。</div>}
        <div className="space-y-1">
          {toolRows.map(([name, n]) => (
            <div key={name} className="flex items-center gap-2 text-[11px]">
              <span className="w-32 truncate mono text-slate-600">{name}</span>
              <div className="flex-1 h-2 bg-slate-100 rounded overflow-hidden">
                <div className="h-full bg-purple-400" style={{ width: `${(n / maxTool) * 100}%` }} />
              </div>
              <span className="w-8 text-right mono text-slate-500">{n}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
