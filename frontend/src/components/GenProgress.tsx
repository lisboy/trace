import { useEffect, useRef, useState } from "react";
import { api } from "../api";

/**
 * 处理过程可视化（音/图/视频工作台共用）。
 *
 * 后端一次生成是阻塞请求（图/视频要几十秒到几分钟），中途只看到按钮「生成中…」像纯等待。
 * 这里把「阶段 + 第几张 + 估算进度 + 时间线」显示出来：阶段/序号是真实状态（来自后端轮询），
 * 进度条是按经验耗时折算的**估算**值（明确标注「估算」）。
 */
export type LivePreview = {
  index: number; total: number; file: string; url: string; size: number;
  view?: string; verdict?: string; issues?: string[]; ok?: boolean;
};

export type JobProgress = {
  stage: string; stage_label: string; detail: string; image_index: number;
  image_count: number; prompt_id: string; queue_state: string;
  queue_position: number | null; elapsed_ms: number; percent: number;
  done: boolean; error: string;
  log?: { label: string; at: number }[];  // 后端时间线（含每张质检结论），防止轮询间的中间态丢失
  previews?: LivePreview[];               // 边生成边质检后已完成的图，供即时显示
};

export function newJobId(): string {
  try { return (crypto as any).randomUUID(); } catch { /* 旧浏览器 */ }
  return `job_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

export const STAGE_SHORT: Record<string, string> = {
  queued: "排队", enhancing: "润色/增强", starting: "准备服务", preparing: "解析参数",
  sampling: "生成中", review: "AI 质检", saving: "保存", done: "完成", failed: "失败",
};

// 各操作适用的阶段序列（用于步骤条，避免显示不相关的阶段）
export const IMAGE_STAGES = ["queued", "enhancing", "starting", "preparing", "sampling", "review", "saving"];
export const AV_STAGES = ["queued", "enhancing", "starting", "sampling", "saving"];

export function fmtElapsed(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/** 轮询任务表的 hook：start(jobId, fetcher) 开始；完成后 stop() 或 setFinal()。 */
export function useJobPoll(intervalMs = 1200) {
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [log, setLog] = useState<{ label: string; at: number }[]>([]);
  const timerRef = useRef<number | null>(null);
  const stoppedRef = useRef(false);
  const lastLabel = useRef("");

  const stop = () => {
    stoppedRef.current = true;
    if (timerRef.current != null) { window.clearInterval(timerRef.current); timerRef.current = null; }
  };

  const start = (jobId: string, fetcher: (id: string) => Promise<JobProgress>) => {
    stop();
    setProgress({
      stage: "queued", stage_label: "已提交，等待开始…", detail: "", image_index: 0,
      image_count: 0, prompt_id: "", queue_state: "", queue_position: null,
      elapsed_ms: 0, percent: 0, done: false, error: "",
    });
    setLog([]);
    lastLabel.current = "";
    stoppedRef.current = false;
    const tick = async () => {
      try {
        const p = await fetcher(jobId);
        if (stoppedRef.current) return;
        if (p?.stage && p.stage !== "unknown") {
          setProgress(p);
          // 优先用后端时间线：它记录了每个阶段文案（含每张质检的结论），
          // 轮询间隔再长也不会漏掉中间态；后端没给时退回本地按变更去重。
          if (Array.isArray(p.log) && p.log.length) {
            setLog(p.log.slice(-8));
            lastLabel.current = p.stage_label || p.stage;
          } else {
            const lbl = p.stage_label || p.stage;
            if (lbl && lbl !== lastLabel.current) {
              lastLabel.current = lbl;
              setLog((l) => [...l, { label: lbl, at: Date.now() }].slice(-8));
            }
          }
        }
      } catch { /* 轮询失败不打断生成 */ }
    };
    timerRef.current = window.setInterval(tick, intervalMs);
    tick();
  };

  const setFinal = (patch: Partial<JobProgress>) =>
    setProgress((p) => (p ? { ...p, ...patch } : p));

  useEffect(() => () => stop(), []);
  return { progress, log, start, stop, setFinal };
}

/** 进度面板：真实阶段 + 第 i/n 张 + 队列位次 + 估算进度条 + 步骤条 + 时间线。 */
export function GenProgress(
  { p, log, steps = IMAGE_STAGES, hint }: {
    p: JobProgress; log: { label: string; at: number }[];
    steps?: string[]; hint?: string;
  }
) {
  const failed = p.stage === "failed";
  const orderIdx = steps.indexOf(p.stage);
  const pct = failed ? 0 : p.percent;
  return (
    <div className="mt-2 rounded-lg border border-slate-300 bg-white shadow-sm px-3 py-2.5">
      <div className="flex items-center gap-2 text-xs">
        <span className={`inline-block w-2 h-2 rounded-full ${failed ? "bg-red-500" : p.done ? "bg-emerald-500" : "bg-sky-500 animate-pulse"}`} />
        <span className="font-semibold text-slate-900">{p.stage_label || STAGE_SHORT[p.stage] || p.stage}</span>
        {p.image_count > 1 && (p.stage === "sampling" || p.stage === "review") && (
          <span className="text-[11px] text-slate-600">第 {Math.min(p.image_index + 1, p.image_count)}/{p.image_count} 张</span>
        )}
        {p.queue_state === "queued" && p.queue_position != null && (
          <span className="text-[11px] text-amber-700">· 队列第 {p.queue_position + 1} 位</span>
        )}
        <span className="ml-auto tabular-nums font-medium text-slate-700">{fmtElapsed(p.elapsed_ms)}</span>
      </div>
      <div className="mt-1.5 h-2 w-full rounded-full bg-slate-200 overflow-hidden">
        <div className={`h-full transition-all duration-500 ease-out ${failed ? "bg-red-500" : p.done ? "bg-emerald-500" : "bg-sky-500"}`}
          style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-700 flex-wrap">
        <span className="font-medium">{failed ? "已中止" : `进度 ${pct}%（估算，仅供进度参考）`}</span>
        {!p.done && !failed && hint && <span className="text-slate-500">· {hint}</span>}
        {p.detail && <span className="text-red-600 break-all">· {p.detail}</span>}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        {steps.map((s, i) => {
          const done = p.done || orderIdx > i;
          const active = orderIdx === i && !p.done && !failed;
          return (
            <span key={s}
              className={`text-[11px] px-1.5 py-0.5 rounded font-medium ${active ? "bg-sky-600 text-white" : done ? "bg-sky-100 text-sky-800" : "bg-slate-100 text-slate-500"}`}>
              {STAGE_SHORT[s] || s}
            </span>
          );
        })}
      </div>
      {log.length > 0 && (
        <ol className="mt-2 space-y-0.5 border-l-2 border-sky-300 pl-2.5">
          {log.map((e, i) => (
            <li key={i} className="text-[11px] text-slate-700 flex gap-1.5">
              <span className="tabular-nums text-slate-500">{new Date(e.at).toLocaleTimeString("zh-CN", { hour12: false })}</span>
              <span>{e.label}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

const VERDICT_BADGE: Record<string, { t: string; cls: string }> = {
  pass: { t: "✓ 质检通过", cls: "bg-emerald-600 text-white" },
  fixed: { t: "✓ 已修复", cls: "bg-emerald-600 text-white" },
  kept: { t: "⚠ 仍异常", cls: "bg-amber-500 text-white" },
  skipped: { t: "未质检", cls: "bg-slate-500 text-white" },
};

/**
 * 边生成边展示：把后端已「生成+质检完成」的图即时显示出来（通过一张显示一张），
 * 未完成的格子沿用最近已加载的 blob URL，避免每 1.2s 轮询重复拉取。
 */
export function LivePreviews(
  { previews, total }: { previews: LivePreview[]; total?: number }
) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const fetched = useRef<Set<string>>(new Set());
  const allUrls = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const p of previews) {
      if (!p.file || fetched.current.has(p.file)) continue;
      fetched.current.add(p.file);
      api.imageGenBlob(p.file)
        .then((u) => { allUrls.current.add(u); setUrls((m) => ({ ...m, [p.file]: u })); })
        .catch(() => { /* 拉取失败不阻断 */ });
    }
  }, [previews]);
  useEffect(() => () => {
    allUrls.current.forEach((u) => { try { URL.revokeObjectURL(u); } catch { /* ignore */ } });
    allUrls.current.clear();
  }, []);

  const n = total && total > 0 ? total : previews.length;
  const done = previews.length;
  if (!done && !(n > 0)) return null;
  return (
    <div className="mt-2">
      <div className="text-[11px] text-slate-600 mb-1">
        已出图 {done}/{n} 张（质检通过即逐张显示，不必等全部生成完）
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
        {previews.map((p) => {
          const src = urls[p.file] || "";
          const vb = p.verdict ? VERDICT_BADGE[p.verdict] : null;
          return (
            <div key={p.file} className="relative rounded-lg overflow-hidden border border-slate-200 bg-slate-50">
              {src
                ? <img src={src} alt={`第 ${p.index + 1} 张`} className="w-full aspect-square object-contain bg-slate-50 cursor-zoom-in"
                    onClick={() => window.open(src, "_blank")} />
                : <div className="w-full aspect-square flex items-center justify-center text-[11px] text-slate-400">载入中…</div>}
              {p.view
                ? <span className="absolute left-1.5 top-1.5 px-1 h-5 rounded-full bg-brand-600 text-white text-[10px] leading-5">{p.view}</span>
                : <span className="absolute left-1.5 top-1.5 min-w-[20px] h-5 px-1 rounded-full bg-brand-600 text-white text-[11px] leading-5 text-center font-semibold">{p.index + 1}</span>}
              {vb && <span className={`absolute right-1.5 top-1.5 px-1.5 py-0.5 rounded text-[10px] font-medium ${vb.cls}`}>{vb.t}</span>}
              <div className="px-1.5 py-1 text-[10px] text-slate-400 truncate"
                title={(p.issues || []).join("、") || undefined}>
                {(p.size / 1024).toFixed(0)} KB{p.issues && p.issues.length ? ` · ${p.issues.join("、")}` : ""}
              </div>
            </div>
          );
        })}
        {/* 占位：还没出的格子，直观体现「还有几张在排队/生成」 */}
        {Array.from({ length: Math.max(0, n - done) }).map((_, i) => (
          <div key={`ph-${i}`} className="w-full aspect-square rounded-lg border border-dashed border-slate-200 flex items-center justify-center text-[11px] text-slate-400">
            待生成…
          </div>
        ))}
      </div>
    </div>
  );
}
