/**
 * 对话朗读的情绪设置（🔊 朗读按钮用）。
 *
 * 背景：IndexTTS2 只给音色参考音频时默认用平静语气，长回复一多就"三句一个调"。
 * 服务端还有三条情绪通路（情感参考音频 / 八维向量 / 文字描述）。对话里的朗读
 * 按钮此前没有任何情绪设置，所以永远平。这里给对话单独一份"朗读情绪"：
 *   auto   — 不给素材，让服务端用台词本身做情感文本（Qwen 自动判情绪，默认最省事）
 *   calm   — 显式沿用参考音色情绪（平）
 *   vector — 预设八维向量（高兴/愤怒/悲伤… 单维拉高）
 *   text   — 一句话文字描述（如"压抑着怒气的低吼"）
 *   audio  — 上传一段带目标情绪的干声（仅本次会话有效，刷新后回到 auto）
 *
 * 非音频类的选择写进 localStorage，刷新/切会话都保留；音频带 File 无法持久化。
 */
export type ChatEmo =
  | { kind: "auto" }
  | { kind: "calm" }
  | { kind: "vector"; label: string; vec: number[] }
  | { kind: "text"; label: string; text: string }
  | { kind: "audio"; label: string; file: File };

// 8 维顺序与服务端严格一致：高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静
export const EMO_DIMS = ["高兴", "愤怒", "悲伤", "害怕", "厌恶", "忧郁", "惊讶", "平静"];

/** 单维拉高的预设（其余维度留 0，服务端会做 bias 归一化并封顶总强度）。 */
export const EMO_PRESETS: { id: string; label: string; vec: number[] }[] = EMO_DIMS.map(
  (name, i) => ({ id: `dim${i}`, label: name, vec: EMO_DIMS.map((_, j) => (i === j ? 0.8 : 0)) })
);

const LS_KEY = "ga_chat_tts_emo";

export function emoLabel(e: ChatEmo): string {
  switch (e.kind) {
    case "auto": return "自动";
    case "calm": return "平静";
    case "vector": return e.label;
    case "text": return e.text ? `文字:${e.text.slice(0, 6)}` : "文字";
    case "audio": return "情感音频";
  }
}

export function loadChatEmo(): ChatEmo {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      const j = JSON.parse(raw);
      if (j?.kind === "calm") return { kind: "calm" };
      if (j?.kind === "vector" && Array.isArray(j.vec)) return { kind: "vector", label: String(j.label || "情绪"), vec: j.vec };
      if (j?.kind === "text" && j.text) return { kind: "text", label: String(j.label || "文字"), text: String(j.text) };
    }
  } catch { /* ignore */ }
  return { kind: "auto" };
}

export function saveChatEmo(e: ChatEmo) {
  try {
    if (e.kind === "auto") localStorage.removeItem(LS_KEY);
    else if (e.kind === "audio") localStorage.removeItem(LS_KEY); // File 不能存，刷新回 auto
    else localStorage.setItem(LS_KEY, JSON.stringify(e));
  } catch { /* ignore */ }
}

/** 转成 /voice-gen 请求体里的情绪字段（音频 kind 需上层先上传文件再补 emo_audio）。 */
export function emoPayload(e: ChatEmo): Record<string, unknown> {
  switch (e.kind) {
    case "auto": return { emo_auto: true };
    case "calm": return { emo_mode: "0" };
    case "vector": return { emo_mode: "2", emo_vector: e.vec };
    case "text": return { emo_mode: "3", emo_text: e.text };
    case "audio": return {}; // 见 speakMessage：先 upload 再把文件名塞 emo_audio
  }
}
