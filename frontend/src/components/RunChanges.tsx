/** 本次 run 的改动卡片：改了哪几个文件、每个文件能点开看「全部内容」或「本次改动」、不满意就回滚。
 *
 * 数据源：``GET /api/runs/{run_id}/changes``（工具层写文件前的快照，与 git 无关）。
 * 用在四处：聊天流里这条 run 的末尾（会话结束后就能看到）、右栏「结果」页、运行详情页、轨迹尾部。
 * 点文件名 → FileViewer：HTML / 图片 / PDF 直接渲染，Markdown 渲染，其余文本带行号显示。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import FileViewer, { type ChangedFile } from "./FileViewer";

type Changes = {
  files: ChangedFile[];
  summary: { count: number; added: number; deleted: number };
  reverted?: { path: string; at: string }[];
};

const STATUS_LABEL: Record<string, { text: string; tone: string; dot: string }> = {
  added: { text: "新建", tone: "bg-emerald-50 text-emerald-700 ring-emerald-200", dot: "bg-emerald-500" },
  modified: { text: "修改", tone: "bg-sky-50 text-sky-700 ring-sky-200", dot: "bg-sky-500" },
  deleted: { text: "删除", tone: "bg-rose-50 text-rose-700 ring-rose-200", dot: "bg-rose-500" },
};

const VISIBLE_ROWS = 6;   // 折叠时只列前几个文件，其余收起（和截图里「全部 N 个文件」一个意思）

export default function RunChanges({
  runId,
  compact = false,
  defaultOpen = false,
  title = "已编辑",
  onReverted,
  autoHideEmpty = true,
  refreshKey,
  lazy = false,
}: {
  runId: string;
  compact?: boolean;
  defaultOpen?: boolean;
  title?: string;
  onReverted?: () => void;
  autoHideEmpty?: boolean;
  /** 变化即重新拉取（跑动中随事件增长刷新） */
  refreshKey?: unknown;
  /** 历史 run 的改动卡片先不拉接口：点一下才加载。
   *  长会话切进来时若每条 run 都拉一遍 /changes，就是切换卡顿的一大来源。 */
  lazy?: boolean;
}) {
  const [data, setData] = useState<Changes | null>(null);
  const [open, setOpen] = useState(defaultOpen);
  const [armed, setArmed] = useState(!lazy);
  const [showAll, setShowAll] = useState(false);
  const [viewing, setViewing] = useState<{ file: ChangedFile; tab: "content" | "diff" } | null>(null);
  const [busy, setBusy] = useState<string>("");
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const r: any = await api.runChanges(runId);
      setData({
        files: r?.files || [],
        summary: r?.summary || { count: 0, added: 0, deleted: 0 },
        reverted: r?.reverted || [],
      });
    } catch {
      setData(null);
    }
  }, [runId]);

  useEffect(() => {
    setData(null);
    setShowAll(false);
    setViewing(null);
    setMsg("");
    setArmed(!lazy);
    if (!lazy) load();
  }, [runId, load, lazy]);

  // 运行中：事件增长/状态变化时静默刷新（不清空已展开的明细）
  useEffect(() => {
    if (!armed || refreshKey === undefined) return;
    load();
  }, [refreshKey, load, armed]);

  if (!armed) {
    return (
      <button
        type="button"
        onClick={() => { setArmed(true); load(); }}
        className="text-[11.5px] text-slate-400 hover:text-brand-600"
        title="加载这次运行的改动文件列表"
      >
        {title}的改动 · 点击查看
      </button>
    );
  }

  const doRevert = async (paths?: string[]) => {
    const what = paths ? `「${paths[0]}」` : `全部 ${data?.summary.count ?? 0} 个文件`;
    if (!window.confirm(`回滚${what}到本次任务开始之前的样子？\n工作区里的这些改动会被覆盖，无法自动恢复。`)) return;
    setBusy(paths ? paths[0] : "__all__");
    setMsg("");
    try {
      const r: any = await api.revertRun(runId, paths);
      const ok: string[] = r?.reverted || [];
      const bad: any[] = r?.failed || [];
      setMsg(
        ok.length
          ? `已回滚 ${ok.length} 个文件${bad.length ? `，${bad.length} 个失败：${bad.map((b) => b.path).join("、")}` : ""}`
          : `回滚失败：${bad.map((b) => `${b.path}（${b.error}）`).join("、") || "未知原因"}`,
      );
      await load();
      if (ok.length) onReverted?.();
    } catch (e: any) {
      setMsg(`回滚失败：${e?.message || e}`);
    } finally {
      setBusy("");
    }
  };

  if (!data) return null;
  if (!data.files.length) {
    if (autoHideEmpty) return null;
    return (
      <div className="rounded border border-slate-200 bg-white p-2 text-xs text-slate-400">
        {title}：本次没有改动任何文件。
      </div>
    );
  }

  const { count, added, deleted } = data.summary;
  const files = data.files;
  const shown = open ? (showAll ? files : files.slice(0, VISIBLE_ROWS)) : [];
  // 新增文件光看 diff 全是 +，默认直接看内容更直观
  const openFile = (f: ChangedFile, tab?: "content" | "diff") =>
    setViewing({ file: f, tab: tab || (f.status === "added" ? "content" : "diff") });

  return (
    <div className={`rounded-lg border border-slate-200 bg-white ${compact ? "" : "shadow-sm"}`}>
      {/* 卡片头：改了几个文件（点开列出文件名） */}
      <div className="flex items-center gap-2.5 px-3 py-2.5">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-slate-100 text-[15px] text-slate-500">
          ✎
        </div>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="min-w-0 flex-1 text-left"
          title={open ? "收起文件列表" : "展开文件列表"}
        >
          <div className="flex items-center gap-2 text-[13px]">
            <span className="font-medium text-slate-800">
              {title} {count} 个文件
            </span>
            {!!added && <span className="mono text-[12px] text-emerald-600">+{added}</span>}
            {!!deleted && <span className="mono text-[12px] text-rose-500">−{deleted}</span>}
            <span className="text-[11px] text-slate-300">{open ? "▲" : "▼"}</span>
          </div>
          <div className="mt-0.5 text-[11px] text-slate-400">
            点文件名查看全部内容或本次改动；HTML / 图片 / Markdown 可直接预览
          </div>
        </button>
        <button
          type="button"
          onClick={() => doRevert()}
          disabled={!!busy}
          className="shrink-0 ga-btn ga-btn-danger ga-btn-sm"
          title="把本次任务改过的文件全部恢复成任务开始前的样子"
        >
          {busy === "__all__" ? "回滚中…" : "全部回滚"}
        </button>
      </div>

      {msg && <div className="px-3 pb-1.5 text-[11px] text-slate-500">{msg}</div>}

      {/* 文件列表 */}
      {open && (
        <div className="border-t border-slate-100">
          {shown.map((f) => {
            const st = STATUS_LABEL[f.status] || {
              text: f.status,
              tone: "bg-slate-100 text-slate-600 ring-slate-200",
              dot: "bg-slate-300",
            };
            return (
              <div
                key={f.path}
                className="group flex items-center gap-2 border-b border-slate-50 px-3 py-[7px] last:border-b-0 hover:bg-slate-50"
              >
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${st.dot}`} title={st.text} />
                <button
                  type="button"
                  onClick={() => openFile(f)}
                  className="mono min-w-0 flex-1 truncate text-left text-[12.5px] text-slate-700 hover:text-brand-600 hover:underline"
                  title={`${f.path}（${st.text}）— 点击查看`}
                >
                  {f.path}
                </button>
                {f.binary_or_big && (
                  <span className="shrink-0 rounded bg-slate-100 px-1 text-[10px] text-slate-400">二进制</span>
                )}
                <span className="mono shrink-0 text-[11.5px] text-emerald-600">+{f.added}</span>
                <span className="mono shrink-0 text-[11.5px] text-rose-500">−{f.deleted}</span>
                <span className="hidden shrink-0 items-center gap-0.5 group-hover:flex">
                  <button
                    type="button"
                    onClick={() => openFile(f, "content")}
                    className="rounded px-1.5 py-0.5 text-[10.5px] text-slate-500 hover:bg-white hover:text-brand-600"
                    title="看这个文件的全部内容"
                  >
                    内容
                  </button>
                  <button
                    type="button"
                    onClick={() => openFile(f, "diff")}
                    className="rounded px-1.5 py-0.5 text-[10.5px] text-slate-500 hover:bg-white hover:text-brand-600"
                    title="只看本次改了什么"
                  >
                    改动
                  </button>
                  <button
                    type="button"
                    onClick={() => doRevert([f.path])}
                    disabled={!!busy}
                    className="rounded px-1.5 py-0.5 text-[10.5px] text-slate-400 hover:bg-white hover:text-rose-600 disabled:opacity-50"
                    title="只把这一个文件恢复成任务开始前的样子"
                  >
                    {busy === f.path ? "回滚中…" : "回滚"}
                  </button>
                </span>
              </div>
            );
          })}

          {files.length > VISIBLE_ROWS && (
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="w-full px-3 py-1.5 text-left text-[11.5px] text-slate-400 hover:text-slate-600"
            >
              {showAll ? "收起" : `全部 ${files.length} 个文件`}
              <span className="ml-1 text-slate-300">{showAll ? "▲" : "▼"}</span>
            </button>
          )}
          {!!data.reverted?.length && (
            <div className="px-3 pb-1.5 text-[10.5px] text-slate-400">
              已回滚过：{data.reverted.slice(-5).map((r) => r.path).join("、")}
            </div>
          )}
        </div>
      )}

      {viewing && (
        <FileViewer
          runId={runId}
          file={viewing.file}
          initialTab={viewing.tab}
          onClose={() => setViewing(null)}
          onRevert={(p) => doRevert([p])}
          reverting={!!busy}
        />
      )}
    </div>
  );
}
