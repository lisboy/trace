import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { navigate } from "../router";
import { ProjectSocket } from "../ws";
import Markdown from "./Markdown";
import RunChanges from "./RunChanges";
import ContextTrace from "./ContextTrace";
import ContextStatsView from "./ContextStats";
import PreviewModal, { type Preview } from "./PreviewModal";
import {
  STATUS_COLOR,
  STATUS_LABEL,
  type Artifact,
  type RunDetail,
  type WSEvent,
} from "../types";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "blocked"]); // blocked=等用户回答，不再刷

type Tab = "result" | "trace" | "stats";

const IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"];
const AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"];

function extOf(path: string): string {
  const i = path.lastIndexOf(".");
  return i >= 0 ? path.slice(i).toLowerCase() : "";
}

/* 运行面板：结果 / 上下文请求 / 上下文统计。
   执行过程（工具步骤、思考流）在聊天流的 RunTraceInline 里看，这里不再重复一份；
   本面板只回答「模型每一轮到底收到了什么、返回了什么、上下文还剩多少」。 */

export default function RunPanel({
  projectId,
  runId,
  compact = false,
  onCollapse,
}: {
  projectId: string;
  runId: string;
  compact?: boolean;
  onCollapse?: () => void;
}) {
  const [liveThinking, setLiveThinking] = useState<string>("");
  const [status, setStatus] = useState<string>("queued");
  const [run, setRun] = useState<RunDetail | null>(null);
  const [tab, setTab] = useState<Tab>("trace");
  const [preview, setPreview] = useState<Preview>(null);
  const becameTerminal = useRef(false);

  useEffect(() => {
    let socket: ProjectSocket | null = null;
    let alive = true;

    // 换 run 必须先把上一轮的状态清干净：ProjectSocket 是项目级的，事件里带 run_id，
    // 以前这里既不过滤 run_id 也不重置，结果切会话后右栏会把上一个 run 的思考流混进来。
    setLiveThinking("");
    setRun(null);
    becameTerminal.current = false;

    async function load() {
      try {
        const r = await api.runDetail(runId);
        if (!alive) return;
        setRun(r);
        setStatus(r.status);
        if (TERMINAL.has(r.status)) setTab("result"); // 打开历史 run 直接看结果
      } catch {
        /* ignore */
      }
    }
    load();

    socket = new ProjectSocket(projectId);
    socket.on((e: WSEvent) => {
      if (e.run_id !== runId) return; // 项目通道推所有 run：只认自己这条
      if (e.type === "status" || e.type === "run_done") {
        setStatus((cur) => e.payload?.status || cur);
      }
      // 流式思考 delta（seq=0 不落库）：整轮 agent_thinking 到达后清空实时块
      if (e.type === "agent_thinking_delta") {
        setLiveThinking((prev) => (prev || "") + String(e.payload?.text || ""));
      } else if (e.type === "agent_thinking") {
        setLiveThinking("");
      }
    });
    socket.onConnection(() => {});
    socket.backfillRun(runId);
    socket.start();

    // 轮询详情，直到终态，以拿到 final_summary / change_report / artifacts
    const timer = setInterval(async () => {
      try {
        const r = await api.runDetail(runId);
        if (!alive) return;
        setRun(r);
        setStatus(r.status);
        if (TERMINAL.has(r.status)) clearInterval(timer);
      } catch {
        /* ignore */
      }
    }, 2500);

    return () => {
      alive = false;
      clearInterval(timer);
      socket?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, runId]);

  // run 刚到终态且用户正看着上下文 → 自动切到结果
  useEffect(() => {
    if (TERMINAL.has(status) && !becameTerminal.current) {
      becameTerminal.current = true;
      setTab((t) => (t === "trace" ? "result" : t));
    }
  }, [status]);

  /** 点素材：图片/音频就地弹层预览（不再把整页跳到运行详情），其余仍交给运行详情页 */
  function onArtifact(a: Artifact) {
    const ext = extOf(a.rel_path);
    const url = api.artifactFile(a.id, a.size ?? undefined);
    if (IMG_EXTS.includes(ext)) setPreview({ kind: "image", title: a.rel_path, url });
    else if (AUDIO_EXTS.includes(ext)) setPreview({ kind: "audio", title: a.rel_path, url });
    else navigate("runs", { id: runId, asset: a.rel_path });
  }

  const cr = run?.change_report;
  const showResult = TERMINAL.has(status) && run;
  const running = !TERMINAL.has(status);

  const tabs: { id: Tab; label: string }[] = [
    ...(showResult ? ([{ id: "result" as Tab, label: "结果" }]) : []),
    { id: "trace", label: "上下文请求" },
    { id: "stats", label: "上下文统计" },
  ];

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-200 bg-white">
        <span
          className={`px-2 py-0.5 rounded text-xs shrink-0 ${
            STATUS_COLOR[status] || "bg-slate-100"
          }`}
        >
          {STATUS_LABEL[status] || status}
        </span>
        <span className="text-xs text-slate-400 mono shrink-0">run {runId.slice(0, 8)}</span>
        {run?.usage && run.usage.calls > 0 && (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 min-w-0 truncate whitespace-nowrap"
            title={`输入 ${run.usage.prompt} / 输出 ${run.usage.completion} tokens · 缓存命中 ${run.usage.cached ?? 0} · 总耗时 ${(run.usage.latency_ms / 1000).toFixed(1)}s（含重试）`}
          >
            {run.usage.calls} 次调用 ·{" "}
            {(run.usage.prompt + run.usage.completion).toLocaleString()} tok
            {run.usage.tokens_per_sec != null ? ` · ${run.usage.tokens_per_sec} tok/s` : ""}
            {(run.usage as any).cache_hit_rate != null
              ? ` · 缓存 ${(run.usage as any).cache_hit_rate}%`
              : " · 缓存未上报"}
          </span>
        )}
        {onCollapse && (
          <button
            className="ml-auto shrink-0 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            title="收起运行面板（右侧只留一条窄边，随时可展开）"
            aria-label="收起运行面板"
            onClick={onCollapse}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </button>
        )}
      </div>

      {/* Tab 栏 */}
      {!compact && (
        <div className="flex items-center gap-1 px-3 pt-2 border-b border-slate-200 bg-white">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={`px-2.5 py-1.5 text-xs rounded-t-md border-b-2 -mb-px transition-colors ${
                tab === t.id
                  ? "border-brand-600 text-brand-700 font-medium"
                  : "border-transparent text-slate-400 hover:text-slate-600"
              }`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}

      {/* 结果 tab（终态） */}
      {!compact && tab === "result" && showResult && (
        <div className="flex-1 overflow-auto p-3 space-y-3">
          {run?.error && (
            <div className="text-sm text-red-600">错误：{run.error}</div>
          )}
          {run?.final_summary && (
            <div className="text-sm bg-white rounded border border-slate-200 p-3">
              <Markdown text={run.final_summary} />
            </div>
          )}

          {/* 本次改动（工具层快照，不依赖 git）：逐个文件看前后差异，审阅不通过就回滚 */}
          <RunChanges runId={runId} title="本次改动" defaultOpen refreshKey={status} />

          {cr && cr.files.length > 0 && (
            <div className="bg-white rounded border border-slate-200 p-3">
              <div className="text-sm font-medium mb-1">
                变更报告（{cr.summary.added} + / {cr.summary.deleted} − ·{" "}
                {cr.summary.count} 文件）
              </div>
              <ul className="text-xs mono text-slate-600 space-y-0.5">
                {cr.files.map((f, i) => (
                  <li key={i}>
                    <button
                      className="hover:text-brand-700 hover:underline"
                      title="在运行详情中查看改动 diff / 预览文件"
                      onClick={() => run && navigate("runs", { id: run.id, file: f.file })}
                    >
                      {f.file}
                    </button>{" "}
                    <span className="text-green-600">+{f.added}</span>{" "}
                    <span className="text-red-500">-{f.deleted}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {cr?.test_report && (
            <div className="bg-white rounded border border-slate-200 p-3 text-sm">
              <div className="font-medium mb-1">测试报告</div>
              <div className="text-slate-600 whitespace-pre-wrap">
                {JSON.stringify(cr.test_report, null, 2)}
              </div>
            </div>
          )}

          {run.artifacts.length > 0 && (
            <div className="bg-white rounded border border-slate-200 p-3">
              <div className="text-sm font-medium mb-1">
                素材（{run.artifacts.length}）
              </div>
              {/* 路径长短不一：按钮占满剩余宽度并单行截断（悬停看全名），
                  占位标记固定不缩，否则长路径会把「占位」挤成竖排两行 */}
              <ul className="text-xs space-y-1">
                {run.artifacts.map((a) => (
                  <li key={a.id} className="flex items-start gap-2">
                    <button
                      className="mono flex-1 min-w-0 truncate text-left hover:text-brand-700 hover:underline"
                      title={a.rel_path}
                      onClick={() => onArtifact(a)}
                    >
                      {a.rel_path}
                    </button>
                    {a.is_placeholder && (
                      <span className="px-1 rounded bg-orange-100 text-orange-700 shrink-0 whitespace-nowrap">
                        占位
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* 上下文请求 tab：逐轮往返现场 */}
      {!compact && tab === "trace" && (
        <>
          {liveThinking && running && (
            <div className="px-3 py-1.5 text-xs flex items-center gap-2 text-slate-500 border-b border-slate-100 bg-slate-50/60">
              <span className="w-2 h-2 rounded-full bg-brand-500 animate-pulse shrink-0" />
              <span className="animate-pulse font-medium">本轮请求中…</span>
              <span className="text-slate-300 truncate mono flex-1">
                {liveThinking.slice(-60)}
              </span>
            </div>
          )}
          <ContextTrace runId={runId} running={running} />
        </>
      )}

      {/* 上下文统计 tab */}
      {!compact && tab === "stats" && (
        <ContextStatsView
          sessionId={run?.session_id || ""}
          runId={runId}
          refreshKey={status + "|" + (run?.usage?.calls || 0)}
        />
      )}

      {preview && (
        <PreviewModal
          preview={preview}
          onClose={() => setPreview(null)}
          onOpenDetail={() => {
            const path = preview.title;
            setPreview(null);
            navigate("runs", { id: runId, asset: path });
          }}
        />
      )}
    </div>
  );
}
