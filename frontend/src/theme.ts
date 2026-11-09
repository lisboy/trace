import { create } from "zustand";
import { api } from "./api";

/**
 * 外观偏好：主题 / 主色 / 背景（含 Wallpaper Engine 壁纸）/ 文字颜色 / 字号。
 * localStorage 立即生效，登录后与服务端用户偏好同步（跨设备一致）。
 */
export type ThemeMode = "light" | "dark" | "system";
export type BgKind = "none" | "url" | "upload" | "we";

export interface Prefs {
  theme: ThemeMode;
  accent: string; // 主色 hex，驱动 brand-* 全部色阶
  bgKind: BgKind;
  bgSrc: string; // 外链 URL / /uploads/xxx / WE 工坊内相对路径
  bgVideo: boolean; // bgSrc 是否视频（upload/we-video 用）
  bgOpacity: number; // 背景层不透明度 0-100
  bgBlur: number; // 背景模糊 px
  bgDim: number; // 背景压暗 0-100
  panelAlpha: number; // 面板不透明度 0-100（背景开启时才明显）
  textColor: string; // ""=默认；正文文字颜色
  fontSize: number; // 根字号 px（14-18）
}

export const DEFAULT_PREFS: Prefs = {
  theme: "light",
  accent: "#1f5fd0",
  bgKind: "none",
  bgSrc: "",
  bgVideo: false,
  bgOpacity: 100,
  bgBlur: 0,
  bgDim: 0,
  panelAlpha: 62, // 面板不透明度默认值要给壁纸留出可见度（用户反馈 85 太闷）
  textColor: "",
  fontSize: 16,
};

const KEY = "ga_prefs";

function loadLocal(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULT_PREFS, ...JSON.parse(raw) };
  } catch {
    /* ignore */
  }
  return { ...DEFAULT_PREFS };
}

function hexToRgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!m) return [31, 95, 208];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(rgb: [number, number, number], target: [number, number, number], t: number): string {
  const c = rgb.map((v, i) => Math.round(v + (target[i] - v) * t));
  return c.join(" ");
}

function apply(p: Prefs): void {
  const root = document.documentElement;
  const dark =
    p.theme === "dark" ||
    (p.theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  root.classList.toggle("dark", dark);
  root.classList.toggle("ga-bg-on", p.bgKind !== "none" && !!p.bgSrc);
  root.classList.toggle("ga-text-on", !!p.textColor);

  const rgb = hexToRgb(p.accent);
  const white: [number, number, number] = [255, 255, 255];
  const black: [number, number, number] = [10, 25, 55];
  const rs = root.style;
  rs.setProperty("--brand-50-rgb", mix(rgb, white, 0.9));
  rs.setProperty("--brand-100-rgb", mix(rgb, white, 0.78));
  // 深色下链接/文字按钮不能直接用 brand-600（那是给浅底配的），
  // 统一提亮一档，用户换主色时也跟着变。
  rs.setProperty("--brand-400-rgb", mix(rgb, white, 0.38));
  rs.setProperty("--brand-500-rgb", rgb.join(" "));
  rs.setProperty("--brand-600-rgb", mix(rgb, black, 0.16));
  rs.setProperty("--brand-700-rgb", mix(rgb, black, 0.34));
  rs.setProperty("--ga-panel-alpha", String(Math.min(1, Math.max(0.15, p.panelAlpha / 100))));
  rs.setProperty("--ga-text", p.textColor || "");
  rs.setProperty("--ga-font-size", `${p.fontSize}px`);
  rs.colorScheme = dark ? "dark" : "light";
}

interface PrefsState {
  prefs: Prefs;
  serverSynced: boolean;
  /** 本地即时生效（输入过程用），自动防抖同步服务端 */
  update: (patch: Partial<Prefs>) => void;
  /** 整体替换（从服务端拉回后） */
  replace: (p: Partial<Prefs>, serverSynced: boolean) => void;
  reset: () => void;
}

let saveTimer: number | undefined;

async function pushServer(prefs: Prefs): Promise<void> {
  try {
    await api.putPrefs(prefs);
  } catch {
    /* 未登录/离线时静默：本地已生效 */
  }
}

export const usePrefs = create<PrefsState>((set, get) => ({
  prefs: loadLocal(),
  serverSynced: false,
  update: (patch) => {
    const prefs = { ...get().prefs, ...patch };
    apply(prefs);
    localStorage.setItem(KEY, JSON.stringify(prefs));
    set({ prefs });
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => pushServer(prefs), 800);
  },
  replace: (p, serverSynced) => {
    const prefs = { ...DEFAULT_PREFS, ...p };
    apply(prefs);
    localStorage.setItem(KEY, JSON.stringify(prefs));
    set({ prefs, serverSynced });
  },
  reset: () => {
    const prefs = { ...DEFAULT_PREFS };
    apply(prefs);
    localStorage.setItem(KEY, JSON.stringify(prefs));
    set({ prefs });
    pushServer(prefs);
  },
}));

/** 登录后调用：拉取服务端偏好（有则覆盖本地），主题系统跟随也要监听系统切换 */
export async function initTheme(): Promise<void> {
  apply(usePrefs.getState().prefs);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const p = usePrefs.getState().prefs;
    if (p.theme === "system") apply(p);
  });
  try {
    const r = await api.mePrefs();
    if (r?.prefs && Object.keys(r.prefs).length) usePrefs.getState().replace(r.prefs, true);
  } catch {
    /* 未登录 */
  }
}
