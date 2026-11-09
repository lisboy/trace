import { useEffect, useState } from "react";
import { api } from "../api";
import { AV_STAGES, GenProgress, newJobId, STAGE_SHORT, useJobPoll } from "./GenProgress";

type Options = {
  available: boolean;
  base_url?: string;
  model?: string;
  model_label?: string;
  default_size?: string;
  default_length?: number;
  default_fps?: number;
  default_steps?: number;
  smooth_fps?: number;
  presets?: { key: string; label: string; size: string; duration_s: number }[];
};
type GenResult = {
  file: string;
  url: string;
  size: number;
  enhanced_prompt: string;
  enhance_note: string;
  elapsed_ms: number;
  model?: string;
  params?: Record<string, any>;
};
type Clip = { key: string; file: string; url: string; size: number; prompt: string; elapsed_ms: number };

const SIZE_OPTS = ["832x480", "512x768", "768x512", "1280x704", "960x544", "704x1280", "480x832", "1024x576"];
// 漫剧镜头建议单段 3–6 秒：越长时间漂移累积越严重，短段分别生成再拼接更稳。
const DUR_OPTS = [2, 3, 4, 5, 6, 8];

function fmtTime(ms: number) {
  return ms >= 60000 ? `${Math.round(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒` : `${(ms / 1000).toFixed(1)} 秒`;
}

/** blob URL 直接触发浏览器下载（结果文件接口需要鉴权，不能用裸 <a href>） */
function downloadUrl(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "clip.mp4";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/**
 * 视频生成工作台：界面直连 ComfyUI（Wan2.2 TI2V 5B）出片（不走 agent）。
 * 主推「图生视频」：先传角色/场景参考图当首帧，锁住人物长相再让模型动起来（防变脸最有效）；
 * 提示词（可选 LLM 增强）+ 尺寸/时长。原生 24fps，出片后自动补到 60fps 顺滑并做轻降噪。
 * 漫剧镜头建议单段 3–6 秒、小动作（眨眼/轻转头/衣袂飘），长镜头拆成多段分别生成再拼。
 */
export default function VideoGenPanel(
  { projectId, onImported }: { projectId?: string; onImported?: () => void } = {}
) {
  const [prompt, setPrompt] = useState("");
  const [refFile, setRefFile] = useState<File | null>(null);
  const [refPreview, setRefPreview] = useState("");
  const [enhance, setEnhance] = useState(true);
  const [size, setSize] = useState("832x480");
  const [duration, setDuration] = useState(3);
  // 接龙长视频：每段 1 秒，逐段以上一段尾帧为首帧续写；段数即总秒数。
  const [chainMode, setChainMode] = useState(false);
  const [chainSegments, setChainSegments] = useState(4);
  const [opts, setOpts] = useState<Options | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [clips, setClips] = useState<Clip[]>([]);
  const [meta, setMeta] = useState<{ elapsed_ms: number; size: number; enhance_note: string; enhanced_prompt: string; params?: Record<string, any> } | null>(null);
  const [toLib, setToLib] = useState(true);
  const [libNote, setLibNote] = useState("");
  // 处理过程可视化：出片是几十秒到几分钟的阻塞操作，轮询后端任务表显示真实阶段 + 估算进度。
  const poll = useJobPoll(1200);

  useEffect(() => {
    api.videoGenOptions()
      .then((o: Options) => {
        setOpts(o);
        if (o.default_size) setSize(o.default_size);
      })
      .catch(() => setOpts({ available: false }));
  }, []);

  // 选参考图时生成本地预览，让用户确认「首帧」是谁、脸是否清楚
  function onPickRef(f: File | null) {
    setRefFile(f);
    setRefPreview((old) => { if (old) URL.revokeObjectURL(old); return f ? URL.createObjectURL(f) : ""; });
  }

  function applyPreset(p: NonNullable<Options["presets"]>[number]) {
    setSize(p.size);
    setDuration(p.duration_s);
  }

  async function importToLib(file: string) {
    setLibNote("");
    if (!projectId || !toLib) return;
    try {
      const url = await api.videoGenBlob(file);
      const blob = await (await fetch(url)).blob();
      const res = await api.importAssets(projectId, [new File([blob], file, { type: "video/mp4" })], "video");
      const n = res.imported?.length ?? 0;
      setLibNote(n ? "已入素材库。" : `未入素材库：${res.skipped?.[0]?.reason || "未知原因"}`);
      if (n) onImported?.();
    } catch (e: any) {
      setLibNote(`入素材库失败：${e?.message || e}（不影响上方结果与下载）`);
    }
  }

  async function generate() {
    setErr("");
    if (!prompt.trim()) { setErr("请输入提示词"); return; }
    const jobId = newJobId();
    setBusy(true);
    poll.start(jobId, (id) => api.videoGenJob(id));
    try {
      let refName = "";
      if (refFile) {
        const up = await api.videoGenUpload(refFile);
        refName = up.file;
      }
      const r: GenResult = await api.videoGen({
        prompt, reference: refName, enhance, size, duration_s: duration,
        ...(chainMode ? { segments: chainSegments, segment_seconds: 1 } : {}),
        job_id: jobId,
      });
      const url = await api.videoGenBlob(r.file);
      const clip: Clip = {
        key: `${r.file}_${Date.now()}`, file: r.file, url, size: r.size,
        prompt, elapsed_ms: r.elapsed_ms,
      };
      setClips((c) => [clip, ...c].slice(0, 12));
      setMeta({ elapsed_ms: r.elapsed_ms, size: r.size, enhance_note: r.enhance_note, enhanced_prompt: r.enhanced_prompt, params: r.params });
      importToLib(r.file);
      poll.stop();
      poll.setFinal({ stage: "done", stage_label: "视频已生成", percent: 100, done: true });
    } catch (e: any) {
      setErr(e?.message || "生成失败");
      poll.stop();
      poll.setFinal({ stage: "failed", stage_label: "生成失败", error: e?.message || "生成失败", done: true });
    } finally {
      setBusy(false);
    }
  }

  const presetList = opts?.presets ?? [];
  const smoothFps = opts?.smooth_fps || 0;

  return (
    <div className="bg-white rounded border border-slate-200 p-4 mb-4">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <span className="text-sm font-semibold">视频生成工作台</span>
        {opts ? (
          <span className={`text-[10px] px-1.5 py-0.5 rounded ${opts.available ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-600"}`}>
            {opts.available ? `ComfyUI 运行中（${opts.model_label || "Wan2.2 TI2V 5B"}）` : "ComfyUI 未运行"}
          </span>
        ) : (
          <span className="text-[10px] px-2 py-0.5 rounded bg-slate-50 text-slate-500">检测中…</span>
        )}
        <span className="text-[11px] text-amber-600">
          原生 24fps 出片，自动补到 {smoothFps || 60}fps 顺滑；单条约 2–5 分钟（8G 显卡，生成时自动启动服务）
        </span>
      </div>

      <div className="mb-2 text-[11px] rounded bg-indigo-50/70 text-indigo-700 px-2 py-1.5 leading-relaxed">
        角色镜头建议「先传参考图」锁住长相再动起来；动作幅度宁小勿大（眨眼、轻转头、衣袂飘），单段 3–6 秒最稳，长镜头拆成多段分别生成再拼接。
        参考图若是透明底 PNG 会自动铺浅灰底（防止背景变黑发怪）；出片保持原图画风，别被 3D 化。
      </div>

      {presetList.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs mb-2">
          <span className="text-slate-500">快捷档</span>
          {presetList.map((p) => (
            <button key={p.key} onClick={() => applyPreset(p)}
              className="rounded border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 px-2 py-1">
              {p.label}
            </button>
          ))}
        </div>
      )}

      <div className="space-y-3">
        <textarea
          className="w-full border border-slate-200 rounded px-2 py-1.5 text-sm h-20 resize-y"
          placeholder="描述想要的动态画面：主体、动作、镜头运动、场景、风格、光照…（如：一位白衣剑客在竹林中缓缓转身，镜头缓慢推进，薄雾，电影感）"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            <span className={refFile ? "text-indigo-600 font-medium" : ""}>首帧参考图（图生视频，防变脸）</span>
            <input type="file" accept="image/*" className="text-xs"
              onChange={(e) => onPickRef(e.target.files?.[0] ?? null)} />
          </label>
          {refPreview && (
            <span className="flex items-center gap-1">
              <img src={refPreview} alt="首帧预览" className="h-8 w-8 rounded object-cover border border-slate-200" />
              <button className="text-[11px] text-slate-400 hover:text-red-500" onClick={() => onPickRef(null)}>移除</button>
            </span>
          )}
          <label className="flex items-center gap-1.5">
            尺寸
            <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={size}
              onChange={(e) => setSize(e.target.value)}>
              {!SIZE_OPTS.includes(size) && <option value={size}>{size}</option>}
              {SIZE_OPTS.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 cursor-pointer" title="开启后每段 1 秒，逐段以上一段最后一帧为首帧续写，拼成连贯长片（比一次生成长片更稳、更省显存）">
            <input type="checkbox" checked={chainMode} onChange={(e) => setChainMode(e.target.checked)} />
            接龙长视频
          </label>
          {chainMode && (
            <label className="flex items-center gap-1.5" title="每段 1 秒；段数越多总时长越长（越多段越久）">
              段数
              <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={chainSegments}
                onChange={(e) => setChainSegments(Number(e.target.value))}>
                {[2, 3, 4, 5, 6, 8, 10, 12].map((n) => <option key={n} value={n}>{n} 段（约 {n} 秒）</option>)}
              </select>
            </label>
          )}
          <label className="flex items-center gap-1.5" title="帧数会按 4n+1 自动收敛；漫剧建议 3–6 秒">
            时长
            <select className="border border-slate-200 rounded px-2 py-1 text-xs" value={duration}
              disabled={chainMode}
              onChange={(e) => setDuration(Number(e.target.value))}>
              {DUR_OPTS.map((d) => <option key={d} value={d}>{d} 秒</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 cursor-pointer">
            <input type="checkbox" checked={enhance} onChange={(e) => setEnhance(e.target.checked)} />
            大模型先分析重组提示词
          </label>
          <button className="ga-btn ga-btn-primary" onClick={generate} disabled={busy}>
            {busy ? (poll.progress?.stage_label ? `生成中… ${STAGE_SHORT[poll.progress.stage] || ""}`.trim() : "生成中…")
              : "生成视频"}
          </button>
          {projectId ? (
            <label className="flex items-center gap-1.5 cursor-pointer text-xs text-slate-600"
              title="生成成功后自动把视频登记进当前选中项目的素材库">
              <input type="checkbox" checked={toLib} onChange={(e) => setToLib(e.target.checked)} />
              结果入素材库
            </label>
          ) : (
            <span className="text-[11px] text-slate-400">上方未选项目，结果不会入素材库</span>
          )}
        </div>
        {busy && poll.progress && (
          <GenProgress p={poll.progress} log={poll.log} steps={AV_STAGES}
            hint={chainMode ? `接龙 ${chainSegments} 段，每段约 1 分钟，请耐心等待` : "单段出片约 1–3 分钟，请耐心等待，无需重复点击"} />
        )}
        {err && <div className="text-xs text-red-600 break-all">{err}</div>}
        {libNote && <div className="text-xs text-emerald-700">{libNote}</div>}
      </div>

      <div className="mt-3 pt-3 border-t border-slate-100">
        {clips.length ? (
          <>
            {meta && (
              <div className="text-[11px] text-slate-400 mb-1.5">
                {fmtTime(meta.elapsed_ms)} · {(meta.size / 1024 / 1024).toFixed(1)} MB
                {meta.params?.src_fps ? ` · 原 ${meta.params.src_fps}fps → ${meta.params.fps}fps` : ""}
                {meta.params?.denoised ? " · 已降噪" : ""}
                {meta.params?.chained ? ` · 接龙 ${meta.params.segments} 段×${meta.params.seg_seconds}s` : ""}
              </div>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {clips.map((c) => (
                <div key={c.key} className="relative rounded-lg overflow-hidden border border-slate-200 bg-black group">
                  <video src={c.url} controls preload="metadata" className="w-full aspect-video object-contain bg-black" />
                  <div className="absolute right-1.5 top-1.5 flex gap-1">
                    <button
                      className="text-[11px] px-2 py-0.5 rounded bg-white/95 border border-slate-300 text-slate-700 hover:bg-white"
                      onClick={() => downloadUrl(c.url, c.file)}
                      title={`下载 ${c.file}`}
                    >下载</button>
                  </div>
                  <div className="px-1.5 py-1 text-[10px] text-slate-300 truncate" title={c.prompt}>
                    {(c.size / 1024 / 1024).toFixed(1)} MB · {c.prompt}
                  </div>
                </div>
              ))}
            </div>
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
            生成的视频会显示在这里
          </div>
        )}
      </div>
    </div>
  );
}
