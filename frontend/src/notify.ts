/**
 * 系统提示（提示音 + 桌面通知）：任务完成 / 需要用户回答，两种事件两种音色。
 *
 * - 音色用 Web Audio 现场合成（不引音频资源）：完成 = 上行双音「叮-咚」，
 *   需要回答 = 三连短促蜂鸣，听感上一听就知道是哪类事。
 * - 桌面通知走 Notification API（tag 去重不堆叠，silent 避免和提示音叠响）。
 * - 点通知：把窗口拉到前面，再执行调用方给的 onClick（跳到那条会话）。
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
  if (!on) return;
  // 开启这一下就是用户手势：当场自检一遍（响一声 + 有权限就弹一条测试通知）。
  // 以前开启后要等下一次任务跑完才知道到底通不通，中间任何一环断了都表现为「静默没提醒」，
  // 排查时根本分不清是开关、权限、还是系统勿扰——现在点一下就知道了。
  playChime("done");
  const testNotice = () => {
    if ("Notification" in window && Notification.permission === "granted") {
      desktop("done", "提醒已开启", "这是一条测试通知：任务完成/需要回答时就会这样提醒你");
    }
  };
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().then(testNotice).catch(() => {});
  } else {
    testNotice();
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
function chime(c: AudioContext, kind: NotifyKind) {
  const t0 = c.currentTime + 0.02;
  if (kind === "done") {
    tone(c, t0, 784, 0.22, "sine", 0.16);
    tone(c, t0 + 0.16, 1046.5, 0.34, "sine", 0.16);
  } else {
    for (let i = 0; i < 3; i++) tone(c, t0 + i * 0.19, 880, 0.1, "triangle", 0.18);
  }
}

export function playChime(kind: NotifyKind) {
  const c = ctx();
  if (!c) return;
  if (c.state === "running") {
    chime(c, kind);
    return;
  }
  // 挂起态以前是直接 return（一声不响）：页面开久了 / 机器睡眠唤醒 / 标签页被压之后
  // AudioContext 会回到 suspended，那时所有提示音都静默消失，看着就像「提醒坏了」。
  // resume() 是异步的，等它真的 running 再响。
  c.resume()
    .then(() => {
      if (c.state === "running") chime(c, kind);
    })
    .catch(() => {
      /* 没有用户手势时浏览器会拒绝，只能不出声 */
    });
}

function desktop(kind: NotifyKind, title: string, body: string, onClick?: () => void) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, {
      body,
      tag: kind === "done" ? "ga-run-done" : "ga-question",
      silent: true, // 声音由 playChime 负责，避免双响
    });
    n.onclick = () => {
      // 先把窗口拉到前面（用户点通知的诉求就是「看那条会话」），再交给调用方跳转。
      // 跳转本身失败（会话已删/路由没就绪）不能让通知卡在没关的状态。
      window.focus();
      try {
        onClick?.();
      } catch {
        /* ignore */
      }
      n.close();
    };
  } catch {
    /* ignore */
  }
}

/**
 * 统一入口：开关关着直接忽略；开着则提示音 + 桌面通知（各自静默失败）。
 *
 * `onClick`：点桌面通知时除了把窗口拉到前面，再做调用方指定的事（跳到那条会话）。
 * 浏览器没给通知权限时只有提示音，没有可点的东西，这是平台限制，不是漏做。
 *
 * 「提醒没反应」排查用：开关关着 / 浏览器不支持 / 权限没给，三种情况都会在控制台留一条
 * 明确的原因（以前一律静默 return，用户只能干瞪眼猜是哪儿断了）。
 */
export function fireNotify(
  kind: NotifyKind,
  title: string,
  body: string,
  onClick?: () => void,
) {
  if (!notifyEnabled()) {
    console.debug("[notify] 提醒开关是关闭状态，已跳过：", title);
    return;
  }
  playChime(kind);
  if (!("Notification" in window)) {
    console.warn("[notify] 这个浏览器不支持桌面通知，只有提示音：", title);
    return;
  }
  if (Notification.permission !== "granted") {
    console.warn(
      `[notify] 桌面通知没有权限（permission=${Notification.permission}），只有提示音；` +
        "点顶栏的铃铛重新开启可再申请一次权限：",
      title,
    );
    return;
  }
  desktop(kind, title, body, onClick);
}

/** 截断给通知正文用的长文本。 */
export function truncBody(s: unknown, n = 120): string {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n) + "…" : t;
}
