/** 后端截断标记解析（`…[中略 1234 字]…`，旧数据是 `…[中略]…`）。
 *
 * 为什么单独成文件：后端的 `_tail_keep_head` / `_tail_head` 在超长思考/续跑摘要里
 * 只保留「开头一小段 + 尾部主体」，中间用这个标记顶替。前端若把它当正文铺出来，
 * 用户看到的就是「…[中略]…」这种既不是模型原话、也不是报错的怪串
 * （现场：用户截图圈住红框问「这个是啥意思」）。凡是展示这类文本的地方都拆开渲染。
 */
export const ELIDED_RE = /…\[中略\s*(\d+)?\s*字?\]…/;

export type ElidedParts = {
  head: string;
  tail: string;
  /** 中间省略的字数；老数据没有数字时为 null */
  omitted: number | null;
  elided: boolean;
};

export function splitElided(text: string): ElidedParts {
  const s = text || "";
  const m = ELIDED_RE.exec(s);
  if (!m) return { head: s, tail: "", omitted: null, elided: false };
  return {
    head: s.slice(0, m.index).replace(/\s+$/, ""),
    tail: s.slice(m.index + m[0].length).replace(/^\s+/, ""),
    omitted: m[1] ? Number(m[1]) : null,
    elided: true,
  };
}

/** 一行摘要用的短标语（放在折叠标题里） */
export function elidedBadge(omitted: number | null): string {
  return omitted ? `⋯[中间省略 ${omitted} 字]⋯` : "⋯[中间省略内容]⋯";
}

/** 纯文本场景（<pre>、日志式展示）：把标记就地换成一句自解释的话。
 *  没有这一层的话，用户看到的就是「…[中略 3920 字]…」这种疑似报错/疑似模型原话的串。 */
export function decorateElided(text: string): string {
  return (text || "").replace(ELIDED_RE, (_m, n) =>
    n ? `\n⋯[此处中间省略 ${n} 字：原始内容太长，未保存全文]⋯\n`
      : "\n⋯[此处中间省略内容：原始内容太长，未保存全文]⋯\n");
}
