import { useEffect, useState } from "react";
import { api } from "../api";
import { DEFAULT_PREFS, usePrefs, type ThemeMode } from "../theme";

/** 外观设置：主题 / 主色 / 背景（外链 / 上传 / Wallpaper Engine）/ 文字颜色 / 字号。
 *  改动经 usePrefs.update() 本地即时生效，防抖同步到服务端用户偏好。 */

const ACCENTS = [
  { hex: "#1f5fd0", label: "蓝" },
  { hex: "#0ea5e9", label: "天蓝" },
  { hex: "#19c2a8", label: "青" },
  { hex: "#059669", label: "绿" },
  { hex: "#ca8a04", label: "金" },
  { hex: "#ea580c", label: "橙" },
  { hex: "#e11d48", label: "玫红" },
  { hex: "#7c3aed", label: "紫" },
  { hex: "#475569", label: "石墨" },
];

const THEME_MODES: { v: ThemeMode; label: string }[] = [
  { v: "light", label: "浅色" },
  { v: "dark", label: "深色" },
  { v: "system", label: "跟随系统" },
];

const BG_KINDS: { v: string; label: string }[] = [
  { v: "none", label: "无" },
  { v: "url", label: "外链图片" },
  { v: "upload", label: "上传图片 / 视频" },
  { v: "we", label: "Wallpaper Engine" },
];

function Slider({ label, value, min, max, step = 1, unit, onChange }: {
  label: string; value: number; min: number; max: number; step?: number;
  unit?: string; onChange: (v: number) => void;
}) {
  return (
    <label className="block text-xs text-slate-500">
      <span className="flex items-center justify-between">
        <span>{label}</span>
        <span className="mono text-slate-400">{value}{unit}</span>
      </span>
      <input
        type="range"
        className="w-full accent-brand-600"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded border border-slate-200 p-4">
      <div className="text-sm font-medium mb-3">{title}</div>
      {children}
    </div>
  );
}

/** Wallpaper Engine 工坊文件缩略图（带鉴权 fetch→blob）。
 *  没有静态 preview.jpg 的视频壁纸拿视频首帧当缩略图（#t=0.1 + preload=metadata 只取帧不播放）。 */
function WeThumb({ path, video = false }: { path: string; video?: boolean }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setUrl("");
    setFailed(false);
    if (!path) return;
    let dead = false;
    let made = "";
    api.weFileBlob(path)
      .then((u) => {
        if (dead) URL.revokeObjectURL(u);
        else {
          made = u;
          setUrl(u);
        }
      })
      .catch(() => {
        if (!dead) setFailed(true);
      });
    return () => {
      dead = true;
      if (made) URL.revokeObjectURL(made);
    };
  }, [path]);
  if (failed) {
    return (
      <div className="flex h-16 w-full items-center justify-center rounded bg-slate-100 text-[10px] text-slate-400">
        {video ? "视频壁纸" : "无预览"}
      </div>
    );
  }
  if (!url) return <div className="h-16 w-full rounded bg-slate-100 animate-pulse" />;
  if (video) {
    return (
      <video
        src={`${url}#t=0.1`}
        muted
        preload="metadata"
        className="h-16 w-full rounded object-cover"
      />
    );
  }
  return (
    <img
      src={url}
      alt=""
      className="h-16 w-full object-cover rounded"
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

const TYPE_LABEL: Record<string, string> = {
  video: "视频",
  image: "图片",
  web: "网页",
  scene: "场景(取预览图)",
  unknown: "未知",
};

/** Wallpaper Engine 壁纸选择器：自动探测 Steam 工坊，扫不到可手填目录 */
function WePicker({ onPick }: { onPick: (src: string, video: boolean) => void }) {
  const [data, setData] = useState<any>(null);
  const [q, setQ] = useState("");
  const [rootInput, setRootInput] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);

  function refresh() {
    setLoading(true);
    setMsg("");
    api.weWallpapers()
      .then((r: any) => {
        setData(r);
        setRootInput(r.configured || "");
      })
      .catch((e: any) => setMsg(e?.message || "扫描失败"))
      .finally(() => setLoading(false));
  }
  useEffect(refresh, []);

  const items: any[] = (data?.items || []).filter(
    (it: any) => !q || (it.title || "").toLowerCase().includes(q.toLowerCase()),
  );
  const shown = items.slice(0, 120);

  return (
    <div className="border border-slate-200 rounded p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        {data?.detected?.length ? (
          <span className="text-green-700">已探测到工坊目录：{data.detected.join("；")}</span>
        ) : (
          <span className="text-amber-600">
            没有自动找到 Wallpaper Engine 工坊目录，可手动填写（形如 …/steamapps/workshop/content/431960）
          </span>
        )}
      </div>
      <div className="flex gap-2">
        <input
          className="flex-1 border rounded px-2 py-1 text-xs"
          placeholder="手填工坊目录（可选，留空用自动探测）"
          value={rootInput}
          onChange={(e) => setRootInput(e.target.value)}
        />
        <button
          className="ga-btn ga-btn-secondary ga-btn-sm"
          onClick={async () => {
            setMsg("");
            try {
              await api.setWeRoot(rootInput.trim());
              refresh();
            } catch (e: any) {
              setMsg(e?.message || "保存失败");
            }
          }}
        >
          保存目录
        </button>
        <button
          className="ga-btn ga-btn-secondary ga-btn-sm"
          onClick={refresh}
          disabled={loading}
        >
          {loading ? "扫描中…" : "重新扫描"}
        </button>
      </div>
      {msg && <div className="text-xs text-red-600">{msg}</div>}
      {items.length > 0 && (
        <input
          className="w-full border rounded px-2 py-1 text-xs"
          placeholder={`筛选壁纸（共 ${items.length} 个${items.length > 120 ? "，只显示前 120 个" : ""}）`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 max-h-80 overflow-auto">
        {shown.map((it) => {
          const video = it.type === "video" && !!it.media;
          // 背景源：视频类用 media(mp4) 才有动态；其余（图片/scene/web）media 缺失时退回 preview 静态图
          const pickFile = (video ? it.media : it.media || it.preview) || "";
          const src = pickFile ? `${it.dir}/${pickFile}` : "";
          if (!src) return null;
          // 缩略图优先静态 preview.jpg（视频有了它就不会截首帧）
          const thumbPath = `${it.dir}/${it.preview || pickFile}`;
          return (
            <button
              key={it.dir + it.id}
              className={`text-left rounded border p-1.5 hover:border-brand-500 ${
                it.type === "scene" || it.type === "web" ? "opacity-90" : ""
              }`}
              title={`${it.title}（${TYPE_LABEL[it.type] || it.type}）${it.type === "scene" || it.type === "web" ? "，动态壁纸以静态预览图作背景" : ""}`}
              onClick={() => onPick(src, video)}
            >
              <WeThumb path={thumbPath} video={video && !it.preview} />
              <div className="mt-1 truncate text-[11px] text-slate-600">{it.title}</div>
              <div className="flex items-center gap-1">
                <span className="text-[10px] px-1 rounded bg-slate-100 text-slate-500">
                  {TYPE_LABEL[it.type] || it.type}
                </span>
              </div>
            </button>
          );
        })}
      </div>
      {!loading && items.length === 0 && (
        <div className="text-xs text-slate-400">
          没有可用的工坊壁纸。确认 Wallpaper Engine 已安装并订阅过壁纸，或手动填工坊目录。
        </div>
      )}
    </div>
  );
}

export default function AppearanceTab() {
  const prefs = usePrefs((s) => s.prefs);
  const update = usePrefs((s) => s.update);
  const reset = usePrefs((s) => s.reset);
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState("");
  const bgOn = prefs.bgKind !== "none" && !!prefs.bgSrc;

  async function onUpload(file: File | undefined) {
    if (!file) return;
    setMsg("");
    setUploading(true);
    try {
      const r = await api.uploadWallpaper(file);
      update({ bgKind: "upload", bgSrc: r.url, bgVideo: r.kind === "video" });
    } catch (e: any) {
      setMsg(e?.message || "上传失败");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-3 max-w-3xl">
      <Card title="主题">
        <div className="flex gap-2">
          {THEME_MODES.map((m) => (
            <button
              key={m.v}
              className={`ga-btn ${prefs.theme === m.v ? "ga-btn-primary" : "ga-btn-secondary"}`}
              onClick={() => update({ theme: m.v })}
            >
              {m.label}
            </button>
          ))}
        </div>
        <div className="mt-3">
          <div className="text-xs text-slate-500 mb-1.5">主色（按钮 / 高亮 / 链接的基准色）</div>
          <div className="flex flex-wrap items-center gap-2">
            {ACCENTS.map((a) => (
              <button
                key={a.hex}
                title={a.label}
                className={`h-7 w-7 rounded-full border-2 ${
                  prefs.accent.toLowerCase() === a.hex ? "border-slate-700 scale-110" : "border-transparent"
                }`}
                style={{ background: a.hex }}
                onClick={() => update({ accent: a.hex })}
              />
            ))}
            <input
              type="color"
              value={/^#[0-9a-f]{6}$/i.test(prefs.accent) ? prefs.accent : "#1f5fd0"}
              onChange={(e) => update({ accent: e.target.value })}
              className="h-7 w-10 rounded cursor-pointer"
              title="自定义主色"
            />
          </div>
        </div>
      </Card>

      <Card title="背景壁纸">
        <div className="flex flex-wrap gap-2 mb-3">
          {BG_KINDS.map((k) => (
            <button
              key={k.v}
              className={`ga-btn ${prefs.bgKind === k.v ? "ga-btn-primary" : "ga-btn-secondary"}`}
              onClick={() => update({ bgKind: k.v as any, bgSrc: "", bgVideo: false })}
            >
              {k.label}
            </button>
          ))}
        </div>
        {prefs.bgKind === "url" && (
          <input
            className="w-full border rounded px-2 py-1 text-sm"
            placeholder="https://example.com/wallpaper.jpg"
            value={prefs.bgKind === "url" ? prefs.bgSrc : ""}
            onChange={(e) => update({ bgSrc: e.target.value.trim() })}
          />
        )}
        {prefs.bgKind === "upload" && (
          <div className="space-y-2">
            <label className="ga-btn ga-btn-secondary cursor-pointer">
              {uploading ? "上传中…" : "选择图片 / 视频文件（≤100MB）"}
              <input
                type="file"
                accept="image/*,video/mp4,video/webm"
                className="hidden"
                onChange={(e) => {
                  onUpload(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
            </label>
            {prefs.bgSrc && prefs.bgKind === "upload" && (
              <div className="text-[11px] text-slate-400 break-all">当前：{prefs.bgSrc}</div>
            )}
          </div>
        )}
        {prefs.bgKind === "we" && (
          <WePicker
            onPick={(src, video) => update({ bgSrc: src, bgVideo: video })}
          />
        )}
        {bgOn && (
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Slider label="背景不透明度" value={prefs.bgOpacity} min={5} max={100} unit="%"
              onChange={(v) => update({ bgOpacity: v })} />
            <Slider label="背景模糊" value={prefs.bgBlur} min={0} max={30} unit="px"
              onChange={(v) => update({ bgBlur: v })} />
            <Slider label="背景压暗" value={prefs.bgDim} min={0} max={80} unit="%"
              onChange={(v) => update({ bgDim: v })} />
            <Slider label="面板不透明度" value={prefs.panelAlpha} min={30} max={100} unit="%"
              onChange={(v) => update({ panelAlpha: v })} />
            <div className="col-span-2 text-[11px] text-slate-400">
              面板不透明度越低，壁纸透出来的越多；视频背景自动循环静音播放。Wallpaper Engine 的「场景 / 网页」类动态壁纸以它的静态预览图作背景。
            </div>
          </div>
        )}
        {msg && <div className="mt-2 text-xs text-red-600">{msg}</div>}
      </Card>

      <Card title="文字">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <div className="text-xs text-slate-500 mb-1.5">正文字体颜色（留空 = 默认）</div>
            <div className="flex items-center gap-2">
              <input
                type="color"
                value={/^#[0-9a-f]{6}$/i.test(prefs.textColor) ? prefs.textColor : "#0f172a"}
                onChange={(e) => update({ textColor: e.target.value })}
                className="h-8 w-12 rounded cursor-pointer"
                aria-label="正文字体颜色"
              />
              <button
                className="ga-btn ga-btn-secondary ga-btn-sm"
                onClick={() => update({ textColor: "" })}
              >
                恢复默认
              </button>
              <span className="text-[11px] text-slate-400">
                {prefs.textColor ? prefs.textColor : "未自定义"}
              </span>
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-500 mb-1.5">界面字号</div>
            <Slider label="根字号" value={prefs.fontSize} min={14} max={18} step={0.5} unit="px"
              onChange={(v) => update({ fontSize: v })} />
          </div>
        </div>
      </Card>

      <div className="flex items-center gap-2">
        <button
          className="ga-btn ga-btn-secondary"
          onClick={() => {
            if (confirm("恢复全部外观设置为默认？")) reset();
          }}
        >
          恢复默认外观
        </button>
        <span className="text-[11px] text-slate-400">
          设置保存在本机并同步到你的账号（换设备登录自动生效）。
        </span>
      </div>
    </div>
  );
}
