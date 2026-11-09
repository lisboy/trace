import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface SelectOption {
  value: string;
  label: string;
  /** 补充搜索关键字（label 被截短显示时，把全名/路径放这里，仍能搜到） */
  keywords?: string;
  /** 分组标题：相邻且同组的选项会渲染在同一段下 */
  group?: string;
  disabled?: boolean;
}

interface PanelPos {
  left: number;
  width: number;
  top?: number;
  bottom?: number;
  maxH: number;
}

/**
 * 可搜索的下拉选择器（替代原生 <select> 用于「选项多 / 名字长」的场景）：
 * 音色预设、LoRA 列表、模型列表、项目/会话筛选等。原生 <select> 在 Windows 上
 * 既不支持输入过滤、也装不下几百个选项，长声音名还会被截断。
 *
 * 交互：点开即聚焦搜索框，键盘 ↑/↓ 移动、Enter 选中、Esc 关闭；选项按 group 分段。
 * 样式与站内其它下拉一致（外层由调用方用 className 传紧凑的边框/内边距）。
 */
export default function SearchSelect({
  value,
  options,
  onChange,
  placeholder = "请选择",
  disabled,
  title,
  className = "",
  panelMinWidth = 160,
  searchPlaceholder = "输入关键字搜索…",
  emptyText = "没有匹配的选项",
  ariaLabel,
  autoFocusSearch = true,
}: {
  value: string;
  options: SelectOption[];
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
  title?: string;
  className?: string;
  panelMinWidth?: number;
  searchPlaceholder?: string;
  emptyText?: string;
  ariaLabel?: string;
  autoFocusSearch?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [pos, setPos] = useState<PanelPos | null>(null);
  const rootRef = useRef<HTMLSpanElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const current = useMemo(() => options.find((o) => o.value === value), [options, value]);
  const display = current ? current.label : (value ? value : placeholder);
  const muted = !current && !value;

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((o) => {
      const hay = `${o.label}\u0000${o.keywords || ""}\u0000${o.value}`.toLowerCase();
      return hay.includes(needle);
    });
  }, [options, q]);

  const measure = () => {
    const b = btnRef.current;
    if (!b) return;
    const r = b.getBoundingClientRect();
    const width = Math.max(r.width, panelMinWidth);
    const left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - width - 8));
    const below = window.innerHeight - r.bottom;
    const dropUp = below < 280 && r.top > below;
    const maxH = Math.min(360, Math.max(160, (dropUp ? r.top : below) - 12));
    setPos({
      left,
      width,
      top: dropUp ? undefined : r.bottom + 4,
      bottom: dropUp ? window.innerHeight - r.top + 4 : undefined,
      maxH,
    });
  };

  // 打开时定位、聚焦搜索框，并把高亮项定位到当前选中项
  useLayoutEffect(() => {
    if (!open) return;
    measure();
    if (autoFocusSearch) {
      const t = window.setTimeout(() => searchRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
  }, [open, autoFocusSearch]);

  useEffect(() => {
    if (!open) return;
    const idx = filtered.findIndex((o) => o.value === value);
    setActive(idx >= 0 ? idx : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t)) return;
      if (panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onScroll = () => measure();
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    // 祖先滚动（面板常嵌在可滚动页里）会让 fixed 面板错位，滚动时重算
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 高亮项滚进可视区
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const choose = (v: string) => {
    onChange(v);
    setOpen(false);
    setQ("");
  };

  const onSearchKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // 阻止冒泡：Chat 输入框等外层可能监听 Enter 发消息，这里只处理下拉自身
    e.stopPropagation();
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, Math.max(0, filtered.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const o = filtered[active];
      if (o && !o.disabled) choose(o.value);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  // 按 group 分段：相邻同 group 归一段，group 为空则无标题
  const rendered = useMemo(() => {
    const out: { group?: string; items: { o: SelectOption; idx: number }[] }[] = [];
    filtered.forEach((o, idx) => {
      const last = out[out.length - 1];
      if (last && last.group === o.group) last.items.push({ o, idx });
      else out.push({ group: o.group, items: [{ o, idx }] });
    });
    return out;
  }, [filtered]);

  return (
    <span ref={rootRef} className="relative inline-block max-w-full align-middle">
      <button
        ref={btnRef}
        type="button"
        className={`inline-flex max-w-full items-center justify-between gap-1.5 text-left disabled:opacity-50 ${className}`}
        title={title || (current ? current.label : display)}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => {
          if (disabled) return;
          setOpen((v) => !v);
          setQ("");
        }}
      >
        <span className={`truncate ${muted ? "text-slate-400" : ""}`}>{display}</span>
        <svg
          width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
          className={`shrink-0 text-slate-400 transition-transform ${open ? "rotate-180" : ""}`}
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>

      {/* portal 到 body：开壁纸后 .ga-bg-on .bg-white 会挂 backdrop-filter，
          成为 position:fixed 后代的包含块，弹层不再相对视口定位 → 会飘到触发器
          上方/别处（与 DirPicker/FileViewer 同一坑）。portal 出去才按视口坐标算。 */}
      {open && pos && createPortal(
        <div
          ref={panelRef}
          className="fixed z-[200] overflow-hidden rounded-lg border border-slate-200 bg-white text-xs shadow-xl"
          style={{
            left: pos.left,
            width: pos.width,
            top: pos.top,
            bottom: pos.bottom,
            backgroundColor: "rgb(var(--ga-panel-rgb, 255 255 255))",
          }}
          role="listbox"
        >
          <div className="border-b border-slate-100 p-1.5">
            <input
              ref={searchRef}
              value={q}
              onChange={(e) => { setQ(e.target.value); setActive(0); }}
              onKeyDown={onSearchKey}
              placeholder={searchPlaceholder}
              className="w-full rounded border border-slate-200 px-2 py-1 text-xs outline-none focus:border-brand-400"
            />
          </div>
          <div ref={listRef} className="overflow-auto p-1" style={{ maxHeight: Math.max(80, pos.maxH - 46) }}>
            {filtered.length === 0 && (
              <div className="px-2 py-2 text-slate-400">{emptyText}</div>
            )}
            {rendered.map((grp, gi) => (
              <div key={`${grp.group ?? ""}-${gi}`}>
                {grp.group && (
                  <div className="px-2 pb-0.5 pt-1.5 text-[11px] font-semibold text-slate-400">
                    {grp.group}
                  </div>
                )}
                {grp.items.map(({ o, idx }) => {
                  const sel = o.value === value;
                  const hot = idx === active;
                  return (
                    <div
                      key={o.value || `__empty_${idx}`}
                      data-idx={idx}
                      role="option"
                      aria-selected={sel}
                      onMouseEnter={() => setActive(idx)}
                      onMouseDown={(e) => { e.preventDefault(); if (!o.disabled) choose(o.value); }}
                      className={`flex items-center gap-1.5 rounded px-2 py-1.5 ${
                        o.disabled
                          ? "cursor-not-allowed text-slate-300"
                          : `cursor-pointer ${hot ? "bg-slate-100" : ""} ${sel ? "font-medium text-brand-700" : "text-slate-600"}`
                      }`}
                      title={o.keywords || o.label}
                    >
                      <span className="flex-1 truncate">{o.label}</span>
                      {sel && (
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
                          className="shrink-0 text-brand-600">
                          <path d="M20 6 9 17l-5-5" />
                        </svg>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>,
        document.body,
      )}
    </span>
  );
}
