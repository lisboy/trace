import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import SearchSelect from "./SearchSelect";
import { GenProgress, IMAGE_STAGES, LivePreviews, newJobId, STAGE_SHORT, useJobPoll } from "./GenProgress";

type Options = {
  available: boolean; checkpoints: string[]; diffusion: string[]; loras?: string[];
  default_lora?: string; default_lora_strength?: number; loras_dir?: string | null;
  error?: string | null;
  views?: { key: string; label: string; group: string }[];
  default_negative_prompt?: string;
};
type Preset = {
  id: string; key: string; label: string; params: Record<string, any>; builtin: boolean;
  image_file?: string;
};
type GenFile = { file: string; url: string; size: number; view?: string };
type GenResult = {
  file: string; url?: string; files?: GenFile[]; count?: number;
  enhanced_prompt: string; enhance_note: string; elapsed_ms: number; size: number;
  params?: Record<string, any>;
  review?: { enabled: boolean; issues: string[]; failed_indexes: number[] } | null;
};
type Shot = {
  key: string; file: string; url: string; size: number; index: number; total: number;
  prompt: string; workflow_mode: string; model: string; ref_mode: string; elapsed_ms: number;
  lora?: string; lora_strength?: number; sparams?: Record<string, any>;
  view?: string;
};

// 尺寸档位。1920x1080 / 1080x1920 为真 16:9（1792x1024 实为 7:4），1024x1536 为整 2:3；
// 2360x1640 / 1640x2360 为 2K 级大图（≈3.9MP，8G 显存实测约 5 分钟，走分块解码）。
// 超大尺寸（>4MP，如 4K）会自动「基准分辨率采样 + 像素放大」两步出图，避免 8G OOM。
// 后端 _parse_size 无白名单，任意 WxH 均可，这里只是把常用档位做成下拉。
const SIZE_OPTS = [
  "2360x1640", "1640x2360",
  "1920x1080", "1080x1920", "1792x1024", "1024x1536",
  "1024x1024", "832x1216", "1216x832", "768x1344",
];
const COUNT_OPTS = [1, 2, 4, 8];
const MODE_LABEL: Record<string, string> = { auto: "自动探测", checkpoint: "Checkpoints（经典 SD）", diffusion: "Diffusion Models（DiT 三件套）" };
const loraShort = (n?: string) => (n ? n.split(/[\\/]/).pop() || n : "");
const canonLora = (n: string) => n.replace(/\\/g, "/").toLowerCase();
/** 预设里存的 LoRA 路径可能是正斜杠，ComfyUI 列表用反斜杠——按归一化结果回填列表里的原样名字 */
function matchLora(name: string, list: string[]) {
  if (!name) return "";
  return list.find((n) => canonLora(n) === canonLora(name)) ?? name;
}

/** blob URL 直接触发浏览器下载（结果文件接口需要鉴权，不能用裸 <a href>） */
function downloadUrl(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "image.png";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function fmtTime(ms: number) {
  return ms >= 60000 ? `${Math.round(ms / 60000)} 分` : `${(ms / 1000).toFixed(1)} 秒`;
}

/**
 * AI 生图工作台：界面直连 ComfyUI 出图（不走 agent）。
 * 支持提示词（可选 LLM 增强）、参考图（风格参考/照图生图）、
 * 生成方式（自动/Checkpoints/Diffusion Models）、手动指定模型、风格 LoRA（可选/调强度/上传）、
 * 预设配方（内置 + 自定义；生成的图满意后一键把本次参数存成预设）、一次多张。
 * 生成成功后自动把图导入当前选中项目的素材库（可勾选关掉）。
 */
export default function ImageGenPanel(
  { projectId, onImported }: { projectId?: string; onImported?: () => void } = {}
) {
  const [prompt, setPrompt] = useState("");
  const [refFile, setRefFile] = useState<File | null>(null);
  const [refMode, setRefMode] = useState<"none" | "style" | "img2img">("none");
  const [workflowMode, setWorkflowMode] = useState<"auto" | "checkpoint" | "diffusion">("diffusion");
  const [model, setModel] = useState("");
  // 「大模型先分析重组提示词」(enhance) 已从本工作台移除：直接用用户原样提示词出图。
  // 出图后 AI 看图质检（多余手指/三条腿等），发现异常自动修提示词重生成。默认勾选。
  const [autoFix, setAutoFix] = useState(true);
  const [reviewNote, setReviewNote] = useState("");
  const [size, setSize] = useState("1920x1080");
  const [count, setCount] = useState(1);
  const [lora, setLora] = useState("");
  const [loraStrength, setLoraStrength] = useState(0.7);
  const [negative, setNegative] = useState("");
  // 「根据描述帮填提示词」：用默认对话模型补全正面/负面词
  const [fillBusy, setFillBusy] = useState(false);
  const [fillNote, setFillNote] = useState("");
  const [viewMode, setViewMode] = useState<"single" | "views">("single");
  const [viewKeys, setViewKeys] = useState<string[]>([
    "front", "front_right_45", "right", "back_right_45",
    "back", "back_left_45", "left", "front_left_45",
  ]);
  const [opts, setOpts] = useState<Options | null>(null);
  const [busy, setBusy] = useState(false);
  const [booting, setBooting] = useState(false);
  const [err, setErr] = useState("");
  // 处理过程可视化：出图是几分钟的阻塞操作，轮询后端任务表，把「真实阶段 + 序号 + 估算进度」显示出来。
  const poll = useJobPoll(1200);
  const jobProgress = poll.progress;
  const stageLog = poll.log;
  // 本次结果 / 历史 / 摘要：跨组件卸载持久化（切换面板、刷新页面后仍能看到刚出的图）。
  // 只存元数据（含后端文件路径 file），图片 blob URL 在恢复时按 file 重新拉取。
  const [shots, setShots] = useState<Shot[]>([]);
  const [history, setHistory] = useState<Shot[]>([]);
  const [meta, setMeta] = useState<{ elapsed_ms: number; size: number; enhance_note: string; enhanced_prompt: string } | null>(null);
  const shotsRestored = useRef<string>(""); // 已恢复完成的项目 key（""=恢复中，禁止回写）
  const blobUrls = useRef<Set<string>>(new Set());

  // 预设 & LoRA 上传
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState("");
  const [presetImg, setPresetImg] = useState<Record<string, string>>({});
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveMsg, setSaveMsg] = useState("");
  const [loraBusy, setLoraBusy] = useState(false);
  const [loraNote, setLoraNote] = useState("");
  // 项目强制预设（agent 工具生图时生效）
  const [projPresetId, setProjPresetId] = useState("");
  // 预设参考图放大预览
  const [preview, setPreview] = useState<{ url: string; label: string } | null>(null);

  // 结果自动入素材库
  const [toLib, setToLib] = useState(true);
  const [libNote, setLibNote] = useState("");

  useEffect(() => {
    api.imageGenOptions().then((o: Options) => {
      setOpts(o);
      setLora(matchLora(o.default_lora ?? "", o.loras ?? []));
      setLoraStrength(o.default_lora_strength ?? 0.7);
      // 默认指定主模型 qwen_image_2.1（走 DiT 三件套）；列表里没有就留空回退第一个，
      // 避免硬编码文件名在换机/换模型后选到空值。
      const preferred = "qwen_image_2.1_int8_convrot.safetensors";
      setModel((o.diffusion ?? []).includes(preferred) ? preferred : "");
      if (o.default_negative_prompt) setNegative(o.default_negative_prompt);
    }).catch(() => setOpts({ available: false, checkpoints: [], diffusion: [], error: "加载失败" }));
    loadPresets();
  }, []);

  useEffect(() => {
    if (!projectId) return;
    api.imageGenProjectPreset(projectId)
      .then((r: any) => setProjPresetId(r?.preset_id || ""))
      .catch(() => setProjPresetId(""));
  }, [projectId]);

  // ---- 结果持久化：切换面板 / 刷新页面后仍能看到刚生成的图 ----
  // 组件卸载会丢 shots/history/meta（用户反馈「生成完切换一下就没了」）。这里只把
  // 元数据（含后端文件路径 file）存 localStorage，图片 blob URL 恢复时按 file 重取。
  // 读写都按 projectId 分键，并等「该项目的恢复完成」才允许回写，避免切换项目时
  // 把上一个项目的结果写进新项目的槽位。
  const stripUrl = (s: Shot): Shot => ({ ...s, url: "" });
  const saveShots = (key: string, list: Shot[]) => {
    if (shotsRestored.current !== (projectId || "_")) return; // 该项目尚未恢复完，不回写
    try { localStorage.setItem(key, JSON.stringify(list.map(stripUrl))); } catch { /* 忽略配额/隐私模式 */ }
  };
  useEffect(() => { saveShots(`ga_imagegen_result:${projectId || "_"}`, shots); }, [shots, projectId]);
  useEffect(() => { saveShots(`ga_imagegen_history:${projectId || "_"}`, history); }, [history, projectId]);
  useEffect(() => { saveShots(`ga_imagegen_meta:${projectId || "_"}`, meta ? [meta as any] : []); }, [meta, projectId]);

  useEffect(() => {
    let alive = true;
    const pkey = projectId || "_";
    shotsRestored.current = ""; // 恢复中：禁止回写
    (async () => {
      const rk = `ga_imagegen_result:${pkey}`;
      const hk = `ga_imagegen_history:${pkey}`;
      const mk = `ga_imagegen_meta:${pkey}`;
      let restoredS: Shot[] = [], restoredH: Shot[] = [], restoredM: any = null;
      try { restoredS = JSON.parse(localStorage.getItem(rk) || "[]"); } catch { /* ignore */ }
      try { restoredH = JSON.parse(localStorage.getItem(hk) || "[]"); } catch { /* ignore */ }
      try { restoredM = (JSON.parse(localStorage.getItem(mk) || "[]") || [])[0] || null; } catch { /* ignore */ }
      // 换项目时先清掉上一个项目的内存状态，避免串号
      if (alive) { setShots([]); setHistory([]); setMeta(null); }
      const backfill = async (list: Shot[]) => Promise.all(list.map(async (s) => {
        if (s.url) return s;
        try { const url = await api.imageGenBlob(s.file); blobUrls.current.add(url); return { ...s, url }; }
        catch { return s; }
      }));
      const [s2, h2] = await Promise.all([backfill(restoredS), backfill(restoredH)]);
      if (!alive) return;
      if (s2.length) setShots(s2);
      if (h2.length) setHistory(h2);
      if (restoredM) setMeta(restoredM);
      shotsRestored.current = pkey; // 恢复完成：此后才允许回写该项目槽位
    })();
    return () => { alive = false; };
  }, [projectId]);

  // 卸载时释放本组件创建的 blob URL（恢复与 generate 都会创建）；进度轮询由 useJobPoll 自行在卸载时停止
  useEffect(() => () => {
    blobUrls.current.forEach((u) => { try { URL.revokeObjectURL(u); } catch { /* ignore */ } });
    blobUrls.current.clear();
  }, []);
  const modelList = workflowMode === "checkpoint" ? opts?.checkpoints ?? [] : opts?.diffusion ?? [];
  const loraList = opts?.loras ?? [];
  const selectedPreset = presets.find((p) => p.id === presetId);

  async function loadPresets() {
    try {
      const r = await api.imageGenPresets();
      const list: Preset[] = r.presets ?? [];
      setPresets(list);
      const imgs: Record<string, string> = {};
      for (const p of list) {
        if (p.image_file) {
          try {
            imgs[p.image_file] = await api.imageGenPresetImage(p.image_file);
          } catch {
            /* 样图缺失不影响预设使用 */
          }
        }
      }
      setPresetImg(imgs);
    } catch {
      setPresets([]);
    }
  }

  async function bindProjectPreset(id: string) {
    if (!projectId) return;
    setProjPresetId(id);
    try {
      await api.imageGenSetProjectPreset({ project_id: projectId, preset_id: id });
      setSaveMsg(id ? "已设为项目强制预设（AI 工具生图时生效）" : "已解除项目强制预设");
    } catch (e: any) {
      setErr(e?.message || "设置失败");
    }
  }

  async function refreshOpts() {
    setOpts(null);
    try { setOpts(await api.imageGenOptions()); } catch { setOpts({ available: false, checkpoints: [], diffusion: [], error: "加载失败" }); }
  }

  function applyPreset(id: string) {
    setPresetId(id);
    setSaveMsg("");
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    const q = p.params || {};
    if (q.workflow_mode) setWorkflowMode(q.workflow_mode);
    if (typeof q.model === "string") setModel(q.model);
    if (typeof q.size === "string" && q.size) setSize(q.size);
    if (q.lora !== undefined) setLora(matchLora(q.lora || "", loraList));
    if (typeof q.lora_strength === "number") setLoraStrength(q.lora_strength);
    if (typeof q.count === "number") setCount(Math.max(1, Math.min(8, q.count)));
    if (typeof q.negative_prompt === "string") setNegative(q.negative_prompt);
    if (Array.isArray(q.views)) {
      const vs = q.views.filter((x: any) => typeof x === "string");
      setViewKeys(vs);
      setViewMode(vs.length ? "views" : "single");
    } else {
      setViewMode("single");
    }
  }

  async function deletePreset() {
    const p = selectedPreset;
    if (!p || p.builtin) return;
    if (!window.confirm(`删除预设「${p.label}」？`)) return;
    try {
      await api.imageGenDeletePreset(p.id);
      setPresetId("");
      await loadPresets();
      setSaveMsg(`已删除预设「${p.label}」`);
    } catch (e: any) {
      setErr(e?.message || "删除失败");
    }
  }

  async function savePresetFromResult() {
    const params = shots[0]?.sparams;
    if (!params) return;
    const label = saveName.trim();
    if (!label) { setErr("请给预设起个名字"); return; }
    setErr("");
    try {
      const r = await api.imageGenSavePreset({ label, params, image: shots[0]?.file || "" });
      await loadPresets();
      setPresetId(r.id);
      setSaveOpen(false);
      setSaveName("");
      setSaveMsg(`已保存预设「${label}」（可在上方预设下拉里直接复用）`);
    } catch (e: any) {
      setErr(e?.message || "保存预设失败");
    }
  }

  async function uploadLora(file: File) {
    setErr("");
    setLoraBusy(true);
    setLoraNote("");
    try {
      const r = await api.imageGenUploadLora(file);
      await refreshOpts();
      setLora(r.name);
      setLoraNote(`LoRA 已上传：${r.name}（${(r.size / 1024 / 1024).toFixed(0)} MB），已自动选中`);
    } catch (e: any) {
      setErr(e?.message || "LoRA 上传失败");
    } finally {
      setLoraBusy(false);
    }
  }

  async function toggleService() {
    setErr("");
    setBooting(true);
    try {
      if (opts?.available) {
        const r = await api.imageGenStop();
        if (!r.stopped) setErr(r.detail || "停止失败");
      } else {
        const r = await api.imageGenStart();
        if (r.error) setErr(r.error);
      }
    } catch (e: any) {
      setErr(e?.message || "操作失败");
    } finally {
      setBooting(false);
      refreshOpts();
    }
  }

  /** 把本次生成的图登记进当前项目的素材库（复用导入接口；失败不影响结果展示与下载）。 */
  async function importToLib(files: GenFile[]) {
    setLibNote("");
    if (!projectId || !toLib) return;
    try {
      const list: File[] = [];
      for (const f of files) {
        const url = await api.imageGenBlob(f.file);
        const blob = await (await fetch(url)).blob();
        list.push(new File([blob], f.file, { type: "image/png" }));
      }
      const res = await api.importAssets(projectId, list, "image");
      const n = res.imported?.length ?? 0;
      setLibNote(n ? `已入素材库 ${n} 张。` : `未入素材库：${res.skipped?.[0]?.reason || "未知原因"}`);
      if (n) onImported?.();
    } catch (e: any) {
      setLibNote(`入素材库失败：${e?.message || e}（不影响上方结果与下载）`);
    }
  }

  /** 用默认对话模型根据「当前输入的内容」补全正面 / 负面提示词。 */
  async function fillPrompts() {
    setErr("");
    setFillNote("");
    if (!prompt.trim()) { setErr("请先在描述框里写下想画的内容，再点「帮我补全提示词」"); return; }
    setFillBusy(true);
    try {
      const r = await api.imageGenFillPrompts({
        user_input: prompt.trim(),
        current_positive: prompt,
        current_negative: negative,
      });
      if (r.positive) setPrompt(r.positive);
      if (r.negative) setNegative(r.negative);
      setFillNote(
        `已用 ${r.model || "默认对话模型"} 补全${r.note ? ` · ${r.note}` : ""} · ${(r.elapsed_ms / 1000).toFixed(1)}s`
      );
    } catch (e: any) {
      setErr(e?.message || "补全提示词失败");
    } finally {
      setFillBusy(false);
    }
  }

  async function generate() {
    setErr("");
    setSaveMsg("");
    setReviewNote("");
    if (!prompt.trim()) { setErr("请输入提示词"); return; }
    if (refFile && refMode === "none") setRefMode("style");
    const jobId = newJobId();
    setBusy(true);
    // 轮询任务表：每 1.2s 拉一次阶段快照；阶段文案一变就往日志里追加一行（去重），
    // 让用户看到「增强→准备服务→采样第 i/n→质检→保存」这样一条真实时间线。
    poll.start(jobId, (id) => api.imageGenJob(id));
    try {
      let refName = "";
      if (refFile) {
        const up = await api.imageGenUpload(refFile);
        refName = up.file;
      }
      const r: GenResult = await api.imageGen({
        prompt, reference: refName,
        reference_mode: refFile ? (refMode === "img2img" ? "img2img" : "style") : "style",
        workflow_mode: workflowMode, model, enhance: false, size, count,
        lora, lora_strength: lora ? loraStrength : 0,
        negative_prompt: negative,
        views: viewMode === "views" ? viewKeys : undefined,
        auto_fix: autoFix,
        job_id: jobId,
      });
      // 兼容：老后端只返回单个 file
      const files: GenFile[] = r.files?.length ? r.files : [{ file: r.file, url: r.url || "", size: r.size }];
      const total = files.length;
      const now: Shot[] = [];
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const url = await api.imageGenBlob(f.file);
        if (url) blobUrls.current.add(url);
        now.push({
          key: `${f.file}_${Date.now()}_${i}`, file: f.file, url, size: f.size,
          index: i + 1, total, prompt,
          workflow_mode: workflowMode, model, ref_mode: refFile ? refMode : "none",
          elapsed_ms: r.elapsed_ms,
          lora, lora_strength: lora ? loraStrength : 0,
          sparams: r.params,
          view: f.view,
        });
      }
      setShots(now);
      setMeta({ elapsed_ms: r.elapsed_ms, size: r.size, enhance_note: r.enhance_note, enhanced_prompt: r.enhanced_prompt });
      setHistory((h) => [...now, ...h].slice(0, 24));
      if (r.review?.issues?.length) {
        setReviewNote(
          `AI 质检未完全通过：${Array.from(new Set(r.review.issues)).join("、")}（已按修复词尝试重生成，仍异常的可再点生成）`
        );
      }
      importToLib(files);
      poll.stop();
      poll.setFinal({ stage: "done", stage_label: `完成，共 ${total} 张`, percent: 100, done: true, image_index: total, image_count: total });
    } catch (e: any) {
      setErr(e?.message || "生成失败");
      poll.stop();
      poll.setFinal({ stage: "failed", stage_label: "生成失败", error: e?.message || "生成失败", done: true });
    } finally {
      setBusy(false);
    }
  }

  function shotMeta(s: Shot) {
    return [
      s.view ? `视角：${s.view}` : "",
      s.prompt,
      `${s.model || MODE_LABEL[s.workflow_mode] || s.workflow_mode}`,
      s.lora ? `LoRA ${loraShort(s.lora)}@${s.lora_strength}` : "无 LoRA",
      s.ref_mode !== "none" ? `参考图${s.ref_mode === "img2img" ? "重绘" : "风格"}` : "文生图",
      `${(s.size / 1024).toFixed(0)} KB`,
      s.total > 1 ? `${s.index}/${s.total}` : "",
    ].filter(Boolean).join(" · ");
  }

  function ShotGrid({ list, showIndex }: { list: Shot[]; showIndex: boolean }) {
    return (
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
        {list.map((s) => (
          <div key={s.key} className="relative rounded-lg overflow-hidden border border-slate-200 bg-slate-50 group">
            <img
              src={s.url}
              alt={`生成结果 ${s.index}`}
              title={shotMeta(s)}
              className="w-full aspect-square object-contain bg-slate-50 cursor-zoom-in"
              onClick={() => window.open(s.url, "_blank")}
            />
            {showIndex && (
              <span className="absolute left-1.5 top-1.5 min-w-[20px] h-5 px-1 rounded-full bg-brand-600 text-white text-[11px] leading-5 text-center font-semibold">
                {s.view ? s.view : s.index}
              </span>
            )}
            <button
              className="absolute right-1.5 top-1.5 text-[11px] px-2 py-0.5 rounded bg-white/95 border border-slate-300 text-slate-700 hover:bg-white"
              onClick={() => downloadUrl(s.url, s.file)}
              title={`下载 ${s.file}`}
            >
              下载
            </button>
            <div className="px-1.5 py-1 text-[10px] text-slate-400 truncate" title={shotMeta(s)}>
              {(s.size / 1024).toFixed(0)} KB
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="bg-white rounded border border-slate-200 p-4 mb-4">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <span className="text-sm font-semibold">AI 生图工作台</span>
        {booting ? (
          <span className="text-[10px] px-2 py-1 rounded bg-amber-50 text-amber-700">
            {opts?.available ? "停止中…" : "启动中…（首次约 30-60 秒）"}
          </span>
        ) : opts ? (
          <>
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${opts.available ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-600"}`}>
              {opts.available
                ? `ComfyUI 运行中（${opts.checkpoints.length} checkpoint / ${opts.diffusion.length} diffusion / ${loraList.length} LoRA）`
                : "ComfyUI 未运行"}
            </span>
            <button
              className={`ga-btn ga-btn-sm ${opts.available ? "ga-btn-danger" : "ga-btn-secondary"}`}
              onClick={toggleService}>
              {opts.available ? "停止服务" : "启动服务"}
            </button>
          </>
        ) : (
          <span className="text-[10px] px-2 py-0.5 rounded bg-slate-50 text-slate-500">检测中…</span>
        )}
        {!opts?.available && !booting && opts && (
          <span className="text-[11px] text-slate-400">生成时会自动启动，也可点上方按钮手动启动</span>
        )}
      </div>

      {/* 预设配方：每个预设选择旁都带参考图 */}
      <div className="mb-2">
        <div className="flex flex-wrap items-center gap-2 text-xs mb-1.5">
          <span className="text-slate-500">预设</span>
          <button onClick={() => applyPreset("")}
            className={`rounded border px-2 py-1 ${presetId === "" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"}`}>
            不使用预设
          </button>
          {presets.map((p) => (
            <button key={p.id} onClick={() => applyPreset(p.id)} title={`${p.label}（点缩略图放大）`}
              className={`flex items-center gap-1.5 rounded border px-1.5 py-1 ${presetId === p.id ? "border-brand-500 bg-brand-50 text-brand-700" : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"}`}>
              {p.image_file && presetImg[p.image_file] ? (
                <img src={presetImg[p.image_file]} alt={p.label} title="点击放大查看"
                  className="w-8 h-8 rounded object-cover cursor-zoom-in"
                  onClick={(e) => { e.stopPropagation(); setPreview({ url: presetImg[p.image_file!], label: p.label }); }} />
              ) : (
                <span className="w-8 h-8 rounded bg-slate-100 text-[9px] text-slate-400 flex items-center justify-center">无图</span>
              )}
              <span className="max-w-[120px] truncate">{p.label}</span>
            </button>
          ))}
          {selectedPreset && !selectedPreset.builtin && (
            <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={deletePreset}>删除该预设</button>
          )}
        </div>
        {projectId && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-slate-500">项目强制预设（AI 工具生图时强制套用）</span>
            <SearchSelect
              className="border border-slate-200 rounded px-2 py-1 text-xs w-[220px]"
              value={projPresetId}
              title="选择项目强制预设"
              searchPlaceholder="搜索预设…"
              options={[
                { value: "", label: "不强制（跟随工具参数）" },
                ...presets.map((p) => ({
                  value: p.id,
                  label: `${p.label}${p.builtin ? "" : "（自定义）"}`,
                  keywords: p.key || p.label,
                })),
              ]}
              onChange={bindProjectPreset}
            />
            {(() => {
              const pp = presets.find((p) => p.id === projPresetId);
              const img = pp?.image_file ? presetImg[pp.image_file] : "";
              return img
                ? <img src={img} alt="项目预设参考图" title={`${pp?.label}（点击放大）`}
                    className="w-10 h-10 rounded object-cover border border-slate-200 cursor-zoom-in"
                    onClick={() => setPreview({ url: img, label: pp?.label || "" })} />
                : pp ? <span className="w-10 h-10 rounded bg-slate-100 text-[9px] text-slate-400 flex items-center justify-center">无图</span> : null;
            })()}
            {projPresetId && <span className="text-[11px] text-amber-700">工具（对话里）生图将强制用这个预设的尺寸 / LoRA / 画风</span>}
            <span className="text-[11px] text-slate-400">上方预设卡片只作用于本页手动出图；对话里的 AI 生图只认这一行</span>
          </div>
        )}
        {saveMsg && <div className="text-[11px] text-emerald-700 mt-1">{saveMsg}</div>}
      </div>

      <div className="space-y-3">
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <span className="text-xs text-slate-600">正面提示词</span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="ga-btn ga-btn-secondary ga-btn-sm"
                onClick={fillPrompts}
                disabled={fillBusy || busy}
                title="用默认对话模型，根据你上面写的内容自动补全正面 + 负面提示词"
              >
                {fillBusy ? "生成中…" : "✨ 帮我补全提示词"}
              </button>
            </div>
          </div>
          <textarea
            className="w-full border border-slate-200 rounded px-2 py-1.5 text-sm h-20 resize-y"
            placeholder="描述想要的画面：主体、场景、构图、风格、配色、用途…（如：火焰宝石图标，圆形徽章构图，亮橙红色调，游戏UI图标）"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
          {fillNote && <span className="text-[11px] text-emerald-700">{fillNote}</span>}
        </div>
        {/* 负面提示词 */}
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-600">负面提示词（要避免的元素，可留空）</span>
            {opts?.default_negative_prompt && (
              <button className="text-[11px] text-brand-600 hover:underline"
                onClick={() => setNegative(opts?.default_negative_prompt || "")}>
                恢复默认负面词
              </button>
            )}
          </div>
          <textarea
            className="w-full border border-slate-200 rounded px-2 py-1.5 text-xs h-14 resize-y"
            placeholder="如：多余手指、文字、水印、低清、模糊、畸形…（留空=用服务默认负面词）"
            value={negative}
            onChange={(e) => setNegative(e.target.value)}
          />
        </div>
        {/* 多视角 / 八方画像 */}
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <span className="text-slate-600">生成模式</span>
            <label className="flex items-center gap-1 cursor-pointer">
              <input type="radio" name="viewMode" checked={viewMode === "single"}
                onChange={() => setViewMode("single")} />
              单张
            </label>
            <label className="flex items-center gap-1 cursor-pointer">
              <input type="radio" name="viewMode" checked={viewMode === "views"}
                onChange={() => {
                  setViewMode("views");
                  // 多视角每张是「单人全身」，必须竖构图；当前若是横版/方版则自动切到竖版，
                  // 否则底模会把全身裁成半身（横版 16:9 只拍到腰以上）。
                  const m = /^(\d+)x(\d+)$/.exec(size);
                  if (m && Number(m[1]) >= Number(m[2]) * 0.9) {
                    if (Number(m[1]) > Number(m[2])) setSize(`${m[2]}x${m[1]}`); // 横→竖 等比换向
                    else setSize("1024x1536");
                  }
                }} />
              多视角（八方画像）
            </label>
            {viewMode === "views" && (
              <span className="text-[11px] text-amber-700">
                将逐张生成选中视角（同一人物同画风、只变方向），共 {viewKeys.length} 张 · 自动用竖版全身构图
              </span>
            )}
          </div>
          {viewMode === "views" && (
            <div className="flex flex-wrap gap-1.5 pl-1">
              <button className="text-[11px] px-2 py-0.5 rounded border border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
                onClick={() => setViewKeys((opts?.views ?? []).filter(v => v.group === "环绕").map(v => v.key))}>
                标准8方位
              </button>
              <button className="text-[11px] px-2 py-0.5 rounded border border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
                onClick={() => setViewKeys((opts?.views ?? []).map(v => v.key))}>
                全选视角
              </button>
              <button className="text-[11px] px-2 py-0.5 rounded border border-slate-300 bg-white text-slate-600 hover:bg-slate-50"
                onClick={() => setViewKeys([])}>
                清空
              </button>
              {(opts?.views ?? []).map((v) => {
                const on = viewKeys.includes(v.key);
                return (
                  <label key={v.key}
                    className={`flex items-center gap-1 cursor-pointer px-2 py-0.5 rounded border text-[11px] ${on ? "border-brand-500 bg-brand-50 text-brand-700" : "border-slate-200 bg-white text-slate-600"}`}>
                    <input type="checkbox" className="hidden" checked={on}
                      onChange={() => setViewKeys((cur) => on ? cur.filter(k => k !== v.key) : [...cur, v.key])} />
                    {on ? "✓ " : ""}{v.label}
                  </label>
                );
              })}
            </div>
          )}
        </div>
        {/* 风格 LoRA */}
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            LoRA
            <SearchSelect
              className="border border-slate-200 rounded px-2 py-1 text-xs w-[260px]"
              value={lora}
              title="选择或搜索 LoRA"
              searchPlaceholder="搜索 LoRA…"
              emptyText="没有匹配的 LoRA"
              options={[
                { value: "", label: "不挂 LoRA" },
                ...(lora && !loraList.some((n) => n.replace(/\\/g, "/") === lora.replace(/\\/g, "/"))
                  ? [{ value: lora, label: `${loraShort(lora)}（当前）`, keywords: lora }]
                  : []),
                ...loraList.map((n) => ({ value: n, label: loraShort(n), keywords: n })),
              ]}
              onChange={setLora}
            />
          </label>
          <label className="flex items-center gap-1.5" title="0.6~0.8 实测较稳；0.9 以上容易坏手">
            强度
            <input type="range" min={0} max={1.2} step={0.05} value={loraStrength}
              disabled={!lora}
              onChange={(e) => setLoraStrength(Number(e.target.value))} />
            <span className="tabular-nums text-slate-600">{lora ? loraStrength.toFixed(2) : "—"}</span>
          </label>
          <label className="flex items-center gap-1.5" title={opts?.loras_dir ? `上传目录：${opts.loras_dir}` : "未找到 ComfyUI 的 models/loras 目录"}>
            <span className={`ga-btn ga-btn-secondary ga-btn-sm ${loraBusy ? "opacity-60" : ""}`}>
              {loraBusy ? "上传中…" : "上传 LoRA"}
            </span>
            <input type="file" accept=".safetensors" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadLora(f); e.target.value = ""; }} />
          </label>
          {loraNote && <span className="text-[11px] text-emerald-700 break-all">{loraNote}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            参考图
            <input type="file" accept="image/*" className="text-xs"
              onChange={(e) => { setRefFile(e.target.files?.[0] ?? null); if (e.target.files?.[0] && refMode === "none") setRefMode("style"); }} />
          </label>
          {refFile && (
            <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={refMode}
              onChange={(e) => setRefMode(e.target.value as any)}>
              <option value="style">按参考图风格出图（内容全新）</option>
              <option value="img2img">照图生图（以参考图为底重绘）</option>
            </select>
          )}
          <label className="flex items-center gap-1.5">
            尺寸
            <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={size}
              onChange={(e) => setSize(e.target.value)}>
              {!SIZE_OPTS.includes(size) && <option value={size}>{size}（预设）</option>}
              {SIZE_OPTS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5" title="一次生成多张，每张独立文件、可单独下载">
            张数
            <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={count}
              disabled={viewMode === "views"}
              onChange={(e) => setCount(Number(e.target.value))}>
              {COUNT_OPTS.map((n) => <option key={n} value={n}>{n} 张</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 cursor-pointer"
            title="出图后让主模型看图检查是否有六指、三条腿等结构异常；发现就自动补负面词并重生成一次（需要主模型开启视觉）">
            <input type="checkbox" checked={autoFix} onChange={(e) => setAutoFix(e.target.checked)} />
            出图后 AI 质检并自动修复
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            生成方式
            <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={workflowMode}
              onChange={(e) => { setWorkflowMode(e.target.value as any); setModel(""); }}>
              {(["auto", "checkpoint", "diffusion"] as const).map((m) => (
                <option key={m} value={m}>{MODE_LABEL[m]}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            模型
            <SearchSelect
              className="border border-slate-200 rounded px-2 py-1 text-xs w-[300px]"
              value={model}
              disabled={workflowMode === "auto"}
              title={workflowMode === "auto" ? "自动选择（探测可用模型）" : "选择或搜索模型"}
              searchPlaceholder="搜索模型…"
              emptyText={modelList.length ? "没有匹配的模型" : "ComfyUI 未连接，无法列出模型"}
              options={
                workflowMode === "auto"
                  ? [{ value: "", label: "自动选择（探测可用模型）" }]
                  : modelList.length
                    ? [{ value: "", label: "默认（列表第一个）" },
                       ...modelList.map((m) => ({ value: m, label: m, keywords: m }))]
                    : [{ value: "", label: "ComfyUI 未连接，无法列出模型" }]
              }
              onChange={setModel}
            />
          </label>
          <button
            className="ga-btn ga-btn-primary"
            onClick={generate} disabled={busy || booting || !opts?.available}>
            {busy ? (jobProgress?.stage_label ? `生成中… ${STAGE_SHORT[jobProgress.stage] || ""}`.trim() : "生成中…")
              : viewMode === "views" ? `生成 ${viewKeys.length} 个视角`
              : count > 1 ? `生成 ${count} 张` : "生成图片"}
          </button>
          {projectId ? (
            <label className="flex items-center gap-1.5 cursor-pointer text-xs text-slate-600"
              title="生成成功后自动把图登记进当前选中项目的素材库">
              <input type="checkbox" checked={toLib} onChange={(e) => setToLib(e.target.checked)} />
              结果入素材库
            </label>
          ) : (
            <span className="text-[11px] text-slate-400">上方未选项目，结果不会入素材库</span>
          )}
        </div>
        {busy && jobProgress && <GenProgress p={jobProgress} log={stageLog} steps={IMAGE_STAGES} hint="2K 大图单张约 5 分钟，请耐心等待，无需重复点击" />}
        {busy && jobProgress && (jobProgress.previews?.length ?? 0) > 0 && (
          <LivePreviews previews={jobProgress.previews || []} total={jobProgress.image_count || undefined} />
        )}
        {err && <div className="text-xs text-red-600 break-all">{err}</div>}
        {reviewNote && <div className="text-xs text-amber-700 break-all">{reviewNote}</div>}
        {libNote && <div className="text-xs text-emerald-700">{libNote}</div>}
      </div>

      {/* 本次结果 */}
      <div className="mt-3 pt-3 border-t border-slate-100">
        {shots.length ? (
          <>
            <div className="flex items-center gap-2 mb-1.5 flex-wrap">
              <span className="text-[11px] text-slate-500">本次结果（{shots.length} 张，点右上角「下载」逐张保存）</span>
              {meta && (
                <span className="text-[11px] text-slate-400">
                  {fmtTime(meta.elapsed_ms)} · {(meta.size / 1024).toFixed(0)} KB
                  {shots[0]?.model ? ` · ${shots[0].model}` : ` · ${MODE_LABEL[shots[0]?.workflow_mode] || ""}`}
                  {` · ${shots[0]?.lora ? `LoRA ${loraShort(shots[0].lora)}@${shots[0].lora_strength}` : "无 LoRA"}`}
                  {shots[0]?.ref_mode !== "none" ? ` · 参考图${shots[0]?.ref_mode === "img2img" ? "重绘" : "风格"}` : " · 文生图"}
                </span>
              )}
              {shots.length > 1 && (
                <button
                  className="ml-auto ga-btn ga-btn-secondary ga-btn-sm"
                  onClick={() => shots.forEach((s, i) => setTimeout(() => downloadUrl(s.url, s.file), i * 250))}>
                  全部下载
                </button>
              )}
              {shots[0]?.sparams && (
                <span className={shots.length > 1 ? "flex items-center gap-1" : "ml-auto flex items-center gap-1"}>
                  {saveOpen ? (
                    <>
                      <input autoFocus className="border border-slate-200 rounded px-2 py-0.5 text-xs w-32"
                        placeholder="预设名称，如 我的厚涂" value={saveName}
                        onChange={(e) => setSaveName(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") savePresetFromResult(); }} />
                      <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={savePresetFromResult}>保存</button>
                      <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setSaveOpen(false)}>取消</button>
                    </>
                  ) : (
                    <button className="ga-btn ga-btn-secondary ga-btn-sm"
                      title="把本次出图实际使用的参数（尺寸/LoRA/强度/生成方式等）存成预设"
                      onClick={() => { setSaveOpen(true); setSaveName(""); setSaveMsg(""); }}>
                      满意，存为预设
                    </button>
                  )}
                </span>
              )}
            </div>
            <ShotGrid list={shots} showIndex={shots.length > 1} />
            {meta?.enhance_note && <div className="text-[11px] text-slate-400 mt-1">{meta.enhance_note}</div>}
            {meta?.enhanced_prompt && (
              <details className="text-[11px] text-slate-500 mt-1">
                <summary className="cursor-pointer">查看增强后的提示词</summary>
                <div className="mt-1 bg-slate-50 rounded p-2 whitespace-pre-wrap">{meta.enhanced_prompt}</div>
              </details>
            )}
          </>
        ) : (
          <div className="h-24 rounded-lg border border-dashed border-slate-200 flex items-center justify-center text-xs text-slate-400">
            生成结果会显示在这里（满意后可一键「存为预设」）
          </div>
        )}
      </div>

      {/* 会话记录 */}
      {history.length > shots.length && (
        <div className="mt-3 pt-3 border-t border-slate-100">
          <div className="text-[11px] text-slate-400 mb-1.5">本次会话生成记录（点右上角「下载」逐张保存）</div>
          <ShotGrid list={history} showIndex={false} />
        </div>
      )}

      {/* 预设参考图放大 */}
      {preview && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6 cursor-zoom-out"
          onClick={() => setPreview(null)} role="dialog" aria-label={preview.label}>
          <div className="flex flex-col items-center gap-2" onClick={(e) => e.stopPropagation()}>
            <img src={preview.url} alt={preview.label}
              className="max-w-[92vw] max-h-[82vh] object-contain rounded shadow-2xl cursor-default" />
            <div className="text-white text-xs flex items-center gap-3">
              <span>{preview.label}</span>
              <button className="px-2 py-0.5 rounded bg-white/15 hover:bg-white/25" onClick={() => setPreview(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
