import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import SearchSelect, { type SelectOption } from "./SearchSelect";
import { AV_STAGES, GenProgress, newJobId, STAGE_SHORT, useJobPoll } from "./GenProgress";

type Status = {
  enabled: boolean;
  reachable: boolean;
  model_loaded: boolean;
  design_ready: boolean;
  presets: string[];
  presets_error?: string | null;
  default_preset: string;
  has_ref_audio: boolean;
  voice_design: boolean;
  emo_mode: string;
  emo_text: string;
  emo_weight: number;
  emo_vector: string;
  has_emo_audio: boolean;
  default_speed?: number | null;
  default_lang?: string | null;
  error?: string | null;
  base_url?: string;
};

type Result = {
  file: string;
  url: string;
  elapsed_ms: number;
  size: number;
  preset: string;
  chars: number;
  used_text: string;
  enhance_note?: string;
  emo_mode?: string | null;
};

type Candidate = { name: string; url: string; seconds?: number; elapsed?: number; localUrl: string };

// IndexTTS2 的情绪四条通路。只给音色参考音频时模型用平静语气（台词一多就"三句一个调"），
// 所以要显式选一种情绪来源。
const EMO_OPTS = [
  { v: "0", label: "沿用参考音色的情绪（平）" },
  { v: "1", label: "情感参考音频（另给一段带情绪的干声）" },
  { v: "2", label: "八维情感向量（逐项调权重）" },
  { v: "3", label: "文字描述情绪（如「愤怒地质问」）" },
];
// 8 维顺序与服务端严格一致，别改顺序
const EMO_VEC_NAMES = ["高兴", "愤怒", "悲伤", "害怕", "厌恶", "忧郁", "惊讶", "平静"];
const EMO_MODE_LABEL: Record<string, string> = {
  "0": "同参考（平）", "1": "情感音频", "2": "情感向量", "3": "文字情绪",
};

// 与 IndexTTS2 语音服务端的限制保持一致（/api/v1/voice-design/status 有回报，
// 这里写死默认值做即时提示，超限会被服务端以 413 直接拒绝，不会静默截断）。
// 描述偏长时看着像"被截断"，多半是小文本框没撑开 + 没有字数提示造成的错觉。
const DESC_MAX = 300;   // 音色描述上限（字）
const SAY_MAX = 120;    // 试听台词上限（字）

function fmtMs(ms: number): string {
  return ms >= 60000 ? `${Math.round(ms / 60000)} 分` : `${(ms / 1000).toFixed(1)} 秒`;
}

/**
 * 语音生成工作台（嵌在素材库页，同 AI 生图面板并列）：界面直连本机 IndexTTS2 出配音（不走 agent）。
 *
 * 三种音色来源，按可靠性从高到低：
 * 1. 音色预设（IndexTTS2 服务端存的"参考音频 + 参数"，最省事，推荐）
 * 2. 临时上传参考音频（3~10 秒干声，一次性使用）
 * 3. 音色设计：写一句中文描述，让 VoxCPM2 造出参考音频再存成预设
 *    （不需要任何现成音频；会先把 IndexTTS2 卸载腾显存，所以首次合成会慢一次）
 *
 * 生成成功后自动把 wav 导入当前选中项目的素材库（可勾选关掉）。
 */
export default function VoiceGenPanel(
  { projectId, onImported }: { projectId?: string; onImported?: () => void } = {}
) {
  const [status, setStatus] = useState<Status | null>(null);
  const [text, setText] = useState("");
  const [preset, setPreset] = useState("");
  const [refFile, setRefFile] = useState<File | null>(null);
  const [emoMode, setEmoMode] = useState("0");
  const [emoText, setEmoText] = useState("");
  const [emoWeight, setEmoWeight] = useState(0.65);
  const [emoVec, setEmoVec] = useState<number[]>(Array(EMO_VEC_NAMES.length).fill(0));
  const [emoAudioFile, setEmoAudioFile] = useState<File | null>(null);
  const [emoAudioTrial, setEmoAudioTrial] = useState("");
  const emoAudioRef = useRef<HTMLInputElement>(null);
  const [speed, setSpeed] = useState(1.0);   // 语速 0.5~2.0（IndexTTS-2.5 的 duration_factor）
  const [label, setLabel] = useState("");
  const [enhance, setEnhance] = useState(false);
  const [starting, setStarting] = useState(false);
  const [gpuNote, setGpuNote] = useState("");

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [resultUrl, setResultUrl] = useState("");
  const [history, setHistory] = useState<(Result & { url: string })[]>([]);
  // 处理过程可视化：合成是阻塞操作（首次加载模型可能要 1 分钟），轮询后端任务表显示真实阶段。
  const poll = useJobPoll(1000);

  // 结果自动入素材库
  const [toLib, setToLib] = useState(true);
  const [libNote, setLibNote] = useState("");

  // 音色设计
  const [designOpen, setDesignOpen] = useState(false);
  const [desc, setDesc] = useState("");
  const [say, setSay] = useState("你好，我是这个声音，很高兴认识你。");
  const [designN, setDesignN] = useState(1);
  const [designing, setDesigning] = useState(false);
  const [cands, setCands] = useState<Candidate[]>([]);
  const [designNote, setDesignNote] = useState("");
  const [savingName, setSavingName] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  // 上传音频 → 直接存成音色预设（不需要先生成）
  const [presetOpen, setPresetOpen] = useState(false);
  const [upFile, setUpFile] = useState<File | null>(null);
  const [upName, setUpName] = useState("");
  const [upTrial, setUpTrial] = useState("");   // 本地试听 url（ObjectURL）
  const [savingPreset, setSavingPreset] = useState(false);
  const [presetNote, setPresetNote] = useState("");
  const upFileRef = useRef<HTMLInputElement>(null);

  // 音色下拉候选（可搜索）：服务端预设 + 当前临时上传项。音色名可能很多且长，
  // 用原生 select 装不下也不好找，这里交给 SearchSelect 过滤。
  const voiceOptions = useMemo<SelectOption[]>(() => {
    const opts: SelectOption[] = (status?.presets ?? []).map((p) => ({ value: p, label: p, keywords: p }));
    if (preset && !opts.some((o) => o.value === preset)) {
      opts.unshift({ value: preset, label: `${preset}（当前）` });
    }
    if (refFile) opts.unshift({ value: "__upload", label: `已选文件：${refFile.name}` });
    else opts.push({ value: "__upload", label: "＋ 上传参考音频…" });
    return opts;
  }, [status?.presets, preset, refFile]);

  async function refresh() {
    try {
      const s: Status = await api.voiceGenStatus();
      setStatus(s);
      if (!preset && s.default_preset) setPreset(s.default_preset);
      if (s.emo_mode && emoMode === "0" && s.emo_mode !== "0") setEmoMode(s.emo_mode);
      if (s.emo_weight) setEmoWeight(s.emo_weight);
      // .env 里若配了默认语速（IndexTTS-2.5 的 duration_factor），初始化到滑块上
      if (typeof s.default_speed === "number" && Number.isFinite(s.default_speed)) setSpeed(s.default_speed);
      // .env 里若配了默认八维向量，初始化到滑块上，方便用户看到当前基线
      if (s.emo_vector) {
        const v = String(s.emo_vector).split(/[,\s]+/).filter(Boolean).map(Number);
        if (v.length) setEmoVec(EMO_VEC_NAMES.map((_, i) => Number.isFinite(v[i]) ? v[i] : 0));
      }
    } catch (e: any) {
      setStatus({ enabled: true, reachable: false, model_loaded: false, design_ready: false,
        presets: [], default_preset: "", has_ref_audio: false, voice_design: false,
        emo_mode: "0", emo_text: "", emo_weight: 0.65, emo_vector: "", has_emo_audio: false,
        error: e?.message || "状态读取失败" });
    }
  }

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  /** 把生成结果登记进当前项目的素材库（复用导入接口；失败不影响结果展示与下载）。 */
  async function importToLib(r: Result, url: string) {
    setLibNote("");
    if (!projectId) return;
    if (!toLib) return;
    try {
      const blob = await (await fetch(url)).blob();
      const file = new File([blob], r.file, { type: "audio/wav" });
      const res = await api.importAssets(projectId, [file], "audio");
      const n = res.imported?.length ?? 0;
      setLibNote(n ? "已入素材库。" : `未入素材库：${res.skipped?.[0]?.reason || "未知原因"}`);
      if (n) onImported?.();
    } catch (e: any) {
      setLibNote(`入素材库失败：${e?.message || e}（不影响上方结果与下载）`);
    }
  }

  async function generate() {
    setErr("");
    if (!text.trim()) { setErr("请输入要合成的文本"); return; }
    const jobId = newJobId();
    setBusy(true);
    const t0 = Date.now();
    try {
      let reference = "";
      if (refFile) {
        const up = await api.voiceGenUpload(refFile);
        reference = up.file;
      }
      // 情感参考音频（mode=1）：单独上传一次，拿回文件名交给合成接口
      let emoAudio = "";
      if (emoMode === "1") {
        if (!emoAudioFile) { setErr("选了「情感参考音频」但还没选文件"); setBusy(false); return; }
        const up = await api.voiceGenUpload(emoAudioFile);
        emoAudio = up.file;
      }
      poll.start(jobId, (id) => api.voiceGenJob(id));
      const r: Result = await api.voiceGen({
        text, preset: reference ? "" : preset, reference,
        emo_mode: emoMode,
        emo_text: emoMode === "3" ? emoText : "",
        emo_weight: emoWeight,
        emo_vector: emoMode === "2" ? emoVec : undefined,
        emo_audio: emoAudio,
        speed: Math.abs(speed - 1.0) < 0.005 ? undefined : Number(speed.toFixed(2)),
        label: label || "voice",
        job_id: jobId,
      });
      const url = await api.voiceGenBlob(r.file);
      setResult(r);
      setResultUrl(url);
      setHistory((h) => [{ ...r, url }, ...h].slice(0, 10));
      importToLib(r, url);
      refresh();   // 生成过一次后模型变成 loaded，状态栏要跟着变
      poll.stop();
      poll.setFinal({ stage: "done", stage_label: "音频已生成", percent: 100, done: true });
    } catch (e: any) {
      setErr(e?.message || `生成失败（${fmtMs(Date.now() - t0)}）`);
      poll.stop();
      poll.setFinal({ stage: "failed", stage_label: "合成失败", error: e?.message || "合成失败", done: true });
    } finally {
      setBusy(false);
    }
  }

  async function toggleService() {
    setErr("");
    setGpuNote("");
    setStarting(true);
    try {
      if (reachable) {
        const r = await api.voiceGenStop();
        if (!r?.stopped) setErr(r?.detail || "停止失败（服务可能已退出）");
      } else {
        const r = await api.voiceGenStart();
        if (r?.error) setErr(`启动失败：${r.error}`);
        else if (r?.gpu_note) setGpuNote(`已腾出显存：${r.gpu_note}`);
      }
    } catch (e: any) {
      setErr(e?.message || "操作失败");
    } finally {
      setStarting(false);
      await refresh();
    }
  }

  async function runDesign() {
    setErr("");
    setDesignNote("");
    if (!desc.trim()) { setErr("请先填写音色描述"); return; }
    // 超限直接说明（服务端 300/120 字会以 413 拒绝，别等它抛错）
    if (desc.trim().length > DESC_MAX) {
      setErr(`音色描述 ${desc.trim().length} 字，超过上限 ${DESC_MAX} 字，请精简后再生成（可勾选「大模型润色」让它帮你提炼声音特征）`);
      return;
    }
    if (say.trim().length > SAY_MAX) {
      setErr(`试听台词 ${say.trim().length} 字，超过上限 ${SAY_MAX} 字（参考音频只要 3~10 秒，一句短语即可）`);
      return;
    }
    setDesigning(true);
    try {
      const j = await api.voiceGenDesign({ desc, say, n: designN, enhance });
      const list: Candidate[] = [];
      for (const f of j.files || []) {
        list.push({ ...f, localUrl: await api.voiceGenDesignBlob(f.name) });
      }
      setCands(list);
      const notes: string[] = [];
      if (j.enhance_note) notes.push(j.enhance_note);
      if (j.used_desc && j.used_desc !== desc.trim()) notes.push(`设计提示词：${j.used_desc}`);
      notes.push(`完成：${list.length} 条候选，总用时 ${j.elapsed ?? "-"} 秒。挑一条存成预设，再回来合成。`);
      setDesignNote(notes.join("。"));
    } catch (e: any) {
      setErr(e?.message || "音色设计失败");
    } finally {
      setDesigning(false);
    }
  }

  async function savePreset(c: Candidate) {
    const name = (savingName || desc.slice(0, 12) || "新音色").trim();
    if (!name) return;
    setErr("");
    try {
      await api.voiceGenSavePreset({ name, file: c.name });
      setDesignNote(`已保存为预设「${name}」，已在上面选好，直接填文本就能合成。`);
      setPreset(name);
      setRefFile(null);
      refresh();
    } catch (e: any) {
      const msg = e?.message || "";
      if (msg.includes("已存在")) {
        if (window.confirm(`预设「${name}」已存在，覆盖吗？`)) {
          try {
            await api.voiceGenSavePreset({ name, file: c.name, overwrite: true });
            setPreset(name); setRefFile(null); refresh();
            setDesignNote(`已覆盖预设「${name}」。`);
          } catch (e2: any) { setErr(e2?.message || "覆盖失败"); }
        }
      } else {
        setErr(msg || "保存预设失败");
      }
    }
  }

  /** 选中要上传的音频文件：本地试听 + 没填名字时用文件名当预设名。 */
  function pickUpFile(f: File | null) {
    setUpFile(f);
    setPresetNote("");
    if (upTrial) URL.revokeObjectURL(upTrial);
    setUpTrial(f ? URL.createObjectURL(f) : "");
    if (f && !upName.trim()) setUpName(f.name.replace(/\.[^.]+$/, "").slice(0, 24));
  }

  /** 上传音频直接存成预设：成功后自动选好这个音色，直接就能合成。 */
  async function saveUploadedPreset(overwrite = false) {
    setErr("");
    if (!upFile) { setErr("请先选择要上传的音频文件"); return; }
    const nm = upName.trim();
    if (!nm) { setErr("请给这个音色起个预设名（之后按名字复用）"); return; }
    setSavingPreset(true);
    try {
      const j = await api.voiceGenUploadPreset(upFile, { name: nm, overwrite });
      const saved = j?.name || nm;
      setPreset(saved);
      setRefFile(null);
      setPresetNote(`已保存为预设「${saved}」，已在上面「音色」里选好，填文本就能合成；`
        + `对话里也能让 agent 用这个音色（generate_audio voice=${saved}）。`);
      // 保持面板展开：确认提示就在面板里，收起会让用户看不到「存好了」的反馈
      refresh();
    } catch (e: any) {
      const msg = e?.message || "";
      if (msg.includes("已存在")) {
        if (window.confirm(`预设「${nm}」已存在，覆盖吗？`)) {
          setSavingPreset(false);
          await saveUploadedPreset(true);
          return;
        }
      } else {
        setErr(msg || "保存预设失败");
      }
    } finally {
      setSavingPreset(false);
    }
  }

  const reachable = status?.reachable;

  return (
    <div className="bg-white rounded border border-slate-200 p-4 mb-4">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <span className="text-sm font-semibold">语音生成工作台</span>
        {status ? (
          <>
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${reachable ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-600"}`}>
              {reachable
                ? `IndexTTS2 ${status.model_loaded ? "已加载" : "待加载（首次合成约 30~60 秒）"} · ${status.presets.length} 个音色`
                : (status.error || "语音服务未连接")}
            </span>
            {reachable && status.design_ready && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-700">
                音色设计可用
              </span>
            )}
            <button
              className={`text-[11px] px-2 py-0.5 rounded border disabled:opacity-50 ${
                reachable
                  ? "border-red-200 text-red-600 hover:bg-red-50"
                  : "border-emerald-300 text-emerald-700 hover:bg-emerald-50"
              }`}
              onClick={toggleService}
              disabled={starting}
              title={reachable ? "结束本机的 IndexTTS2 进程" : "在本机拉起 IndexTTS2 服务"}
            >
              {starting ? (reachable ? "停止中…" : "启动中…（首次约 10~30 秒）") : (reachable ? "停止服务" : "启动服务")}
            </button>
            <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={refresh}>
              刷新状态
            </button>
          </>
        ) : (
          <span className="text-[10px] px-2 py-0.5 rounded bg-slate-50 text-slate-500">检测中…</span>
        )}
        <span className="ml-auto text-[11px] text-slate-400">{status?.base_url}</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* 左：输入 */}
        <div className="lg:col-span-2 space-y-3">
          <textarea
            className="w-full border border-slate-200 rounded px-2 py-1.5 text-sm h-24 resize-y"
            placeholder="要合成的台词。Markdown 符号会自动清掉。"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <label className="flex items-center gap-1.5">
              音色
              <SearchSelect
                className="border border-slate-200 rounded px-2 py-1 text-xs w-[240px]"
                value={refFile ? "__upload" : preset}
                options={voiceOptions}
                title="选择或搜索音色预设"
                searchPlaceholder="搜索音色…"
                emptyText={status?.presets.length ? "没有匹配的音色" : "服务端还没有音色预设"}
                onChange={(v) => {
                  if (v === "__upload") { fileRef.current?.click(); return; }
                  setRefFile(null); setPreset(v);
                }}
              />
            </label>
            {refFile && (
              <span className="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">
                已选文件：{refFile.name}
                <button className="ml-1 underline" onClick={() => { setRefFile(null); if (fileRef.current) fileRef.current.value = ""; }}>
                  取消
                </button>
              </span>
            )}
            <input ref={fileRef} type="file" accept="audio/*" className="hidden"
              onChange={(e) => { setRefFile(e.target.files?.[0] ?? null); }} />
            <label className="flex items-center gap-1.5">
              情绪
              <SearchSelect
                className="border border-slate-200 rounded px-2 py-1 text-xs w-[210px]"
                value={emoMode}
                options={EMO_OPTS.map((o) => ({ value: o.v, label: o.label }))}
                title="选择情绪通路"
                onChange={setEmoMode}
              />
            </label>
            {(emoMode === "1" || emoMode === "2") && (
              <label className="flex items-center gap-1.5">
                强度
                <input type="range" min={0} max={1} step={0.05} value={emoWeight}
                  onChange={(e) => setEmoWeight(Number(e.target.value))} />
                <span className="text-slate-500">{emoWeight.toFixed(2)}</span>
              </label>
            )}
            <label className="flex items-center gap-1.5" title="语速控制（IndexTTS-2.5 生效）：大于 1 更慢更从容，小于 1 更快更急促">
              语速
              <input type="range" min={0.5} max={2} step={0.05} value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))} />
              <span className="text-slate-500">{speed.toFixed(2)}×</span>
            </label>
            <input className="border border-slate-200 rounded px-2 py-1 text-xs w-28"
              placeholder="文件名标签" value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          {/* 情绪：三种"加感情"通路各自的输入区（只给音色参考音频时模型会用平静语气） */}
          {emoMode === "1" && (
            <div className="flex flex-wrap items-center gap-2 text-xs pl-1">
              <input ref={emoAudioRef} type="file" accept="audio/*" className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  setEmoAudioFile(f);
                  if (emoAudioTrial) URL.revokeObjectURL(emoAudioTrial);
                  setEmoAudioTrial(f ? URL.createObjectURL(f) : "");
                }} />
              <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => emoAudioRef.current?.click()}>
                选择情感音频…
              </button>
              {emoAudioFile ? (
                <span className="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">
                  {emoAudioFile.name}
                  <button className="ml-1 underline" onClick={() => {
                    setEmoAudioFile(null); setEmoAudioTrial("");
                    if (emoAudioRef.current) emoAudioRef.current.value = "";
                  }}>移除</button>
                </span>
              ) : (
                <span className="text-[11px] text-slate-400">
                  另给一段「带目标情绪」的干声（3~10 秒），用它的情绪念这段文本
                </span>
              )}
              {emoAudioTrial && <audio src={emoAudioTrial} controls className="h-8 max-w-[240px]" />}
            </div>
          )}
          {emoMode === "2" && (
            <div className="rounded border border-slate-100 bg-slate-50/60 p-2 space-y-1">
              <div className="text-[11px] text-slate-500">
                逐项调情感权重（0~1，总和≤0.8 最稳）；例如想让语气开心就把「高兴」拉高
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1">
                {EMO_VEC_NAMES.map((nm, i) => (
                  <label key={nm} className="flex items-center gap-1.5 text-[11px]">
                    <span className="w-8 shrink-0 text-slate-600">{nm}</span>
                    <input type="range" min={0} max={1} step={0.05} value={emoVec[i]}
                      className="flex-1 min-w-0"
                      onChange={(e) => {
                        const v = Number(e.target.value);
                        setEmoVec((arr) => arr.map((x, j) => (j === i ? v : x)));
                      }} />
                    <span className="w-8 text-right text-slate-400">{emoVec[i].toFixed(2)}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
          {emoMode === "3" && (
            <div className="flex flex-wrap items-center gap-2 text-xs pl-1">
              <input className="border border-slate-200 rounded px-2 py-1 text-xs w-64"
                placeholder="情绪描述，如：愤怒地质问 / 压抑着怒气的低吼 / 温柔地哄"
                value={emoText} onChange={(e) => setEmoText(e.target.value)} />
              <label className="flex items-center gap-1.5">
                强度
                <input type="range" min={0} max={1} step={0.05} value={emoWeight}
                  onChange={(e) => setEmoWeight(Number(e.target.value))} />
                <span className="text-slate-500">{emoWeight.toFixed(2)}</span>
              </label>
            </div>
          )}
          <div className="flex items-center gap-3 flex-wrap">
            <button
              className="ga-btn ga-btn-primary"
              onClick={generate}
              disabled={busy}
            >
              {busy ? (poll.progress?.stage_label ? `合成中… ${STAGE_SHORT[poll.progress.stage] || ""}`.trim() : "合成中…")
                : "生成语音"}
            </button>
            {projectId ? (
              <label className="flex items-center gap-1.5 cursor-pointer text-xs text-slate-600"
                title="生成成功后自动把 wav 登记进当前选中项目的素材库">
                <input type="checkbox" checked={toLib} onChange={(e) => setToLib(e.target.checked)} />
                结果入素材库
              </label>
            ) : (
              <span className="text-[11px] text-slate-400">上方未选项目，结果不会入素材库</span>
            )}
            {!reachable && status && (
              <span className="text-[11px] text-slate-400">
                服务未连接；直接点「生成语音」也会自动启动 IndexTTS2
              </span>
            )}
          </div>
          {err && <div className="text-xs text-red-600">{err}</div>}
          {libNote && <div className="text-xs text-emerald-700">{libNote}</div>}
          {gpuNote && <div className="text-[11px] text-slate-500">{gpuNote}</div>}
          {busy && poll.progress && (
            <GenProgress p={poll.progress} log={poll.log} steps={AV_STAGES}
              hint="8G 单卡串行推理：一段 10 秒音频通常 20~60 秒；首次还要加载模型" />
          )}
        </div>

        {/* 右：结果 */}
        <div>
          {result ? (
            <div>
              <audio src={resultUrl} controls className="w-full" />
              <div className="text-[11px] text-slate-400 mt-1.5">
                {fmtMs(result.elapsed_ms)} · {(result.size / 1024).toFixed(0)} KB · {result.chars} 字
                {result.preset ? ` · ${result.preset}` : " · 上传参考音频"}
                {result.emo_mode ? ` · 情绪：${EMO_MODE_LABEL[result.emo_mode] || result.emo_mode}` : ""}
              </div>
              <a className="text-[11px] text-brand-600 hover:underline" href={resultUrl} download={result.file}>
                下载 wav
              </a>
              {result.enhance_note && (
                <div className="text-[11px] text-slate-400 mt-1 break-all">{result.enhance_note}</div>
              )}
            </div>
          ) : (
            <div className="h-40 rounded-lg border border-dashed border-slate-200 flex items-center justify-center text-xs text-slate-400">
              生成结果会显示在这里
            </div>
          )}
        </div>
      </div>

      {history.length > 1 && (
        <div className="mt-3 pt-3 border-t border-slate-100">
          <div className="text-[11px] text-slate-400 mb-1.5">本次会话生成记录</div>
          <div className="space-y-1.5">
            {history.map((h) => (
              <div key={h.file} className="flex items-center gap-2 text-[11px]">
                <button className={`shrink-0 ${result?.file === h.file ? "text-brand-600" : "text-slate-400 hover:text-brand-600"}`}
                  onClick={() => { setResult(h); setResultUrl(h.url); }}>
                  {fmtMs(h.elapsed_ms)}
                </button>
                <span className="flex-1 truncate text-slate-500">{h.used_text}</span>
                <audio src={h.url} controls className="h-7 max-w-[200px]" />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 上传音频 → 直接存为预设 */}
      <div className="mt-3 pt-3 border-t border-slate-100">
        <button className="flex items-center gap-2 text-sm font-semibold" onClick={() => setPresetOpen((v) => !v)}>
          <span className="text-slate-400">{presetOpen ? "▾" : "▸"}</span>
          上传音频存为预设（手里已有干声？直接做成可复用的音色）
        </button>
        {presetOpen && (
          <div className="mt-3 space-y-2">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <input ref={upFileRef} type="file" accept="audio/*" className="hidden"
                onChange={(e) => pickUpFile(e.target.files?.[0] ?? null)} />
              <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => upFileRef.current?.click()}>
                选择音频文件…
              </button>
              {upFile ? (
                <span className="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">
                  {upFile.name} · {(upFile.size / 1024).toFixed(0)} KB
                  <button className="ml-1 underline" onClick={() => {
                    pickUpFile(null); if (upFileRef.current) upFileRef.current.value = "";
                  }}>移除</button>
                </span>
              ) : (
                <span className="text-[11px] text-slate-400">3~10 秒干声效果最好（wav / mp3 / m4a / flac / ogg，≤20MB）</span>
              )}
            </div>
            {upTrial && <audio src={upTrial} controls className="h-9 w-full max-w-md" />}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <input className="border border-slate-200 rounded px-2 py-1 text-xs w-48"
                placeholder="预设名（如：沙哑大叔），必填"
                value={upName} maxLength={64}
                onChange={(e) => setUpName(e.target.value)} />
              <button className="ga-btn ga-btn-primary ga-btn-sm"
                onClick={() => saveUploadedPreset(false)}
                disabled={savingPreset || !upFile}>
                {savingPreset ? "保存中…" : "存为预设"}
              </button>
              <span className="text-[11px] text-slate-400">同名会提示是否覆盖；存好后会自动选上该音色</span>
            </div>
            {presetNote && <div className="text-[11px] text-emerald-700">{presetNote}</div>}
          </div>
        )}
      </div>

      {/* 音色设计 */}
      <div className="mt-3 pt-3 border-t border-slate-100">
        <button className="flex items-center gap-2 text-sm font-semibold" onClick={() => setDesignOpen((v) => !v)}>
          <span className="text-slate-400">{designOpen ? "▾" : "▸"}</span>
          音色设计（没有现成音频？用一句话描述造一个）
        </button>
        {designOpen && (
          <div className="mt-3 space-y-2">
            <textarea
              className="w-full border border-slate-200 rounded px-2 py-1.5 text-sm h-40 resize-y"
              placeholder="例如：三十岁左右的成熟女性，声音微哑慵懒，尾音带笑，语速偏慢（也可以直接贴整段角色介绍/人设，勾上「大模型润色」让它提炼声音特征）"
              value={desc} onChange={(e) => setDesc(e.target.value)}
            />
            <div className={`text-[11px] text-right ${desc.trim().length > DESC_MAX ? "text-red-600" : "text-slate-400"}`}>
              {desc.trim().length} / {DESC_MAX} 字{desc.trim().length > DESC_MAX ? "（超限，服务端会拒绝）" : ""}
            </div>
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <label className="flex items-center gap-1.5 cursor-pointer"
                title="勾上后，先用大模型把这里的描述（可以是角色介绍/人设）改写成只含声音特征的音色设计提示词，再送去造音色；失败自动回退原文">
                <input type="checkbox" checked={enhance} onChange={(e) => setEnhance(e.target.checked)} />
                大模型润色设计提示词
              </label>
              <input className="flex-1 min-w-[220px] border border-slate-200 rounded px-2 py-1 text-xs"
                placeholder="试听台词（8~20 字最佳）" value={say} onChange={(e) => setSay(e.target.value)} />
              <span className={`text-[11px] ${say.trim().length > SAY_MAX ? "text-red-600" : "text-slate-400"}`}>
                {say.trim().length}/{SAY_MAX}
              </span>
              <label className="flex items-center gap-1.5">
                候选
                <SearchSelect
                  className="border border-slate-200 rounded px-2 py-1 text-xs w-[90px]"
                  value={String(designN)}
                  options={[{ value: "1", label: "1 条" }, { value: "2", label: "2 条" }, { value: "3", label: "3 条" }]}
                  onChange={(v) => setDesignN(Number(v))}
                />
              </label>
              <input className="border border-slate-200 rounded px-2 py-1 text-xs w-36"
                placeholder="预设名（可选）" value={savingName} onChange={(e) => setSavingName(e.target.value)} />
              <button className="ga-btn ga-btn-primary ga-btn-sm"
                onClick={runDesign} disabled={designing || !reachable}>
                {designing ? "设计中…（首次约 1~2 分钟）" : "生成候选"}
              </button>
            </div>
            <div className="text-[11px] text-slate-400">
              这条路会先用 VoxCPM2 造参考音频，期间 IndexTTS2 被卸载腾显存，所以之后第一次合成会慢一次。
              同一条描述每次结果都不同，建议出 2~3 条挑。
            </div>
            {designNote && <div className="text-[11px] text-emerald-700">{designNote}</div>}
            {cands.length > 0 && (
              <div className="space-y-2 pt-1">
                {cands.map((c, i) => (
                  <div key={c.name} className="flex items-center gap-2 border border-slate-100 rounded px-2 py-1.5">
                    <span className="text-xs text-slate-500 w-16 shrink-0">候选 {i + 1}</span>
                    <audio src={c.localUrl} controls className="h-8 flex-1 min-w-0" />
                    <span className="text-[11px] text-slate-400 shrink-0">{c.seconds ?? "-"}s</span>
                    <button className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
                      onClick={() => savePreset(c)}>
                      存为预设
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
