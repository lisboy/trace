import { createPortal } from "react-dom";
import Markdown from "./Markdown";

/** 文件预览：图片 / 音频直连（走 blob 才能带鉴权头），文本读内容渲染 Markdown。 */
export type Preview =
  | { kind: "image"; title: string; url: string }
  | { kind: "audio"; title: string; url: string }
  | { kind: "text"; title: string; content: string }
  | null;

export default function PreviewModal({
  preview,
  onClose,
  onOpenDetail,
}: {
  preview: NonNullable<Preview>;
  onClose: () => void;
  /** 可选：把「跳到运行详情」这个逃生口留在弹层里，而不是点素材就跳页 */
  onOpenDetail?: () => void;
}) {
  // portal 到 body：ga-bg-on 给 .bg-white 加了 backdrop-filter，会把 fixed 定位的弹层
  // 困在顶栏那个 47px 高的包含块里（弹窗只露出一条、其余跑出屏幕）。
  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-8" onClick={onClose}>
      <div
        className="bg-white rounded-lg border border-slate-200 max-w-3xl w-full max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-slate-100">
          <span className="text-sm font-medium truncate flex-1 mono">{preview.title}</span>
          {onOpenDetail && (
            <button
              className="text-xs text-slate-500 hover:text-brand-700 shrink-0"
              onClick={onOpenDetail}
            >
              在运行详情中打开
            </button>
          )}
          <button className="text-slate-400 hover:text-slate-600 text-lg leading-none shrink-0" onClick={onClose}>×</button>
        </div>
        <div className="p-4 overflow-auto">
          {preview.kind === "image" && (
            <img src={preview.url} alt={preview.title} className="max-w-full max-h-[65vh] mx-auto" />
          )}
          {preview.kind === "audio" && (
            <audio controls autoPlay src={preview.url} className="w-full mt-4" />
          )}
          {preview.kind === "text" && <Markdown text={preview.content} />}
        </div>
      </div>
    </div>,
    document.body,
  );
}
