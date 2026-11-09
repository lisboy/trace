import { useEffect, useRef, useState } from "react";
import { api } from "../api";

/** 模型配置：供应商（地址 + 密钥 + UA）只在这里存一次，
 *  每个供应商维护自己的模型目录（模型名 + 上下文窗口）。
 *  对话实际用哪个模型，在输入框右下角的模型选择器里选（选完自动建档位），
 *  设置页不再单独配置「对话档位」。 */

export type ProviderModel = {
  /** 模型名（供应商 /models 返回的 id，或手动填写） */
  name: string;
  /** 上下文窗口 tokens；0 = 未设置（运行时按默认窗口保守估算） */
  context_window: number;
  /** 是否支持视觉输入：关闭时发图会被适配层替换成一句提示、模型看不到画面 */
  vision?: boolean;
};

export type LlmProviderRow = {
  id: string;
  key: string;
  label: string;
  protocol: string;
  base_url: string | null;
  has_key: boolean;
  key_hint: string | null;
  user_agent: string | null;
  enabled: boolean;
  models: ProviderModel[];
  bound_profiles: number;
};
export type ModelProfileRow = {
  id: string;
  key: string;
  label: string;
  kind: string;
  provider: string;
  model: string;
  provider_id: string | null;
  provider_label: string | null;
  base_url: string | null;
  has_key: boolean;
  key_hint: string | null;
  params_json: { context_window?: number; vision?: boolean; voice?: string; reasoning_effort?: string; [k: string]: any };
  bound_roles: string[];
};

const ROLE_NAMES: Record<string, string> = { main: "助手" };
/** 推理强度：空=不传（走端点默认）；其余原样透传 reasoning_effort 字段 */
const REASONING_EFFORTS = [
  { v: "", t: "推理强度：模型默认" },
  { v: "none", t: "关闭推理（none）" },
  { v: "minimal", t: "很低（minimal）" },
  { v: "low", t: "低（low）" },
  { v: "medium", t: "中（medium）" },
  { v: "high", t: "高（high）" },
];
function effortLabel(v: string): string {
  return REASONING_EFFORTS.find((o) => o.v === v)?.t || v;
}
const PROTOCOLS = [
  { v: "openai", t: "OpenAI 兼容（/v1/chat/completions）" },
  { v: "anthropic", t: "Anthropic（/v1/messages 原生）" },
];
const BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
  + "(KHTML, like Gecko) Chrome/124.0 Safari/537.36";

/** 归一化密钥：去空白/零宽/换行、全角转半角、合并重复 sk- 前缀；纯 hex 裸密钥补 sk-。 */
function cleanKey(raw: string): string {
  let v = raw.replace(/[\s\u00a0\u3000\u200b-\u200d\ufeff]+/g, "");
  v = v.replace(/[\uff01-\uff5e]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
  v = v.replace(/^(?:sk-)+/i, "sk-");
  if (/^[0-9a-f]{32,}$/i.test(v)) v = `sk-${v}`;
  return v;
}

/** 由 Base URL 猜一个能直接粘贴的标识： gonode.si/v1 → gonode-si */
function slugFromUrl(url: string): string {
  try {
    const host = new URL(url).hostname;
    return host.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").slice(0, 32).toLowerCase();
  } catch {
    return "";
  }
}

/** 模型名兼容两种存量格式：字符串 或 {name, context_window} 对象 */
function modelName(m: string | ProviderModel | null | undefined): string {
  if (!m) return "";
  return typeof m === "string" ? m : (m.name || "");
}

/** 新模型的默认上下文窗口：128k，避免输入框空着、运行时按最小窗口保守压缩 */
const DEFAULT_WINDOW = 128000;

/** 解析窗口输入：接受 128000 这样的原始 token 数，也接受 128k / 256k / 1M / 1.5m 简写。
 *  k=1000、m=1000000（与业界标称的 128k / 1M 对齐）。无法解析返回 NaN。 */
function parseWindow(raw: string): number {
  const s = raw.trim().toLowerCase().replace(/[,\s_]/g, "");
  if (!s) return 0;
  const m = s.match(/^(\d+(?:\.\d+)?)([km])?$/);
  if (!m) return NaN;
  const num = parseFloat(m[1]);
  const mult = m[2] === "m" ? 1_000_000 : m[2] === "k" ? 1000 : 1;
  return Math.round(num * mult);
}

/** 把 token 数回显成简写：128000 → 128k、1000000 → 1M、131072 → 131.07k */
function windowText(n: number): string {
  if (!n) return "";
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${+(n / 1000).toFixed(2)}k`;
  return String(n);
}

function windowLabel(n: number): string {
  if (!n) return "窗口未设";
  return `${windowText(n)} 窗口`;
}

/** 上下文窗口输入框：边输边解析，支持 128000 或 128k / 256k / 1M 简写。
 *  编辑期间不打断用户输入，失焦后再统一回显成简写（如 1.5m → 1.5M）。 */
function WindowInput({ value, onCommit }: { value: number; onCommit: (n: number) => void }) {
  const [draft, setDraft] = useState(value ? windowText(value) : "");
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (!editing) setDraft(value ? windowText(value) : "");
  }, [value, editing]);
  const parsed = parseWindow(draft);
  const bad = Number.isNaN(parsed);
  return (
    <div className="flex items-center gap-1.5 shrink-0">
      <input
        className={`border rounded px-2 py-1 text-xs w-28 ${bad ? "border-red-400 bg-red-50" : ""}`}
        placeholder="如 128k / 256k / 1M"
        value={draft}
        onFocus={() => setEditing(true)}
        onChange={(e) => {
          const v = e.target.value;
          setDraft(v);
          const n = parseWindow(v);
          if (!Number.isNaN(n)) onCommit(n);
        }}
        onBlur={() => {
          setEditing(false);
          const n = parseWindow(draft);
          if (Number.isNaN(n)) setDraft(value ? windowText(value) : "");
          else setDraft(n ? windowText(n) : "");
        }}
      />
      <span className={`text-[10px] tabular-nums w-24 shrink-0 ${bad ? "text-red-600" : "text-slate-400"}`}
        title={value ? `${value} tokens` : ""}>
        {bad ? "格式不对" : parsed ? `= ${parsed.toLocaleString()} tokens` : "未设"}
      </span>
    </div>
  );
}

type Props = {
  profiles: ModelProfileRow[];
  providers: LlmProviderRow[];
  refresh: () => void;
};

const emptyPf = {
  key: "", label: "", kind: "image", provider: "comfyui", model: "", base_url: "",
  context_window: "", vision: false, api_key: "", voice: "", provider_id: "",
  reasoning_effort: "",
};

export default function ModelConfigTab({ profiles, providers, refresh }: Props) {
  const [pf, setPf] = useState({ ...emptyPf });
  const [pfOpen, setPfOpen] = useState(false);
  const [err, setErr] = useState("");
  const [showKey, setShowKey] = useState(false);
  // 正在编辑的供应商 id（"" = 新建），null = 表单收起
  const [provForm, setProvForm] = useState<string | null>(null);
  const [pv, setPv] = useState({
    key: "", label: "", protocol: "openai", base_url: "", api_key: "",
    user_agent: "", enabled: true, keyHint: "", models: [] as ProviderModel[],
  });
  const [pvErr, setPvErr] = useState("");
  const [pulling, setPulling] = useState("");
  const [pullMsg, setPullMsg] = useState("");
  // 模型多的供应商默认只展示前 10 个，可展开看全量目录（「罗列所有可加载模型」）
  const [modelsExpanded, setModelsExpanded] = useState<Record<string, boolean>>({});
  const provFormRef = useRef<HTMLDivElement>(null);

  // LLM 就绪自检（GET /doctor/llm）：直接把「这一轮到底走不走真实大模型」显示在顶部，
  // 免得在别的机器上 .env 还是 mock、界面能聊却从没调过真模型。没有供应商时可点此刷新。
  const [health, setHealth] = useState<any>(null);
  const [healthOpen, setHealthOpen] = useState(false);
  function loadHealth() { api.doctorLlm().then((r: any) => setHealth(r)).catch(() => setHealth(null)); }
  useEffect(() => { loadHealth(); }, [providers, profiles]);

  // 点模型 chip 或「有模型未设窗口」时把编辑表单滚到视野内，避免用户以为没反应
  useEffect(() => {
    if (provForm === null) return;
    const t = window.setTimeout(() => provFormRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }), 30);
    return () => window.clearTimeout(t);
  }, [provForm]);

  const mediaProfiles = profiles.filter((p) => p.kind === "image" || p.kind === "audio");

  async function createProfile() {
    setErr("");
    if (!pf.key.trim()) { setErr("标识必填"); return; }
    if (!pf.model.trim()) { setErr("请填写模型名"); return; }
    if (!pf.base_url.trim()) { setErr("媒体档位必须填 Base URL"); return; }
    const params: Record<string, any> = {};
    if (pf.kind === "audio" && pf.voice.trim()) params.voice = pf.voice.trim();
    if (pf.api_key) params.api_key = pf.api_key;
    const kindProvider = pf.kind === "image"
      ? (pf.provider === "openai" ? "openai_image" : pf.provider)
      : "openai_tts";
    try {
      await api.createModelProfile({
        key: pf.key.trim(), label: pf.label.trim() || pf.key.trim(), kind: pf.kind,
        provider: kindProvider,
        model: pf.model.trim(),
        provider_id: null,
        base_url: pf.base_url || null,
        params_json: params,
      });
      setPf({ ...emptyPf });
      setPfOpen(false);
      refresh();
    } catch (e: any) {
      setErr(e?.message || "创建失败");
    }
  }

  function provOf(id: string) {
    return providers.find((x) => x.id === id);
  }

  /** 供应商模型目录里的窗口优先；旧版在「对话档位」里配过的窗口作为兜底显示，
   *  避免页面显示「窗口未设」而运行时其实沿用了档位里的值。 */
  function profileWindowFor(r: LlmProviderRow, name: string): number {
    const p = profiles.find((x) => x.kind === "llm" && x.provider_id === r.id && x.model === name);
    return Number(p?.params_json?.context_window) || 0;
  }

  /** 「这个模型运行时到底看不看图」：模型目录优先，旧数据（目录里是字符串、
   *  从未勾过）回落到已存在档位里的 vision，避免卡片显示「不看图」而实际在传图。 */
  function profileVisionFor(r: LlmProviderRow, name: string): boolean {
    const p = profiles.find((x) => x.kind === "llm" && x.provider_id === r.id && x.model === name);
    return !!p?.params_json?.vision;
  }

  function modelWindowFor(r: LlmProviderRow, m: ProviderModel): number {
    return Number(m?.context_window) || profileWindowFor(r, modelName(m));
  }

  function modelVisionFor(r: LlmProviderRow, m: ProviderModel): boolean {
    return !!m?.vision || profileVisionFor(r, modelName(m));
  }

  /** 档位行内直接改供应商 / 模型 / 窗口：改完即存 */
  async function patchProfile(p: ModelProfileRow, body: Record<string, any>) {
    try {
      await api.updateModelProfile(p.id, body);
      refresh();
    } catch (e: any) {
      setErr(e?.message || "保存失败");
    }
  }

  function openProvForm(r?: LlmProviderRow) {
    setPvErr("");
    setProvForm(r ? r.id : "new");
    setPv({
      key: r?.key || "", label: r?.label || "",
      protocol: PROTOCOLS.some((o) => o.v === (r?.protocol || "")) ? (r as LlmProviderRow).protocol : "openai",
      base_url: r?.base_url || "", api_key: "", user_agent: r?.user_agent || "",
      enabled: r ? r.enabled : true, keyHint: r?.has_key ? (r.key_hint || "") : "",
      models: (r?.models || []).map((m) => ({
        name: modelName(m),
        context_window: Number(m?.context_window) || (r ? profileWindowFor(r, modelName(m)) : 0),
        vision: !!(m as any)?.vision || (r ? profileVisionFor(r, modelName(m)) : false),
      })),
    });
  }

  function patchPvModel(idx: number, patch: Partial<ProviderModel>) {
    setPv((cur) => ({
      ...cur,
      models: cur.models.map((m, i) => (i === idx ? { ...m, ...patch } : m)),
    }));
  }

  function addPvModel() {
    setPv((cur) => ({ ...cur, models: [...cur.models, { name: "", context_window: DEFAULT_WINDOW, vision: false }] }));
  }

  function removePvModel(idx: number) {
    setPv((cur) => ({ ...cur, models: cur.models.filter((_, i) => i !== idx) }));
  }

  async function saveProv() {
    setPvErr("");
    const models = pv.models
      .map((m) => ({ name: (m.name || "").trim(), context_window: Number(m.context_window) || 0, vision: !!m.vision }))
      .filter((m) => m.name);
    const body: Record<string, any> = {
      // 本产品当前适配 OpenAI 兼容与 Anthropic 两种协议；历史数据里残留的
      // google/ollama 等值统一落成 openai，避免被后端拒绝。
      label: pv.label.trim(), protocol: PROTOCOLS.some((o) => o.v === pv.protocol) ? pv.protocol : "openai",
      base_url: pv.base_url.trim(),
      user_agent: pv.user_agent.trim(), enabled: pv.enabled, models,
    };
    try {
      if (provForm === "new") {
        await api.createLlmProvider({ ...body, key: pv.key.trim(), api_key: pv.api_key || null });
      } else {
        // 密钥字段留空 = 不动原密钥（后端只在显式提交时才改），所以不能无条件带上
        if (pv.api_key) body.api_key = pv.api_key;
        await api.updateLlmProvider(provForm || "", body);
      }
      setProvForm(null);
      refresh();
    } catch (e: any) {
      setPvErr(e?.message || "保存失败");
    }
  }

  async function pullFor(r: LlmProviderRow) {
    setPulling(r.id);
    setPullMsg("");
    try {
      const res = await api.llmModels({ providerId: r.id, protocol: r.protocol });
      const list = (res.models || []) as ProviderModel[];
      const usedBase: string = res?.base_url || "";
      const autoFixed = !!usedBase && usedBase.replace(/\/+$/, "") !== (r.base_url || "").replace(/\/+$/, "");
      // 表单正开着这家供应商：把拉取结果并进表单，窗口沿用已有设置
      if (provForm === r.id) {
        setPv((cur) => {
          const old = new Map(cur.models.map((m) => [modelName(m), m]));
          return {
            ...cur,
            base_url: usedBase || cur.base_url,
            models: list.map((m) => ({
              name: modelName(m),
              context_window: Number(m?.context_window) || Number(old.get(modelName(m))?.context_window) || 0,
              vision: !!(m as any)?.vision || !!old.get(modelName(m))?.vision,
            })),
          };
        });
      }
      setPullMsg(`已拉取 ${list.length} 个模型`
        + (autoFixed ? `（地址自动用了可用的 ${usedBase}）` : "")
        + (provForm === r.id ? "（记得点保存）" : ""));
      refresh();
    } catch (e: any) {
      setPullMsg(e?.message || "拉取失败");
    } finally {
      setPulling("");
    }
  }

  async function delProv(r: LlmProviderRow) {
    setPullMsg("");
    if (!window.confirm(
      `删除供应商「${r.label}」？\n`
      + `它下面的 ${r.models.length} 个模型会一起删除；正在使用其中模型的会话会回落到默认模型。`,
    )) return;
    try {
      const res: any = await api.deleteLlmProvider(r.id);
      const n = Number(res?.deleted_profiles) || 0;
      setPullMsg(`已删除供应商「${r.label}」${n ? `，并清理 ${n} 条自动生成的模型记录` : ""}`);
      if (provForm === r.id) setProvForm(null);
      refresh();
    } catch (e: any) {
      setPullMsg(e?.message || "删除失败");
    }
  }

  return (
    <div className="space-y-4">
      {/* ---------- 0. 运行自检：这一轮到底走不走真实大模型 ---------- */}
      {health && (
        <div className={`rounded-lg border p-3 ${health.ready ? "border-emerald-200 bg-emerald-50/50" : "border-rose-300 bg-rose-50/70"}`}>
          <div className="flex items-center gap-2 text-sm">
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${health.ready ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}>
              {health.ready ? "已接通真实大模型" : "还没有可用的模型供应商"}
            </span>
            <span className="text-slate-700">
              {health.model ? <>当前模型 <span className="mono">{health.model}</span></> : "未配置模型"}
              {health.provider ? <> · 供应商 {health.provider}</> : null}
            </span>
            <button className="ml-auto text-[11px] text-slate-500 hover:text-slate-800 underline underline-offset-2"
              onClick={() => { setHealthOpen((v) => !v); loadHealth(); }}>
              {healthOpen ? "收起" : "详情 / 自检"}
            </button>
            <button className="text-[11px] text-slate-400 hover:text-slate-700" onClick={loadHealth} title="重新检查">↻</button>
          </div>
          {!health.ready && (health.issues?.length > 0 || health.hints?.length > 0) && (
            <div className="mt-1.5 text-[11px] text-rose-800 leading-5">
              {health.issues?.map((s: string, i: number) => <div key={"i" + i}>• {s}</div>)}
              {health.hints?.map((s: string, i: number) => <div key={"h" + i} className="text-rose-700/90">→ {s}</div>)}
            </div>
          )}
          {healthOpen && (
            <div className="mt-2 rounded bg-white/70 border border-slate-200 p-2 text-[11px] text-slate-600 space-y-1">
              <div>角色绑定档位：{health.profile_ok ? <span className="text-emerald-700">已就绪</span> : <span className="text-amber-700">{health.profile_issue || "未绑定"}</span>}</div>
              <div>本轮模型：<span className="mono">{health.model || "未配置"}</span>{health.provider ? ` · ${health.provider}` : ""}</div>
              <div>全局兜底（.env）：<span className="mono">{health.fallback_model}</span> · {health.fallback_has_key ? <span className="text-emerald-700">已配 OPENAI_API_KEY</span> : <span className="text-rose-700">未配 OPENAI_API_KEY</span>}</div>
            </div>
          )}
        </div>
      )}
      {/* ---------- 1. LLM 供应商 + 模型目录 ---------- */}
      <div className="bg-white rounded-lg border border-slate-200 p-4">
        <div className="flex items-center gap-2 mb-1">
          <span className="font-medium text-sm">LLM 供应商</span>
          <span className="text-[11px] text-slate-400">地址与密钥只在这里存一次；每个供应商维护自己的模型目录和上下文窗口。对话实际用哪个模型，在聊天输入框右下角选</span>
          <button
            className="ml-auto ga-btn ga-btn-secondary ga-btn-sm"
            onClick={() => openProvForm()}
          >
            + 新增供应商
          </button>
        </div>
        {pullMsg && <div className="text-[11px] text-amber-700 mb-2 break-all whitespace-pre-line">{pullMsg}</div>}

        {provForm !== null && (
          <div ref={provFormRef} className="mb-3 rounded border border-brand-200 bg-brand-50/40 p-3 grid grid-cols-2 gap-2 text-sm">
            <input
              className="border rounded px-2 py-1 mono text-xs"
              placeholder={provForm === "new" ? "标识（如 gonode，由地址自动填）" : "标识（不可改）"}
              value={pv.key}
              disabled={provForm !== "new"}
              onChange={(e) => setPv({ ...pv, key: e.target.value })}
            />
            <input
              className="border rounded px-2 py-1"
              placeholder="显示名（如 中转站 / DeepSeek 官方）"
              value={pv.label}
              onChange={(e) => setPv({ ...pv, label: e.target.value })}
            />
            <select className="border rounded px-2 py-1" value={pv.protocol}
              onChange={(e) => setPv({ ...pv, protocol: e.target.value })}>
              {PROTOCOLS.map((o) => <option key={o.v} value={o.v}>{o.t}</option>)}
            </select>
            <div className="col-span-2 -mt-1 text-[11px] text-slate-500">
              对话协议按供应商实际接口选：OpenAI 兼容（/v1/chat/completions）或 Anthropic 原生（/v1/messages）。google/ollama 暂未适配。
            </div>
            <input
              className="border rounded px-2 py-1 mono text-xs col-span-2"
              placeholder="Base URL（填根域名或 /v1 都行，如 https://api.deepseek.com 或 https://xxx/v1）"
              value={pv.base_url}
              onChange={(e) => {
                const v = e.target.value;
                setPv((cur) => ({
                  ...cur, base_url: v,
                  // 新建时标识跟着地址走，省一次输入；label 空着由后端按域名兜底
                  key: provForm === "new" && !cur.key ? slugFromUrl(v) : cur.key,
                }));
              }}
            />
            <div className="col-span-2 flex items-center gap-2">
              <input
                className="border rounded px-2 py-1 flex-1 mono text-xs"
                type={showKey ? "text" : "password"}
                placeholder={pv.keyHint ? `已存 ${pv.keyHint}｜留空不改，粘贴新密钥覆盖`
                  : "API Key（留空=不改；本地无鉴权端点可不填）"}
                value={pv.api_key}
                autoComplete="off" spellCheck={false}
                onPaste={(e) => {
                  e.preventDefault();
                  setPv((cur) => ({ ...cur, api_key: cleanKey(e.clipboardData.getData("text")) }));
                }}
                onChange={(e) => setPv({ ...pv, api_key: cleanKey(e.target.value) })}
              />
              <button type="button" className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
                onClick={() => setShowKey((v) => !v)}>
                {showKey ? "隐藏" : "显示"}
              </button>
              {pv.api_key && (
                <span className={`text-[11px] shrink-0 ${pv.api_key.startsWith("sk-") ? "text-emerald-700" : "text-amber-700"}`}>
                  {pv.api_key.length} 字符 · {pv.api_key.slice(0, 6)}…{pv.api_key.slice(-4)}
                </span>
              )}
            </div>
            <div className="col-span-2 flex items-center gap-2">
              <input
                className="border rounded px-2 py-1 flex-1 mono text-xs"
                placeholder="User-Agent（可选：站点挂了 Cloudflare 并拦脚本时，点右侧填浏览器 UA）"
                value={pv.user_agent}
                onChange={(e) => setPv({ ...pv, user_agent: e.target.value })}
              />
              <button type="button" className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
                onClick={() => setPv({ ...pv, user_agent: BROWSER_UA })}
                title="填一个常见浏览器 UA，绕过按 UA 拦脚本的人机验证">
                填浏览器 UA
              </button>
              <label className="flex items-center gap-1 text-xs text-slate-600 shrink-0">
                <input type="checkbox" checked={pv.enabled}
                  onChange={(e) => setPv({ ...pv, enabled: e.target.checked })} />
                启用
              </label>
            </div>

            {/* 模型目录：每个模型一行，窗口按模型单独设置 */}
            <div className="col-span-2 rounded border border-slate-200 bg-white/70 p-2">
              <div className="flex items-center gap-2 mb-1.5">
                <span className="text-xs font-medium text-slate-700">模型目录</span>
                <span className="text-[11px] text-slate-400">每个模型单独设上下文窗口；对话里选中即建档位，点模型名可直接编辑窗口</span>
                <div className="ml-auto flex items-center gap-1.5">
                  {provForm !== "new" && (
                    <button type="button" className="ga-btn ga-btn-secondary ga-btn-sm"
                      onClick={() => { const r = provOf(provForm || ""); if (r) pullFor(r); }}
                      disabled={pulling === provForm}>
                      {pulling === provForm ? "拉取中…" : "拉取模型"}
                    </button>
                  )}
                  <button type="button" className="ga-btn ga-btn-secondary ga-btn-sm" onClick={addPvModel}>
                    + 添加模型
                  </button>
                </div>
              </div>
              {pv.models.length === 0 && (
                <div className="text-[11px] text-slate-400 py-1">
                  还没有模型。点「拉取模型」从端点同步，或「+ 添加模型」手填模型名。
                </div>
              )}
              {pv.models.map((m, i) => (
                <div key={i} className="flex items-center gap-1.5 mb-1">
                  <input
                    className="border rounded px-2 py-1 mono text-xs flex-1 min-w-0"
                    placeholder="模型名（如 deepseek-v4-pro）"
                    value={m.name}
                    onChange={(e) => patchPvModel(i, { name: e.target.value })}
                  />
                  <WindowInput
                    value={Number(m.context_window) || 0}
                    onCommit={(n) => patchPvModel(i, { context_window: n })}
                  />
                  <label
                    className="flex items-center gap-1 text-[11px] text-slate-600 shrink-0 cursor-pointer select-none"
                    title="勾选=支持看图。不勾时，发给模型的附图会被忽略（模型看不到画面，只能读到「图片已附在本条消息中」）。带视觉能力的模型（如 deepseek-v4.1-flash / kimi-k3-1）请勾上。"
                  >
                    <input
                      type="checkbox"
                      checked={!!m.vision}
                      onChange={(e) => patchPvModel(i, { vision: e.target.checked })}
                    />
                    看图
                  </label>
                  <button type="button" className="ga-btn ga-btn-danger ga-btn-sm shrink-0"
                    onClick={() => removePvModel(i)}>删除</button>
                </div>
              ))}
            </div>

            <div className="col-span-2 flex items-center gap-2">
              <button className="ga-btn ga-btn-primary" onClick={saveProv}>保存</button>
              <button className="ga-btn ga-btn-secondary" onClick={() => setProvForm(null)}>取消</button>
              {pvErr && <span className="text-xs text-red-600 break-all">{pvErr}</span>}
            </div>
          </div>
        )}

        <div className="space-y-2">
          {providers.map((r) => (
            <div key={r.id} className="rounded border border-slate-200 p-3">
              <div className="flex items-center gap-2 text-sm flex-wrap">
                <span className="font-medium">{r.label}</span>
                <span className="text-[11px] mono text-slate-400">{r.key}</span>
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{r.protocol}</span>
                {!r.enabled && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700">已停用</span>
                )}
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
                  {r.models.length} 个模型
                </span>
                <div className="ml-auto flex items-center gap-1.5">
                  <button
                    className="ga-btn ga-btn-secondary ga-btn-sm"
                    onClick={() => pullFor(r)} disabled={pulling === r.id}>
                    {pulling === r.id ? "拉取中…" : "拉取模型"}
                  </button>
                  <button className="ga-btn ga-btn-secondary ga-btn-sm"
                    onClick={() => openProvForm(r)}>编辑</button>
                  <button className="ga-btn ga-btn-danger ga-btn-sm"
                    onClick={() => delProv(r)}>删除</button>
                </div>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
                <span className="mono truncate max-w-[320px]" title={r.base_url || ""}>{r.base_url || "（未填地址）"}</span>
                <span className={r.has_key ? "text-emerald-700" : "text-amber-700"}>
                  {r.has_key ? `Key ${r.key_hint}` : "未存 Key（走环境变量）"}
                </span>
                {!!r.user_agent && (
                  <span className="text-violet-700 truncate max-w-[220px]" title={r.user_agent}>
                    UA 已自定义
                  </span>
                )}
              </div>
              {r.models.length > 0 ? (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {r.models.slice(0, modelsExpanded[r.id] ? r.models.length : 10).map((m) => {
                    const win = modelWindowFor(r, m);
                    const vis = modelVisionFor(r, m);
                    return (
                    <button key={modelName(m)} type="button"
                      className="text-[10px] px-1.5 py-0.5 rounded bg-slate-50 text-slate-500 mono border border-slate-100 hover:border-brand-300 hover:bg-brand-50/40"
                      title={`${modelName(m)} · ${windowLabel(win)}${vis ? " · 支持看图" : " · 不看图"}｜点击编辑供应商、设置上下文窗口与看图开关`}
                      onClick={() => openProvForm(r)}>
                      {modelName(m)}
                      <span className={win ? "text-sky-600" : "text-amber-600"}>
                        {" · "}{windowLabel(win)}
                      </span>
                      {vis && <span className="text-emerald-600" title="支持看图">{" · 看图"}</span>}
                    </button>
                    );
                  })}
                  {r.models.length > 10 && (
                    <button type="button"
                      className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 hover:bg-slate-200"
                      onClick={() => setModelsExpanded((cur) => ({ ...cur, [r.id]: !cur[r.id] }))}>
                      {modelsExpanded[r.id] ? "收起" : `+${r.models.length - 10} 个（展开全部）`}
                    </button>
                  )}
                  {r.models.some((m) => !modelWindowFor(r, m)) && (
                    <button type="button"
                      className="text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100"
                      onClick={() => openProvForm(r)}>
                      有模型未设窗口，点此设置
                    </button>
                  )}
                </div>
              ) : (
                <div className="mt-1.5 text-[11px] text-amber-700">
                  还没有模型：点「拉取模型」或「编辑」里手动添加。
                </div>
              )}
            </div>
          ))}
          {providers.length === 0 && (
            <div className="text-xs text-slate-400">
              还没有供应商。加一个供应商并在它的模型目录里添加模型（可逐个设置上下文窗口）后，聊天输入框右下角就能直接选模型对话，不用再在设置页配置对话档位。
            </div>
          )}
        </div>
      </div>

      {/* ---------- 2. 图像 / 音频档位 ---------- */}
      <div className="bg-white rounded-lg border border-slate-200 p-4">
        <div className="flex items-center gap-2 mb-1">
          <span className="font-medium text-sm">图像 / 音频档位</span>
          <span className="text-[11px] text-slate-400">出图 / 配音走各自的驱动，仍在这里内联地址和密钥</span>
          <button
            className="ml-auto ga-btn ga-btn-primary ga-btn-sm"
            onClick={() => { setErr(""); setPfOpen((v) => !v); }}
          >
            {pfOpen ? "收起表单" : "＋ 新建档位"}
          </button>
        </div>
        <div className={pfOpen ? "grid grid-cols-2 gap-2 text-sm mt-3" : "hidden"}>
          <input className="border rounded px-2 py-1" placeholder="标识（如 comfy-local / tts-main）" value={pf.key}
            onChange={(e) => setPf({ ...pf, key: e.target.value })} />
          <input className="border rounded px-2 py-1" placeholder="显示名（如 本地 ComfyUI）" value={pf.label}
            onChange={(e) => setPf({ ...pf, label: e.target.value })} />
          <select className="border rounded px-2 py-1" value={pf.kind}
            onChange={(e) => setPf({ ...pf, kind: e.target.value, provider: e.target.value === "image" ? "comfyui" : "openai_tts" })}>
            <option value="image">图像生成模型</option>
            <option value="audio">音频生成模型</option>
          </select>
          <input className="border rounded px-2 py-1" placeholder="模型名（如 gemini-2.5-flash-image / tts-1）"
            value={pf.model} onChange={(e) => setPf({ ...pf, model: e.target.value })} />
          <select className="border rounded px-2 py-1" value={pf.provider}
            onChange={(e) => setPf({ ...pf, provider: e.target.value })}>
            {pf.kind === "image" ? (
              <>
                <option value="comfyui">ComfyUI（本地工作流）</option>
                <option value="openai_image">OpenAI 兼容（/images/generations）</option>
              </>
            ) : (
              <option value="openai_tts">OpenAI 兼容语音（/audio/speech）</option>
            )}
          </select>
          {pf.kind === "audio" && (
            <input className="border rounded px-2 py-1" placeholder="音色（voice，可选，如 alloy）" value={pf.voice}
              onChange={(e) => setPf({ ...pf, voice: e.target.value })} />
          )}
          <input className="border rounded px-2 py-1 col-span-2" placeholder="Base URL（媒体档位必填，如 http://127.0.0.1:8188）"
            value={pf.base_url} onChange={(e) => setPf({ ...pf, base_url: e.target.value })} />
          <input className="border rounded px-2 py-1 col-span-2 mono text-xs" type={showKey ? "text" : "password"}
            placeholder="API Key（本地 ComfyUI 不用填）" value={pf.api_key}
            autoComplete="off" spellCheck={false}
            onPaste={(e) => {
              e.preventDefault();
              setPf((cur) => ({ ...cur, api_key: cleanKey(e.clipboardData.getData("text")) }));
            }}
            onChange={(e) => setPf({ ...pf, api_key: cleanKey(e.target.value) })} />
          <div className="col-span-2 flex items-center gap-2">
            <button className="ga-btn ga-btn-primary" onClick={createProfile}>添加档位</button>
            {err && <span className="text-xs text-red-600">{err}</span>}
            <span className="text-[11px] text-slate-400">
              {pf.kind === "image"
                ? "图像档位创建后在「智能体」页绑定为助手的美术模型；出图失败时会用档位参数直通生图。"
                : "音频档位创建后在「智能体」页绑定为助手的配音模型，文案走 /audio/speech 合成。"}
            </span>
          </div>
        </div>

        <div className="mt-3 space-y-2">
          {mediaProfiles.map((p) => (
            <div key={p.id} className="rounded border border-slate-200 p-3 flex items-center gap-3 text-sm flex-wrap">
              <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${
                p.kind === "image" ? "bg-sky-100 text-sky-700" : "bg-amber-100 text-amber-700"}`}>
                {p.kind === "image" ? "图像" : "音频"}
              </span>
              <span className="font-medium">{p.label}</span>
              <span className="text-[11px] mono text-slate-400">{p.key}</span>
              <span className="text-xs text-slate-500">{p.provider}{p.model ? ` / ${p.model}` : ""}</span>
              {p.has_key && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700">
                  Key {p.key_hint}
                </span>
              )}
              {p.bound_roles.length > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700">
                  {p.bound_roles.map((r) => ROLE_NAMES[r] || r).join("、")} 在用
                </span>
              )}
              {p.params_json?.voice && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
                  音色 {p.params_json.voice}
                </span>
              )}
              {p.base_url && (
                <span className="text-[11px] text-slate-400 mono truncate max-w-[260px]">{p.base_url}</span>
              )}
              <button className="ml-auto ga-btn ga-btn-danger ga-btn-sm"
                onClick={async () => { await api.deleteModelProfile(p.id); refresh(); }}>
                删除
              </button>
            </div>
          ))}
          {mediaProfiles.length === 0 && (
            <div className="text-xs text-slate-400">暂无图像 / 音频档位；未配置时图像/音频使用本地占位生成。</div>
          )}
        </div>
      </div>
    </div>
  );
}

/** 供父组件加载：配置页同时要用档位列表填「智能体」页的绑定下拉。 */
export async function loadModelConfig() {
  const [pr, pv] = await Promise.all([api.modelProfiles(), api.llmProviders()]);
  return { profiles: (pr.profiles || []) as ModelProfileRow[],
           providers: (pv.providers || []) as LlmProviderRow[] };
}
