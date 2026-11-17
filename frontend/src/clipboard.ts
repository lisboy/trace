export type CopyPayload = { text?: string; html?: string; images?: string[] };
/** full=要的内容全进了剪贴板；text=只要有图，图没写进去（只写成了文字）；false=整体失败 */
export type CopyOutcome = "full" | "text" | false;

/** dataURL → PNG Blob（画布转一道，剪贴板只认 PNG） */
async function pngBlob(dataUrl: string): Promise<Blob | null> {
  try {
    const img = await loadImage(dataUrl);
    const c = document.createElement("canvas");
    c.width = img.naturalWidth || 1;
    c.height = img.naturalHeight || 1;
    c.getContext("2d")?.drawImage(img, 0, 0);
    return await new Promise<Blob | null>((res) => c.toBlob(res, "image/png"));
  } catch {
    return null;
  }
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise<HTMLImageElement>((res, rej) => {
    const el = new Image();
    el.onload = () => res(el);
    el.onerror = () => rej(new Error("图片解码失败"));
    el.src = src;
  });
}

const STITCH_MAX_W = 1600;
const STITCH_MAX_H = 16000;

/** 多张图竖向拼成一张 PNG：Windows 剪贴板同一时刻只能放一张图，
 *  不拼的话粘到聊天框只会有第一张（用户 2026-11-17 反馈「复制没带图」）。
 *  拼不下（超长/解码失败）返回 null，由调用方退回单张。 */
async function stitchedPng(images: string[]): Promise<Blob | null> {
  if (images.length < 2) return null;
  try {
    const els = await Promise.all(images.map(loadImage));
    const scale = Math.min(1, STITCH_MAX_W / Math.max(...els.map((e) => e.naturalWidth || 1)));
    const gap = Math.max(2, Math.round(8 * scale));
    const widths = els.map((e) => Math.max(1, Math.round((e.naturalWidth || 1) * scale)));
    const heights = els.map((e) => Math.max(1, Math.round((e.naturalHeight || 1) * scale)));
    const w = Math.max(...widths);
    const h = heights.reduce((a, b) => a + b, 0) + gap * (els.length - 1);
    if (h > STITCH_MAX_H) return null;
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const ctx = c.getContext("2d");
    if (!ctx) return null;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, w, h);
    let y = 0;
    els.forEach((img, i) => {
      ctx.drawImage(img, Math.round((w - widths[i]) / 2), y, widths[i], heights[i]);
      y += heights[i] + gap;
    });
    return await new Promise<Blob | null>((res) => c.toBlob(res, "image/png"));
  } catch {
    return null;
  }
}

/** 图片通道只放一张 PNG：单图直接转，多图先拼成一张。 */
async function pngForClipboard(images: string[]): Promise<Blob | null> {
  if (images.length === 1) return pngBlob(images[0]);
  return (await stitchedPng(images)) || pngBlob(images[0]);
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
 *
 * 只传 images、不传文字时写「纯图片」剪贴板：很多应用（微信/Qoder 等聊天框）
 * 只要剪贴板里有文字就只收文字、图直接丢掉，想带走图必须让剪贴板里只有图。
 */
export async function copyToClipboard(payload: string | CopyPayload): Promise<CopyOutcome> {
  const p = typeof payload === "string" ? { text: payload } : payload;
  const text = p.text || "";
  const images = (p.images || []).filter(Boolean);
  const imageOnly = images.length > 0 && !text && !p.html;

  if (imageOnly) {
    try {
      if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
        const b = await pngForClipboard(images);
        if (b) {
          await navigator.clipboard.write([
            new ClipboardItem({ "image/png": Promise.resolve(b) }),
          ]);
          return "full";
        }
      }
    } catch {
      /* 落到失败结果：图没进去就是没成功，不能报「已复制」 */
    }
    return false;
  }

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
        // 再给一份 image/png：部分目标只认位图，不解析 HTML 里的 dataURL
        if (images.length) {
          const b = await pngForClipboard(images);
          if (b) data["image/png"] = Promise.resolve(b);
        }
        await navigator.clipboard.write([new ClipboardItem(data as any)]);
        return "full";
      }
    } catch {
      /* 落到纯文本 */
    }
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return images.length ? "text" : "full";
    }
  } catch {
    /* 落到 execCommand */
  }
  return legacyCopy(text) ? (images.length ? "text" : "full") : false;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
}
