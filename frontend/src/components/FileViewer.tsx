/** 单文件查看器：一次 run 里某个改动文件的全屏预览。
 *
 * 两种看法（对应「已编辑 N 个文件」里点文件名的两种诉求）：
 *  - 本次改动：本次 run 对这个文件做了什么（前后 diff 行）
 *  - 全部内容：这个文件现在长什么样（整个文件）
 * 按类型自动挑渲染方式：HTML 走 iframe 真渲染（同目录 css/js/图片会一起加载）、
 * 图片/音频/PDF 直接内嵌、Markdown 走 Markdown 渲染、其余文本按纯文本带行号显示。
 * 删掉的文件自动改看「改动前快照」，不会看到 404。
 */
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type { DiffRow } from "../diff";
import DiffView from "./DiffView";
import Markdown from "./Markdown";

export type ChangedFile = {
  path: string;
  status: string;              // added / modified / deleted
  added: number;
  deleted: number;
  diff?: DiffRow[];
  preview?: string;            // image / audio / html / markdown / pdf / text / unknown
  size?: number;
  binary_or_big?: boolean;
};

type Tab = "content" | "diff";

const STATUS_TEXT: Record<string, string> = {
  added: "新增",
  modified: "修改",
  deleted: "删除",
};

const STATUS_TONE: Record<string, string> = {
  added: "bg-emerald-50 text-emerald-700",
  modified: "bg-amber-50 text-amber-700",
  deleted: "bg-rose-50 text-rose-700",
};

function fmtSize(n?: number): string {
  if (!n || n <= 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function ext(path: string): string {
  const m = /\.([^./\\]+)$/.exec(path);
  return m ? m[1].toLowerCase() : "";
}

/** 纯文本：带行号的只读视图（超长截断，避免一次渲染几万行卡住）。 */
function CodeText({ text }: { text: string }) {
  const lines = useMemo(() => text.replace(/\r\n/g, "\n").split("\n"), [text]);
  const LIMIT = 5000;
  const shown = lines.slice(0, LIMIT);
  return (
    <div className="min-w-full text-[12px] leading-[1.55]">
      {shown.map((line, i) => (
        <div key={i} className="flex hover:bg-slate-50">
          <span className="w-14 shrink-0 select-none pr-3 text-right text-slate-300">{i + 1}</span>
          <span className="whitespace-pre pr-4 text-slate-700">{line || " "}</span>
        </div>
      ))}
      {lines.length > LIMIT && (
        <div className="px-14 py-2 text-[11px] text-slate-400">
          … 共 {lines.length} 行，只显示前 {LIMIT} 行
        </div>
      )}
    </div>
  );
}

export default function FileViewer({
  runId,
  file,
  initialTab = "diff",
  onClose,
  onRevert,
  reverting,
}: {
  runId: string;
  file: ChangedFile;
  initialTab?: Tab;
  onClose: () => void;
  onRevert?: (path: string) => void;
  reverting?: boolean;
}) {
  const deleted = file.status === "deleted";
  // 新增文件没有「改动前」，默认看内容更自然；其余默认看改动
  const [tab, setTab] = useState<Tab>(deleted ? "content" : initialTab);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState<any>(null);
  const [showOrig, setShowOrig] = useState(deleted);

  const rawUrl = useMemo(() => api.runFileRawUrl(runId, file.path), [runId, file.path]);
  const kind: string = data?.kind || file.preview || "text";
  // 图片/音频/PDF 用 blob 取（<img>/<audio>/<iframe> 发不出 Authorization 头，换 cookie 名的
  // 部署只靠 cookie 会 401 一片空）。HTML 预览不能转 blob：它要按原 URL 层级加载同目录
  // 的 css/js/图片，blob: 会破坏相对路径，保持 rawUrl（依赖会话 cookie）。
  const [mediaUrl, setMediaUrl] = useState<string>("");
  useEffect(() => {
    setMediaUrl("");
    if (tab !== "content" || !["image", "audio", "pdf"].includes(kind)) return;
    let alive = true;
    let made = "";
    api
      .fetchBlobUrl(rawUrl)
      .then((u) => {
        if (alive) {
          made = u;
          setMediaUrl(u);
        } else URL.revokeObjectURL(u);
      })
      .catch(() => {
        if (alive) setMediaUrl(rawUrl); // 取不到就退回原地址（有 cookie 时仍可用）
      });
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [rawUrl, kind, tab]);
  const mUrl = mediaUrl || rawUrl;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (tab !== "content") return;
    let alive = true;
    setLoading(true);
    setError("");
    api
      .runFileText(runId, file.path, showOrig ? "orig" : "text")
      .then((d: any) => {
        if (alive) setData(d);
      })
      .catch((e: any) => {
        if (alive) setError(e?.detail || e?.message || "读取失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId, file.path, tab, showOrig]);

  const body = () => {
    if (tab === "diff") {
      return file.diff && file.diff.length ? (
        <DiffView rows={file.diff} />
      ) : (
        <div className="p-6 text-[12px] text-slate-400">
          {file.binary_or_big ? "二进制或超大文件，没有逐行改动可看" : "没有可显示的改动内容"}
        </div>
      );
    }
    if (loading) return <div className="p-6 text-[12px] text-slate-400">读取中…</div>;
    if (error) return <div className="p-6 text-[12px] text-rose-500">{error}</div>;
    if (!data) return null;

    if (data.truncated) {
      return (
        <div className="p-6 text-[12px] text-slate-500">
          文件较大（{fmtSize(data.size)}），不做内嵌预览。
          <a className="ml-1 text-brand-600 hover:underline" href={rawUrl} target="_blank" rel="noreferrer">
            在新窗口打开
          </a>
        </div>
      );
    }
    if (kind === "image") {
      return (
        <div className="flex min-h-[240px] items-center justify-center bg-[linear-gradient(45deg,#f8fafc_25%,transparent_25%,transparent_75%,#f8fafc_75%),linear-gradient(45deg,#f8fafc_25%,transparent_25%,transparent_75%,#f8fafc_75%)] bg-[length:16px_16px] bg-[position:0_0,8px_8px] p-4">
          <img src={mUrl} alt={file.path} className="max-h-[70vh] max-w-full object-contain" />
        </div>
      );
    }
    if (kind === "audio") {
      return (
        <div className="p-6">
          <audio controls src={mUrl} className="w-full" />
        </div>
      );
    }
    if (kind === "pdf") {
      return <iframe title={file.path} src={mUrl} className="h-full w-full border-0" />;
    }
    if (kind === "html") {
      return (
        <div className="flex h-full flex-col">
          <div className="border-b border-slate-100 px-3 py-1.5 text-[11px] text-slate-400">
            HTML 渲染预览（同目录的 css / js / 图片会一起加载；脚本已禁用沙箱逃逸）
          </div>
          <iframe
            title={file.path}
            src={rawUrl}
            sandbox="allow-scripts allow-forms allow-popups allow-modals"
            className="min-h-0 flex-1 border-0 bg-white"
          />
        </div>
      );
    }
    if (kind === "binary") {
      return (
        <div className="p-6 text-[12px] text-slate-500">
          二进制文件（{fmtSize(data.size)}），不支持预览。
          <a className="ml-1 text-brand-600 hover:underline" href={rawUrl} target="_blank" rel="noreferrer">
            下载
          </a>
        </div>
      );
    }
    if (kind === "markdown") {
      return (
        <div className="p-5">
          <Markdown text={data.content || ""} />
        </div>
      );
    }
    if (!data.content) {
      return <div className="p-6 text-[12px] text-slate-400">（空文件）</div>;
    }
    return (
      <div className="overflow-auto p-3">
        <CodeText text={data.content} />
      </div>
    );
  };

  // portal 到 body，避开 backdrop-filter 造成 fixed 弹层被困在顶栏包含块的问题（同 SystemLogs）。
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex h-[88vh] w-[min(1120px,96vw)] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部：路径 + 状态 + 操作 */}
        <div className="flex items-start gap-2 border-b border-slate-100 px-4 py-2.5">
          <span
            className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10.5px] ${
              STATUS_TONE[file.status] || "bg-slate-100 text-slate-600"
            }`}
          >
            {STATUS_TEXT[file.status] || file.status}
          </span>
          <div className="min-w-0 flex-1">
            <div className="mono truncate text-[12.5px] text-slate-800" title={file.path}>
              {file.path}
            </div>
            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-slate-400">
              {!!file.added && <span className="text-emerald-600">+{file.added}</span>}
              {!!file.deleted && <span className="text-rose-500">−{file.deleted}</span>}
              {!!file.size && <span>{fmtSize(file.size)}</span>}
              <span>{ext(file.path) || "无扩展名"}</span>
            </div>
          </div>
          {onRevert && file.status !== "deleted" && (
            <button
              className="shrink-0 ga-btn ga-btn-secondary ga-btn-sm"
              disabled={reverting}
              onClick={() => onRevert(file.path)}
            >
              {reverting ? "回滚中…" : "回滚此文件"}
            </button>
          )}
          <button
            className="shrink-0 rounded px-2 py-1 text-[12px] text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            onClick={onClose}
            title="关闭（Esc）"
          >
            ✕
          </button>
        </div>

        {/* 两个视图 + 改动前切换 */}
        <div className="flex items-center gap-1 border-b border-slate-100 px-3 py-1.5">
          {(
            [
              ["content", "全部内容"],
              ["diff", "本次改动"],
            ] as [Tab, string][]
          ).map(([k, label]) => (
            <button
              key={k}
              className={`ga-btn ga-btn-sm ${
                tab === k ? "ga-btn-primary" : "ga-btn-ghost"
              }`}
              onClick={() => setTab(k)}
            >
              {label}
            </button>
          ))}
          {tab === "content" && (deleted || showOrig) && (
            <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10.5px] text-slate-500">
              改动前版本
            </span>
          )}
          {tab === "content" && !deleted && (
            <button
              className="ml-2 text-[11px] text-slate-400 hover:text-brand-600 hover:underline"
              onClick={() => setShowOrig((v) => !v)}
            >
              {showOrig ? "看当前版本" : "看改动前版本"}
            </button>
          )}
          <a
            className="ml-auto text-[11px] text-slate-400 hover:text-brand-600 hover:underline"
            href={rawUrl}
            target="_blank"
            rel="noreferrer"
          >
            新窗口打开
          </a>
        </div>

        {/* 内容区 */}
        <div className="min-h-0 flex-1 overflow-auto bg-white">{body()}</div>
      </div>
    </div>,
    document.body,
  );
}
