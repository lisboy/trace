import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { navigate } from "../router";
import { useAuth } from "../store";
import Markdown from "../components/Markdown";
import RunChanges from "../components/RunChanges";
import PreviewModal, { type Preview as PreviewT } from "../components/PreviewModal";
import {
  ROLE_LABEL,
  STATUS_COLOR,
  STATUS_LABEL,
  type RunDetail as RunDetailT,
} from "../types";

type DiffLine = { kind: string; old_no: number | null; new_no: number | null; text: string };
type DiffFile = { file: string; status: string; hunks: { header: string; lines: DiffLine[] }[] };
type DiffComment = { id: string; body: string; resolved: boolean; author_id: string; created_at: string };

const IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"];
const AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"];
const TEXT_EXTS = [".md", ".txt", ".py", ".json", ".js", ".ts", ".tsx", ".jsx", ".css",
  ".html", ".csv", ".log", ".yml", ".yaml", ".toml", ".cfg", ".ini"];

function extOf(path: string): string {
  const i = path.lastIndexOf(".");
  return i >= 0 ? path.slice(i).toLowerCase() : "";
}

type Preview = PreviewT;

function DiffReview({ runId, focusFile, onFocused }: {
  runId: string;
  focusFile: string | null;
  onFocused: () => void;
}) {
  const [files, setFiles] = useState<DiffFile[]>([]);
  const [comments, setComments] = useState<Record<string, DiffComment[]>>({});
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [target, setTarget] = useState<{ file: string; line: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const fileRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const user = useAuth((s) => s.user);

  function refresh() {
    api.runDiff(runId).then((r) => {
      setFiles(r.files || []);
      setComments(r.comments || {});
    }).catch(() => {});
  }
  useEffect(refresh, [runId]);

  // 变更报告点击文件 → 展开并滚动到对应 diff
  useEffect(() => {
    if (!focusFile || files.length === 0) return;
    const base = focusFile.split("/").pop() || focusFile;
    const hit =
      files.find((f) => f.file === focusFile) ||
      files.find((f) => f.file.endsWith("/" + base)) ||
      files.find((f) => f.file === base);
    if (hit) {
      setOpenFile(hit.file);
      setTimeout(() => {
        fileRefs.current[hit.file]?.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 60);
    }
    onFocused();
  }, [focusFile, files]);

  if (files.length === 0) return null;

  async function submit() {
    const text = inputRef.current?.value?.trim();
    if (!target || !text || busy) return;
    setErr("");
    setBusy(true);
    try {
      await api.addComment(runId, { body: text, file: target.file, line: target.line });
      if (inputRef.current) inputRef.current.value = "";
      setTarget(null);
      refresh();
    } catch (e: any) {
      setErr(e?.message || "评论失败");
    } finally {
      setBusy(false);
    }
  }

  const lineBg = (k: string) =>
    k === "add" ? "bg-green-50" : k === "del" ? "bg-red-50" : "";

  return (
    <section className="bg-white rounded border border-slate-200 p-4 mb-4">
      <h3 className="font-medium mb-2">代码评审（{files.length} 个文件）</h3>
      <div className="space-y-2">
        {files.map((f) => {
          const open = openFile === f.file;
          const fComments = Object.entries(comments).filter(([k]) => k.startsWith(`diff:${f.file}#`));
          return (
            <div key={f.file} className="border border-slate-100 rounded"
                 ref={(el) => { fileRefs.current[f.file] = el; }}>
              <button
                className="w-full text-left px-3 py-2 text-xs mono flex items-center gap-2 hover:bg-slate-50"
                onClick={() => setOpenFile(open ? null : f.file)}
              >
                <span>{open ? "▾" : "▸"}</span>
                <span className="flex-1 truncate">{f.file}</span>
                <span className={`px-1 rounded ${f.status === "added" ? "bg-green-100 text-green-700" : f.status === "deleted" ? "bg-red-100 text-red-700" : "bg-slate-100 text-slate-500"}`}>
                  {f.status === "added" ? "新增" : f.status === "deleted" ? "删除" : "修改"}
                </span>
                {fComments.length > 0 && (
                  <span className="px-1 rounded bg-amber-100 text-amber-700">{fComments.length} 评论</span>
                )}
              </button>
              {open && f.hunks.map((h, hi) => (
                <div key={hi}>
                  <div className="px-3 py-1 text-[10px] mono text-slate-400 bg-slate-50">{h.header}</div>
                  {h.lines.map((l, li) => {
                    const anchor = `diff:${f.file}#L${l.new_no ?? 0}`;
                    const cs = comments[anchor] || [];
                    return (
                      <div key={li}>
                        <div
                          className={`px-3 py-0.5 text-xs mono flex gap-2 ${lineBg(l.kind)} group cursor-pointer hover:bg-brand-50`}
                          onClick={() => setTarget({ file: f.file, line: l.new_no ?? 0 })}
                          title="点击添加行内评论"
                        >
                          <span className="w-10 text-right text-slate-300 select-none">{l.new_no ?? ""}</span>
                          <span className="w-4 text-slate-400 select-none">{l.kind === "add" ? "+" : l.kind === "del" ? "-" : " "}</span>
                          <span className="whitespace-pre-wrap flex-1">{l.text}</span>
                        </div>
                        {cs.map((c) => (
                          <div key={c.id} className={`ml-12 mr-3 my-1 px-2 py-1 rounded text-xs ${c.resolved ? "bg-slate-50 text-slate-400" : "bg-amber-50 text-amber-900"}`}>
                            <div className="flex items-center gap-2">
                              <span className="font-medium">{user?.id === c.author_id ? "我" : "成员"}</span>
                              {!c.resolved && user && (
                                <button className="ml-auto text-[10px] text-slate-400 hover:text-green-600"
                                  onClick={async () => { await api.resolveComment(c.id); refresh(); }}>
                                  标记已解决
                                </button>
                              )}
                            </div>
                            <div>{c.body}</div>
                          </div>
                        ))}
                        {target && target.file === f.file && target.line === (l.new_no ?? 0) && (
                          <div className="ml-12 mr-3 my-1 flex gap-2">
                            <input autoFocus className="flex-1 border rounded px-2 py-1 text-xs"
                              placeholder="输入评论后回车"
                              ref={inputRef}
                              defaultValue=""
                              onKeyDown={(e) => e.key === "Enter" && submit()}
                            />
                            <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={submit}>发送</button>
                            <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setTarget(null)}>取消</button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          );
        })}
      </div>
      {err && <div className="text-xs text-red-600 mt-2">{err}</div>}
      <p className="text-[11px] text-slate-400 mt-2">点击任意代码行可添加行内评论；评论随 run 保存，便于逐行记下了哪不对。</p>
    </section>
  );
}

export default function RunDetail({ runId, initialFile, initialAsset }: {
  runId: string;
  initialFile?: string;
  initialAsset?: string;
}) {
  const [run, setRun] = useState<RunDetailT | null>(null);
  const [focusFile, setFocusFile] = useState<string | null>(initialFile ?? null);
  const [preview, setPreview] = useState<Preview>(null);
  const assetConsumed = useRef(false);

  useEffect(() => {
    let alive = true;
    const t = setInterval(async () => {
      try {
        const r = await api.runDetail(runId);
        if (alive) setRun(r);
      } catch {
        /* ignore */
      }
    }, 3000);
    api
      .runDetail(runId)
      .then(setRun)
      .catch(() => {});
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId]);

  // 从 RunPanel 跳转过来时直接预览指定素材（消费后清理参数，避免重复弹出）
  useEffect(() => {
    if (!run || !initialAsset || assetConsumed.current) return;
    assetConsumed.current = true;
    const hit = run.artifacts.find((a) => a.rel_path === initialAsset);
    if (hit) openArtifact(hit);
    navigate("runs", { id: runId });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run]);

  /** 点击变更报告中的文件：md/文本 → 读取内容弹层；图片/音频 → 直连预览；其余 → 定位 diff */
  async function openCrFile(path: string) {
    const ext = extOf(path);
    const pid = run?.project_id;
    if (IMG_EXTS.includes(ext) && pid) {
      setPreview({ kind: "image", title: path, url: api.projectFileRaw(pid, path) });
      return;
    }
    if (AUDIO_EXTS.includes(ext) && pid) {
      setPreview({ kind: "audio", title: path, url: api.projectFileRaw(pid, path) });
      return;
    }
    if (TEXT_EXTS.includes(ext) && pid) {
      try {
        const r = await api.projectFileText(pid, path);
        setPreview({ kind: "text", title: path, content: r.content ?? "" });
      } catch (e: any) {
        setPreview({ kind: "text", title: path, content: `读取失败：${e?.message || e}` });
      }
      return;
    }
    setFocusFile(path); // 代码文件 → 滚动/展开 diff
  }

  /** 素材项点击预览 */
  async function openArtifact(a: { rel_path: string; id: string; size?: number | null }) {
    const ext = extOf(a.rel_path);
    const url = api.artifactFile(a.id, a.size ?? undefined);
    if (IMG_EXTS.includes(ext)) setPreview({ kind: "image", title: a.rel_path, url });
    else if (AUDIO_EXTS.includes(ext)) setPreview({ kind: "audio", title: a.rel_path, url });
    else if (TEXT_EXTS.includes(ext)) {
      try {
        const r = await api.projectFileText(run?.project_id || "", a.rel_path);
        setPreview({ kind: "text", title: a.rel_path, content: r.content ?? "" });
      } catch (e: any) {
        setPreview({ kind: "text", title: a.rel_path, content: `读取失败：${e?.message || e}` });
      }
    } else window.open(url, "_blank");
  }

  if (!run)
    return <div className="h-full flex items-center justify-center text-slate-400">加载中…</div>;

  const cr = run.change_report;

  return (
    <div className="h-full overflow-auto p-5">
      <div className="flex items-center gap-3 mb-4">
        <span className={`px-2 py-0.5 rounded text-sm ${STATUS_COLOR[run.status] || "bg-slate-100"}`}>
          {STATUS_LABEL[run.status] || run.status}
        </span>
        <h2 className="text-lg font-semibold">Run {runId.slice(0, 8)}</h2>
      </div>

      {run.error && <div className="text-sm text-red-600 mb-3">错误：{run.error}</div>}

      {run.final_summary && (
        <section className="bg-white rounded border border-slate-200 p-4 mb-4 text-sm">
          <Markdown text={run.final_summary} />
        </section>
      )}

      {/* 本次改动（快照版，任何项目都可用）：逐个文件看前后差异，审阅不通过就回滚 */}
      <div className="mb-4">
        <RunChanges runId={runId} title="本次改动" defaultOpen refreshKey={run.status} autoHideEmpty={false} />
      </div>

      <DiffReview runId={runId} focusFile={focusFile} onFocused={() => setFocusFile(null)} />

      {/* 任务树 */}
      <section className="bg-white rounded border border-slate-200 p-4 mb-4">
        <h3 className="font-medium mb-2">任务树</h3>
        <ul className="space-y-1 text-sm">
          {run.tasks.map((t) => (
            <li key={t.id} className="flex items-center gap-2">
              <span className="px-1 rounded bg-slate-100 text-xs text-slate-500">
                {ROLE_LABEL[t.agent_key] || t.agent_key}
              </span>
              <span>{t.title}</span>
              <span className="text-xs text-slate-400">{t.status}</span>
            </li>
          ))}
        </ul>
      </section>

      {/* 工单 */}
      {run.work_order && (
        <section className="bg-white rounded border border-slate-200 p-4 mb-4">
          <h3 className="font-medium mb-2">工单</h3>
          <p className="text-sm mb-2">{run.work_order.goal}</p>
          {run.work_order.checklist?.length > 0 && (
            <ul className="text-sm list-disc pl-5 space-y-0.5">
              {run.work_order.checklist.map((c: any, i: number) => (
                <li key={i}>
                  {c.id} · {c.desc}（验证：{c.verify}）
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* 待办清单（计划机制：组长列出，完成后逐项打勾） */}
      {!!run.plan?.length && (
        <section className="bg-white rounded border border-slate-200 p-4 mb-4">
          <h3 className="font-medium mb-2">
            待办清单（{run.plan.filter((p: any) => p.status === "completed").length}/
            {run.plan.length} 已完成）
          </h3>
          <ul className="text-sm space-y-0.5">
            {run.plan.map((p: any, i: number) => (
              <li key={p.id || i} className="flex items-start gap-1.5">
                <span>{p.status === "completed" ? "✅" : p.status === "in_progress" ? "🔄" : "⬜"}</span>
                <span className={p.status === "completed" ? "text-slate-400 line-through" : ""}>
                  {i + 1}. {p.content}
                  {p.note && <span className="ml-1 text-xs text-slate-400">— {p.note}</span>}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* 变更报告 */}
      {cr && (
        <section className="bg-white rounded border border-slate-200 p-4 mb-4">
          <h3 className="font-medium mb-2">
            变更报告（{cr.summary.added} + / {cr.summary.deleted} − · {cr.summary.count} 文件）
          </h3>
          <ul className="text-xs mono text-slate-600 space-y-0.5">
            {cr.files.map((f, i) => {
              const ext = extOf(f.file);
              return (
                <li key={i}>
                  <button
                    className="hover:text-brand-700 hover:underline text-left"
                    title={
                      TEXT_EXTS.includes(ext) ? "点击预览内容"
                      : IMG_EXTS.includes(ext) || AUDIO_EXTS.includes(ext) ? "点击预览"
                      : "点击在下方 diff 中定位该文件"
                    }
                    onClick={() => openCrFile(f.file)}
                  >
                    {f.file}
                  </button>{" "}
                  <span className="text-green-600">+{f.added}</span>{" "}
                  <span className="text-red-500">-{f.deleted}</span>
                </li>
              );
            })}
          </ul>
          {cr.has_placeholder && (
            <div className="mt-2 text-xs text-orange-600">
              ⚠ 含 {cr.placeholder_count} 个占位素材，需在「素材库」配置供应商后重画。
            </div>
          )}
        </section>
      )}

      {/* 素材 */}
      {run.artifacts.length > 0 && (
        <section className="bg-white rounded border border-slate-200 p-4 mb-4">
          <h3 className="font-medium mb-2">素材（{run.artifacts.length}）</h3>
          <ul className="text-sm space-y-1">
            {run.artifacts.map((a) => (
              <li key={a.id} className="flex items-center gap-2">
                <button
                  className="mono hover:text-brand-700 hover:underline text-left"
                  title="点击预览"
                  onClick={() => openArtifact(a)}
                >
                  {a.rel_path}
                </button>
                {a.is_placeholder && (
                  <span className="px-1 rounded bg-orange-100 text-orange-700 text-xs">占位</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {preview && <PreviewModal preview={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
