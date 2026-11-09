/** 后端目录选择器：浏览本机文件夹（GET /api/fs/dirs）。
 *  新建工作区时用「选择」按钮打开，避免手敲路径出错。 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";

type DirEntry = { name: string; path: string };

export default function DirPicker({
  open,
  initialPath,
  onClose,
  onPick,
}: {
  open: boolean;
  initialPath?: string;
  onClose: () => void;
  onPick: (path: string) => void;
}) {
  const [cur, setCur] = useState("");
  const [parent, setParent] = useState("");
  const [dirs, setDirs] = useState<DirEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!open) return;
    let dead = false;
    setLoading(true);
    setErr("");
    api
      .fsDirs(initialPath || "")
      .then((r: any) => {
        if (dead) return;
        setCur(r?.path || "");
        setParent(r?.parent || "");
        setDirs(Array.isArray(r?.dirs) ? r.dirs : []);
      })
      .catch((e: any) => { if (!dead) setErr(e?.message || "读取目录失败"); })
      .finally(() => { if (!dead) setLoading(false); });
    return () => { dead = true; };
  }, [open, initialPath]);

  function go(path: string) {
    setLoading(true);
    setErr("");
    api
      .fsDirs(path)
      .then((r: any) => {
        setCur(r?.path || "");
        setParent(r?.parent || "");
        setDirs(Array.isArray(r?.dirs) ? r.dirs : []);
      })
      .catch((e: any) => setErr(e?.message || "读取目录失败"))
      .finally(() => setLoading(false));
  }

  if (!open) return null;
  // portal 到 body：开背景壁纸后祖先带 backdrop-filter 会成为 fixed 后代的包含块，
  // 不 portal 的话弹窗会被压进左侧会话栏（实测踩过，和系统日志弹窗同一坑）。
  return createPortal(
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-900/40 p-4" onClick={onClose}>
      <div
        className="w-[min(680px,92vw)] h-[70vh] bg-white rounded-lg shadow-xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shrink-0 flex items-center gap-2 px-3 h-11 border-b border-slate-200 bg-slate-50">
          <span className="font-semibold text-sm text-slate-700">选择文件夹</span>
          <span className="text-[11px] text-slate-400 truncate flex-1" title={cur}>{cur || "…"}</span>
          <button className="ga-btn ga-btn-ghost ga-btn-sm" onClick={() => go(parent || "")} disabled={!cur && !parent}>
            上一级
          </button>
          <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={() => onPick(cur)} disabled={!cur}>
            选择此文件夹
          </button>
          <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={onClose}>取消</button>
        </div>
        <div className="flex-1 overflow-auto p-1.5">
          {err && <div className="text-xs text-rose-600 px-2 py-1">{err}</div>}
          {loading && <div className="text-xs text-slate-400 px-2 py-1">读取中…</div>}
          {!loading && !dirs.length && <div className="text-xs text-slate-400 px-2 py-4 text-center">没有子文件夹</div>}
          {dirs.map((d) => (
            <button
              key={d.path}
              className="w-full flex items-center gap-2 rounded px-2 py-1.5 text-left text-[12.5px] text-slate-700 hover:bg-slate-100"
              onDoubleClick={() => onPick(d.path)}
              onClick={() => go(d.path)}
              title={`${d.path}（单击进入，双击选中）`}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-amber-500">
                <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
              </svg>
              <span className="truncate">{d.name}</span>
            </button>
          ))}
        </div>
        <div className="shrink-0 px-3 py-1.5 border-t border-slate-200 bg-slate-50 text-[11px] text-slate-400">
          单击文件夹进入，双击或点右上角「选择此文件夹」使用当前目录；也支持直接选择磁盘根目录。
        </div>
      </div>
    </div>,
    document.body,
  );
}
