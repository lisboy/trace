import { useState } from "react";
import { copyToClipboard, type CopyOutcome, type CopyPayload } from "../clipboard";

/** 小复制按钮：点击后原地变「已复制 / 已复制（仅文字）/ 复制失败」再复原，不引全局 toast。
 *  「仅文字」= 本来要连图一起复制，但图没能写进剪贴板（浏览器拒绝/不支持）——
 *  不能把这种情况也显示成「已复制」，否则用户粘出来发现没图，以为点了没用。 */
export default function CopyButton({
  get,
  label = "复制",
  title = "复制到剪贴板",
  className = "",
}: {
  get: () => CopyPayload | string | Promise<CopyPayload | string>;
  label?: string;
  title?: string;
  className?: string;
}) {
  const [state, setState] = useState<"" | "ok" | "partial" | "fail" | "busy">("");
  return (
    <button
      type="button"
      className={`shrink-0 ${className || "text-[11px] text-slate-500 hover:text-brand-700"}`}
      title={title}
      disabled={state === "busy"}
      onClick={async (e) => {
        // preventDefault：按钮可能嵌在 <summary> 里，不拦会把折叠面板一起点开/收起
        e.preventDefault();
        e.stopPropagation();
        // 会话带十几张附图时，拉图 + 转 dataURL 要好几秒；不亮「复制中」看着就是点了没反应
        setState("busy");
        let res: CopyOutcome = false;
        try {
          res = await copyToClipboard(await get());
        } finally {
          setState(res === "full" ? "ok" : res === "text" ? "partial" : "fail");
          window.setTimeout(() => setState(""), 1400);
        }
      }}
    >
      {state === "ok" ? "已复制"
        : state === "partial" ? "已复制（仅文字）"
        : state === "fail" ? "复制失败"
        : state === "busy" ? "复制中…" : label}
    </button>
  );
}
