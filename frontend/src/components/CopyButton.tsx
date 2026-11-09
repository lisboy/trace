import { useState } from "react";
import { copyToClipboard, type CopyPayload } from "../clipboard";

/** 小复制按钮：点击后原地变「已复制 / 复制失败」再复原，不引全局 toast。 */
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
  const [state, setState] = useState<"" | "ok" | "fail" | "busy">("");
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
        let ok = false;
        try {
          ok = await copyToClipboard(await get());
        } finally {
          setState(ok ? "ok" : "fail");
          window.setTimeout(() => setState(""), 1400);
        }
      }}
    >
      {state === "ok" ? "已复制" : state === "fail" ? "复制失败" : state === "busy" ? "复制中…" : label}
    </button>
  );
}
