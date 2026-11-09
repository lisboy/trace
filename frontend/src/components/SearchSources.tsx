/** 联网搜索/抓取结果的「来源」渲染。
 *
 *  web_search / web_fetch 的返回以一段结构化清单结尾（后端见 web.py 的 [Sources] 段）：
 *      [Sources]
 *      1. 标题 | https://...
 *      2. 标题 | https://...
 *  这里把它从正文里剥出来，正文照旧显示，来源渲染成可点击的域名芯片——
 *  长 URL 不再挤在 pre 里看不清，也方便用户核对模型到底引用了哪些页面。 */

export type SourceItem = { title: string; url: string };

export function parseSources(result?: string): { body: string; sources: SourceItem[] } {
  const text = result || "";
  const idx = text.indexOf("[Sources]");
  if (idx < 0) return { body: text, sources: [] };
  const body = text.slice(0, idx).replace(/\s+$/, "");
  const sources: SourceItem[] = [];
  const seen = new Set<string>();
  for (const line of text.slice(idx + "[Sources]".length).split("\n")) {
    const m = line.match(/^\s*\d+\.\s*(.*?)\s*\|\s*(https?:\/\/\S+)\s*$/);
    if (!m) continue;
    const url = m[2];
    if (seen.has(url)) continue;
    seen.add(url);
    sources.push({ title: (m[1] || url).trim(), url });
  }
  return { body, sources };
}

export function hostOf(u: string): string {
  try {
    return new URL(u).hostname.replace(/^www\./, "");
  } catch {
    return u;
  }
}

/** 来源芯片行：序号 + 域名，点开新标签页。标题挂在 tooltip 上。 */
export function SourceChips({ sources }: { sources: SourceItem[] }) {
  if (!sources.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
      <span className="shrink-0 text-[11px] text-slate-400">来源 {sources.length}</span>
      {sources.map((s, i) => (
        <a
          key={s.url}
          href={s.url}
          target="_blank"
          rel="noreferrer noopener"
          title={`${s.title}\n${s.url}`}
          className="inline-flex max-w-[220px] items-center gap-1 rounded border border-slate-200 bg-white/70 px-1.5 py-0.5 text-[11px] text-slate-600 hover:border-brand-300 hover:text-brand-700"
        >
          <span className="shrink-0 text-slate-400">{i + 1}</span>
          <span className="truncate">{hostOf(s.url)}</span>
        </a>
      ))}
    </div>
  );
}
