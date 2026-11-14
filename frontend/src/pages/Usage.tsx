import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";

type Usage = {
  days: number;
  tokens: {
    prompt: number; completion: number; calls: number; latency_ms: number;
    cached?: number; cache_hit_rate?: number | null;
    tokens_per_sec: number | null;
  };
  runs?: Record<string, number>;
  ledger: Record<string, number>;
  by_model: {
    model: string; calls: number; prompt: number; completion: number; latency_ms: number;
    cached?: number; cache_hit_rate?: number | null; tokens_per_sec: number | null;
    profile?: string; provider?: string | null; kind?: string;
  }[];
  by_project?: { project_id: string; project: string; calls: number; prompt: number; completion: number; latency_ms: number; tokens_per_sec: number | null }[];
  by_day?: { date: string; calls: number; tokens: number; completion: number; tokens_per_sec: number | null }[];
  by_session?: {
    session_id: string; title: string; calls: number; prompt: number; completion: number;
    latency_ms: number; cached?: number; cache_hit_rate?: number | null; tokens_per_sec: number | null;
  }[];
  agent_facets?: string[];
  model_facets?: string[];
  by_agent: Record<string, {
    calls: number; tokens: number; prompt?: number; cached?: number;
    cache_hit_rate?: number | null; tokens_per_sec?: number | null;
  }>;
};
type AuditRow = {
  id: string; action: string; target: string | null; created_at: string;
  actor?: string | null; ip?: string | null; detail?: Record<string, any>;
};

const ROLE_NAMES: Record<string, string> = {
  main: "助手",
  // 旧用量统计里的角色 key，统一显示成「助手」
  planner: "助手", artist: "助手", voice: "助手",
};
const RETIRED_ROLES = ["developer", "tester", "tester_qa"];
const AUDIT_CN: Record<string, string> = {
  "auth.login": "登录", "auth.login_failed": "登录失败",
  "provider.add": "新增供应商", "provider.update": "修改供应商", "provider.delete": "删除供应商",
  "profile.add": "新增模型档位", "profile.update": "修改模型档位", "profile.delete": "删除模型档位",
  "agent.update": "修改智能体", "merge": "合并改动到主分支",
};
const DAY_OPTS = [7, 30, 90, 365];

function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** 后端存 UTC，显示按本地时间（直接截字符串会把凌晨的操作显示到前一天） */
function localTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.slice(0, 16);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function Usage() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [days, setDays] = useState(30);
  const [agentF, setAgentF] = useState("");
  const [modelF, setModelF] = useState("");
  const [auditQ, setAuditQ] = useState("");
  const [auditOpen, setAuditOpen] = useState<string | null>(null);

  function refresh() {
    if (!currentTeamId) return;
    api.usage(days, agentF || undefined, modelF || undefined).then(setUsage).catch(() => {});
    api.audit(100, auditQ.trim() || undefined).then((r) => setAudit(r.logs)).catch(() => {});
  }
  useEffect(refresh, [currentTeamId, days, agentF, modelF]);

  const totalTokens = usage ? usage.tokens.prompt + usage.tokens.completion : 0;
  const agentRows = Object.entries(usage?.by_agent || {}).sort((a, b) => b[1].tokens - a[1].tokens);
  const runs = usage?.runs || {};
  const runTotal = Object.values(runs).reduce((a, b) => a + b, 0);
  const runDone = (runs.succeeded || 0) + (runs.failed || 0);
  const maxDay = Math.max(1, ...(usage?.by_day || []).map((d) => d.tokens));

  return (
    <div className="h-full overflow-auto p-5">
      <div className="flex items-center gap-2 mb-4">
        <h2 className="text-lg font-semibold">用量</h2>
        {usage && (
          <div className="ml-auto flex items-center gap-1 text-xs">
            <span className="text-slate-400 mr-1">统计窗口</span>
            {DAY_OPTS.map((d) => (
              <button key={d}
                className={`ga-btn ga-btn-sm ${days === d ? "ga-btn-primary" : "ga-btn-secondary"}`}
                onClick={() => setDays(d)}>
                {d} 天
              </button>
            ))}
            <span className="text-slate-400 mr-1 ml-2">类型筛选</span>
            <select className="border rounded px-1.5 py-1 bg-white" value={agentF}
              onChange={(e) => setAgentF(e.target.value)}
              title="只统计这个角色发起的调用">
              <option value="">全部角色</option>
              {(usage.agent_facets || []).map((k) => (
                <option key={k} value={k}>{ROLE_NAMES[k] || k}</option>
              ))}
            </select>
            <select className="border rounded px-1.5 py-1 bg-white" value={modelF}
              onChange={(e) => setModelF(e.target.value)}
              title="只统计发往这个模型的调用">
              <option value="">全部模型</option>
              {(usage.model_facets || []).map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
            {(agentF || modelF) && (
              <button className="ga-btn ga-btn-secondary ga-btn-sm"
                onClick={() => { setAgentF(""); setModelF(""); }}>
                清除
              </button>
            )}
          </div>
        )}
      </div>

      {/* 用量 + 审计 */}
      {usage && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
          <div className="bg-white rounded border border-slate-200 p-4">
            <div className="font-medium mb-3 text-sm">近 {usage.days} 天用量</div>
            <div className="grid grid-cols-4 gap-3 text-center">
              <Stat label="LLM 调用" value={usage.tokens.calls}
                sub={runTotal ? `${runTotal} 次任务` : undefined} />
              <Stat label="输入 tokens" value={fmt(usage.tokens.prompt)} />
              <Stat label="输出 tokens" value={fmt(usage.tokens.completion)} />
              <Stat
                label="平均生成速度"
                value={usage.tokens.tokens_per_sec != null ? usage.tokens.tokens_per_sec : "—"}
                unit={usage.tokens.tokens_per_sec != null ? "tok/s" : undefined}
              />
            </div>
            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
              <span>合计 {fmt(totalTokens)} tokens</span>
              <span>·</span>
              <span title={usage.tokens.cached ? `命中缓存的输入 ${fmt(usage.tokens.cached)}` : "供应商没上报缓存字段"}>
                缓存命中率 {usage.tokens.cache_hit_rate != null ? `${usage.tokens.cache_hit_rate}%` : "未知"}
              </span>
              {Object.entries(runs).filter(([k]) => k !== "succeeded" && k !== "failed").length > 0 && (
                <span>· 进行中/其它 {Object.entries(runs).filter(([k]) => k !== "succeeded" && k !== "failed").reduce((a, [, v]) => a + v, 0)}</span>
              )}
              {runTotal > 0 && (
                <>
                  <span>·</span>
                  <span className={runs.failed ? "text-red-600" : "text-emerald-700"}>
                    成功 {runs.succeeded || 0} / 失败 {runs.failed || 0}
                  </span>
                </>
              )}
            </div>

            {/* 逐日趋势：只有总量看不出是哪天烧掉的 */}
            {(usage.by_day?.length ?? 0) > 0 && (
              <div className="mt-4">
                <div className="text-xs font-medium text-slate-500 mb-1.5">按天 tokens</div>
                <div className="flex items-end gap-[3px] h-16">
                  {usage.by_day!.map((d) => (
                    <div key={d.date} className="flex-1 flex flex-col justify-end"
                      title={`${d.date} · ${d.calls} 次调用 · ${d.tokens.toLocaleString()} tokens`}>
                      <div className="bg-brand-500 rounded-t" style={{
                        height: `${Math.max(2, (d.tokens / maxDay) * 100)}%`,
                        opacity: d.tokens ? 1 : 0.25,
                      }} />
                    </div>
                  ))}
                </div>
                <div className="flex justify-between text-[10px] text-slate-400 mt-1">
                  <span>{usage.by_day![0]?.date?.slice(5)}</span>
                  <span>{usage.by_day![usage.by_day!.length - 1]?.date?.slice(5)}</span>
                </div>
              </div>
            )}

            {/* 按角色 */}
            <div className="mt-4 space-y-2">
              {agentRows.map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 text-xs">
                  <span className="w-20 text-slate-500">
                    {RETIRED_ROLES.includes(k) ? `${k}（已停用）` : ROLE_NAMES[k] || k}
                  </span>
                  <div className="flex-1 h-2 bg-slate-100 rounded">
                    <div className="h-2 bg-brand-500 rounded" style={{
                      width: `${Math.min(100, (v.tokens / Math.max(1, totalTokens)) * 100)}%`,
                    }} />
                  </div>
                  <span className="text-slate-400 w-44 text-right">
                    {v.tokens.toLocaleString()} tok · {v.calls} 次
                    {v.cache_hit_rate != null ? ` · 命中 ${v.cache_hit_rate}%` : ""}
                  </span>
                </div>
              ))}
              {agentRows.length === 0 && <div className="text-xs text-slate-400">该窗口内没有调用</div>}
            </div>

            {/* 按项目 */}
            {(usage.by_project?.length ?? 0) > 0 && (
              <div className="mt-4">
                <div className="text-xs font-medium text-slate-500 mb-1.5">按项目</div>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-400 text-left">
                      <th className="py-1 font-normal">项目</th>
                      <th className="py-1 font-normal text-right">调用</th>
                      <th className="py-1 font-normal text-right">输入</th>
                      <th className="py-1 font-normal text-right">输出</th>
                      <th className="py-1 font-normal text-right">速度</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_project!.map((p) => (
                      <tr key={p.project_id} className="border-t border-slate-50">
                        <td className="py-1 text-slate-600 truncate max-w-[140px]" title={p.project}>{p.project}</td>
                        <td className="py-1 text-right">{p.calls}</td>
                        <td className="py-1 text-right">{p.prompt.toLocaleString()}</td>
                        <td className="py-1 text-right">{p.completion.toLocaleString()}</td>
                        <td className="py-1 text-right">
                          {p.tokens_per_sec != null ? `${p.tokens_per_sec} tok/s` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* 按模型：带上档位/供应商，否则一堆裸模型名认不出走的哪个站点 */}
            {usage.by_model.length > 0 && (
              <div className="mt-4">
                <div className="text-xs font-medium text-slate-500 mb-1.5">按模型</div>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-400 text-left">
                      <th className="py-1 font-normal">模型</th>
                      <th className="py-1 font-normal">档位 / 供应商</th>
                      <th className="py-1 font-normal text-right">调用</th>
                      <th className="py-1 font-normal text-right">输入</th>
                      <th className="py-1 font-normal text-right">输出</th>
                      <th className="py-1 font-normal text-right">缓存</th>
                      <th className="py-1 font-normal text-right">速度</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_model.map((m) => (
                      <tr key={m.model} className="border-t border-slate-50">
                        <td className="py-1 mono text-slate-600" title={m.profile ? `档位 ${m.profile}` : "没有对应档位（走全局默认）"}>
                          {m.model}
                        </td>
                        <td className="py-1 text-slate-500 truncate max-w-[150px]">
                          {m.profile ? `${m.profile}${m.provider ? ` · ${m.provider}` : ""}` : "—"}
                        </td>
                        <td className="py-1 text-right">{m.calls}</td>
                        <td className="py-1 text-right">{m.prompt.toLocaleString()}</td>
                        <td className="py-1 text-right">{m.completion.toLocaleString()}</td>
                        <td className="py-1 text-right">
                          {m.cache_hit_rate != null ? `${m.cache_hit_rate}%` : "—"}
                        </td>
                        <td className="py-1 text-right">
                          {m.tokens_per_sec != null ? `${m.tokens_per_sec}` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* 按会话：哪条会话把整体命中率/用量拉低的一眼可见 */}
            {(usage.by_session?.length ?? 0) > 0 && (
              <div className="mt-4">
                <div className="text-xs font-medium text-slate-500 mb-1.5">
                  按会话
                  <span className="font-normal ml-1">
                    按输入 tokens 排序的前 {usage.by_session!.length} 条会话
                  </span>
                </div>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-400 text-left">
                      <th className="py-1 font-normal">会话</th>
                      <th className="py-1 font-normal text-right">调用</th>
                      <th className="py-1 font-normal text-right">输入</th>
                      <th className="py-1 font-normal text-right">输出</th>
                      <th className="py-1 font-normal text-right">缓存</th>
                      <th className="py-1 font-normal text-right">速度</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_session!.map((r) => (
                      <tr key={r.session_id} className="border-t border-slate-50">
                        <td className="py-1 text-slate-600 truncate max-w-[200px]" title={r.title}>{r.title}</td>
                        <td className="py-1 text-right">{r.calls}</td>
                        <td className="py-1 text-right">{fmt(r.prompt)}</td>
                        <td className="py-1 text-right">{fmt(r.completion)}</td>
                        <td className={`py-1 text-right ${r.cache_hit_rate != null && r.cache_hit_rate < 70 ? "text-amber-600" : ""}`}>
                          {r.cache_hit_rate != null ? `${r.cache_hit_rate}%` : "—"}
                        </td>
                        <td className="py-1 text-right">
                          {r.tokens_per_sec != null ? `${r.tokens_per_sec}` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* 审计 */}
          <div className="bg-white rounded border border-slate-200 p-4">
            <div className="flex items-center gap-2 mb-2">
              <span className="font-medium text-sm">操作记录</span>
              <input
                className="ml-auto text-xs border rounded px-2 py-1 w-40"
                placeholder="筛选：改档位 / 合并 / 登录"
                value={auditQ}
                onChange={(e) => setAuditQ(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && refresh()}
              />
              <button className="ga-btn ga-btn-secondary ga-btn-sm"
                onClick={refresh}>筛选</button>
            </div>
            <div className="space-y-1 max-h-[520px] overflow-auto">
              {audit.map((a) => (
                <div key={a.id} className="text-xs border-t border-slate-50 py-1">
                  <button className="w-full flex gap-2 items-baseline text-left"
                    onClick={() => setAuditOpen(auditOpen === a.id ? null : a.id)}>
                    <span className="mono text-brand-700 shrink-0">{AUDIT_CN[a.action] || a.action}</span>
                    <span className="text-slate-500 truncate flex-1" title={a.target || ""}>{a.target}</span>
                    {a.actor && <span className="text-slate-400 shrink-0">{a.actor}</span>}
                    <span className="text-slate-300 shrink-0">{localTime(a.created_at)}</span>
                  </button>
                  {auditOpen === a.id && (
                    <div className="mt-1 ml-2 text-[11px] text-slate-500 bg-slate-50 rounded p-2 space-y-0.5">
                      <div>原始动作：<span className="mono">{a.action}</span></div>
                      {a.ip && <div>来源：<span className="mono">{a.ip}</span></div>}
                      {a.detail && Object.keys(a.detail).length > 0 && (
                        <div className="mono break-all">{JSON.stringify(a.detail)}</div>
                      )}
                    </div>
                  )}
                </div>
              ))}
              {audit.length === 0 && (
                <div className="text-xs text-slate-400">
                  暂无记录。登录、改模型配置、合并改动都会记在这里。
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      {!usage && <div className="text-xs text-slate-400">用量数据加载中…</div>}
    </div>
  );
}

function Stat({ label, value, unit, sub }: { label: string; value: number | string; unit?: string; sub?: string }) {
  return (
    <div className="bg-slate-50 rounded p-3">
      <div className="text-lg font-bold text-brand-700">
        {typeof value === "number" ? value.toLocaleString() : value}
        {unit && <span className="text-[11px] font-normal text-slate-400 ml-0.5">{unit}</span>}
      </div>
      <div className="text-[11px] text-slate-400">{label}</div>
      {sub && <div className="text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}
