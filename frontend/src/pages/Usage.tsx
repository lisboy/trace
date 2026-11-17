import { useEffect, useMemo, useState } from "react";
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
/** 明细表默认先露这么多行：一屏能扫完，剩下的点开再看，不要一进页面就是几千像素长的表 */
const PREVIEW_ROWS = 8;

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
  const [usageLoading, setUsageLoading] = useState(false);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [days, setDays] = useState(30);
  const [agentF, setAgentF] = useState("");
  const [modelF, setModelF] = useState("");
  const [auditQ, setAuditQ] = useState("");
  const [auditOpen, setAuditOpen] = useState<string | null>(null);
  /** 明细表展开状态：默认只显示前 N 行 */
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  function loadUsage() {
    if (!currentTeamId) return;
    setUsageLoading(true);
    api.usage(days, agentF || undefined, modelF || undefined)
      .then(setUsage)
      .catch(() => {})
      .finally(() => setUsageLoading(false));
  }
  function loadAudit() {
    if (!currentTeamId) return;
    api.audit(100, auditQ.trim() || undefined).then((r) => setAudit(r.logs)).catch(() => setAudit([]));
  }
  // 用量与操作记录互不相干：分开加载、分开显示，右边不该等左边的聚合
  useEffect(loadUsage, [currentTeamId, days, agentF, modelF]);
  useEffect(loadAudit, [currentTeamId]);

  const totalTokens = usage ? usage.tokens.prompt + usage.tokens.completion : 0;
  const agentRows = useMemo(
    () => Object.entries(usage?.by_agent || {}).sort((a, b) => b[1].tokens - a[1].tokens),
    [usage]);
  const runs = usage?.runs || {};
  const runTotal = Object.values(runs).reduce((a, b) => a + b, 0);
  const maxDay = Math.max(1, ...(usage?.by_day || []).map((d) => d.tokens));
  const maxAgent = Math.max(1, ...agentRows.map(([, v]) => v.tokens));

  function toggle(key: string) {
    setExpanded((e) => ({ ...e, [key]: !e[key] }));
  }

  return (
    <div className="h-full overflow-hidden flex flex-col">
      {/* 工具栏常驻：滚到多深都能切窗口/切筛选，不用滚回去 */}
      <div className="shrink-0 px-5 pt-4 pb-3 border-b border-slate-200 bg-white/95 backdrop-blur z-10">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold">用量</h2>
          <div className="ml-auto flex flex-wrap items-center gap-1 text-xs">
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
              {(usage?.agent_facets || []).map((k) => (
                <option key={k} value={k}>{ROLE_NAMES[k] || k}</option>
              ))}
            </select>
            <select className="border rounded px-1.5 py-1 bg-white" value={modelF}
              onChange={(e) => setModelF(e.target.value)}
              title="只统计发往这个模型的调用">
              <option value="">全部模型</option>
              {(usage?.model_facets || []).map((k) => (
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
        </div>
        {usageLoading && usage && (
          <div className="mt-2 h-0.5 rounded bg-brand-500/20 overflow-hidden">
            <div className="h-full w-1/3 bg-brand-500 animate-pulse" />
          </div>
        )}
      </div>

      {/* 两栏各自滚动：左统计可长可短，右操作记录固定在自己的框里，不把整页拉长 */}
      <div className="flex-1 min-h-0 overflow-hidden grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_380px] gap-4 p-5">
        <div className="min-h-0 overflow-auto pr-1 space-y-4">
          {!usage && usageLoading && <UsageSkeleton />}
          {!usage && !usageLoading && (
            <div className="text-xs text-slate-400">用量数据加载中…</div>
          )}
          {usage && (
            <>
              {/* 总览 */}
              <Card title={`近 ${usage.days} 天用量`}>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-center">
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
                    <div className="flex items-end gap-[3px] h-20">
                      {usage.by_day!.map((d) => (
                        <div key={d.date}
                          className="flex-1 h-full flex flex-col justify-end group relative"
                          title={`${d.date} · ${d.calls} 次调用 · ${d.tokens.toLocaleString()} tokens`}>
                          {/* 柱子用 h-full + 百分比高度：父层必须给确定高度，
                              否则百分比高度按 auto 解析、整排柱子塌成 0 像素（原来就是这个毛病）。 */}
                          <div className="bg-brand-500 rounded-t group-hover:bg-brand-600" style={{
                            height: `${Math.max(3, (d.tokens / maxDay) * 100)}%`,
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
              </Card>

              {/* 按角色 */}
              <Card title="按角色" hint={`${agentRows.length} 个角色有调用`}>
                <div className="space-y-2">
                  {agentRows.map(([k, v]) => (
                    <div key={k} className="flex items-center gap-2 text-xs">
                      <span className="w-28 shrink-0 text-slate-500 truncate" title={k}>
                        {ROLE_NAMES[k] || k}
                        {/* 旧角色 key 的显示名都并成「助手」了，光看名字分不清是哪一行，
                            把原始 key 缀在后面（main 是本体，不用重复标）。 */}
                        {ROLE_NAMES[k] && k !== "main" && (
                          <span className="ml-1 text-[10px] text-slate-400 mono">{k}</span>
                        )}
                        {RETIRED_ROLES.includes(k) && (
                          <span className="ml-1 text-[10px] text-amber-600">停用</span>
                        )}
                      </span>
                      <div className="flex-1 h-2 bg-slate-100 rounded overflow-hidden">
                        <div className="h-2 bg-brand-500 rounded" style={{
                          width: `${Math.min(100, (v.tokens / maxAgent) * 100)}%`,
                        }} />
                      </div>
                      <span className="text-slate-400 w-44 shrink-0 text-right tabular-nums">
                        {v.tokens.toLocaleString()} tok · {v.calls} 次
                        {v.cache_hit_rate != null ? ` · 命中 ${v.cache_hit_rate}%` : ""}
                      </span>
                    </div>
                  ))}
                  {agentRows.length === 0 && <div className="text-xs text-slate-400">该窗口内没有调用</div>}
                </div>
              </Card>

              {/* 明细三张表：默认只露前 8 行，点开再看全部 */}
              <Card
                title="按模型"
                hint={`${usage.by_model.length} 个模型`}
                right={usage.by_model.length > PREVIEW_ROWS && (
                  <MoreBtn open={!!expanded.model} count={usage.by_model.length}
                    onClick={() => toggle("model")} />
                )}
              >
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
                    {(expanded.model ? usage.by_model : usage.by_model.slice(0, PREVIEW_ROWS)).map((m) => (
                      <tr key={m.model} className="border-t border-slate-50 hover:bg-slate-50/60">
                        <td className="py-1 mono text-slate-600" title={m.profile ? `档位 ${m.profile}` : "没有对应档位（走全局默认）"}>
                          {m.model}
                        </td>
                        <td className="py-1 text-slate-500 truncate max-w-[150px]">
                          {m.profile ? `${m.profile}${m.provider ? ` · ${m.provider}` : ""}` : "—"}
                        </td>
                        <td className="py-1 text-right tabular-nums">{m.calls}</td>
                        <td className="py-1 text-right tabular-nums">{m.prompt.toLocaleString()}</td>
                        <td className="py-1 text-right tabular-nums">{m.completion.toLocaleString()}</td>
                        <td className="py-1 text-right tabular-nums">
                          {m.cache_hit_rate != null ? `${m.cache_hit_rate}%` : "—"}
                        </td>
                        <td className="py-1 text-right tabular-nums">
                          {m.tokens_per_sec != null ? `${m.tokens_per_sec}` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>

              {(usage.by_project?.length ?? 0) > 0 && (
                <Card
                  title="按项目"
                  hint={`${usage.by_project!.length} 个项目`}
                  right={usage.by_project!.length > PREVIEW_ROWS && (
                    <MoreBtn open={!!expanded.project} count={usage.by_project!.length}
                      onClick={() => toggle("project")} />
                  )}
                >
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
                      {(expanded.project ? usage.by_project! : usage.by_project!.slice(0, PREVIEW_ROWS)).map((p) => (
                        <tr key={p.project_id} className="border-t border-slate-50 hover:bg-slate-50/60">
                          <td className="py-1 text-slate-600 truncate max-w-[180px]" title={p.project}>{p.project}</td>
                          <td className="py-1 text-right tabular-nums">{p.calls}</td>
                          <td className="py-1 text-right tabular-nums">{p.prompt.toLocaleString()}</td>
                          <td className="py-1 text-right tabular-nums">{p.completion.toLocaleString()}</td>
                          <td className="py-1 text-right tabular-nums">
                            {p.tokens_per_sec != null ? `${p.tokens_per_sec} tok/s` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              )}

              {(usage.by_session?.length ?? 0) > 0 && (
                <Card
                  title="按会话"
                  hint={`按输入 tokens 排序的前 ${usage.by_session!.length} 条`}
                  right={usage.by_session!.length > PREVIEW_ROWS && (
                    <MoreBtn open={!!expanded.session} count={usage.by_session!.length}
                      onClick={() => toggle("session")} />
                  )}
                >
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
                      {(expanded.session ? usage.by_session! : usage.by_session!.slice(0, PREVIEW_ROWS)).map((r) => (
                        <tr key={r.session_id} className="border-t border-slate-50 hover:bg-slate-50/60">
                          <td className="py-1 text-slate-600 truncate max-w-[240px]" title={r.title}>{r.title}</td>
                          <td className="py-1 text-right tabular-nums">{r.calls}</td>
                          <td className="py-1 text-right tabular-nums">{fmt(r.prompt)}</td>
                          <td className="py-1 text-right tabular-nums">{fmt(r.completion)}</td>
                          <td className={`py-1 text-right tabular-nums ${r.cache_hit_rate != null && r.cache_hit_rate < 70 ? "text-amber-600" : ""}`}>
                            {r.cache_hit_rate != null ? `${r.cache_hit_rate}%` : "—"}
                          </td>
                          <td className="py-1 text-right tabular-nums">
                            {r.tokens_per_sec != null ? `${r.tokens_per_sec}` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              )}
            </>
          )}
        </div>

        {/* 操作记录：固定高度自己滚，筛选框跟着卡片头一起常驻 */}
        <div className="min-h-0 overflow-hidden bg-white rounded border border-slate-200 flex flex-col">
          <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-slate-100">
            <span className="font-medium text-sm">操作记录</span>
            {audit && <span className="text-[11px] text-slate-400">{audit.length} 条</span>}
            <input
              className="ml-auto text-xs border rounded px-2 py-1 w-36"
              placeholder="筛选：改档位 / 合并 / 登录"
              value={auditQ}
              onChange={(e) => setAuditQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && loadAudit()}
            />
            <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={loadAudit}>筛选</button>
          </div>
          <div className="flex-1 min-h-0 overflow-auto px-3 py-1">
            {audit === null && (
              <div className="space-y-2 py-2">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="h-4 rounded bg-slate-100 animate-pulse" />
                ))}
              </div>
            )}
            {audit?.map((a) => (
              <div key={a.id} className="text-xs border-t border-slate-50 py-1 first:border-t-0">
                <button className="w-full flex gap-2 items-baseline text-left"
                  onClick={() => setAuditOpen(auditOpen === a.id ? null : a.id)}>
                  <span className="mono text-brand-700 shrink-0">{AUDIT_CN[a.action] || a.action}</span>
                  <span className="text-slate-500 truncate flex-1" title={a.target || ""}>{a.target}</span>
                  {a.actor && <span className="text-slate-400 shrink-0">{a.actor}</span>}
                  <span className="text-slate-300 shrink-0 tabular-nums">{localTime(a.created_at)}</span>
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
            {audit?.length === 0 && (
              <div className="text-xs text-slate-400 py-2">
                暂无记录。登录、改模型配置、合并改动都会记在这里。
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Card({ title, hint, right, children }: {
  title: string; hint?: string; right?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="bg-white rounded border border-slate-200 p-4">
      <div className="flex items-baseline gap-2 mb-3">
        <span className="font-medium text-sm">{title}</span>
        {hint && <span className="text-[11px] text-slate-400">{hint}</span>}
        {right && <div className="ml-auto">{right}</div>}
      </div>
      {children}
    </div>
  );
}

function MoreBtn({ open, count, onClick }: { open: boolean; count: number; onClick: () => void }) {
  return (
    <button className="text-[11px] text-brand-700 hover:underline" onClick={onClick}>
      {open ? "收起" : `全部 ${count} 行`}
    </button>
  );
}

/** 首屏骨架：以前这里只有一行「用量数据加载中…」，看着像空白页 */
function UsageSkeleton() {
  return (
    <div className="space-y-4">
      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="h-4 w-24 rounded bg-slate-100 animate-pulse mb-3" />
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-16 rounded bg-slate-100 animate-pulse" />
          ))}
        </div>
        <div className="h-20 rounded bg-slate-100 animate-pulse mt-4" />
      </div>
      <div className="bg-white rounded border border-slate-200 p-4 space-y-2">
        <div className="h-4 w-20 rounded bg-slate-100 animate-pulse mb-3" />
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-3 rounded bg-slate-100 animate-pulse" />
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value, unit, sub }: { label: string; value: number | string; unit?: string; sub?: string }) {
  return (
    <div className="bg-slate-50 rounded p-3">
      <div className="text-lg font-bold text-brand-700 tabular-nums">
        {typeof value === "number" ? value.toLocaleString() : value}
        {unit && <span className="text-[11px] font-normal text-slate-400 ml-0.5">{unit}</span>}
      </div>
      <div className="text-[11px] text-slate-400">{label}</div>
      {sub && <div className="text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}
