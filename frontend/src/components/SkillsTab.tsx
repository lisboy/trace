import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";
import SearchSelect from "./SearchSelect";

/** 技能管理面板：源目录（项目/全局/自定义文件夹）+ 技能清单 + 全局技能 CRUD + 正文预览。
 *  渐进式披露：run 里模型只看到「名称+描述」，正文由 load_skill 工具按需加载。 */

interface SkillMeta {
  name: string;
  title: string;
  description: string;
  scope: string; // project | global | root
  path: string;
  dir: string;
}

interface SkillsResp {
  skills: SkillMeta[];
  roots: { scope: string; path: string; exists: boolean }[];
  global_dir: string;
  tool_roles: Record<string, string[]>;
}

const SCOPE_LABEL: Record<string, string> = {
  project: "项目",
  global: "全局",
  root: "自定义目录",
};
const SCOPE_STYLE: Record<string, string> = {
  project: "bg-sky-100 text-sky-700",
  global: "bg-emerald-50 text-emerald-700",
  root: "bg-amber-50 text-amber-700",
};

export default function SkillsTab() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [data, setData] = useState<SkillsResp | null>(null);
  const [msg, setMsg] = useState("");
  const [openBody, setOpenBody] = useState<string | null>(null);
  const [bodyText, setBodyText] = useState("");
  // 新建全局技能
  const [nName, setNName] = useState("");
  const [nDesc, setNDesc] = useState("");
  const [nContent, setNContent] = useState("");
  // 添加自定义源目录
  const [rootInput, setRootInput] = useState("");

  useEffect(() => {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps) => {
      const list = ps.map((p: any) => ({ id: p.id, name: p.name }));
      setProjects(list);
      setProjectId((cur) => cur || list[0]?.id || "");
    }).catch(() => {});
  }, [currentTeamId]);

  function refresh() {
    api.skills(projectId || undefined).then(setData).catch((e) => setMsg(e?.message || "加载失败"));
  }
  useEffect(refresh, [projectId]);

  async function viewBody(sk: SkillMeta) {
    if (openBody === sk.name) {
      setOpenBody(null);
      return;
    }
    setMsg("");
    try {
      const r: any = await api.skillContent(sk.name, projectId || undefined);
      setBodyText(r.content || "");
      setOpenBody(sk.name);
    } catch (e: any) {
      setMsg(e?.message || "读取正文失败");
    }
  }

  async function saveGlobal() {
    setMsg("");
    try {
      await api.saveGlobalSkill({ name: nName.trim(), description: nDesc.trim(), content: nContent });
      setNName("");
      setNDesc("");
      setNContent("");
      setMsg("全局技能已保存");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    }
  }

  async function addRoot() {
    setMsg("");
    try {
      await api.addSkillRoot(rootInput.trim());
      setRootInput("");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "添加失败");
    }
  }

  const skills = data?.skills || [];
  const loadRoles = Object.entries(data?.tool_roles || {})
    .filter(([, ts]) => ts.includes("load_skill"))
    .map(([r]) => r);

  return (
    <div className="space-y-3">
      {msg && <div className="text-xs text-green-700">{msg}</div>}

      {/* 顶部：项目选择 + 说明 */}
      <div className="bg-white rounded border border-slate-200 p-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <SearchSelect
            className="border rounded px-2 py-1 text-sm w-[180px]"
            value={projectId}
            title="选择项目"
            searchPlaceholder="搜索项目…"
            options={[{ value: "", label: "（不区分项目）" }, ...projects.map((p) => ({ value: p.id, label: p.name, keywords: p.name }))]}
            onChange={setProjectId}
          />
          <span className="text-[11px] text-slate-400">
            同名技能按「项目 &gt; 全局 &gt; 自定义目录」优先生效。清单只注入名称+描述（渐进式披露），
            run 中模型用 load_skill 按需加载正文
            {loadRoles.length ? `（可用角色：${loadRoles.join("、")}）` : ""}。
          </span>
        </div>
      </div>

      {/* 源目录 */}
      <div className="bg-white rounded border border-slate-200 p-4 text-sm">
        <div className="font-medium mb-2">技能源目录</div>
        <div className="space-y-1.5">
          {(data?.roots || []).map((r) => (
            <div key={r.scope + r.path} className="flex items-center gap-2 text-xs">
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${SCOPE_STYLE[r.scope] || "bg-slate-100 text-slate-500"}`}>
                {SCOPE_LABEL[r.scope] || r.scope}
              </span>
              <span className="mono text-slate-600 break-all">{r.path}</span>
              {!r.exists && <span className="text-[10px] text-amber-600">目录不存在（已跳过）</span>}
              {r.scope === "root" && (
                <button
                  className="ml-auto ga-btn ga-btn-danger ga-btn-sm shrink-0"
                  onClick={async () => {
                    try {
                      await api.removeSkillRoot(r.path);
                      refresh();
                    } catch (e: any) {
                      setMsg(e?.message || "移除失败");
                    }
                  }}
                >
                  移除
                </button>
              )}
            </div>
          ))}
        </div>
        <div className="mt-2 flex gap-2">
          <input
            className="flex-1 border rounded px-2 py-1 text-xs"
            placeholder="添加自定义技能文件夹（绝对路径，里面放 <name>.md 或 <name>/SKILL.md）"
            value={rootInput}
            onChange={(e) => setRootInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addRoot()}
          />
          <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={addRoot}>
            添加目录
          </button>
        </div>
      </div>

      {/* 新建全局技能 */}
      <div className="bg-white rounded border border-slate-200 p-4 text-sm">
        <div className="font-medium mb-2">
          新建全局技能
          <span className="ml-2 text-[11px] font-normal text-slate-400">
            存到 {data?.global_dir || "服务端 skills 目录"}，所有项目可用
          </span>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <input
            className="border rounded px-2 py-1 text-xs"
            placeholder="技能名（字母/数字/_/-，如 pixel-art）"
            value={nName}
            onChange={(e) => setNName(e.target.value)}
          />
          <input
            className="border rounded px-2 py-1 text-xs"
            placeholder="一句话描述（注入给模型的唯一线索，写清楚什么时候用）"
            value={nDesc}
            onChange={(e) => setNDesc(e.target.value)}
          />
        </div>
        <textarea
          className="w-full h-32 border rounded p-2 text-xs mono mt-2"
          placeholder="技能正文（Markdown）：做法、约定、注意事项……模型在需要时才加载这段内容"
          value={nContent}
          onChange={(e) => setNContent(e.target.value)}
        />
        <button
          className="mt-2 ga-btn ga-btn-primary"
          disabled={!nName.trim() || !nContent.trim()}
          onClick={saveGlobal}
        >
          保存技能
        </button>
      </div>

      {/* 技能清单 */}
      {skills.length === 0 ? (
        <div className="text-sm text-slate-400">
          暂无技能。把技能文件夹拖进项目 .agent/skills/，或在上面新建全局技能 / 添加自定义目录。
        </div>
      ) : (
        <div className="space-y-1.5">
          {skills.map((sk) => {
            return (
              <div key={sk.scope + "/" + sk.name} className="bg-white rounded border border-slate-200 p-3 text-sm">
                <div className="flex items-start gap-2">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${SCOPE_STYLE[sk.scope] || "bg-slate-100 text-slate-500"}`}>
                    {SCOPE_LABEL[sk.scope] || sk.scope}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="font-medium">
                      {sk.name}
                      {sk.title && sk.title !== sk.name && (
                        <span className="ml-2 font-normal text-slate-500">{sk.title}</span>
                      )}
                    </div>
                    {sk.description && (
                      <div className="text-[12.5px] text-slate-600 mt-0.5 break-words">{sk.description}</div>
                    )}
                    <div className="text-[11px] text-slate-400 mt-1 mono break-all">{sk.path}</div>
                  </div>
                  <button
                    className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
                    onClick={() => viewBody(sk)}
                  >
                    {openBody === sk.name ? "收起" : "看正文"}
                  </button>
                  {sk.scope === "global" && (
                    <button
                      className="ga-btn ga-btn-danger ga-btn-sm shrink-0"
                      onClick={async () => {
                        if (!confirm(`删除全局技能 ${sk.name}？`)) return;
                        try {
                          await api.deleteGlobalSkill(sk.name);
                          if (openBody === sk.name) setOpenBody(null);
                          refresh();
                        } catch (e: any) {
                          setMsg(e?.message || "删除失败");
                        }
                      }}
                    >
                      删除
                    </button>
                  )}
                </div>
                {openBody === sk.name && (
                  <pre className="mt-2 max-h-80 overflow-auto rounded bg-slate-50 border border-slate-100 p-2 text-[11.5px] mono whitespace-pre-wrap break-words text-slate-700">
                    {bodyText}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
