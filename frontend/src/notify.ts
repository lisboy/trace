/**
 * 系统提示（提示音 + 桌面通知）：任务完成 / 需要用户回答，两种事件两种音色。
 *
 * - 音色用 Web Audio 现场合成（不引音频资源）：完成 = 上行双音「叮-咚」，
 *   需要回答 = 三连短促蜂鸣，听感上一听就知道是哪类事。
 * - 桌面通知走 Notification API（tag 去重不堆叠，silent 避免和提示音叠响）。
 * - 开关持久化在 localStorage；浏览器要求音频必须先有用户手势，
 *   所以首次任意点击/按键就解锁 AudioContext（没解锁前只发桌面通知不出声）。
 */

const STORE_KEY = "ga_notify_v1";

export type NotifyKind = "done" | "question";

export function notifyEnabled(): boolean {
  try {
    return localStorage.getItem(STORE_KEY) === "on";
  } catch {
    return false;
  }
}

/** 开提示时顺便申请桌面通知权限（必须在用户手势里调用，点击铃铛正好是）。 */
export function setNotifyEnabled(on: boolean) {
  try {
    if (on) localStorage.setItem(STORE_KEY, "on");
    else localStorage.removeItem(STORE_KEY);
  } catch {
    /* ignore */
  }
  if (on && "Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}

export function notifyPermission(): string {
  return "Notification" in window ? Notification.permission : "unsupported";
}

let audioCtx: AudioContext | null = null;

function ctx(): AudioContext | null {
  const AC = window.AudioContext || (window as any).webkitAudioContext;
  if (!AC) return null;
  if (!audioCtx) {
    try {
      audioCtx = new AC();
    } catch {
      return null;
    }
  }
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

// 首次手势解锁（浏览器自动播放策略：没有手势时 AudioContext 保持 suspended）
if (typeof window !== "undefined") {
  const unlock = () => {
    ctx();
    window.removeEventListener("pointerdown", unlock);
    window.removeEventListener("keydown", unlock);
  };
  window.addEventListener("pointerdown", unlock);
  window.addEventListener("keydown", unlock);
}

function tone(c: AudioContext, at: number, freq: number, dur: number, type: OscillatorType, vol: number) {
  const o = c.createOscillator();
  const g = c.createGain();
  o.type = type;
  o.frequency.setValueAtTime(freq, at);
  g.gain.setValueAtTime(0, at);
  g.gain.linearRampToValueAtTime(vol, at + 0.012);
  g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  o.connect(g);
  g.connect(c.destination);
  o.start(at);
  o.stop(at + dur + 0.02);
}

/** 完成：上行双音 G5 → C6（柔和正弦）。提问：三连 880Hz 短蜂鸣（三角波，更「催人」）。 */
export function playChime(kind: NotifyKind) {
  const c = ctx();
  if (!c || c.state !== "running") return;
  const t0 = c.currentTime + 0.02;
  if (kind === "done") {
    tone(c, t0, 784, 0.22, "sine", 0.16);
    tone(c, t0 + 0.16, 1046.5, 0.34, "sine", 0.16);
  } else {
    for (let i = 0; i < 3; i++) tone(c, t0 + i * 0.19, 880, 0.1, "triangle", 0.18);
  }
}

function desktop(kind: NotifyKind, title: string, body: string) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, {
      body,
      tag: kind === "done" ? "ga-run-done" : "ga-question",
      silent: true, // 声音由 playChime 负责，避免双响
    });
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch {
    /* ignore */
  }
}

/** 统一入口：开关关着直接忽略；开着则提示音 + 桌面通知（各自静默失败）。 */
export function fireNotify(kind: NotifyKind, title: string, body: string) {
  if (!notifyEnabled()) return;
  playChime(kind);
  desktop(kind, title, body);
}

/** 截断给通知正文用的长文本。 */
export function truncBody(s: unknown, n = 120): string {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n) + "…" : t;
}
