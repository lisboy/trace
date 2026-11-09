export type CopyPayload = { text: string; html?: string; images?: string[] };

/** dataURL → PNG Blob（画布转一道，剪贴板只认 PNG） */
async function pngBlob(dataUrl: string): Promise<Blob | null> {
  try {
    const img = await new Promise<HTMLImageElement>((res, rej) => {
      const el = new Image();
      el.onload = () => res(el);
      el.onerror = () => rej(new Error("图片解码失败"));
      el.src = dataUrl;
    });
    const c = document.createElement("canvas");
    c.width = img.naturalWidth || 1;
    c.height = img.naturalHeight || 1;
    c.getContext("2d")?.drawImage(img, 0, 0);
    return await new Promise<Blob | null>((res) => c.toBlob(res, "image/png"));
  } catch {
    return null;
  }
}

/** 老式兜底：非安全上下文或剪贴板 API 被拒时，用隐藏 textarea + execCommand。 */
function legacyCopy(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  return ok;
}

/**
 * 复制到剪贴板。带 html 时走 ClipboardItem（图片以 data URL 内联，
 * 粘到聊天/文档里能看到图）；浏览器不支持或写失败就退回纯文本，
 * 再退回 execCommand —— 复制文字这条主路径不能因为图片失败而整体失败。
 */
export async function copyToClipboard(payload: string | CopyPayload): Promise<boolean> {
  const p = typeof payload === "string" ? { text: payload } : payload;
  const text = p.text || "";
  const images = (p.images || []).filter(Boolean);
  const html = p.html || (images.length
    ? images.map((u) => `<img src="${u}">`).join("<br>")
      + (text ? `<br>${escapeHtml(text).replace(/\n/g, "<br>")}` : "")
    : "");
  if (html || images.length) {
    try {
      if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
        const data: Record<string, Promise<Blob>> = {
          "text/plain": Promise.resolve(new Blob([text], { type: "text/plain" })),
          "text/html": Promise.resolve(new Blob([html], { type: "text/html" })),
        };
        // 再给一份 image/png：微信/飞书/文档粘 HTML 时不一定抓 dataURL，PNG 才稳
        for (const u of images) {
          const b = await pngBlob(u);
          if (b) {
            data["image/png"] = Promise.resolve(b);
            break;
          }
        }
        await navigator.clipboard.write([new ClipboardItem(data as any)]);
        return true;
      }
    } catch {
      /* 落到纯文本 */
    }
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 落到 execCommand */
  }
  return legacyCopy(text);
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
}
