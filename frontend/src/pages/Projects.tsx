import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";
import { navigate } from "../router";
import type { ProjectSummary } from "../types";

export default function Projects() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: "",
    slug: "",
    repo_path: "",
  });
  const [err, setErr] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  function refresh() {
    if (!currentTeamId) return;
    api.projects(currentTeamId, showArchived).then(setProjects).catch(() => {});
  }
  useEffect(refresh, [currentTeamId, showArchived]);

  async function create() {
    if (!currentTeamId) return;
    setErr("");
    if (!form.name || !form.slug || !form.repo_path) {
      setErr("名称 / slug / 仓库路径 必填");
      return;
    }
    try {
      await api.createProject(currentTeamId, { ...form, project_type: "python" });
      setShowForm(false);
      setForm({ name: "", slug: "", repo_path: "" });
      refresh();
    } catch (e: any) {
      setErr(e?.message || "创建失败");
    }
  }

  // 移除项目（软删除归档）：从列表消失，历史与工作区文件保留，可在「已移除」视图恢复
  async function archive(p: ProjectSummary) {
    if (!window.confirm(`移除项目「${p.name}」？\n将从项目列表消失（历史记录与工作区文件保留），可在「已移除」视图中恢复。`)) return;
    setErr("");
    try {
      await api.archiveProject(p.id);
      refresh();
    } catch (e: any) {
      setErr(e?.message || "移除失败");
    }
  }

  async function restore(p: ProjectSummary) {
    setErr("");
    try {
      await api.restoreProject(p.id);
      refresh();
    } catch (e: any) {
      setErr(e?.message || "恢复失败");
    }
  }

  return (
    <div className="h-full overflow-auto p-5">
      <div className="flex items-center mb-4">
        <h2 className="text-lg font-semibold">项目</h2>
        <label className="ml-4 flex items-center gap-1.5 text-sm text-slate-600 cursor-pointer select-none">
          <input type="checkbox" className="accent-brand-600" checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)} />
          已移除
        </label>
        <button
          className="ml-auto ga-btn ga-btn-primary"
          onClick={() => setShowForm((v) => !v)}
        >
          + 新建项目
        </button>
      </div>

      {err && <div className="text-xs text-red-600 mb-2">{err}</div>}

      {showForm && (
        <div className="bg-white rounded border border-slate-200 p-4 mb-4 grid grid-cols-2 gap-3 text-sm">
          <input className="border rounded px-2 py-1" placeholder="名称" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="border rounded px-2 py-1" placeholder="slug（英文）" value={form.slug}
            onChange={(e) => setForm({ ...form, slug: e.target.value })} />
          <input className="border rounded px-2 py-1 col-span-2" placeholder="仓库路径（PROJECTS_ROOT 下相对路径，如 mygame）" value={form.repo_path}
            onChange={(e) => setForm({ ...form, repo_path: e.target.value })} />
          <div className="col-span-2 flex gap-2">
            <button className="ga-btn ga-btn-primary" onClick={create}>
              创建
            </button>
            <button className="ga-btn ga-btn-secondary" onClick={() => setShowForm(false)}>
              取消
            </button>
          </div>
          <p className="col-span-2 text-[11px] text-slate-400">
            智能体直接改动工作区文件（不建分支、不提交）；每次改动的内容在运行详情的「本次改动」里逐个文件审阅，不满意就回滚。
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {projects.map((p) => (
          <div key={p.id} className="bg-white rounded border border-slate-200 p-4">
            <div className="font-medium">{p.name}</div>
            <div className="text-xs text-slate-400 mono">{p.repo_path}</div>
            <div className="text-xs text-slate-500 mt-1">{p.run_count} 次运行</div>
            <div className="mt-3 flex gap-2">
              {showArchived ? (
                <button
                  className="text-xs px-2 py-1 rounded bg-green-600 text-white hover:bg-green-700"
                  onClick={() => restore(p)}
                >
                  恢复项目
                </button>
              ) : (
                <>
                  <button
                    className="text-xs px-2 py-1 rounded bg-brand-600 text-white hover:bg-brand-700"
                    onClick={() => navigate("chat", { project: p.id })}
                  >
                    打开对话
                  </button>
                  <button
                    className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
                    onClick={() => navigate("assets", { project: p.id })}
                  >
                    素材
                  </button>
                  <button
                    className="ml-auto text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50"
                    onClick={() => archive(p)}
                  >
                    移除
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
        {projects.length === 0 && (
          <div className="text-sm text-slate-400">还没有项目，点右上角新建。</div>
        )}
      </div>
    </div>
  );
}
