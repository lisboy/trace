import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { useAuth } from "../store";
import type { ProjectMemory, ProjectNote } from "../types";
import ModelConfigTab, { loadModelConfig, type LlmProviderRow, type ModelProfileRow } from "../components/ModelConfigTab";
import AppearanceTab from "../components/AppearanceTab";
import SkillsTab from "../components/SkillsTab";
import SystemLogs from "../components/SystemLogs";
import ProfileTab from "../components/ProfileTab";
import AssistantTab from "../components/AssistantTab";
import StorageTab from "../components/StorageTab";
import ChannelsTab from "../components/ChannelsTab";
import SearchSelect from "../components/SearchSelect";

const ROLE_NAMES: Record<string, string> = {
  main: "助手",
};

/** 推理强度档位的显示名（与后端 REASONING_EFFORTS 对齐；空=端点默认） */
const EFFORT_LABELS: Record<string, string> = {
  none: "None", minimal: "Minimal", low: "Low", medium: "Medium", high: "High",
};

/** 后端时间是 UTC 裸串（`2026-10-30 06:06:12+00:00`）。直接 slice 会既带 +00:00 又差 8 小时，
 *  这里按本地时区格式化成「年月日 时分」；旧的无时区串按 UTC 归一化。 */
function localStamp(iso: string): string {
  const s = String(iso || "").replace(" ", "T");
  if (!s) return "";
  const d = new Date(/[zZ+]|-\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
  if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

type Agent = {
  key: string;
  system_prompt: string;
  model_profile_key: string | null;
  params_json: Record<string, any>;
  tools: string[];
  is_default: boolean;
};
type Profile = ModelProfileRow;
type Provider = { id: string; driver_key: string; label: string; kind: string; enabled: boolean };
export type ConfigTab = "profile" | "assistant" | "appearance" | "skills" | "agents" | "profiles" | "providers" | "memory" | "mcp" | "channels" | "storage" | "logs";

/** 设置弹窗：盖在当前页上，左侧分类 + 右侧内容（对齐 Qoder 的设置对话框）。 */
export default function Config({ onClose, initialTab }: { onClose?: () => void; initialTab?: ConfigTab }) {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [tab, setTab] = useState<ConfigTab>(initialTab || "appearance");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [llmProviders, setLlmProviders] = useState<LlmProviderRow[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState("");

  function refresh() {
    api.agents().then((r) => setAgents(r.agents)).catch(() => {});
    loadModelConfig().then((r) => {
      setProfiles(r.profiles);
      setLlmProviders(r.providers);
    }).catch(() => {});
    api.providers().then(setProviders).catch(() => {});
  }
  useEffect(refresh, [currentTeamId]);

  useEffect(() => {
    if (!onClose) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function saveAgent(a: Agent) {
    setMsg("");
    try {
      await api.updateAgent(a.key, {
        system_prompt: draft,
        model_profile_key: a.model_profile_key ?? "",
      });
      setEditing(null);
      refresh();
      setMsg("已保存");
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    }
  }

  async function bindModel(a: Agent, key: string) {
    await api.updateAgent(a.key, { model_profile_key: key, system_prompt: a.system_prompt });
    refresh();
  }

  /** 在「对话模型」下拉里直接选「供应商 · 模型」时调用：先确保该模型有对应档位
   *  （同供应商+同模型已建档位就复用，否则后端自动建档位），再绑到 main。
   *  这样「加完供应商 + 拉取模型」就能直接在这里选模型对话，不必再手工建档位——
   *  以前下拉只列已有档位，新加的供应商模型一个都不出现，看着像「无法配置模型」。 */
  async function bindProviderModel(a: Agent, providerId: string, model: string) {
    setMsg("");
    try {
      const r: any = await api.autoModelProfile(providerId, model);
      const key = r?.key || "";
      if (!key) { setMsg("未能创建模型档位"); return; }
      await api.updateAgent(a.key, { model_profile_key: key, system_prompt: a.system_prompt });
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "选择模型失败");
    }
  }

  // 媒体档位绑定（美术→image_profile / 配音→audio_profile），存 agent.params_json
  async function bindMedia(a: Agent, field: string, key: string) {
    await api.updateAgent(a.key, {
      system_prompt: a.system_prompt,
      model_profile_key: a.model_profile_key ?? "",
      params_json: { [field]: key },
    });
    refresh();
  }

  /** 默认推理强度（params_json.reasoning_effort）：输入框右下角切换「推理等级」时也写这里。
   *  只传 params_json，避免顺手覆盖 system_prompt / 打上「自定义提示词」标记。 */
  async function bindEffort(a: Agent, key: string) {
    await api.updateAgent(a.key, { params_json: { reasoning_effort: key } });
    refresh();
  }

  const ico = (d: ReactNode) => (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      {d}
    </svg>
  );
  const CATS: { v: typeof tab; label: string; icon: ReactNode }[] = [
    { v: "profile", label: "个人资料", icon: ico(<><circle cx="12" cy="8" r="3.6" /><path d="M4.5 20a7.5 7.5 0 0 1 15 0" /></>) },
    { v: "assistant", label: "助手行为", icon: ico(<><path d="M12 3a5 5 0 0 1 5 5v1a2 2 0 0 1 2 2v1a2 2 0 0 1-2 2h-1v2H8v-2H7a2 2 0 0 1-2-2v-1a2 2 0 0 1 2-2V8a5 5 0 0 1 5-5z" /><path d="M9.5 9h.01M14.5 9h.01M9 13.5h6" /></>) },
    { v: "appearance", label: "外观", icon: ico(<><circle cx="12" cy="12" r="9" /><path d="M8 14s1.4 2 4 2 4-2 4-2" /><path d="M9 9h.01M15 9h.01" /></>) },
    { v: "skills", label: "技能", icon: ico(<><circle cx="12" cy="12" r="4" /><path d="M12 3v3M12 18v3M3 12h3M18 12h3" /></>) },
    { v: "agents", label: "智能体", icon: ico(<><rect x="4" y="7" width="16" height="12" rx="2" /><path d="M12 3v4M9 12h.01M15 12h.01M9.5 16h5" /></>) },
    { v: "profiles", label: "模型配置", icon: ico(<><path d="M4 8h9M19 8h1M4 16h3M13 16h7" /><circle cx="16" cy="8" r="2.2" /><circle cx="10" cy="16" r="2.2" /></>) },
    { v: "providers", label: "素材供应商", icon: ico(<><path d="M21 8 12 3 3 8v8l9 5 9-5z" /><path d="M3 8l9 5 9-5M12 13v8" /></>) },
    { v: "mcp", label: "MCP 服务器", icon: ico(<><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path d="M7 7.5h.01M7 16.5h.01" /></>) },
    { v: "channels", label: "消息通道", icon: ico(<><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.9-.9L3 21l1.9-4.9A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z" /><path d="M8.5 11h.01M12 11h.01M15.5 11h.01" /></>) },
    { v: "memory", label: "项目记忆", icon: ico(<><path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2z" /><path d="M8 3v18M12 8h4M12 12h4" /></>) },
    { v: "storage", label: "存储空间", icon: ico(<><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>) },
    { v: "logs", label: "系统日志", icon: ico(<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 8h10M7 12h10M7 16h6" /></>) },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      onClick={onClose}
    >
      <div
        className="w-[min(1180px,94vw)] h-[86vh] bg-white rounded-lg shadow-xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shrink-0 flex items-center gap-2 px-4 h-12 border-b border-slate-200 bg-slate-50">
          <span className="font-semibold text-slate-700">设置</span>
          <button
            className="ml-auto p-1 rounded hover:bg-slate-200 text-slate-500"
            onClick={onClose}
            title="关闭设置（Esc）"
            aria-label="关闭设置"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex-1 flex overflow-hidden">
          <nav className="w-44 shrink-0 border-r border-slate-200 bg-slate-50 p-2 space-y-1 overflow-auto">
            {CATS.map((c) => (
              <button
                key={c.v}
                onClick={() => setTab(c.v)}
                className={`w-full flex items-center gap-2 px-2.5 py-2 rounded text-sm text-left ${
                  tab === c.v
                    ? "bg-brand-50 text-brand-700 font-medium"
                    : "text-slate-600 hover:bg-slate-200"
                }`}
              >
                {c.icon}
                <span>{c.label}</span>
              </button>
            ))}
          </nav>

          <div className="flex-1 overflow-auto p-5">
      {msg && <div className="text-xs text-green-700 mb-2">{msg}</div>}

      {tab === "profile" && <ProfileTab />}

      {tab === "assistant" && <AssistantTab />}

      {tab === "appearance" && <AppearanceTab />}

      {tab === "skills" && <SkillsTab />}

      {tab === "memory" && <MemoryTab />}

      {tab === "storage" && <StorageTab />}

      {tab === "mcp" && <McpTab />}

      {tab === "channels" && <ChannelsTab />}

      {tab === "logs" && <SystemLogs open onClose={() => {}} embedded />}

      {tab === "agents" && (
        <div className="space-y-3">
          {agents.map((a) => {
            const bound = profiles.find((p) => p.key === a.model_profile_key) || null;
            // 对话档位才能当"大脑"：不过滤的话助手能绑上一个出图档位，运行时报模型不存在
            const llmProfiles = profiles.filter((p) => (p.kind || "llm") === "llm");
            const imageProfiles = profiles.filter((p) => p.kind === "image");
            const audioProfiles = profiles.filter((p) => p.kind === "audio");
            const boundImage = imageProfiles.find((p) => p.key === a.params_json?.image_profile) || null;
            const boundAudio = audioProfiles.find((p) => p.key === a.params_json?.audio_profile) || null;
            // 「对话模型」下拉直接列出「供应商 → 模型目录」，选中即自动建档位并绑定。
            // 以前只列已有档位(llmProfiles)，新加的供应商就算拉到了模型也一个都不出现，
            // 用户以为「配了模型却选不了」。已建档位的模型显示为「已就绪」。
            const modelRows: { pid: string; plabel: string; model: string; key: string }[] = [];
            const seenModel = new Set<string>();
            for (const pv of llmProviders) {
              if (!pv.enabled) continue;
              for (const m of Array.isArray(pv.models) ? pv.models : []) {
                const name = typeof m === "string" ? m : m?.name || "";
                if (!name) continue;
                const sig = pv.id + "\u0000" + name;
                if (seenModel.has(sig)) continue;
                seenModel.add(sig);
                const exist = llmProfiles.find((p) => p.provider_id === pv.id && p.model === name);
                modelRows.push({ pid: pv.id, plabel: pv.label, model: name, key: exist?.key || "" });
              }
            }
            return (
            <div key={a.key} className="bg-white rounded border border-slate-200 p-4">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium">{ROLE_NAMES[a.key] || a.key}</span>
                <span className="text-[11px] mono text-slate-400">{a.key}</span>
                {a.is_default && <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">默认</span>}
                <div className="ml-auto flex items-center gap-2 text-sm flex-wrap">
                  {/* 默认对话模型：写入 agents.model_profile_key。输入框右下角选择器选「跟随默认」时
                      就走这里的档位；不绑则回退到 .env 全局模型。以前这里只是只读提示、只能去输入框
                      选，导致「选完模型自检仍报未绑定档位」——现在直接给下拉能绑。 */}
                  <label className="text-xs text-slate-500 flex items-center gap-1" title="输入框右下角选「跟随默认」时用这个档位；不绑则走 .env 全局模型">
                    对话模型
                    <SearchSelect
                      className="border rounded px-2 py-1 text-xs w-[200px]"
                      value={a.model_profile_key ?? ""}
                      title="选择要用的对话模型：列的是各供应商已拉取的模型；选中会自动建档位并绑定。不绑则走 .env 全局模型"
                      searchPlaceholder="搜索模型…"
                      options={[
                        { value: "", label: "跟随 .env 全局（不绑定）" },
                        ...modelRows.map((r) => ({
                          value: r.key || `pm:${r.pid}:${r.model}`,
                          label: `${r.model}${r.key ? "（已就绪）" : ""}`,
                          keywords: `${r.model} ${r.plabel || ""}`,
                          group: r.plabel || "供应商",
                        })),
                        ...(bound && !modelRows.some((r) => r.key === a.model_profile_key)
                          ? [{
                              value: a.model_profile_key ?? "",
                              label: `${bound.label}${bound.model ? ` · ${bound.model}` : ""}${bound.provider_id ? "（供应商已停用）" : ""}`,
                            }]
                          : []),
                      ]}
                      onChange={(v) => {
                        if (!v) { bindModel(a, ""); return; }
                        // 已有档位（含历史档位）→ 直接绑
                        if (llmProfiles.some((p) => p.key === v)) { bindModel(a, v); return; }
                        // 供应商模型目录里的模型 → 自动建档位再绑
                        if (v.startsWith("pm:")) {
                          const rest = v.slice(3);
                          const i = rest.indexOf(":");
                          bindProviderModel(a, rest.slice(0, i), rest.slice(i + 1));
                          return;
                        }
                        bindModel(a, v);
                      }}
                    />
                  </label>
                  <label className="text-xs text-slate-500 flex items-center gap-1"
                    title="默认推理强度：输入框右下角切换「推理等级」会写回这里；不指定=走模型端点默认">
                    推理等级
                    <SearchSelect
                      className="border rounded px-2 py-1 text-xs w-[112px]"
                      value={a.params_json?.reasoning_effort ?? ""}
                      title="默认推理强度。输入框右下角切换「推理等级」时也写这里；不指定=走端点默认"
                      searchPlaceholder="搜索…"
                      options={[
                        { value: "", label: "Default" },
                        { value: "none", label: "None" },
                        { value: "minimal", label: "Minimal" },
                        { value: "low", label: "Low" },
                        { value: "medium", label: "Medium" },
                        { value: "high", label: "High" },
                      ]}
                      onChange={(v) => bindEffort(a, v)}
                    />
                  </label>
                  <label className="text-xs text-slate-500 flex items-center gap-1">美术模型
                    <SearchSelect
                      className="border rounded px-2 py-1 text-xs w-[130px]"
                      value={a.params_json?.image_profile ?? ""}
                      title="选择美术（图像生成）模型档位"
                      searchPlaceholder="搜索档位…"
                      options={[
                        { value: "", label: "默认（ComfyUI 环境变量）" },
                        ...imageProfiles.map((p) => ({ value: p.key, label: p.label, keywords: p.label })),
                        ...(boundImage && !imageProfiles.some((p) => p.key === a.params_json?.image_profile)
                          ? [{ value: a.params_json?.image_profile ?? "", label: `${a.params_json?.image_profile}（已失效）` }]
                          : []),
                      ]}
                      onChange={(v) => bindMedia(a, "image_profile", v)}
                    />
                  </label>
                  <label className="text-xs text-slate-500 flex items-center gap-1">配音模型
                    <SearchSelect
                      className="border rounded px-2 py-1 text-xs w-[130px]"
                      value={a.params_json?.audio_profile ?? ""}
                      title="选择配音（音频生成）模型档位"
                      searchPlaceholder="搜索档位…"
                      options={[
                        { value: "", label: "默认（占位模式）" },
                        ...audioProfiles.map((p) => ({ value: p.key, label: p.label, keywords: p.label })),
                        ...(boundAudio && !audioProfiles.some((p) => p.key === a.params_json?.audio_profile)
                          ? [{ value: a.params_json?.audio_profile ?? "", label: `${a.params_json?.audio_profile}（已失效）` }]
                          : []),
                      ]}
                      onChange={(v) => bindMedia(a, "audio_profile", v)}
                    />
                  </label>
                  <button
                    className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
                    onClick={() => { setEditing(editing === a.key ? null : a.key); setDraft(a.system_prompt); }}
                  >
                    {editing === a.key ? "收起" : "编辑 Prompt"}
                  </button>
                  {!a.is_default && (
                    <button
                      className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50 text-red-600"
                      onClick={async () => { await api.resetAgent(a.key); refresh(); }}
                    >
                      重置
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                {a.tools.map((t) => (
                  <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 mono">{t}</span>
                ))}
              </div>
              {/* 三个绑定各自跑在哪个模型/哪个站点上：绑错或供应商停用要能一眼看出来 */}
              <div className="mt-2 text-[11px] text-slate-500 flex flex-wrap items-center gap-x-4 gap-y-1">
                <span>
                  会话：{bound ? (
                    <>
                      <span className="mono text-slate-700">{bound.model || "（未选模型）"}</span>
                      <span className="text-slate-400"> · </span>
                      <span>{bound.provider_label
                        ? `供应商 ${bound.provider_label}`
                        : bound.base_url ? `内联地址 ${bound.base_url}` : "供应商未绑（走环境变量）"}</span>
                      {!bound.provider_id && !bound.base_url && (
                        <span className="ml-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">
                          没有密钥来源，将使用环境变量默认
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="text-slate-400">全局默认（环境变量）</span>
                  )}
                  <span className="text-slate-400"> · </span>
                  <span title="默认推理强度（输入框右下角切换会写回这里）">
                    推理：{a.params_json?.reasoning_effort
                      ? <span className="mono text-slate-700">{EFFORT_LABELS[a.params_json.reasoning_effort] || a.params_json.reasoning_effort}</span>
                      : <span className="text-slate-400">端点默认</span>}
                  </span>
                </span>
                <span>
                  美术：{boundImage ? (
                    <span className="mono text-slate-700">{boundImage.model || boundImage.label}</span>
                  ) : (
                    <span className="text-slate-400">ComfyUI 环境变量</span>
                  )}
                </span>
                <span>
                  配音：{boundAudio ? (
                    <span className="mono text-slate-700">{boundAudio.model || boundAudio.label}</span>
                  ) : (
                    <span className="text-slate-400">占位模式（未配置）</span>
                  )}
                </span>
              </div>
              {editing === a.key && (
                <div className="mt-3">
                  <textarea
                    className="w-full h-40 border rounded p-2 text-xs mono"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                  <div className="mt-2 flex gap-2">
                    <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={() => saveAgent(a)}>保存</button>
                    <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setEditing(null)}>取消</button>
                  </div>
                </div>
              )}
            </div>
            );
          })}
        </div>
      )}

      {tab === "profiles" && (
        <ModelConfigTab profiles={profiles} providers={llmProviders} refresh={refresh} />
      )}

      {tab === "providers" && <ProvidersTab providers={providers} refresh={refresh} />}
          </div>
        </div>
      </div>
    </div>
  );
}

function ProvidersTab({ providers, refresh }: { providers: Provider[]; refresh: () => void }) {
  const [specs, setSpecs] = useState<any[]>([]);
  const [sel, setSel] = useState("");
  const [label, setLabel] = useState("");
  const [conf, setConf] = useState("{}");
  const [err, setErr] = useState("");

  useEffect(() => {
    api.driverSpecs().then((r) => {
      setSpecs(r.drivers);
      if (r.drivers.length) setSel(r.drivers[0].key);
    }).catch(() => {});
  }, []);

  const spec = specs.find((d) => d.key === sel);

  async function add() {
    setErr("");
    try {
      const config_json = JSON.parse(conf || "{}");
      await api.createProvider({
        driver_key: sel, label: label || sel,
        kind: spec?.kind || "image", enabled: true, config_json,
      });
      setLabel("");
      setConf("{}");
      refresh();
    } catch (e: any) {
      setErr(e?.message?.includes("JSON") ? "配置 JSON 格式错误" : e?.message || "添加失败");
    }
  }

  return (
    <div>
      <div className="bg-white rounded border border-slate-200 p-4 mb-4 text-sm">
        <div className="grid grid-cols-2 gap-3">
          <select className="border rounded px-2 py-1" value={sel} onChange={(e) => setSel(e.target.value)}>
            {specs.map((d) => (
              <option key={d.key} value={d.key}>{d.label}（{d.kind}）</option>
            ))}
          </select>
          <input className="border rounded px-2 py-1" placeholder="显示名" value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
        {spec && (
          <div className="mt-2 text-[11px] text-slate-400">
            配置字段：{(spec.config_schema?.properties && Object.keys(spec.config_schema.properties).join(", ")) || "无"}
          </div>
        )}
        <textarea className="w-full h-20 border rounded p-2 text-xs mono mt-2" value={conf} onChange={(e) => setConf(e.target.value)} />
        <div className="mt-2">
          <button className="ga-btn ga-btn-primary" onClick={add}>添加供应商</button>
          {err && <span className="text-xs text-red-600 ml-2">{err}</span>}
        </div>
      </div>
      <div className="space-y-2">
        {providers.map((p) => (
          <div key={p.id} className="bg-white rounded border border-slate-200 p-3 flex items-center gap-3 text-sm">
            <span className="font-medium">{p.label}</span>
            <span className="text-[11px] mono text-slate-400">{p.driver_key}</span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${p.enabled ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-400"}`}>
              {p.enabled ? "启用" : "停用"}
            </span>
            <button className="ml-auto ga-btn ga-btn-danger ga-btn-sm" onClick={async () => { await api.deleteProvider(p.id); refresh(); }}>删除</button>
          </div>
        ))}
        {providers.length === 0 && <div className="text-sm text-slate-400">暂无供应商；未配置时图像/音频使用本地占位生成。</div>}
      </div>
    </div>
  );
}

type McpServer = {
  id: string; name: string; transport: string;
  command: string; args: string[]; env: Record<string, string>;
  url: string; enabled: boolean;
};

function McpTab() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [items, setItems] = useState<McpServer[]>([]);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState<McpServer>({
    id: "", name: "", transport: "stdio", command: "", args: [],
    env: {}, url: "", enabled: true,
  });
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");

  useEffect(() => {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps) => {
      const list = ps.map((p: any) => ({ id: p.id, name: p.name }));
      setProjects(list);
      // 进页面默认选中第一个项目，省掉每次手动选
      setProjectId((cur) => cur || list[0]?.id || "");
    }).catch(() => {});
  }, [currentTeamId]);

  function refresh() {
    if (!projectId) return;
    api.mcpServers(projectId).then(setItems).catch(() => {});
  }
  useEffect(refresh, [projectId]);

  function resetForm() {
    setForm({ id: "", name: "", transport: "stdio", command: "", args: [], env: {}, url: "", enabled: true });
    setArgsText(""); setEnvText("");
  }

  async function save() {
    setMsg("");
    let args: string[] = [];
    let env: Record<string, string> = {};
    try {
      if (argsText.trim()) args = JSON.parse(argsText);
      if (envText.trim()) env = JSON.parse(envText);
    } catch {
      setMsg("args / env 必须是合法 JSON");
      return;
    }
    const body = { ...form, args, env };
    try {
      if (form.id) {
        await api.updateMcpServer(projectId, form.id, body);
        setMsg("已更新（下次 run 生效）");
      } else {
        await api.createMcpServer(projectId, body);
        setMsg("已添加（下次 run 生效）");
      }
      resetForm();
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    }
  }

  function editOne(m: McpServer) {
    setForm({ ...m });
    setArgsText(JSON.stringify(m.args || []));
    setEnvText(JSON.stringify(m.env || {}, null, 0));
  }

  return (
    <div>
      <div className="bg-white rounded border border-slate-200 p-4 mb-4 text-sm">
        <div className="flex items-center gap-2 mb-3">
          <SearchSelect className="border rounded px-2 py-1 text-sm w-[180px]" value={projectId}
            title="选择项目" searchPlaceholder="搜索项目…"
            options={[{ value: "", label: "选择项目" }, ...projects.map((p) => ({ value: p.id, label: p.name, keywords: p.name }))]}
            onChange={(v) => { setProjectId(v); resetForm(); }} />
          <span className="text-[11px] text-slate-400">
            项目级 MCP 服务器：工具以 mcp_名称_工具名 注入各角色。改配置下次 run 生效。
          </span>
        </div>
        {projectId && (
          <div className="grid grid-cols-2 gap-2">
            <input className="border rounded px-2 py-1 text-xs" placeholder="名称（如 fs、browser）"
              value={form.name} disabled={!!form.id}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <select className="border rounded px-2 py-1 text-xs" value={form.transport}
              onChange={(e) => setForm({ ...form, transport: e.target.value })}>
              <option value="stdio">stdio（本地进程）</option>
              <option value="http">http（远程 URL）</option>
            </select>
            {form.transport === "stdio" ? (
              <>
                <input className="border rounded px-2 py-1 text-xs col-span-2"
                  placeholder='command（如 npx / python）'
                  value={form.command}
                  onChange={(e) => setForm({ ...form, command: e.target.value })} />
                <textarea className="border rounded px-2 py-1 text-xs col-span-2 mono"
                  placeholder='args JSON 数组，如 ["-y", "@modelcontextprotocol/server-fs", "./"]'
                  value={argsText} onChange={(e) => setArgsText(e.target.value)} />
                <textarea className="border rounded px-2 py-1 text-xs col-span-2 mono"
                  placeholder='env JSON 对象，如 {"API_KEY": "xxx"}'
                  value={envText} onChange={(e) => setEnvText(e.target.value)} />
              </>
            ) : (
              <input className="border rounded px-2 py-1 text-xs col-span-2" placeholder="MCP 端点 URL"
                value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
            )}
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <input type="checkbox" checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              启用
            </label>
            <div className="flex gap-2 justify-end">
              {form.id && (
                <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={resetForm}>取消编辑</button>
              )}
              <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={save}>
                {form.id ? "保存修改" : "添加"}
              </button>
            </div>
          </div>
        )}
        {msg && <div className="text-xs text-green-700 mt-2">{msg}</div>}
      </div>

      {!projectId ? (
        <div className="text-sm text-slate-400">请先选择一个项目。</div>
      ) : items.length === 0 ? (
        <div className="text-sm text-slate-400">
          该项目暂无 MCP 服务器；添加后智能体可在 run 中调用其工具（如文件系统、浏览器、搜索等）。
        </div>
      ) : (
        <div className="space-y-1.5">
          {items.map((m) => (
            <div key={m.id} className="bg-white rounded border border-slate-200 p-3 flex items-start gap-2 text-sm">
              <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${
                m.enabled ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-500"
              }`}>
                {m.enabled ? "启用" : "停用"}
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-medium">{m.name}
                  <span className="text-[11px] text-slate-400 ml-2 mono">{m.transport}</span>
                </div>
                <div className="text-[11px] text-slate-500 mono truncate">
                  {m.transport === "http" ? m.url : `${m.command} ${(m.args || []).join(" ")}`}
                </div>
              </div>
              <button className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
                onClick={() => editOne(m)}>编辑</button>
              <button className="ga-btn ga-btn-danger ga-btn-sm shrink-0"
                onClick={async () => {
                  if (!confirm(`删除 MCP 服务器 ${m.name}？`)) return;
                  await api.deleteMcpServer(projectId, m.id);
                  refresh();
                }}>删除</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MemoryCard({ m, onDelete }: { m: ProjectMemory; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  const tags = (m.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
  const title = (m.title || "").trim() || m.content.split("\n")[0].slice(0, 24);
  const stamp = localStamp(m.updated_at || m.created_at || "");
  return (
    <div className="bg-white rounded-lg border border-slate-200 p-3">
      <div className="flex items-start gap-2">
        <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${
          m.kind === "manual" ? "bg-sky-100 text-sky-700" : "bg-slate-100 text-slate-500"
        }`}>
          {m.kind === "manual" ? "手动" : "自动"}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-medium text-slate-800 text-sm">{title}</span>
            {tags.map((t) => (
              <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{t}</span>
            ))}
          </div>
          <div className={`mt-1 text-[12.5px] leading-relaxed text-slate-600 whitespace-pre-wrap break-words ${open ? "" : "line-clamp-3"}`}>
            {m.content}
          </div>
          <div className="mt-1 flex items-center gap-2 flex-wrap text-[11px] text-slate-400">
            <span>{stamp}</span>
            {m.source_run_id && <span>· 来源 run {m.source_run_id.slice(0, 8)}</span>}
            <button className="text-slate-500 hover:text-slate-700 underline underline-offset-2"
              onClick={() => setOpen((v) => !v)}>
              {open ? "收起" : "展开正文"}
            </button>
          </div>
        </div>
        <button className="text-xs text-red-600 shrink-0" onClick={onDelete}>删除</button>
      </div>
    </div>
  );
}

function MemoryTab() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [items, setItems] = useState<ProjectMemory[]>([]);
  const [notes, setNotes] = useState<ProjectNote[]>([]);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState("");
  const [merging, setMerging] = useState(false);

  useEffect(() => {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps) => {
      const list = ps.map((p: any) => ({ id: p.id, name: p.name }));
      setProjects(list);
      // 进页面默认选中第一个项目，省掉每次手动选
      setProjectId((cur) => cur || list[0]?.id || "");
    }).catch(() => {});
  }, [currentTeamId]);

  function refresh() {
    if (!projectId) return;
    api.memories(projectId).then(setItems).catch(() => {});
    api.projectNotes(projectId).then((r: any) => setNotes(r || [])).catch(() => setNotes([]));
  }
  useEffect(refresh, [projectId]);

  async function add() {
    if (!draft.trim()) return;
    setMsg("");
    try {
      await api.addMemory(projectId, draft.trim());
      setDraft("");
      setMsg("已添加记忆");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "添加失败");
    }
  }

  async function merge() {
    if (!projectId || merging) return;
    setMsg("");
    setMerging(true);
    try {
      const r: any = await api.mergeMemories(projectId);
      setMsg(r.used_ai
        ? `融合完成：${r.before} 条 → ${r.after} 条（模型已合并全部记忆）`
        : `融合完成：${r.before} 条 → ${r.after} 条（模型不可用，已按本地规则合并为一份）`);
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "融合失败");
    } finally {
      setMerging(false);
    }
  }

  const sortedItems = [...items].sort((a, b) => {
    const am = a.kind === "manual" ? 0 : 1;
    const bm = b.kind === "manual" ? 0 : 1;
    if (am !== bm) return am - bm;
    return (b.updated_at || b.created_at).localeCompare(a.updated_at || a.created_at);
  });

  return (
    <div>
      <div className="bg-white rounded border border-slate-200 p-4 mb-4 text-sm">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <SearchSelect
            className="border rounded px-2 py-1 text-sm w-[180px]"
            value={projectId}
            title="选择项目"
            searchPlaceholder="搜索项目…"
            options={[{ value: "", label: "选择项目" }, ...projects.map((p) => ({ value: p.id, label: p.name, keywords: p.name }))]}
            onChange={setProjectId}
          />
          <span className="text-[11px] text-slate-400 flex-1 min-w-[260px] leading-relaxed">
            每轮 run 收尾由模型把本次结果与历史对话的记忆融合成结构化条目（重复合并、冲突以新为准，上限 12 条）；
            对话时只注入标题索引，需要细节由助手用 memory_read 按需读取。手动约定不参与改写。
          </span>
          <button
            className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
            disabled={!projectId || merging}
            onClick={merge}
            title="立即让模型融合一次：合并跨对话的重复记忆。不想要这步就等 run 收尾自动融合"
          >
            {merging ? "融合中…" : "AI 融合"}
          </button>
        </div>
        {projectId && (
          <div className="flex gap-2">
            <input
              className="flex-1 border rounded px-2 py-1 text-xs"
              placeholder="手动补充一条项目记忆，如：美术风格为像素风、配色以深蓝为主"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && add()}
            />
            <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={add}>
              添加
            </button>
          </div>
        )}
        {msg && <div className="text-xs text-green-700 mt-2">{msg}</div>}
      </div>

      {projectId && notes.length > 0 && (
        <div className="mb-4">
          <div className="text-xs text-slate-500 mb-1.5">
            改动经验（run 收尾判断「值得记」才写，落在工作区 .trace/notes/ 的分类 md；后续 run 注入的是条目标题索引，智能体按标题自己去读正文）。
            每类满 40 条后按「已失效最先淘汰 → 其余丢最旧 → 钉住的永不淘汰」处理；「标失效」是留痕，不是删除。
          </div>
          <div className="space-y-1.5">
            {notes.map((n) => (
              <div key={`${n.category}:${n.key}`}
                   className={`bg-white rounded border p-3 flex items-start gap-2 text-sm ${
                     n.stale ? "border-slate-200 opacity-60" : "border-emerald-100"}`}>
                <span className="text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 bg-emerald-50 text-emerald-700">
                  {n.category_cn}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-700 break-words">
                    {n.title}
                    {n.keep && (
                      <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-700 align-middle">
                        钉住
                      </span>
                    )}
                    {n.stale && (
                      <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-slate-100 text-slate-500 align-middle">
                        已失效
                      </span>
                    )}
                  </div>
                  <div className="whitespace-pre-wrap break-words text-[12.5px] text-slate-600 mt-0.5">{n.detail}</div>
                  {n.files.length > 0 && (
                    <div className="text-[11px] text-slate-400 mt-1 break-words">涉及：{n.files.join("、")}</div>
                  )}
                  <div className="text-[11px] text-slate-400 mt-0.5">
                    {n.when}{n.run_id && ` · run ${n.run_id.slice(0, 8)}`}
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <button
                    className="text-xs text-amber-700"
                    title={n.keep
                      ? "取消钉住：不再豁免淘汰"
                      : "钉住：每类满了 40 条时优先淘汰最旧的，钉住的永不淘汰"}
                    onClick={async () => {
                      await api.setProjectNoteFlag(projectId, n.category, n.key, { keep: !n.keep });
                      refresh();
                    }}
                  >
                    {n.keep ? "取消钉住" : "钉住"}
                  </button>
                  <button
                    className="text-xs text-slate-500"
                    title={n.stale
                      ? "恢复：重新参与注入与正常淘汰"
                      : "标失效：不再注入上下文、优先淘汰（留痕，不删除）"}
                    onClick={async () => {
                      await api.setProjectNoteFlag(projectId, n.category, n.key, { stale: !n.stale });
                      refresh();
                    }}
                  >
                    {n.stale ? "恢复" : "标失效"}
                  </button>
                  <button
                    className="text-xs text-red-600"
                    onClick={async () => {
                      if (!confirm("删除这条改动经验？")) return;
                      await api.deleteProjectNote(projectId, n.category, n.key);
                      refresh();
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {!projectId ? (
        <div className="text-sm text-slate-400">请先选择一个项目。</div>
      ) : items.length === 0 ? (
        <div className="text-sm text-slate-400">该项目暂无记忆；完成一次需求后要点会自动沉淀到这里。</div>
      ) : (
        <div>
          <div className="text-xs text-slate-500 mb-1.5">
            结构化记忆 · 共 {items.length} 条（自动 {items.filter((m) => m.kind !== "manual").length}
            {" / "}手动 {items.filter((m) => m.kind === "manual").length}）
          </div>
          <div className="space-y-1.5">
            {sortedItems.map((m) => (
              <MemoryCard
                key={m.id}
                m={m}
                onDelete={async () => {
                  if (!confirm("删除这条记忆？")) return;
                  await api.deleteMemory(projectId, m.id);
                  refresh();
                }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
