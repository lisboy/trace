/** 单文件查看器：一次 run 里某个改动文件的全屏预览。
 *
 * 两种看法（对应「已编辑 N 个文件」里点文件名的两种诉求）：
 *  - 本次改动：本次 run 对这个文件做了什么（前后 diff 行）
 *  - 全部内容：这个文件现在长什么样（整个文件）
 * 按类型自动挑渲染方式：HTML 走 iframe 真渲染（同目录 css/js/图片会一起加载）、
 * 图片/音频/PDF 直接内嵌、Markdown 走 Markdown 渲染、其余文本按纯文本带行号显示。
 * 删掉的文件自动改看「改动前快照」，不会看到 404。
 */
import { useEffect, useMemo, useRef, useState } from "react";
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

/** Markdown 预览 + 左侧目录。
 *  目录不从源码正则提取，而是渲染后从 DOM 里抓真实 h1~h4（顺序、层级、去重 slug 与正文
 *  天然一致，代码块里的 # 井号也不会误收）。点击平滑滚动到对应标题；滚动时高亮当前章节。
 *  没有标题（或只有 1 个）时不显示目录，正文铺满。
 */
function MarkdownWithToc({ text }: { text: string }) {
  const contentRef = useRef<HTMLDivElement>(null);
  // id -> 标题元素。id 由标题文本生成，可能含中文/引号等，不能走全局 getElementById
  // （既怕转义出错，也怕和页面其他 DOM 撞 id），直接持有元素引用最稳。
  const headingEls = useRef<Map<string, HTMLElement>>(new Map());
  const [headings, setHeadings] = useState<{ id: string; text: string; level: number }[]>([]);
  const [activeId, setActiveId] = useState("");

  // Markdown 渲染完成后扫一遍标题。依赖 text：内容变了目录跟着重建。
  useEffect(() => {
    const root = contentRef.current;
    if (!root) return;
    const hs = Array.from(root.querySelectorAll("h1, h2, h3, h4")) as HTMLElement[];
    const map = new Map<string, HTMLElement>();
    if (!hs.length) {
      headingEls.current = map;
      setHeadings([]);
      return;
    }
    const seen = new Map<string, number>();
    const items = hs.map((el) => {
      const base = (el.textContent || "").trim() || "（无标题）";
      let id = base;
      // 同名标题去重：-1、-2 递增，保证定位不串
      const n = seen.get(base) ?? 0;
      seen.set(base, n + 1);
      if (n > 0) id = `${base}-${n}`;
      el.id = id;
      map.set(id, el);
      return { id, text: base, level: Number(el.tagName[1]) };
    });
    headingEls.current = map;
    setHeadings(items);
  }, [text]);

  // 滚动高亮：正文面板自己就是滚动容器（contentRef），找判定线之上最近的一个标题。
  useEffect(() => {
    if (!headings.length) return;
    const scroller = contentRef.current;
    if (!scroller) return;
    const onScroll = () => {
      const top = scroller.getBoundingClientRect().top + 40; // 视觉判定线
      let cur = "";
      for (const h of headings) {
        const el = headingEls.current.get(h.id);
        if (!el) continue;
        if (el.getBoundingClientRect().top <= top) cur = h.id;
      }
      setActiveId(cur);
    };
    onScroll();
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [headings]);

  const scrollTo = (id: string) => {
    const el = headingEls.current.get(id);
    if (!el) return;
    setActiveId(id); // 点击即高亮，不等 scroll 事件
    const panel = contentRef.current;
    const startY = panel ? panel.scrollTop : 0;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    // 无合成器的环境（headless / 部分远程桌面）里 smooth 会静默不动：
    // 300ms 后 scrollTop 仍原地就退回瞬时跳转，保证点击目录永远有反应。
    window.setTimeout(() => {
      const p = contentRef.current;
      if (!p) return;
      if (Math.abs(p.scrollTop - startY) < 4) {
        el.scrollIntoView({ behavior: "auto", block: "start" });
      }
    }, 300);
  };

  // 结构保持稳定（nav 常驻、空时隐藏），避免「有标题后插入目录栏」导致 React 重建
  // 正文节点、effect 里抓到的标题元素引用全部失效。
  return (
    <div className="flex h-full min-h-0">
      <nav
        className={`w-56 shrink-0 overflow-y-auto border-r border-slate-100 bg-slate-50/60 px-2 py-3 ${
          headings.length ? "" : "hidden"
        }`}
      >
        <div className="px-2 pb-1.5 text-[10.5px] font-medium tracking-wide text-slate-400">
          目录
        </div>
        {headings.map((h) => (
          <a
            key={h.id}
            href={`#${encodeURIComponent(h.id)}`}
            onClick={(e) => {
              e.preventDefault();
              scrollTo(h.id);
            }}
            className={`block truncate rounded px-2 py-1 text-[11.5px] leading-snug transition-colors ${
              activeId === h.id
                ? "bg-brand-50 font-medium text-brand-700"
                : "text-slate-500 hover:bg-slate-100 hover:text-slate-700"
            }`}
            style={{ paddingLeft: `${(h.level - 1) * 10 + 8}px` }}
            title={h.text}
          >
            {h.text}
          </a>
        ))}
      </nav>
      {/* 正文：自己就是滚动容器（供滚动高亮监听） */}
      <div ref={contentRef} className="min-w-0 flex-1 overflow-y-auto p-5">
        <Markdown text={text} />
      </div>
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

  const [revealErr, setRevealErr] = useState("");
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
      return <MarkdownWithToc text={data.content || ""} />;
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
            <button
              className="mono block max-w-full truncate text-left text-[12.5px] text-slate-800 hover:text-brand-600 hover:underline"
              title={`${file.path}（点击在文件管理器中定位）`}
              onClick={async () => {
                setRevealErr("");
                try {
                  await api.runReveal(runId, file.path);
                } catch {
                  setRevealErr("定位失败（文件可能已回滚或不在工作区内）");
                }
              }}
            >
              {file.path}
            </button>
            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-slate-400">
              {!!file.added && <span className="text-emerald-600">+{file.added}</span>}
              {!!file.deleted && <span className="text-rose-500">−{file.deleted}</span>}
              {!!file.size && <span>{fmtSize(file.size)}</span>}
              <span>{ext(file.path) || "无扩展名"}</span>
              {revealErr && <span className="text-rose-500">{revealErr}</span>}
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
