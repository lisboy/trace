import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";

/** 工具结果里出现的图片路径：截图、回灌视觉的本地图、view_images 合成的联系表，都从这些
 *  前缀里取。「合图预览：<path>」是 view_images 落盘后写回结果文本的标记（合图本身只以
 *  data-url 回灌模型，路径不入库就永远没有缩略图可看）。 */
const IMG_RE =
  /(?:截图已保存|图片已附在本条消息中|图片路径|合图预览)[：:]\s*`?([^\s`（）()]+\.(?:png|jpe?g|webp|gif|bmp))/gi;

/** 「固定预览：run-shot/<team>/<run>/<file>」：工具落图时另存到 DATA_DIR/run_shots 的稳定
 *  标记（与工作区生命周期解耦）。认到它就走稳定目录取图，工作区被清空/换机器也还在。 */
const STABLE_RE = /固定预览[：:]\s*`?(run-shot\/[^\s`（）()]+)/i;

/** view_images 老结果（合图还没落盘那批）里只有「第1格=path、第2格=path」的清单：
 *  没有可显示的合图路径，就把这几张原图当缩略图顶上——总比一条都看不到强。
 *  新结果同时带「合图预览：…」，那时优先显示那张合图，不再铺这堆原图。 */
const CELL_RE =
  /第\s*\d+\s*格\s*=\s*`?([^\s`（）()、，,]+\.(?:png|jpe?g|webp|gif|bmp))/gi;

function collect(re: RegExp, text: string, out: string[]) {
  re.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    const p = m[1].replace(/^["'`]+|["'`]+$/g, "").replace(/\\/g, "/");
    if (p && !out.includes(p)) out.push(p);
  }
}

export function shotsFromText(text?: string | null): string[] {
  if (!text) return [];
  const out: string[] = [];
  collect(IMG_RE, text, out);
  // 没有可显示的工具产出图，但结果是多图合并的格子清单 → 退回显示原始各格图
  if (!out.length && /第\s*\d+\s*格\s*=/.test(text)) collect(CELL_RE, text, out);
  return out;
}

/** 稳定预览标记（没有就空串）。 */
export function stableShotFromText(text?: string | null): string {
  if (!text) return "";
  const m = STABLE_RE.exec(text);
  return m ? m[1].replace(/\\/g, "/") : "";
}

/**
 * 工具产出的图片（截图/回灌图）缩略图 + 点击放大。
 * 以前只显示一行「截图已保存：xxx.png」文本，用户根本看不到组长到底看见了什么。
 *
 * 取图优先级：
 *   ① 结果文本带「固定预览：run-shot/…」→ 走稳定目录 /api/run-shots（带 Bearer 头 → blob URL）。
 *      这条路与工作区是否被清空无关，也不受 cookie 名（ga_session / dsh-auth）影响；
 *   ② 老结果没有稳定标记 → 退回工作区 /runs/{id}/raw/（原地文件还在时兼容）。
 */
export default function ShotThumbs({
  runId,
  text,
  max = 8,
}: {
  runId: string;
  text?: string | null;
  max?: number;
}) {
  const stable = useMemo(() => stableShotFromText(text), [text]);
  const paths = useMemo(() => shotsFromText(text).slice(0, max), [text, max]);
  const [zoom, setZoom] = useState<string | null>(null);
  const [broken, setBroken] = useState<Record<string, boolean>>({});
  // 稳定目录图：先带 Bearer 取字节转 blob URL（<img> 发不出 Authorization）。
  const [blob, setBlob] = useState<{ thumb?: string; full?: string; err?: boolean }>({});
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    setBlob({});
    if (!stable) return;
    let alive = true;
    const urls: string[] = [];
    const thumbUrl = api.runShotUrl(stable, 320);
    const fullUrl = api.runShotUrl(stable);
    Promise.all([api.fetchBlobUrl(thumbUrl), api.fetchBlobUrl(fullUrl)])
      .then(([t, f]) => {
        if (!alive) {
          URL.revokeObjectURL(t);
          URL.revokeObjectURL(f);
          return;
        }
        urls.push(t, f);
        setBlob({ thumb: t, full: f });
      })
      .catch(() => {
        if (alive) setBlob({ err: true });
      });
    return () => {
      alive = false;
      urls.forEach((u) => URL.revokeObjectURL(u));
    };
  }, [stable, retry]);

  const name = (p: string) => p.split("/").pop() || p;

  // 有稳定标记：只渲染这一张稳定图（截图/合图），不再拿工作区路径去猜是否已失效。
  if (stable) {
    if (blob.err) {
      return (
        <div className="mt-1.5">
          <div
            className="w-[190px] rounded border border-dashed border-slate-300 bg-slate-50 px-2 py-1.5 text-[10px] leading-4"
            title={stable}
          >
            <div className="truncate text-slate-500">{name(stable)}</div>
            <div className="truncate text-slate-400">预览加载失败</div>
            <button
              type="button"
              className="mt-0.5 text-brand-600 hover:underline"
              onClick={() => setRetry((n) => n + 1)}
            >
              点击重试
            </button>
          </div>
        </div>
      );
    }
    return (
      <>
        <div className="mt-1.5 flex flex-wrap gap-2">
          {blob.thumb ? (
            <button
              type="button"
              className="group text-left"
              title={`${stable}（点击放大）`}
              onClick={() => blob.full && setZoom(blob.full)}
            >
              <img
                src={blob.thumb}
                alt={name(stable)}
                className="max-h-28 rounded border border-slate-200 object-contain bg-slate-50 shadow-sm group-hover:border-brand-400 group-hover:shadow"
              />
            </button>
          ) : (
            <div className="h-20 w-[190px] animate-pulse rounded border border-slate-200 bg-slate-100" />
          )}
        </div>
        {zoom &&
          createPortal(
            <div
              className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6"
              onClick={() => setZoom(null)}
              onKeyDown={(e) => e.key === "Escape" && setZoom(null)}
              role="presentation"
            >
              <img src={zoom} alt="放大查看" className="max-h-full max-w-full rounded shadow-2xl" />
              <button
                type="button"
                className="absolute right-4 top-4 h-9 w-9 rounded-full bg-white/85 text-lg text-slate-700 hover:bg-white"
                onClick={() => setZoom(null)}
                title="关闭（Esc）"
              >
                ×
              </button>
            </div>,
            document.body,
          )}
      </>
    );
  }

  if (!paths.length) return null;
  return (
    <>
      <div className="mt-1.5 flex flex-wrap gap-2">
        {paths.map((p) => {
          const url = api.runFileRawUrl(runId, p);
          const thumb = `${url}${url.includes("?") ? "&" : "?"}thumb=320`;
          const nm = name(p);
          if (broken[p]) {
            return (
              <div
                key={p}
                className="w-[190px] rounded border border-dashed border-slate-300 bg-slate-50 px-2 py-1.5 text-[10px] leading-4"
                title={`${p}\n原图已不在工作区：过程截图在 run 收尾后会清理；上传附件若被清理/换机器也会如此`}
              >
                <div className="truncate text-slate-500">{nm}</div>
                <div className="truncate text-slate-400">图片已清理 / 不在工作区</div>
              </div>
            );
          }
          return (
            <button
              key={p}
              type="button"
              className="group text-left"
              title={`${p}（点击放大）`}
              onClick={() => setZoom(url)}
            >
              <img
                src={thumb}
                alt={nm}
                loading="lazy"
                decoding="async"
                onError={() => setBroken((b) => ({ ...b, [p]: true }))}
                className="max-h-28 rounded border border-slate-200 object-contain bg-slate-50 shadow-sm group-hover:border-brand-400 group-hover:shadow"
              />
              <div className="mt-0.5 max-w-[190px] truncate text-[10px] text-slate-400 group-hover:text-brand-600">
                {nm}
              </div>
            </button>
          );
        })}
      </div>
      {zoom &&
        createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6"
          onClick={() => setZoom(null)}
          onKeyDown={(e) => e.key === "Escape" && setZoom(null)}
          role="presentation"
        >
          <img
            src={zoom}
            alt="放大查看"
            className="max-h-full max-w-full rounded shadow-2xl"
            onError={() => {
              // 大图也取不到：把对应的缩略图标坏，关掉放大层
              const hit = paths.find((p) => api.runFileRawUrl(runId, p) === zoom);
              if (hit) setBroken((b) => ({ ...b, [hit]: true }));
              setZoom(null);
            }}
          />
          <button
            type="button"
            className="absolute right-4 top-4 h-9 w-9 rounded-full bg-white/85 text-lg text-slate-700 hover:bg-white"
            onClick={() => setZoom(null)}
            title="关闭（Esc）"
          >
            ×
          </button>
        </div>,
        document.body,
      )}
    </>
  );
}
