/** 工具调用追踪：事件 → 按 agent 分组的折叠步骤列表（RunPanel 与聊天流内嵌条共用）。 */
import { useMemo, useState } from "react";
import { ROLE_LABEL } from "../types";
import { diffFromToolArgs, diffStats, lineDiff } from "../diff";
import DiffView from "./DiffView";
import CopyButton from "./CopyButton";
import ShotThumbs from "./ShotThumbs";

export const TOOL_META: Record<string, { label: string; color: string }> = {
  write_file: { label: "写文件", color: "bg-blue-50 text-blue-700" },
  edit_file: { label: "编辑文件", color: "bg-blue-50 text-blue-700" },
  read_file: { label: "读文件", color: "bg-slate-100 text-slate-600" },
  run_command: { label: "执行命令", color: "bg-amber-50 text-amber-700" },
  web_search: { label: "联网搜索", color: "bg-teal-50 text-teal-700" },
  web_fetch: { label: "读取网页", color: "bg-teal-50 text-teal-700" },
  start_service: { label: "启动服务", color: "bg-cyan-50 text-cyan-700" },
  service_logs: { label: "服务日志", color: "bg-cyan-50 text-cyan-700" },
  stop_service: { label: "停止服务", color: "bg-cyan-50 text-cyan-700" },
  screenshot_page: { label: "截图验收", color: "bg-cyan-50 text-cyan-700" },
  view_image: { label: "查看图片", color: "bg-cyan-50 text-cyan-700" },
  view_images: { label: "合并看图", color: "bg-cyan-50 text-cyan-700" },
  generate_image: { label: "生成图片", color: "bg-pink-50 text-pink-700" },
  generate_audio: { label: "生成音频", color: "bg-pink-50 text-pink-700" },
  // 旧版派发工具名（历史 run 轨迹仍会出现）
  dispatch_art: { label: "派美术出图", color: "bg-pink-50 text-pink-700" },
  dispatch_audio: { label: "派配音出音频", color: "bg-pink-50 text-pink-700" },
  upload_asset: { label: "上传素材", color: "bg-emerald-50 text-emerald-700" },
  tts: { label: "语音合成", color: "bg-purple-50 text-purple-700" },
  report_to_user: { label: "汇报结果", color: "bg-emerald-50 text-emerald-700" },
};

export type ToolStep = {
  key: number;
  tool: string;
  args?: Record<string, unknown>;
  result?: string;
  /** 工具调用发起时间（ISO）：显示「HH:MM:SS 开始」用 */
  at?: string;
  /** 本步执行耗时（毫秒）：优先来自 tool_result 事件 payload.duration_ms（精确计时） */
  durationMs?: number;
  /** durationMs 是否只是按事件时间戳估算（老数据/未重启时）：渲染成「≈Ns」以示区别 */
  durationEstimated?: boolean;
};

export type ToolGroup = {
  agent: string;
  steps: ToolStep[];
  done: boolean;
};

/** 后端时间 UTC；旧格式无时区标记按 UTC 归一化，避免差 8 小时。 */
function parseTs(s: string): number {
  return new Date(
    /[Z+]|-\d{2}:?\d{2}$/.test(s) ? s.replace(" ", "T") : s.replace(" ", "T") + "Z",
  ).getTime();
}

/** 本地「时:分:秒」。 */
function localClock(iso?: string): string {
  if (!iso) return "";
  const t = parseTs(iso);
  if (!t || Number.isNaN(t)) return "";
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 毫秒 → 人类可读耗时：<1s 用 XXXms，随后 X.Xs，超过 1 分钟用 Xm Ys。 */
export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  return `${m}m ${rs}s`;
}

/** 把 tool_call / tool_result 事件按 agent 分组、按顺序配对成步骤列表。 */
export function buildToolGroups(events: { type: string; agent?: string | null; payload?: any }[]): ToolGroup[] {
  const groups: ToolGroup[] = [];
  let cur: ToolGroup | null = null;
  let key = 0;
  for (const e of events) {
    const p = e.payload || {};
    if (e.type === "tool_call") {
      const agent = String(e.agent || p.agent_key || "");
      if (!cur || cur.agent !== agent) {
        cur = { agent, steps: [], done: false };
        groups.push(cur);
      }
      cur.steps.push({ key: key++, tool: String(p.tool || ""), args: p.args, at: (e as any).created_at });
    } else if (e.type === "tool_result") {
      const agent = String(e.agent || p.agent_key || "");
      const g = [...groups].reverse().find((gr) => gr.agent === agent);
      const step =
        g &&
        [...g.steps]
          .reverse()
          .find((s) => s.tool === String(p.tool || "") && s.result === undefined);
      if (g && step) {
        step.result = String(p.result || "");
        if (typeof p.duration_ms === "number") {
          step.durationMs = p.duration_ms;
          step.durationEstimated = false;
        } else if (step.at && (e as any).created_at) {
          // 老数据没有精确耗时：用「调用→结果」的事件时间戳估算（秒级，标 ≈）
          const d = parseTs((e as any).created_at) - parseTs(step.at);
          if (Number.isFinite(d) && d >= 0) { step.durationMs = d; step.durationEstimated = true; }
        }
        if (g.steps.every((s) => s.result !== undefined)) g.done = true;
      }
    }
  }
  return groups;
}

function argsSummary(args?: Record<string, unknown>): string {
  if (!args) return "";
  // 看图类工具：只显示路径（多图显示张数），别把 {"path": "..."} 整段 JSON 甩出来
  if (typeof args.path === "string" && args.path) return args.path;
  if (Array.isArray(args.paths)) return `${args.paths.length} 张`;
  const s = JSON.stringify(args);
  if (s.length <= 90) return s;
  const content = args.content ?? args.report ?? args.result;
  if (typeof content === "string" && s.length - content.length < 400) {
    const head = JSON.stringify({ ...args, [content === args.report ? "report" : "content"]: undefined });
    return `${head.slice(1, 60)}… 内容 ${content.length} 字`;
  }
  return `${s.slice(0, 88)}…`;
}

export function StepRow({ s, runId }: { s: ToolStep; runId?: string }) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const meta = TOOL_META[s.tool] || { label: s.tool, color: "bg-slate-100 text-slate-600" };
  const pending = s.result === undefined;
  const failed = (s.result || "").startsWith("工具") && (s.result || "").includes("不在你的权限白名单内");
  // 写文件类工具：直接把「改之前 / 改之后」的差异摆在展开区里
  const dp = useMemo(() => diffFromToolArgs(s.tool, s.args), [s.tool, s.args]);
  const rows = useMemo(() => (dp ? lineDiff(dp.before, dp.after) : []), [dp]);
  const st = useMemo(() => diffStats(rows), [rows]);
  const isWrite = !!dp;
  return (
    <div className="text-xs border-l-2 border-slate-100 hover:border-brand-300 pl-2 py-0.5">
      <button
        className="w-full flex items-center gap-1.5 text-left min-w-0"
        onClick={() => setOpen(!open)}
      >
        <span className={`shrink-0 px-1 rounded mono ${meta.color}`}>{meta.label}</span>
        {isWrite ? (
          <>
            <span className="truncate flex-1 mono text-slate-600">{dp!.path}</span>
            <span className="shrink-0 mono text-emerald-600">+{st.added}</span>
            <span className="shrink-0 mono text-rose-500">−{st.deleted}</span>
          </>
        ) : (
          <span className="text-slate-400 truncate flex-1 mono">{argsSummary(s.args)}</span>
        )}
        {/* 每条命令的「开始时刻」：一眼看清是先跑哪条、各在什么时间点 */}
        {localClock(s.at) && (
          <span className="shrink-0 text-[10.5px] text-slate-400 mono" title="这条命令开始执行的时间">
            {localClock(s.at)}
          </span>
        )}
        {pending ? (
          <span className="shrink-0 text-amber-500 animate-pulse">执行中…</span>
        ) : (
          <>
            {/* 该条命令的执行耗时（精确计时来自后端；老数据按事件时间戳估算则标 ≈，且只在 ≥1s 时显示以免误导） */}
            {typeof s.durationMs === "number" && (!s.durationEstimated || s.durationMs >= 1000) && (
              <span className="shrink-0 text-[10.5px] text-slate-400 mono"
                title={s.durationEstimated ? "按事件时间戳估算的耗时（秒级）" : "这条命令的执行耗时"}>
                {s.durationEstimated ? "≈" : ""}{fmtDuration(s.durationMs)}
              </span>
            )}
            <span className={`shrink-0 ${failed ? "text-red-500" : "text-emerald-500"}`}>
              {failed ? "✕" : "✓"}
            </span>
          </>
        )}
        <span className="shrink-0 text-slate-300">{open ? "▲" : "▼"}</span>
      </button>
      {/* 截图/回灌图缩略图：点开看大图 */}
      {runId && (
        <div className="ml-1">
          <ShotThumbs runId={runId} text={s.result} />
        </div>
      )}
      {open && (
        <div className="mt-1 ml-1 space-y-1">
          {isWrite && (
            <DiffView
              rows={rows}
              title={dp!.path}
              maxHeight={320}
            />
          )}
          {s.result !== undefined && (
            <pre className="bg-slate-50 rounded p-1.5 overflow-auto max-h-40 mono text-[11px] text-slate-500 whitespace-pre-wrap">
              {s.result || "（无输出）"}
            </pre>
          )}
          {s.args && Object.keys(s.args).length > 0 && (
            <>
              <div className="flex items-center gap-2">
                <button
                  className="text-[10.5px] text-slate-400 hover:text-slate-600"
                  onClick={() => setRaw((v) => !v)}
                >
                  {raw ? "收起参数" : "查看参数 JSON"}
                </button>
                <CopyButton
                  get={() => JSON.stringify(s.args, null, 2)}
                  label="复制参数"
                  title="复制这次工具调用的完整参数 JSON"
                  className="text-[10.5px] text-slate-400 hover:text-slate-600"
                />
              </div>
              {raw && (
                <pre className="bg-slate-50 rounded p-1.5 overflow-auto max-h-40 mono text-[11px] text-slate-500 whitespace-pre-wrap">
                  {JSON.stringify(s.args, null, 2)}
                </pre>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function ToolGroupBlock({ g, runId }: { g: ToolGroup; runId?: string }) {
  // 组未完成时默认展开；用户手动切换后以用户为准
  const [manual, setManual] = useState<boolean | null>(null);
  const open = manual ?? !g.done;
  const okCount = g.steps.filter((s) => s.result !== undefined).length;
  return (
    <div className="bg-slate-50/70 rounded-lg border border-slate-100 overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-2 py-1.5 text-xs hover:bg-slate-100/70"
        onClick={() => setManual(!open)}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-slate-400 shrink-0">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
        </svg>
        <span className="font-medium text-slate-600">
          {ROLE_LABEL[g.agent] || g.agent || "agent"} · 工具调用 {g.steps.length} 项
        </span>
        <span className="text-slate-400">
          {okCount < g.steps.length ? `${okCount}/${g.steps.length}` : ""}
        </span>
        <span className="ml-auto text-slate-300">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div className="px-2 pb-1.5 space-y-0.5">
          {g.steps.map((s) => (
            <StepRow key={s.key} s={s} runId={runId} />
          ))}
        </div>
      )}
    </div>
  );
}
