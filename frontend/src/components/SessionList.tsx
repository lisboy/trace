import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import DirPicker from "./DirPicker";
import type { ProjectSummary, Session } from "../types";

/** 会话列表里的相对时间：刚刚 / N分钟前 / N小时前 / N天前。 */
function relTime(iso?: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!t || Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "刚刚";
  const m = Math.floor(diff / 60_000);
  if (m < 60) return `${m}分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}小时前`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}天前`;
  return new Date(t).toLocaleDateString();
}

function FolderIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-slate-500">
      <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
    </svg>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="12" height="12" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      className={`shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
    >
      <path d="m9 18 6-6-6-6" />
    </svg>
  );
}

/** 工作区（项目）里可折叠的一组会话。点击整行切换收起/展开；悬停显示「进入 / 删除」。 */
function ProjectGroup({
  p, sessions, current, archivedMode, collapsed, showAll, query, activeId,
  onToggle, onPick, onPickProject, onDeleteProject, onShowAll, onArchiveToggle, onRemove,
}: {
  p: ProjectSummary;
  sessions: Session[];
  current: boolean;
  archivedMode: boolean;
  collapsed: boolean;
  showAll: boolean;
  query: string;
  activeId: string | null;
  onToggle: () => void;
  onPick: (p: ProjectSummary, s: Session) => void;
  onPickProject: (id: string) => void;
  onDeleteProject: (p: ProjectSummary) => void;
  onShowAll: () => void;
  onArchiveToggle: (s: Session, status: "active" | "archived") => void;
  onRemove: (s: Session) => void;
}) {
  const q = query.trim().toLowerCase();
  const list = sessions.filter((s) => !q || (s.title || "（无标题）").toLowerCase().includes(q));
  const open = !collapsed || !!q;
  const visible = showAll || q ? list : list.slice(0, 5);
  const rest = list.length - visible.length;
  return (
    <div className="mb-0.5">
      <div
        className={`group flex items-center gap-1 rounded px-1.5 py-1.5 cursor-pointer select-none ${
          current && !archivedMode ? "bg-slate-100" : "hover:bg-slate-50"
        }`}
        onClick={onToggle}
        title={`${p.name}（点击${open ? "收起" : "展开"}会话）`}
      >
        <FolderIcon />
        <span className="truncate flex-1 text-[12.5px] font-medium">{p.name}</span>
        {!current && (
          <button
            className="hidden group-hover:flex h-5 px-1 shrink-0 items-center rounded text-[10px] text-brand-600 hover:bg-brand-50"
            title={`切换到项目「${p.name}」`}
            onClick={(e) => { e.stopPropagation(); onPickProject(p.id); }}
          >
            进入
          </button>
        )}
        <button
          className="hidden group-hover:flex h-5 px-1 shrink-0 items-center rounded text-[10px] text-red-600 hover:bg-red-50"
          title="删除工作区（其下会话记录一并清理）"
          onClick={(e) => { e.stopPropagation(); onDeleteProject(p); }}
        >
          删除
        </button>
        <button
          className="h-5 w-5 shrink-0 flex items-center justify-center rounded text-slate-400 hover:text-slate-600 hover:bg-slate-100"
          title={open ? "收起会话" : "展开会话"}
          aria-label={open ? "收起会话" : "展开会话"}
          onClick={(e) => { e.stopPropagation(); onToggle(); }}
        >
          <Chevron open={open} />
        </button>
      </div>
      {open && (
        <div className="ml-[11px] mt-0.5 space-y-0.5 border-l border-slate-200 pl-1.5">
          {list.length === 0 && (
            <div className="text-[11px] text-slate-400 px-2 py-1">
              {archivedMode ? "暂无已归档会话" : "暂无会话，点上方「新会话」开始"}
            </div>
          )}
          {visible.map((s) => (
            <SessionRow key={s.id} p={p} s={s} activeId={activeId} archivedMode={archivedMode}
              onPick={onPick} onArchiveToggle={onArchiveToggle} onRemove={onRemove} />
          ))}
          {rest > 0 && (
            <button
              className="w-full text-left text-[11px] text-slate-400 hover:text-brand-600 px-2 py-1"
              onClick={onShowAll}
            >
              展开其余 {rest} 个会话
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function SessionRow({
  p, s, activeId, archivedMode, onPick, onArchiveToggle, onRemove,
}: {
  p: ProjectSummary;
  s: Session;
  activeId: string | null;
  archivedMode: boolean;
  onPick: (p: ProjectSummary, s: Session) => void;
  onArchiveToggle: (s: Session, status: "active" | "archived") => void;
  onRemove: (s: Session) => void;
}) {
  const active = s.id === activeId && !archivedMode;
  return (
    <div className={`group relative rounded ${active ? "bg-brand-50" : "hover:bg-slate-50"}`}>
      <button
        onClick={() => onPick(p, s)}
        className="w-full text-left text-[12.5px] pl-2 pr-16 py-1.5 flex items-center gap-1.5"
        title={`${s.title || "（无标题）"} · ${relTime(s.created_at)}`}
      >
        {s.running && (
          <svg className="shrink-0 animate-spin text-brand-600" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" aria-label="正在执行">
            <path d="M21 12a9 9 0 1 1-6.22-8.56" />
          </svg>
        )}
        <span className="truncate flex-1">{s.title || "（无标题）"}</span>
        <span className="shrink-0 text-[10px] text-slate-400 group-hover:hidden">{relTime(s.last_active || s.created_at)}</span>
      </button>
      <div className="absolute right-1 top-1/2 -translate-y-1/2 hidden group-hover:flex items-center gap-0.5">
        {archivedMode ? (
          <button
            className="text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-green-100 text-slate-600"
            title="恢复会话"
            onClick={(e) => { e.stopPropagation(); onArchiveToggle(s, "active"); }}
          >
            恢复
          </button>
        ) : (
          <button
            className="text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-amber-100 text-slate-600"
            title="归档会话（可稍后在「已归档」中恢复）"
            onClick={(e) => { e.stopPropagation(); onArchiveToggle(s, "archived"); }}
          >
            归档
          </button>
        )}
        <button
          className="text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-red-100 text-red-600"
          title="删除会话"
          onClick={(e) => { e.stopPropagation(); onRemove(s); }}
        >
          删除
        </button>
      </div>
    </div>
  );
}

export default function SessionList({
  teamId,
  projects,
  projectId,
  activeId,
  onPick,
  onPickProject,
  onProjectsChanged,
  activeIsArchived = false,
}: {
  teamId: string;
  projects: ProjectSummary[];
  projectId: string;
  activeId: string | null;
  onPick: (projectId: string, s: Session) => void;
  onPickProject: (projectId: string) => void;
  /** 工作区增删后通知 Chat 重新拉 projects（并处理「当前项目被删」的情况） */
  onProjectsChanged?: () => void;
  activeIsArchived?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [archivedMode, setArchivedMode] = useState(false);
  const [byProject, setByProject] = useState<Record<string, Session[]>>({});
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState<Record<string, boolean>>({});
  // 已删除（归档）工作区：只列项目本身，不恢复其下任何会话记录；
  // 全部项目管理都在这个面板完成，不再需要跳到「项目」页。
  const [archivedProjects, setArchivedProjects] = useState<ProjectSummary[]>([]);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const [msg, setMsg] = useState("");
  // 新建工作区（项目）：全部项目管理都在这个会话面板里完成
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newPath, setNewPath] = useState("");
  const [dirPickOpen, setDirPickOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  const idsKey = projects.map((p) => p.id).join("|");

  // 所有项目的会话一起拉：侧栏按项目分组展示（图二的多项目会话树）。
  // 2.5s 静默刷新 + ga-sessions-changed 事件立即刷新，running 转圈和标题变化都能及时亮/灭。
  useEffect(() => {
    if (!projects.length) return;
    let dead = false;
    let inFlight = false;
    async function tick() {
      if (inFlight) return;
      inFlight = true;
      try {
        const [pairs, archived] = await Promise.all([
          Promise.all(
            projects.map(async (p) => [p.id, await api.sessions(p.id, archivedMode).catch(() => [])] as const),
          ),
          teamId ? api.projects(teamId, true).catch(() => []) : Promise.resolve([]),
        ]);
        if (!dead) {
          setByProject(Object.fromEntries(pairs));
          setArchivedProjects(Array.isArray(archived) ? archived : []);
        }
      } finally {
        inFlight = false;
      }
    }
    tick();
    const t = window.setInterval(tick, 2500);
    const onChanged = () => tick();
    window.addEventListener("ga-sessions-changed", onChanged);
    window.addEventListener("focus", onChanged);
    return () => {
      dead = true;
      window.clearInterval(t);
      window.removeEventListener("ga-sessions-changed", onChanged);
      window.removeEventListener("focus", onChanged);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey, archivedMode, teamId]);

  // 第一次进入某个项目时展开它；之后尊重用户自己的收起/展开选择。
  // 旧实现每次 projectId 变化都强制把当前项目置回展开，点击当前项目时
  // 收起状态会被随后的重渲染重置，视觉上就是「点击无法收缩」。
  const expandedOnce = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!projectId || expandedOnce.current.has(projectId)) return;
    expandedOnce.current.add(projectId);
    setCollapsed((c) => ({ ...c, [projectId]: false }));
  }, [projectId]);

  const q = query.trim().toLowerCase();

  async function setStatus(s: Session, status: "active" | "archived") {
    setMsg("");
    try {
      await api.updateSession(s.id, { status });
      window.dispatchEvent(new Event("ga-sessions-changed"));
    } catch (e: any) {
      setMsg(e?.message || "操作失败");
    }
  }

  async function remove(s: Session) {
    if (!confirm(`删除会话「${s.title || "（无标题）"}」？删除后不再显示（消息记录保留）。`)) return;
    setMsg("");
    try {
      await api.deleteSession(s.id);
      if (s.id === activeId) onPick(projectId, { id: "__new__", title: "新会话", created_at: "" });
      else window.dispatchEvent(new Event("ga-sessions-changed"));
    } catch (e: any) {
      setMsg(e?.message || "删除失败");
    }
  }

  async function deleteProject(p: ProjectSummary) {
    if (!confirm(`删除工作区「${p.name}」？\n其下的会话记录会一并从面板清理（工作区文件保留，可在「已删除的工作区」恢复）。`)) return;
    setMsg("");
    try {
      await api.archiveProject(p.id);
      // 立刻从面板消失，不等下一轮轮询
      setByProject((cur) => {
        const next = { ...cur };
        delete next[p.id];
        return next;
      });
      window.dispatchEvent(new Event("ga-sessions-changed"));
      onProjectsChanged?.();
    } catch (e: any) {
      setMsg(e?.message || "删除工作区失败");
    }
  }

  async function restoreProject(p: ProjectSummary) {
    setMsg("");
    try {
      await api.restoreProject(p.id);
      window.dispatchEvent(new Event("ga-sessions-changed"));
      onProjectsChanged?.();
    } catch (e: any) {
      setMsg(e?.message || "恢复工作区失败");
    }
  }

  async function createProject() {
    if (!teamId || !newName.trim()) return;
    setCreating(true);
    setMsg("");
    try {
      const slugBase = newName.trim().toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5_-]+/g, "-").replace(/^-+|-+$/g, "") || `ws-${Date.now()}`;
      const repoPath = newPath.trim() || `.agent/projects/${slugBase}`;
      await api.createProject(teamId, { name: newName.trim(), slug: slugBase, repo_path: repoPath, project_type: "python" });
      setCreateOpen(false);
      setNewName("");
      setNewPath("");
      onProjectsChanged?.();
      window.dispatchEvent(new Event("ga-sessions-changed"));
    } catch (e: any) {
      setMsg(e?.message || "新建工作区失败");
    } finally {
      setCreating(false);
    }
  }

  function newSession() {
    if (!projectId) {
      setCreateOpen(true);
      setMsg("请先新建一个工作区");
      return;
    }
    onPick(projectId, { id: "__new__", title: "新会话", created_at: "" });
  }

  const currentName = useMemo(
    () => projects.find((p) => p.id === projectId)?.name || "",
    [projects, projectId],
  );

  return (
    <div className="ga-session-pane w-64 shrink-0 border-r border-slate-200 bg-white flex flex-col">
      <div className="p-2 border-b border-slate-200 space-y-2">
        <button className="ga-btn ga-btn-primary w-full flex items-center justify-center gap-1.5" onClick={newSession}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
          新会话
        </button>

        <div className="flex items-center gap-1 px-0.5">
          <span className="flex-1 text-[11px] font-semibold tracking-wide text-slate-500" title={currentName}>
            工作区
          </span>
          <button
            className={`h-6 w-6 flex items-center justify-center rounded hover:bg-slate-100 ${searchOpen ? "text-brand-600" : "text-slate-400"}`}
            title="搜索会话"
            aria-label="搜索会话"
            onClick={() => setSearchOpen((v) => !v)}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
          </button>
          <button
            className={`h-6 w-6 flex items-center justify-center rounded hover:bg-slate-100 ${archivedMode ? "text-brand-600 bg-brand-50" : "text-slate-400"}`}
            title={archivedMode ? "返回活跃会话" : "查看已归档会话"}
            aria-label={archivedMode ? "返回活跃会话" : "查看已归档会话"}
            onClick={() => setArchivedMode((v) => !v)}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 6h18M7 12h10M10 18h4" />
            </svg>
          </button>
          <button
            className={`h-6 w-6 flex items-center justify-center rounded hover:bg-slate-100 ${createOpen ? "text-brand-600 bg-brand-50" : "text-slate-400"}`}
            title="新建工作区（项目）"
            aria-label="新建工作区"
            onClick={() => setCreateOpen((v) => !v)}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
        </div>

        {createOpen && (
          <div className="rounded border border-slate-200 bg-slate-50 p-2 space-y-1.5">
            <div className="text-[11px] font-medium text-slate-600">新建工作区</div>
            <input
              className="w-full rounded border border-slate-200 bg-white px-2 py-1 text-xs outline-none focus:border-brand-400"
              placeholder="工作区名称"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createProject()}
            />
            <input
              className="w-full rounded border border-slate-200 bg-white px-2 py-1 text-xs outline-none focus:border-brand-400"
              placeholder="目录（可选，默认 .agent/projects/名称）"
              value={newPath}
              onChange={(e) => setNewPath(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createProject()}
            />
            <div className="flex items-center gap-1">
              <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setDirPickOpen(true)} title="浏览本机文件夹并选择">
                选择文件夹…
              </button>
            </div>
            <div className="flex justify-end gap-1">
              <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setCreateOpen(false)}>取消</button>
              <button className="ga-btn ga-btn-primary ga-btn-sm" disabled={creating || !newName.trim()} onClick={createProject}>
                {creating ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        )}

        {searchOpen && (
          <input
            autoFocus
            className="w-full rounded border border-slate-200 px-2 py-1 text-xs outline-none focus:border-brand-400"
            placeholder="搜索会话…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        )}
        {archivedMode && (
          <div className="rounded bg-amber-50 px-2 py-1 text-[10.5px] text-amber-700">
            正在查看已归档会话
          </div>
        )}
      </div>

      <div className="ga-list-tint flex-1 overflow-auto p-1.5 space-y-0.5">
        {msg && <div className="text-[11px] text-red-600 px-1">{msg}</div>}
        {projects.length === 0 && (
          <div className="text-xs text-slate-400 px-1 py-2">暂无工作区。点右上角「+」新建一个。</div>
        )}
        {projects.map((p) => (
          <ProjectGroup
            key={p.id}
            p={p}
            sessions={byProject[p.id] || []}
            current={p.id === projectId}
            archivedMode={archivedMode}
            collapsed={!!collapsed[p.id]}
            showAll={!!showAll[p.id]}
            query={q}
            activeId={activeId}
            onToggle={() => setCollapsed((c) => ({ ...c, [p.id]: !(c[p.id] ?? false) }))}
            onPick={(pp, s) => onPick(pp.id, s)}
            onPickProject={onPickProject}
            onDeleteProject={deleteProject}
            onShowAll={() => setShowAll((v) => ({ ...v, [p.id]: true }))}
            onArchiveToggle={setStatus}
            onRemove={remove}
          />
        ))}

        {archivedProjects.length > 0 && !archivedMode && (
          <div className="pt-1 border-t border-slate-100 mt-1">
            <button
              type="button"
              className="w-full flex items-center gap-1 rounded px-1.5 py-1.5 text-[11px] text-slate-400 hover:bg-slate-50 hover:text-slate-600"
              onClick={() => setArchivedOpen((v) => !v)}
              title={archivedOpen ? "收起已删除的工作区" : "展开已删除的工作区"}
            >
              <Chevron open={archivedOpen} />
              <span className="flex-1 text-left">已删除的工作区</span>
              <span className="shrink-0">{archivedProjects.length}</span>
            </button>
            {archivedOpen && archivedProjects.map((p) => (
              <div key={p.id} className="flex items-center gap-1 rounded px-1.5 py-1.5 ml-4 text-[12.5px] text-slate-500">
                <span className="truncate flex-1" title={p.name}>{p.name}</span>
                <button
                  type="button"
                  className="shrink-0 text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-green-100 text-slate-600"
                  title="恢复工作区（历史会话一并回来）"
                  onClick={() => restoreProject(p)}
                >
                  恢复
                </button>
              </div>
            ))}
          </div>
        )}

        {activeIsArchived && !archivedMode && (
          <div className="text-[11px] text-amber-600 px-1 pt-1">当前打开的会话已归档，可在「已归档」中恢复。</div>
        )}
      </div>
      <DirPicker
        open={dirPickOpen}
        initialPath={newPath.trim() || undefined}
        onClose={() => setDirPickOpen(false)}
        onPick={(path) => { setNewPath(path); setDirPickOpen(false); }}
      />
    </div>
  );
}
