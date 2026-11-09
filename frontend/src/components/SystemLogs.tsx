import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";

type LogLine = {
  seq: number;
  ts: string;
  level: string;
  logger: string;
  message: string;
};

const LEVEL_OPTIONS = [
  { value: "", label: "全部级别" },
  { value: "DEBUG", label: "DEBUG+" },
  { value: "INFO", label: "INFO+" },
  { value: "WARNING", label: "WARNING+" },
  { value: "ERROR", label: "ERROR+" },
];

const LEVEL_STYLE: Record<string, string> = {
  DEBUG: "text-slate-400",
  INFO: "text-slate-600",
  WARNING: "text-amber-600",
  ERROR: "text-red-600 font-semibold",
  CRITICAL: "text-red-700 font-bold",
};

const LEVEL_RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

const MAX_CLIENT_LINES = 4000;
const PAGE = 1000;

/** 日志 ts 是「本地时间的裸 ISO」：`2026-10-30T13:42:21`（后端已按本地时区生成，不带 Z）。
 *  直接打印会看到中间那个 T，不是常规的「年月日 时分秒」。这里只做格式规整——
 *  不要用 new Date() 去解析再格式化：没有时区标记时浏览器会按本地处理，看着对，
 *  但一旦串里带了 +00:00 就会被再减 8 小时。正则截取最稳，也不受时区影响。 */
function fmtLogTs(ts: string): string {
  const m = String(ts || "").match(
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/,
  );
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : String(ts || "");
}

/** 系统日志弹窗：尾部轮询后端内存环形缓冲，实时看后台正在跑的日志。
 *
 * 为什么不做前端过滤就够：过滤在本地做（级别/关键字），轮询游标永远只按 seq 前进，
 * 这样切筛选条件不会丢行，也不会因为筛选把游标推过去。
 */
export default function SystemLogs({ open, onClose, embedded = false }: { open: boolean; onClose: () => void; embedded?: boolean }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [auto, setAuto] = useState(true);
  const [wrap, setWrap] = useState(true);
  const [info, setInfo] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [stick, setStick] = useState(true);

  const seqRef = useRef(0);
  // 已经画进视图的最大 seq：轮询和手动刷新可能重入，靠它去重，避免重复 key / 重复闪烁
  const appliedRef = useRef(0);
  const boxRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async (
    fromSeq: number,
    opts: { reset?: boolean; silent?: boolean } = {},
  ) => {
    const { reset = false, silent = false } = opts;
    if (!silent) setLoading(true);
    try {
      let cursor = fromSeq;
      const added: LogLine[] = [];
      for (let i = 0; i < 10; i += 1) {
        const r: any = await api.systemLogs({ since: cursor, limit: PAGE });
        const batch: LogLine[] = r?.lines ?? [];
        added.push(...batch);
        cursor = r?.last_seq ?? cursor;
        if (r?.truncated) setTruncated(true);
        if (batch.length < PAGE) break;
      }
      seqRef.current = cursor;
      const fresh = added.filter((l) => l.seq > appliedRef.current);
      if (fresh.length > 0) {
        appliedRef.current = fresh[fresh.length - 1].seq;
        // 只有真的多出新行才 setState —— 否则每 1.5s 都换一个数组引用，
        // 整张表重渲染 + 自动滚底，看起来就是「一闪一闪」
        setLines((prev) => {
          const next = reset ? fresh : prev.concat(fresh);
          return next.length > MAX_CLIENT_LINES ? next.slice(next.length - MAX_CLIENT_LINES) : next;
        });
      }
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // 打开即从头拉一次（缓冲区本来就有最近 5000 行），顺便拿进程/文件信息
  useEffect(() => {
    if (!open) return;
    seqRef.current = 0;
    appliedRef.current = 0;
    setLines([]);
    setTruncated(false);
    setStick(true);
    void load(0, { reset: true });
    api.systemInfo().then(setInfo).catch(() => setInfo(null));
  }, [open, load]);

  // 打开期间尾部轮询：静默刷新，不改 loading 状态，没新行就不触发重渲染
  useEffect(() => {
    if (!open || !auto) return;
    const timer = window.setInterval(() => void load(seqRef.current, { silent: true }), 1500);
    return () => window.clearInterval(timer);
  }, [open, auto, load]);

  // 只有「本来就在底部」才自动跟到最新，往上翻的时候不打断阅读
  useEffect(() => {
    const box = boxRef.current;
    if (!box || !stick) return;
    box.scrollTop = box.scrollHeight;
  }, [lines, stick, wrap]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const shown = useMemo(() => {
    const floor = LEVEL_RANK[level] ?? 0;
    const needle = q.trim().toLowerCase();
    return lines.filter((l) => {
      if (floor && (LEVEL_RANK[l.level] ?? 0) < floor) return false;
      if (needle && !l.message.toLowerCase().includes(needle)
        && !l.logger.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [lines, level, q]);

  const errorCount = useMemo(
    () => lines.filter((l) => l.level === "ERROR" || l.level === "CRITICAL").length,
    [lines],
  );

  if (!open) return null;

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    setStick(box.scrollHeight - box.scrollTop - box.clientHeight < 60);
  };

  const copyAll = async () => {
    const text = shown
      .map((l) => `${fmtLogTs(l.ts)} ${l.level} [${l.logger}] ${l.message}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* 剪贴板被拒就算了，用户可以自己选中复制 */
    }
  };

  const jumpLatest = () => {
    setStick(true);
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  };

  // portal 到 body：开背景壁纸后顶栏带 backdrop-filter，会成为 fixed 后代的包含块，
  // 不 portal 的话弹窗的 inset-0 是相对顶栏的 47px 高度，整个面板被顶出屏幕（实测踩过）
  const panel = (
      <div
        className={embedded
          ? "w-full h-full bg-white flex flex-col overflow-hidden border border-slate-200 rounded"
          : "w-[min(1240px,96vw)] h-[82vh] bg-white rounded-lg shadow-xl flex flex-col overflow-hidden"}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-slate-200 bg-slate-50">
          <span className="font-semibold text-slate-700">系统日志</span>
          <span className="text-xs text-slate-400 truncate max-w-[38ch]" title={info?.log_file || ""}>
            {info ? `${info.mode} · py${info.python} · ${info.log_file || "仅内存"} · 已记 ${info.log_lines} 行` : "…"}
          </span>
          {errorCount > 0 && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-red-50 text-red-600 border border-red-200">
              {errorCount} ERROR
            </span>
          )}
          <div className="ml-auto flex items-center gap-2 text-xs">
            <select
              className="border border-slate-200 rounded px-1.5 py-1 bg-white"
              value={level}
              onChange={(e) => setLevel(e.target.value)}
            >
              {LEVEL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            <input
              className="border border-slate-200 rounded px-2 py-1 w-40"
              placeholder="搜索日志…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <label className="flex items-center gap-1 text-slate-600 cursor-pointer select-none">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              自动刷新
            </label>
            <label className="flex items-center gap-1 text-slate-600 cursor-pointer select-none">
              <input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} />
              换行
            </label>
            <button className="ga-btn ga-btn-ghost ga-btn-sm" onClick={jumpLatest} title="滚到最新一行">↓ 最新</button>
            <button className="ga-btn ga-btn-ghost ga-btn-sm" onClick={() => void load(seqRef.current)} disabled={loading}>
              {loading ? "读取中…" : "刷新"}
            </button>
            <button className="ga-btn ga-btn-ghost ga-btn-sm" onClick={copyAll}>复制</button>
            <button className="ga-btn ga-btn-ghost ga-btn-sm" onClick={() => { setLines([]); setTruncated(false); }}>清屏</button>
            {!embedded && (
              <button className="ga-btn ga-btn-danger ga-btn-sm" onClick={onClose}>
                关闭
              </button>
            )}
          </div>
        </div>

        {(err || truncated) && (
          <div className="shrink-0 px-3 py-1 text-xs bg-amber-50 text-amber-700 border-b border-amber-200">
            {err ? `读取失败：${err}` : "更早的行已被内存缓冲挤出（只保留最近 5000 行），完整历史见 app.log"}
          </div>
        )}

        {/* 日志正文 */}
        <div
          ref={boxRef}
          onScroll={onScroll}
          className="ga-log flex-1 overflow-auto bg-slate-900 text-slate-100 font-mono text-[11.5px] leading-[1.55] px-3 py-2"
        >
          {shown.length === 0 ? (
            <div className="text-slate-500 py-6 text-center">
              {lines.length === 0 ? "暂无日志" : "当前筛选没有匹配的日志"}
            </div>
          ) : (
            shown.map((l) => (
              <div
                key={l.seq}
                className={`flex gap-2 ${wrap ? "" : "whitespace-pre"}`}
              >
                <span className="text-slate-500 shrink-0">{fmtLogTs(l.ts)}</span>
                <span className={`shrink-0 w-[4.5rem] ${LEVEL_STYLE[l.level] || "text-slate-300"}`}>
                  {l.level}
                </span>
                <span className="shrink-0 text-brand-500/80 max-w-[16ch] truncate" title={l.logger}>
                  {l.logger.replace(/^game_agent\./, "")}
                </span>
                <span className={`${wrap ? "whitespace-pre-wrap break-all" : ""} ${LEVEL_STYLE[l.level] || "text-slate-100"}`}>
                  {l.message}
                </span>
              </div>
            ))
          )}
        </div>

        {/* 底部 */}
        <div className="shrink-0 flex items-center gap-3 px-3 py-1.5 border-t border-slate-200 bg-slate-50 text-[11px] text-slate-500">
          <span>显示 {shown.length} / {lines.length} 行</span>
          <span>{auto ? "每 1.5s 自动刷新" : "已暂停刷新"}</span>
          <span className="truncate" title={info?.data_dir || ""}>数据目录 {info?.data_dir || "…"}</span>
          <span className="ml-auto">日志文件：{info?.log_file || "未启用"}</span>
        </div>
      </div>
  );

  if (embedded) return <div className="h-full">{panel}</div>;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      onClick={onClose}
    >
      {panel}
    </div>,
    document.body,
  );
}
