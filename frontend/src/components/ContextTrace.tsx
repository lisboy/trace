import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import Markdown from "./Markdown";
import CopyButton from "./CopyButton";
import { ROLE_LABEL, type OaiMessage, type TranscriptDetail, type TranscriptRound } from "../types";

/* 上下文请求：逐轮往返的现场（发给模型什么 / 模型返回什么）。
   数据来自 llm_transcripts —— 那是压缩之后真正上线的 payload，
   所以这里看到的「发给模型的内容」和端点收到的完全一致，不是本地拼装。 */

function fmtTok(n: number): string {
  if (!n) return "0";
  if (n < 1000) return String(n);
  if (n < 100000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n / 1000)}k`;
}

/** created_at 是 UTC 裸串：直接截出来会显示成"凌晨 4 点"（实际是本地 12 点），先转本地时区 */
function localClock(iso: string, withSec = true): string {
  const s = String(iso || "").replace(" ", "T");
  const d = new Date(/[zZ+]|-\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
  if (isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return withSec
    ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    : `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** content 可能是字符串，也可能是 [{type:text},{type:image_ref}] 分部数组 */
function contentText(content: any): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content == null ? "" : JSON.stringify(content, null, 2);
  return content
    .map((p: any) => {
      if (p?.type === "text") return String(p.text || "");
      if (p?.type === "image_ref") return String(p.text || "[图片]");
      return JSON.stringify(p);
    })
    .join("\n");
}

/** 工具入参可能是字符串，也可能是对象：直接 String(obj) 会渲染成 [object Object]（用户截图里见过）。 */
function argText(v: any): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

const ROLE_STYLE: Record<string, string> = {
  system: "bg-indigo-50 text-indigo-700",
  user: "bg-emerald-50 text-emerald-700",
  assistant: "bg-sky-50 text-sky-700",
  tool: "bg-amber-50 text-amber-700",
};

/** 一条消息的可读全文（含工具调用入参）：复制出去要能直接看懂是谁说的、说了什么、调了什么。 */
function msgText(m: OaiMessage): string {
  let s = `[${m.role}${m.name ? `:${m.name}` : ""}]\n${contentText(m.content)}`;
  for (const tc of (m.tool_calls || []) as any[]) {
    s += `\n\n→ ${tc?.function?.name || tc?.name || "tool"} ${argText(
      tc?.function?.arguments ?? tc?.arguments ?? "")}`;
  }
  return s;
}

/** 估算 token：与后端 context_window.estimate_tokens 严格同口径——
    CJK（含全角标点/假名 U+3000–30FF、汉字 U+4E00–9FFF、全角 U+FF00–FFEF）按 1 字，
    其余按 4 字符向下取整（不是 ceil：以前用 ceil 会让每条消息都比后端略大，累加后对不上）。
    只用于「这条消息大概占多少」的相对排序，不当作账单。 */
function estTokens(text: string): number {
  if (!text) return 0;
  let cjk = 0;
  for (const ch of text) {
    const c = ch.charCodeAt(0);
    if ((c >= 0x3000 && c <= 0x30ff) || (c >= 0x4e00 && c <= 0x9fff) ||
        (c >= 0xff00 && c <= 0xffef)) cjk += 1;
  }
  const other = text.length - cjk;
  return cjk + Math.max(1, Math.floor(other / 4));
}

/** 一条消息的估算 token，与后端 transcript._msg_tokens 完全同口径：
    正文 + 工具调用入参（write_file 的 arguments 就是整份文件正文，是大头）+ 固定开销。
    以前这里只算正文，导致「每条消息的 tok 之和」远小于头部的 X tok 估算——
    用户截图里 7 条消息加起来才 8k，头部却写 16.2k，两边对不上。 */
const MSG_OVERHEAD = 4;   // 每条消息固定开销（与后端 _msg_tokens 的 4 一致）
const TOOLCALL_OVERHEAD = 8; // 每个 tool_call 固定开销（与后端一致）
// 图片按 900 tok 计（后端 context_window.IMAGE_TOKENS）：老记录图片已被替换成
// image_ref 占位文本，若按占位文字长度算就会漏掉每张 900——「各消息之和」又会对不上头部。
const IMAGE_TOKENS = 900;
function contentTokens(content: any): number {
  if (typeof content === "string") return estTokens(content);
  if (!Array.isArray(content)) return content == null ? 0 : estTokens(JSON.stringify(content));
  let t = 0;
  for (const p of content) {
    if (p?.type === "image_url" || p?.type === "image_ref") t += IMAGE_TOKENS;
    else if (p?.type === "text") t += estTokens(String(p.text || ""));
    else t += estTokens(JSON.stringify(p));
  }
  return t;
}
function msgTokens(m: OaiMessage): number {
  let t = MSG_OVERHEAD + contentTokens(m.content);
  for (const tc of (m.tool_calls || []) as any[]) {
    const fn = tc?.function ?? tc;
    const name = typeof fn?.name === "string" ? fn.name : "";
    const args = fn?.arguments;
    // 后端用 str(dict) 估长度；这里用紧凑 JSON（去掉缩进）与之字符数接近
    const argsStr = typeof args === "string" ? args : (args == null ? "" : JSON.stringify(args));
    t += TOOLCALL_OVERHEAD + estTokens(name) + estTokens(argsStr);
  }
  return t;
}

function MsgBlock({ m, i, tok }: { m: OaiMessage; i: number; tok?: number }) {
  const text = contentText(m.content);
  const calls = m.tool_calls || [];
  const [open, setOpen] = useState(false);
  const long = text.length > 220;
  const shown = open || !long ? text : text.slice(0, 220) + "…";
  return (
    <div className="border border-slate-200 rounded bg-white">
      <div className="flex items-center gap-1.5 px-2 py-1 text-[11px] border-b border-slate-100">
        <span className="text-slate-400 mono">{i + 1}</span>
        <span className={`px-1.5 rounded ${ROLE_STYLE[m.role] || "bg-slate-100 text-slate-600"}`}>
          {m.role}
        </span>
        {m.name && <span className="text-slate-400 mono">{m.name}</span>}
        <span className="text-slate-400">{fmtTok(tok ?? msgTokens(m))} tok</span>
        {calls.length > 0 && (
          <span className="text-purple-600">调用 {calls.length} 个工具</span>
        )}
        {long && (
          <button className="ml-auto text-brand-600 hover:underline" onClick={() => setOpen(!open)}>
            {open ? "收起" : `展开 ${text.length.toLocaleString()} 字`}
          </button>
        )}
        <CopyButton
          className={long ? "" : "ml-auto"}
          title="复制这条消息的原文（含工具调用入参）"
          get={() => msgText(m)}
        />
      </div>
      <pre className="px-2 py-1.5 text-[11px] leading-relaxed text-slate-600 whitespace-pre-wrap break-all max-h-[420px] overflow-auto">
        {shown || <span className="text-slate-300">（空）</span>}
      </pre>
      {calls.map((tc: any, k: number) => (
        <div key={k} className="px-2 py-1 border-t border-slate-100 text-[11px]">
          <span className="text-purple-700">{tc?.function?.name || tc?.name || "tool"}</span>
          <pre className="mt-0.5 text-slate-500 whitespace-pre-wrap break-all max-h-32 overflow-auto">
            {argText(tc?.function?.arguments ?? tc?.arguments ?? "")}
          </pre>
        </div>
      ))}
    </div>
  );
}

function RoundDetail({ id }: { id: string }) {
  const [d, setD] = useState<TranscriptDetail | null>(null);
  const [err, setErr] = useState("");
  const [showTools, setShowTools] = useState(false);

  useEffect(() => {
    let alive = true;
    setD(null);
    setErr("");
    api
      .transcriptDetail(id)
      .then((r) => alive && setD(r))
      .catch((e: any) => alive && setErr(e?.message || String(e)));
    return () => {
      alive = false;
    };
  }, [id]);

  if (err) return <div className="text-xs text-red-600 p-2">载入失败：{err}</div>;
  if (!d) return <div className="text-xs text-slate-400 p-2">加载这一轮的现场…</div>;

  const cats = d.stats?.categories || {};
  const req = d.request || { messages: [] as OaiMessage[] };
  const msgs = req.messages || [];
  const resp = d.response || ({} as any);
  const toolNames: string[] = (req.tools || [])
    .map((t: any) => t?.function?.name || t?.name || "")
    .filter(Boolean);
  // 每条消息的 tok 用后端落库时存下的 msg_tokens（_msg_tokens 同口径），保证
  // 「各消息之和 + 工具定义 = 头部 est_prompt_tokens」严格对得上，不再靠前端另算一份。
  // 老记录没有 msg_tokens 时回落前端估算（近似值）。
  const sentTok: number[] | null = Array.isArray(req.msg_tokens) ? req.msg_tokens : null;
  const tokOf = (m: OaiMessage, i: number): number =>
    sentTok && i < sentTok.length ? Number(sentTok[i]) : msgTokens(m);
  // 工具定义这一项在分类里（后端每次请求按实际 tools 估的），直接取它最准。
  const toolDefTokens = Number((cats["工具定义"] || {}).tokens || 0);
  const msgSum = msgs.reduce((a, m, i) => a + tokOf(m, i), 0) + toolDefTokens;

  return (
    <div className="space-y-2">
      <div className="text-[11px] text-slate-500 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-slate-700">{ROLE_LABEL[d.agent_key || ""] || d.agent_key}</span>
        <span className="mono">第 {d.round_no} 轮</span>
        {d.purpose === "summarize" && (
          <span className="px-1 rounded bg-slate-100">历史摘要调用</span>
        )}
        <span className="mono">{d.model}</span>
        <span
          title={
            `本页 ${msgs.length} 条消息 + 工具定义合计 ${msgSum.toLocaleString()} tok（下方每条消息的 tok 之和）` +
            (d.stats?.est_prompt_tokens
              ? `；后端落库时另估 ${d.stats.est_prompt_tokens.toLocaleString()} tok`
              : "")
          }
        >
          {fmtTok(msgSum)} tok 估算
        </span>
        <span>{(d.req_bytes / 1024).toFixed(0)}KB 请求</span>
        <span title="这一轮请求落库的时间（后端存 UTC，这里已按本地时区显示）">
          发送于 {localClock(d.created_at)}
        </span>
        {d.compressed && (
          <span className="px-1 rounded bg-orange-100 text-orange-700">
            已压缩 · 丢弃 {d.dropped_msgs} 条
          </span>
        )}
        {d.truncated && (
          <span className="px-1 rounded bg-red-50 text-red-600" title="超出存储上限，只留了最近几条">
            正文有截断
          </span>
        )}
      </div>

      {/* 这一轮 prompt 被谁吃掉。分类合计 = 头部「X tok 估算」：
          系统提示词/用户消息/助手消息/工具结果合计 = est_prompt_tokens。工具定义每轮都发
          但不在 messages 里（只有首轮存了原文，其余记 tools_omitted），所以当本轮没存
          工具定义原文时，分类里就不含它，两者仍对得上。 */}
      <div className="flex flex-wrap gap-1">
        {Object.entries(cats).map(([k, v]) => (
          <span key={k} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-50 text-slate-600">
            {k} {fmtTok(v.tokens)}
          </span>
        ))}
      </div>

      {/* 发给模型 */}
      <div>
        <div className="text-xs font-medium text-slate-700 mb-1 flex items-center gap-2">
          发送给模型
          <span className="text-slate-400 font-normal">{msgs.length} 条消息</span>
          {(req.tools_omitted ?? 0) > 0 && (
            <span className="text-slate-400 font-normal">
              · 工具 {req.tools_omitted} 个（与首轮相同，未重复存储）
            </span>
          )}
          <CopyButton
            className="ml-auto"
            label="复制本轮请求"
            title="复制这一轮真正发给模型的全部消息原文（压缩后的 payload）"
            get={() => msgs.map(msgText).join("\n\n——————\n\n")}
          />
        </div>
        {toolNames.length > 0 && (
          <div className="mb-1">
            <button
              className="text-[11px] text-slate-500 hover:text-brand-700"
              onClick={() => setShowTools(!showTools)}
            >
              工具定义 {toolNames.length} 个 {showTools ? "▲" : "▼"}
            </button>
            {showTools && (
              <div className="mt-1 text-[11px] mono text-slate-500 bg-slate-50 rounded p-2 grid grid-cols-2 gap-x-2">
                {toolNames.map((n, i) => (
                  <span key={n + i}>{n}</span>
                ))}
              </div>
            )}
          </div>
        )}
        {req.truncated_note && (
          <div className="text-[11px] text-red-600 mb-1">{req.truncated_note}</div>
        )}
        <div className="space-y-1">
          {msgs.map((m, i) => (
            <MsgBlock key={i} m={m} i={i} tok={tokOf(m, i)} />
          ))}
        </div>
      </div>

      {/* 模型返回 */}
      <div>
        <div className="text-xs font-medium text-slate-700 mb-1 flex items-center gap-2">
          模型返回
          <span className="text-slate-400 font-normal">
            {fmtTok(d.stats?.completion_tokens || 0)} tok 输出 ·{" "}
            {((d.resp_bytes || 0) / 1024).toFixed(1)}KB
          </span>
          <CopyButton
            className="ml-auto"
            label="复制返回"
            title="复制这一轮模型返回的正文 + 思考 + 工具调用"
            get={() => [
              resp.reasoning ? `【思考】\n${resp.reasoning}` : "",
              resp.text ? `【正文】\n${resp.text}` : "",
              (resp.tool_calls || []).length
                ? `【工具调用】\n${(resp.tool_calls || []).map((tc: any) =>
                    `→ ${tc?.name || "tool"} ${argText(tc?.arguments ?? "")}`).join("\n")}`
                : "",
            ].filter(Boolean).join("\n\n")}
          />
        </div>
        {resp.reasoning && (
          <details className="mb-1">
            <summary className="text-[11px] text-purple-600 cursor-pointer flex items-center gap-2">
              思考过程
              <CopyButton title="复制这段思考" get={() => String(resp.reasoning)} />
            </summary>
            <div className="mt-1 pl-2 border-l-2 border-purple-200 text-slate-500 text-[11px] whitespace-pre-wrap">
              {resp.reasoning}
            </div>
          </details>
        )}
        {resp.text ? (
          <div className="text-xs bg-white rounded border border-slate-200 p-2">
            <Markdown text={resp.text} />
          </div>
        ) : (
          <div className="text-[11px] text-slate-400">（本轮没有正文输出）</div>
        )}
        {(resp.tool_calls || []).length > 0 && (
          <div className="mt-1 space-y-1">
            {resp.tool_calls.map((tc: any, i: number) => (
              <div key={i} className="border border-purple-100 bg-purple-50/40 rounded px-2 py-1">
                <span className="text-[11px] text-purple-700">{tc?.name || "tool"}</span>
                <pre className="text-[11px] text-slate-600 whitespace-pre-wrap break-all max-h-40 overflow-auto">
                  {argText(tc?.arguments ?? "")}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ContextTrace({ runId, running }: { runId: string; running: boolean }) {
  const [rounds, setRounds] = useState<TranscriptRound[]>([]);
  const [sel, setSel] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  // 用户手动选过就不再自动跟随最新轮，否则正看着第 3 轮会被新一轮抢走焦点。
  // 只在换 run 时清除：running 翻转（进终态）会重跑轮询，不该抹掉用户的位置。
  const pinned = useRef(false);
  const selRef = useRef("");
  // 只有「自动跟随最新轮」才滚到底，用户自己点选时列表保持原位
  const willFollow = useRef(false);

  useEffect(() => {
    pinned.current = false;
    selRef.current = "";
    willFollow.current = false;
    setSel("");
    setRounds([]);
  }, [runId]);

  const select = (id: string, follow: boolean) => {
    selRef.current = id;
    willFollow.current = follow;
    setSel(id);
  };

  useEffect(() => {
    let alive = true;
    async function pull() {
      try {
        const r = await api.runTranscripts(runId);
        if (!alive) return;
        const items: TranscriptRound[] = r?.rounds || [];
        setRounds(items);
        if (!items.length) return;
        const lastId = items[items.length - 1].id;
        if (pinned.current && selRef.current) return;
        if (selRef.current !== lastId) select(lastId, true);
      } catch {
        /* 轮询失败下一轮再说 */
      }
    }
    setLoading(true);
    pull().finally(() => alive && setLoading(false));
    const timer = setInterval(pull, running ? 3000 : 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [runId, running]);

  useEffect(() => {
    if (!willFollow.current) return;
    willFollow.current = false;
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [sel]);

  const byAgent = useMemo(() => {
    const m: Record<string, number> = {};
    rounds.forEach((r) => {
      m[r.agent_key || "?"] = (m[r.agent_key || "?"] || 0) + 1;
    });
    return m;
  }, [rounds]);

  if (!rounds.length) {
    return (
      <div className="p-3 text-sm text-slate-400">
        {loading ? "读取上下文请求记录…" : "本次运行还没有 LLM 往返记录。"}
        {!loading && (
          <div className="mt-1 text-[11px] text-slate-400">
            只统计后端启动且建表之后的运行；更早的 run 没有留现场。
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex">
      <div
        ref={listRef}
        className="w-[210px] shrink-0 overflow-auto border-r border-slate-200 p-1.5 space-y-1"
      >
        {Object.entries(byAgent).map(([k, n]) => (
          <div key={k} className="text-[10px] text-slate-400 px-1">
            {ROLE_LABEL[k] || k} · {n} 轮
          </div>
        ))}
        {rounds.map((r, i) => (
          <button
            key={r.id}
            onClick={() => {
              if (sel === r.id && pinned.current) {
                // 再点一次已选中的那一轮 = 取消固定，立刻回到最新轮并恢复跟随
                pinned.current = false;
                select(rounds[rounds.length - 1].id, true);
                return;
              }
              pinned.current = true;
              select(r.id, false);
            }}
            title="点击固定在这一轮，不再自动跳到最新轮；再点一次恢复跟随"
            className={`w-full text-left px-1.5 py-1 rounded text-[11px] leading-tight ${
              sel === r.id ? "bg-brand-50 text-brand-800 ring-1 ring-brand-200" : "hover:bg-slate-50"
            }`}
          >
            <div className="flex items-center gap-1">
              <span className="mono text-slate-500">#{i + 1}</span>
              <span className="text-slate-400">{ROLE_LABEL[r.agent_key || ""] || r.agent_key}</span>
              <span className="ml-auto text-slate-400">{localClock(r.created_at, false)}</span>
            </div>
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-slate-600">{fmtTok(r.prompt_tokens)} tok</span>
              <span className="text-slate-400">{(r.req_bytes / 1024).toFixed(0)}KB</span>
              {r.latency_ms > 0 && <span className="text-slate-400">{(r.latency_ms / 1000).toFixed(1)}s</span>}
              {r.compressed && (
                <span className="px-1 rounded bg-orange-100 text-orange-700">压缩-{r.dropped_msgs}</span>
              )}
            </div>
            {Object.keys(r.stats?.tool_calls || {}).length > 0 && (
              <div className="text-[10px] text-purple-600 truncate">
                {Object.entries(r.stats.tool_calls || {})
                  .map(([n, c]) => `${n}${c > 1 ? "×" + c : ""}`)
                  .join("、")}
              </div>
            )}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-auto p-2">
        {sel ? <RoundDetail id={sel} /> : <div className="text-xs text-slate-400">选一轮看现场。</div>}
      </div>
    </div>
  );
}
