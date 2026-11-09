import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export interface ModelChoice {
  /** ModelProfile.key；空 = 跟随角色绑定的默认档位 */
  profile: string;
  /** reasoning_effort；空 = 模型默认（Default） */
  effort: string;
}

export const DEFAULT_MODEL_CHOICE: ModelChoice = { profile: "", effort: "" };

const EFFORTS = [
  { v: "", t: "Default" },
  { v: "none", t: "None" },
  { v: "minimal", t: "Minimal" },
  { v: "low", t: "Low" },
  { v: "medium", t: "Medium" },
  { v: "high", t: "High" },
];

interface ProfileRow {
  key: string;
  label: string;
  kind: string;
  model: string;
  provider_id?: string | null;
  provider_label?: string | null;
  params_json?: { reasoning_effort?: string; context_window?: number };
}

interface ProviderRow {
  id: string;
  key: string;
  label: string;
  enabled: boolean;
  /** 兼容两种存量格式：字符串列表 或 {name, context_window} 对象列表 */
  models: Array<string | { name?: string; context_window?: number }>;
}

function modelName(m: string | { name?: string } | null | undefined): string {
  if (!m) return "";
  return typeof m === "string" ? m : m.name || "";
}

function modelWindow(m: string | { context_window?: number } | null | undefined): number {
  if (!m || typeof m === "string") return 0;
  return Number(m.context_window) || 0;
}

/**
 * 输入框右下角的模型选择器（图三/图四样式）：
 * 胶囊按钮显示「模型名 + 推理等级」，点开是「模型 / 推理等级」两级面板。
 * 只选对话档位（kind=llm）；选择随会话记忆，没选过就跟随角色绑定的默认档位。
 */
export default function ModelPicker({
  value,
  onChange,
  disabled,
}: {
  value: ModelChoice;
  /** silent=true 表示这是程序化校正（挡位已不存在→清回跟随默认），不是用户手点。
   *  执行中的 Chat 据此区分：只有用户手点才触发「运行中切换模型」。 */
  onChange: (v: ModelChoice, silent?: boolean) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [sub, setSub] = useState<"model" | "effort" | null>(null);
  const [profiles, setProfiles] = useState<ProfileRow[]>([]);
  const [providers, setProviders] = useState<ProviderRow[]>([]);
  const [defaultKey, setDefaultKey] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [profilesReady, setProfilesReady] = useState(false);
  const [pendingModel, setPendingModel] = useState<string>("");
  const [pickError, setPickError] = useState<string>("");
  const [modelQ, setModelQ] = useState<string>("");
  const boxRef = useRef<HTMLDivElement>(null);

  const loadAll = () => {
    return Promise.all([
      api.modelProfiles().catch(() => ({})),
      api.llmProviders().catch(() => ({ providers: [] })),
      api.agents().catch(() => ({ agents: [] })),
    ]).then(([pr, pv, ag]: any) => {
      setProfiles(Array.isArray(pr?.profiles) ? pr.profiles : []);
      if (Array.isArray(pr?.profiles)) setProfilesReady(true);
      setProviders(Array.isArray(pv?.providers) ? pv.providers : []);
      const main = (ag?.agents || []).find((a: any) => a.key === "main");
      setDefaultKey(main?.model_profile_key || "");
    });
  };

  useEffect(() => {
    let dead = false;
    setLoading(true);
    loadAll().then(() => { if (!dead) setLoading(false); });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 每次展开再刷一次：设置页刚改过供应商/档位时不用刷新整页
  useEffect(() => {
    if (!open) return;
    let dead = false;
    setPickError("");
    Promise.all([
      api.modelProfiles().catch(() => ({})),
      api.llmProviders().catch(() => ({ providers: [] })),
      api.agents().catch(() => ({ agents: [] })),
    ]).then(([pr, pv, ag]: any) => {
      if (dead) return;
      setProfiles(Array.isArray(pr?.profiles) ? pr.profiles : []);
      if (Array.isArray(pr?.profiles)) setProfilesReady(true);
      setProviders(Array.isArray(pv?.providers) ? pv.providers : []);
      const main = (ag?.agents || []).find((a: any) => a.key === "main");
      setDefaultKey(main?.model_profile_key || "");
    });
    return () => { dead = true; };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
        setSub(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); setSub(null); }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const llmProfiles = profiles.filter((p) => (p.kind || "llm") === "llm");
  const effectiveKey = value.profile || defaultKey;
  const current = llmProfiles.find((p) => p.key === effectiveKey) || null;
  const modelLabel = current?.model || current?.label || (loading ? "加载中…" : "默认模型");
  const effortLabel = EFFORTS.find((e) => e.v === value.effort)?.t || "Default";

  // 供应商/档位被删后，localStorage 里可能还留着旧 key：继续带着它发消息会被后端
  // 以「所选模型档位不存在」拒绝。这里发现档位已不存在就清回「跟随默认」，避免卡死。
  useEffect(() => {
    if (!profilesReady || !value.profile) return;
    if (llmProfiles.some((p) => p.key === value.profile)) return;
    onChange({ ...value, profile: "" }, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profilesReady, profiles, value.profile]);

  // 供应商 → 可加载模型：目录里的 {name, context_window} ∪ 已建档位的模型名，去重后保留窗口
  const groups = providers.map((pv) => {
    const map = new Map<string, number>();
    for (const m of Array.isArray(pv.models) ? pv.models : []) {
      const name = modelName(m);
      if (name && !map.has(name)) map.set(name, modelWindow(m));
    }
    for (const p of llmProfiles) {
      if (p.provider_id === pv.id && p.model && !map.has(p.model)) {
        map.set(p.model, Number(p.params_json?.context_window) || 0);
      }
    }
    return { pv, models: [...map.entries()].sort((a, b) => a[0].localeCompare(b[0])) };
  }).filter((g) => g.pv.enabled);

  const isSelected = (providerId: string, model: string) =>
    !!current && current.provider_id === providerId && current.model === model;

  // 模型列表常有一长串（多供应商 × 每供应商几十个模型），展开后按关键字过滤，免得翻半天
  const shownGroups = (() => {
    const needle = modelQ.trim().toLowerCase();
    if (!needle) return groups;
    return groups
      .map(({ pv, models }) => ({
        pv,
        models: models.filter(([m]) =>
          m.toLowerCase().includes(needle) || (pv.label || "").toLowerCase().includes(needle)),
      }))
      .filter((g) => g.models.length > 0);
  })();

  const pick = (patch: Partial<ModelChoice>) => {
    onChange({ ...value, ...patch });
    setOpen(false);
    setSub(null);
  };

  /** 选「供应商 → 模型」：后端复用/自动建档位后按会话记住，无需进设置页 */
  const pickModel = async (providerId: string, model: string) => {
    if (pendingModel) return;
    setPendingModel(model);
    setPickError("");
    try {
      const r: any = await api.autoModelProfile(providerId, model);
      if (r?.key) {
        await loadAll().catch(() => undefined);
        pick({ profile: r.key });
      } else {
        setPickError("未能创建模型档位");
      }
    } catch (e: any) {
      setPickError(e?.message || "选择模型失败");
    } finally {
      setPendingModel("");
    }
  };

  return (
    <div className="relative shrink-0" ref={boxRef}>
      <button
        type="button"
        className={`h-8 max-w-[240px] flex items-center gap-1 rounded-full border px-3 text-[12px] transition-colors ${
          open
            ? "border-brand-400 bg-brand-50 text-brand-700"
            : "border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100"
        }`}
        title="选择模型与推理等级（按会话记住）"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => { setOpen((v) => !v); setSub(null); }}
      >
        <span className="truncate font-medium">{modelLabel}</span>
        <span className="shrink-0 text-slate-400">{effortLabel}</span>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`}>
          <path d="m18 15-6-6-6 6" />
        </svg>
      </button>

      {open && (
        <div
          className="absolute bottom-full right-0 z-[80] mb-2 max-h-[70vh] w-[280px] max-w-[calc(100vw-24px)] overflow-auto rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl"
          style={{ backgroundColor: "rgb(var(--ga-panel-rgb, 255 255 255))" }}
        >
          {/* 模型行 */}
          <button
            type="button"
            className={`w-full flex items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-slate-50 ${sub === "model" ? "bg-slate-50" : ""}`}
            onClick={() => setSub((s) => (s === "model" ? null : "model"))}
          >
            <span className="shrink-0 w-14 text-[12px] font-medium text-brand-600">模型</span>
            <span className="flex-1 truncate text-right text-[12.5px] text-slate-700">{modelLabel}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 text-slate-400 transition-transform ${sub === "model" ? "rotate-90" : ""}`}>
              <path d="m9 18 6-6-6-6" />
            </svg>
          </button>
          {sub === "model" && (
            <div className="mb-1 rounded-lg border border-slate-100 bg-slate-50/60 p-1">
              <div className="sticky -top-1 z-10 mb-1 bg-slate-50/95 p-0.5 backdrop-blur">
                <input
                  autoFocus
                  value={modelQ}
                  onChange={(e) => setModelQ(e.target.value)}
                  onKeyDown={(e) => e.stopPropagation()}
                  placeholder="搜索模型 / 供应商…"
                  className="w-full rounded border border-slate-200 px-2 py-1 text-[12px] outline-none focus:border-brand-400"
                />
              </div>
              <div className="max-h-52 overflow-auto">
                <button
                  type="button"
                  className={`w-full text-left rounded px-2 py-1.5 text-[12px] hover:bg-white ${!value.profile ? "text-brand-700 font-medium" : "text-slate-600"}`}
                  onClick={() => pick({ profile: "" })}
                >
                  跟随默认{current ? `（${modelLabel}）` : ""}
                </button>
                {groups.length === 0 && !loading && (
                  <div className="px-2 py-1.5 text-[11px] text-slate-400">
                    还没有可用的 LLM 供应商，请到「设置 → 模型配置」添加。
                  </div>
                )}
                {groups.length > 0 && shownGroups.length === 0 && (
                  <div className="px-2 py-1.5 text-[11px] text-slate-400">没有匹配的模型</div>
                )}
                {pickError && (
                  <div className="px-2 py-1 text-[11px] text-red-500">{pickError}</div>
                )}
                {shownGroups.map(({ pv, models }) => (
                  <div key={pv.id} className="mb-1">
                    <div className="px-2 pt-1.5 pb-0.5 text-[11px] font-semibold text-slate-500">
                      {pv.label}
                    </div>
                    {models.length === 0 && (
                      <div className="px-2 py-1 text-[11px] text-slate-400">
                        尚未拉取模型列表，可到「设置 → 模型配置」点「拉取模型」。
                      </div>
                    )}
                    {models.map(([m, win]) => {
                      const sel = isSelected(pv.id, m);
                      const busy = pendingModel === m;
                      return (
                        <button
                          key={m}
                          type="button"
                          disabled={busy}
                          className={`w-full flex items-center gap-1.5 rounded px-2 py-1.5 text-left text-[12px] hover:bg-white ${sel ? "text-brand-700 font-medium" : "text-slate-600"} ${busy ? "opacity-60" : ""}`}
                          onClick={() => pickModel(pv.id, m)}
                          title={`${pv.label} · ${m}${win ? ` · 上下文窗口 ${win} tokens` : ""}`}
                        >
                          <span className="flex-1 truncate">{m}</span>
                          {!!win && (
                            <span className="shrink-0 text-[10px] text-slate-400">
                              {win >= 1000000 ? `${Math.round(win / 1000000)}M` : `${Math.round(win / 1000)}k`}
                            </span>
                          )}
                          {busy ? (
                            <span className="shrink-0 text-[10px] text-slate-400">…</span>
                          ) : sel ? (
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-brand-600">
                              <path d="M20 6 9 17l-5-5" />
                            </svg>
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 推理等级行 */}
          <button
            type="button"
            className={`w-full flex items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-slate-50 ${sub === "effort" ? "bg-slate-50" : ""}`}
            onClick={() => setSub((s) => (s === "effort" ? null : "effort"))}
          >
            <span className="shrink-0 w-14 text-[12px] font-medium text-brand-600">推理等级</span>
            <span className="flex-1 truncate text-right text-[12.5px] text-slate-700">{effortLabel}</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`shrink-0 text-slate-400 transition-transform ${sub === "effort" ? "rotate-90" : ""}`}>
              <path d="m9 18 6-6-6-6" />
            </svg>
          </button>
          {sub === "effort" && (
            <div className="mb-1 rounded-lg border border-slate-100 bg-slate-50/60 p-1">
              {EFFORTS.map((e) => (
                <button
                  key={e.v || "default"}
                  type="button"
                  className={`w-full text-left rounded px-2 py-1.5 text-[12px] hover:bg-white ${value.effort === e.v ? "text-brand-700 font-medium" : "text-slate-600"}`}
                  onClick={() => pick({ effort: e.v })}
                >
                  {e.t}
                </button>
              ))}
            </div>
          )}

          <div className="px-2 pt-1 pb-0.5 text-[10px] text-slate-400">
            选择按会话记住；「Default」= 模型档位自带的推理强度。
          </div>
        </div>
      )}
    </div>
  );
}
