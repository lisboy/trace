import { useEffect, useState } from "react";
import { api } from "../api";

type Artifact = {
  id: string;
  asset_kind: string;
  slug: string;
  rel_path: string;
  is_placeholder: boolean;
  provider: string;
  size?: number | null;
};

/**
 * 对话内素材预览：展示该 run 实际生成的图片缩略图与音频播放条。
 * 图片/音频点击可在新窗口打开原图；占位素材带灰色标记。
 * 生成过程中每落一个 artifact（ws 广播 artifact 事件）就重拉一次——
 * 以前只在挂载时拉一次，批量出图 20 张要等整批结束才见得到图。
 */
export default function RunArtifacts({ runId }: { runId: string }) {
  const [arts, setArts] = useState<Artifact[] | null>(null);
  const [broken, setBroken] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let alive = true;
    const doFetch = () => {
      api
        .artifacts({ run_id: runId })
        .then((rows: Artifact[]) => {
          if (alive) setArts(Array.isArray(rows) ? rows : []);
        })
        .catch(() => {});
    };
    doFetch();
    function onEv(e: Event) {
      const ev = (e as CustomEvent).detail as { type?: string };
      if (ev?.type === "artifact") doFetch();
    }
    window.addEventListener(`ga-run-ev-${runId}`, onEv);
    return () => {
      alive = false;
      window.removeEventListener(`ga-run-ev-${runId}`, onEv);
    };
  }, [runId]);

  if (!arts || arts.length === 0) return null;
  const images = arts.filter((a) => a.asset_kind === "image" || a.asset_kind === "icon");
  const videos = arts.filter((a) => a.asset_kind === "video");
  // 占位音频不出声（静音 wav 播放器看着和真配音一样）：只展示真实的音频
  const audios = arts.filter(
    (a) => a.asset_kind !== "image" && a.asset_kind !== "icon" && a.asset_kind !== "video" && !a.is_placeholder,
  );
  if (images.length === 0 && audios.length === 0 && videos.length === 0) return null;

  return (
    <div className="mt-1 space-y-1.5 max-w-2xl">
      {images.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {images.map((a) => (
            <div key={a.id} className="relative group">
              {broken[a.id] ? (
                <div className="w-[240px] h-32 rounded-lg border border-dashed border-slate-300 bg-slate-50 flex items-center justify-center text-xs text-slate-400 px-3 text-center">
                  图片文件已被移动或删除
                  <br />
                  {a.rel_path}
                </div>
              ) : (
                <img
                  src={api.artifactThumb(a.id, 320, (a as any).size)}
                  alt={a.slug}
                  loading="lazy"
                  decoding="async"
                  className="max-h-44 max-w-[240px] rounded-lg border border-slate-200 object-contain cursor-zoom-in bg-slate-50"
                  onClick={() => window.open(api.artifactFile(a.id, a.size ?? undefined), "_blank")}
                  onError={() => setBroken((b) => ({ ...b, [a.id]: true }))}
                />
              )}
              {a.is_placeholder && (
                <span className="absolute left-1 top-1 text-[10px] px-1 py-0.5 rounded bg-slate-700/70 text-white">
                  占位
                </span>
              )}
              <div className="text-[10px] text-slate-400 truncate max-w-[240px]">
                {a.rel_path}
              </div>
            </div>
          ))}
        </div>
      )}
      {videos.map((a) => (
        <div key={a.id} className="relative group inline-block">
          <video
            controls
            preload="metadata"
            src={api.artifactFile(a.id, a.size ?? undefined)}
            className="max-h-56 max-w-[320px] rounded-lg border border-slate-200 bg-black"
          />
          {a.is_placeholder && (
            <span className="absolute left-1 top-1 text-[10px] px-1 py-0.5 rounded bg-slate-700/70 text-white">
              占位
            </span>
          )}
          <div className="text-[10px] text-slate-400 truncate max-w-[320px]">{a.rel_path}</div>
        </div>
      ))}
      {audios.map((a) => (
        <div key={a.id} className="flex items-center gap-2 text-xs text-slate-500">
          <audio controls src={api.artifactFile(a.id, a.size ?? undefined)} className="h-8 max-w-[280px]" />
          <span className="truncate">{a.rel_path}</span>
        </div>
      ))}
    </div>
  );
}
