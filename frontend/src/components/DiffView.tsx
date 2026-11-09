/** 统一的行级 diff 视图：编辑前/后对比、整轮改动审阅都用它渲染。
 *
 * 长段未改动的上下文会折叠成「… N 行未变 …」，点一下展开，避免一次铺几百行。
 */
import { useMemo, useState } from "react";
import type { DiffRow } from "../diff";
import { diffStats } from "../diff";

type Block =
  | { kind: "lines"; rows: DiffRow[] }
  | { kind: "gap"; rows: DiffRow[] };

const CTX_KEEP = 3; // 折叠时保留首尾各几行上下文

function toBlocks(rows: DiffRow[]): Block[] {
  const out: Block[] = [];
  let buf: DiffRow[] = [];
  let bufIsCtx = false;
  const flush = () => {
    if (!buf.length) return;
    if (bufIsCtx && buf.length > CTX_KEEP * 2 + 3) out.push({ kind: "gap", rows: buf });
    else out.push({ kind: "lines", rows: buf });
    buf = [];
  };
  for (const r of rows) {
    const isCtx = r.kind === "ctx";
    if (buf.length && isCtx !== bufIsCtx) flush();
    bufIsCtx = isCtx;
    buf.push(r);
  }
  flush();
  return out;
}

function Row({ r, number }: { r: DiffRow; number: boolean }) {
  const tone =
    r.kind === "add"
      ? "bg-emerald-50 text-emerald-900"
      : r.kind === "del"
      ? "bg-rose-50 text-rose-900"
      : "text-slate-600";
  const sign = r.kind === "add" ? "+" : r.kind === "del" ? "−" : " ";
  return (
    <div className={`flex gap-2 px-2 py-[1px] mono text-[11.5px] leading-[1.45] ${tone}`}>
      {number && (
        <>
          <span className="w-9 shrink-0 select-none text-right text-[10px] text-slate-400/80">
            {r.old_no ?? ""}
          </span>
          <span className="w-9 shrink-0 select-none text-right text-[10px] text-slate-400/80">
            {r.new_no ?? ""}
          </span>
        </>
      )}
      <span className={`w-2 shrink-0 select-none ${r.kind === "ctx" ? "text-slate-300" : ""}`}>{sign}</span>
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-all">{r.text || " "}</span>
    </div>
  );
}

export default function DiffView({
  rows,
  maxHeight = 320,
  title,
  defaultNumber = true,
  showStats = true,
}: {
  rows: DiffRow[];
  maxHeight?: number;
  title?: string;
  defaultNumber?: boolean;
  showStats?: boolean;
}) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const blocks = useMemo(() => toBlocks(rows), [rows]);
  const stats = useMemo(() => diffStats(rows), [rows]);
  if (!rows.length) {
    return <div className="px-2 py-1 text-[11.5px] text-slate-400">（没有内容差异）</div>;
  }
  return (
    <div className="overflow-hidden rounded border border-slate-200 bg-white">
      {(title || showStats) && (
        <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50/80 px-2 py-1 text-[11px]">
          {title && <span className="mono truncate text-slate-600">{title}</span>}
          {showStats && (
            <span className="ml-auto shrink-0 space-x-1.5">
              <span className="text-emerald-600">+{stats.added}</span>
              <span className="text-rose-500">−{stats.deleted}</span>
            </span>
          )}
        </div>
      )}
      <div className="overflow-auto" style={{ maxHeight }}>
        {blocks.map((b, bi) => {
          if (b.kind === "gap") {
            if (expanded[bi]) {
              return (
                <div key={bi}>
                  {b.rows.map((r, i) => <Row key={i} r={r} number={defaultNumber} />)}
                </div>
              );
            }
            return (
              <button
                key={bi}
                type="button"
                onClick={() => setExpanded((cur) => ({ ...cur, [bi]: true }))}
                className="w-full bg-slate-50 px-2 py-[2px] text-left text-[10.5px] text-slate-400 hover:bg-slate-100"
              >
                ⋯ 未改动 {b.rows.length} 行（点开）
              </button>
            );
          }
          return (
            <div key={bi}>
              {b.rows.map((r, i) => <Row key={i} r={r} number={defaultNumber} />)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
