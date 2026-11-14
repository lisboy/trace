/** 会话级用量条：总消耗 token / 缓存命中率 / 生成速度 / 上下文窗口占用。
 *  速度那一格已合并（2026-11-14）：顶栏只留瞬时速度，首字/平均速度/调用/动作/累计执行点开才看。
 *
 * 数据源 GET /api/sessions/{id}/usage（后端按会话聚合 llm_calls，落库持久化）——
 * 所以「下次进来这个会话」还能看到累计值，不只是本次 run。
 * 「速度」取**最近一轮往返**的瞬时值（不是会话累计平均）：累计平均会被历史拖住，
 * 换了更快的模型后顶栏几乎不动，看起来像「切换没生效」（用户 2026-11-12 实测）。
 * 上下文占用取该角色最近一次往返的 prompt_tokens（就是压缩后模型真正看到的量），
 * 与档位窗口的 75% 预算线比较 → 用户能判断「是否已经开始丢历史了」。
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type { ContextOccupancy, SessionUsage as Usage } from "../types";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

/** 秒 → 人话时长（累计执行用：几十小时和几十秒不该占一样宽的位） */
function fmtDur(s: number): string {
  const n = Math.max(0, Math.round(s || 0));
  if (n < 60) return `${n} 秒`;
  if (n < 3600) return `${Math.floor(n / 60)} 分`;
  return `${Math.floor(n / 3600)} 时 ${Math.round((n % 3600) / 60)} 分`;
}

/** 毫秒 → 首字耗时人话（1.2s / 840ms）：这一格要在顶栏窄条里塞得下 */
function fmtTtft(ms: number): string {
  const n = Math.max(0, Math.round(ms || 0));
  if (n < 1000) return `${n}ms`;
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}s`;
}

/** 占用比例 → 文案与颜色：越过预算线后模型的历史真的在被裁，必须显眼。 */
function ctxTone(pct: number, willCompress: boolean) {
  if (willCompress) return { cls: "text-orange-600", bar: "bg-orange-500", tag: "压缩中" };
  if (pct >= 60) return { cls: "text-amber-600", bar: "bg-amber-400", tag: "接近阈值" };
  return { cls: "text-slate-600", bar: "bg-emerald-400", tag: "余量充足" };
}

/** 顶栏小格 → 弹层的通用锚点逻辑。
 * 弹层一律 fixed + portal 挂到 body：绝对定位会被消息列表盖住（祖先层叠上下文）/裁剪，
 * portal 到根节点后谁也压不住它（2026-10-29 用户实测弹窗被轨迹区遮挡）。
 */
function useAnchorPop() {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      if (next && btnRef.current) {
        const r = btnRef.current.getBoundingClientRect();
        setPos({ top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) });
      }
      return next;
    });
  };
  return { open, setOpen, pos, btnRef, toggle };
}

/** 弹层壳：半透明遮罩（点空白关闭，同时把背后信息流压暗）+ 白卡。 */
function PopOverlay({ open, onClose, pos, width = 440, children }: {
  open: boolean;
  onClose: () => void;
  pos: { top: number; right: number } | null;
  width?: number;
  children: ReactNode;
}) {
  if (!open) return null;
  return createPortal(
    <>
      <span className="fixed inset-0 z-40 bg-slate-900/30 backdrop-blur-[1px]" onClick={onClose} />
      <div
        className="fixed z-50 rounded-xl border border-slate-300 bg-white p-3 text-left shadow-2xl ring-1 ring-black/5"
        style={{ top: pos?.top ?? 52, right: pos?.right ?? 8, width }}
      >
        {children}
      </div>
    </>,
    document.body,
  );
}

function ContextChip({ c, compressAt }: { c: ContextOccupancy; compressAt: number }) {
  const { open, setOpen, pos, btnRef, toggle } = useAnchorPop();
  const tone = ctxTone(c.used_pct, c.will_compress);
  const pctShow = Math.min(999, c.used_pct);
  return (
    <span className="relative flex items-center">
      <button
        ref={btnRef}
        onClick={toggle}
        title={`最近一次发给模型的上下文 ${c.last_prompt_tokens.toLocaleString()} tok / 窗口 ${c.context_window.toLocaleString()}（${c.agent_label}）\n超过 ${compressAt}% 预算线就把较早的历史合并成摘要`}
        className={`flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-slate-100 ${tone.cls}`}
      >
        <span className="text-[11px] text-slate-400">上下文</span>
        <span className="relative inline-block h-1.5 w-12 rounded-full bg-slate-200 align-middle">
          <span
            className={`absolute left-0 top-0 h-1.5 rounded-full ${tone.bar}`}
            style={{ width: `${Math.min(100, pctShow)}%` }}
          />
        </span>
        <span className="font-medium tabular-nums">{pctShow.toFixed(0)}%</span>
      </button>
      <PopOverlay open={open} onClose={() => setOpen(false)} pos={pos}>
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-[12px] font-medium text-slate-700">
            上下文占用 · {tone.tag}
          </span>
          <span className="text-[11px] text-slate-400">阈值 {compressAt}%</span>
        </div>
        <table className="w-full text-[11px]">
          <tbody>
            {[
              ["角色", c.agent_label],
              ["模型", c.model || "默认档位"],
              ["窗口", `${c.context_window.toLocaleString()} tok${c.window_configured ? "" : "（未配置，按保守值估算）"}`],
              ["预算线", `${c.budget_tokens.toLocaleString()} tok`],
              ["最近一次输入", `${c.last_prompt_tokens.toLocaleString()} tok`],
              ["本会话峰值", `${c.peak_prompt_tokens.toLocaleString()} tok`],
              ["距预算线", c.will_compress ? "已越过（较早历史正在被丢弃）" : `还差 ${c.remaining_tokens.toLocaleString()} tok`],
              ["LLM 往返", `${c.rounds} 轮`],
              ...(c.skewed_usage_calls
                ? [[`上报异常`, `${c.skewed_usage_calls} 轮（端点把累计量当成单次用量上报，已排除）`]]
                : []),
            ].map(([k, v]) => (
              <tr key={k as string}>
                <td className="py-0.5 pr-2 text-slate-400">{k}</td>
                <td className="py-0.5 text-slate-600">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!c.window_configured && (
          <div className="mt-1.5 rounded bg-amber-50 px-2 py-1 text-[11px] leading-relaxed text-amber-700">
            模型档位没填 context_window，系统按 {c.context_window.toLocaleString()} 估算
            （当前主流模型的常见量级）。真实窗口更大就在「模型档位」里填实际值，免得白压；
            更小则会在端点报超长时自动按半预算重压一次。
          </div>
        )}
        {c.will_compress && (
          <div className="mt-1.5 rounded bg-orange-50 px-2 py-1 text-[11px] leading-relaxed text-orange-700">
            已超过预算线：每轮发请求前会把较早的历史合并成一条要点摘要（最近几轮保留原文，
            摘要会随对话滚动更新）。长任务可以接着跑，重要结论仍建议写进项目记忆。
          </div>
        )}
      </PopOverlay>
    </span>
  );
}

/** token 数带千分位（弹层里看真实数字，别像顶栏那样压成 7M 看不出差额） */
function tok(n: number): string {
  return Math.max(0, Math.round(n || 0)).toLocaleString();
}

/** 缓存命中那一格：点开看「Token 用量」明细（用户 2026-11-13 要求，参考其它客户端的面板）。
 *
 * 为什么不是只给一个百分比：命中率的分母是「输入 token」，而用户真正想核对的是
 * 「总量 = 未缓存输入 + 缓存读取 + 输出」这条加法。只显示 99% 时，99% 背后是
 * 6.9M 还是 69k 完全看不出来，也就判断不了「这个会话到底贵不贵」。
 */
function CacheChip({ u }: { u: Usage }) {
  const { open, setOpen, pos, btnRef, toggle } = useAnchorPop();
  const hit = u.cache_hit_rate;
  const cached = u.cached_tokens ?? 0;
  const promptTok = u.prompt_tokens ?? 0;
  const uncached = u.uncached_tokens ?? Math.max(0, promptTok - cached);
  const lr = u.last_round;
  // 「有缓存字段上报」与「命中 0%」必须分开说：没上报是不知道，0% 是真没命中
  const reported = hit !== null || !!u.cache_reported;
  const cls = !reported ? "text-slate-400"
    : hit === null ? "text-slate-400"
    : hit >= 50 ? "text-emerald-600" : hit > 0 ? "text-slate-600" : "text-slate-400";

  const rows: [string, string, string?][] = [
    ["缓存命中", reported ? `${hit ?? 0}%` : "未上报",
      reported
        ? `${tok(cached)} / ${tok(promptTok)} 输入 token 走了前缀缓存`
        : "端点没返回缓存字段（DeepSeek prompt_cache_hit_tokens / OpenAI cached_tokens）"],
    ["未缓存输入", `${tok(uncached)} tok`, "输入里按原价计费的部分：新内容 + 缓存未命中的前缀"],
    ["缓存读取", `${tok(cached)} tok`, "命中前缀缓存的部分（各家折扣不同，DeepSeek 约 1/10 价）"],
    ["输出", `${tok(u.completion_tokens)} tok`, "模型新生成的内容"],
  ];

  return (
    <span className="relative flex items-center">
      <button
        ref={btnRef}
        onClick={toggle}
        title="点开看 Token 用量明细（缓存命中 / 未缓存输入 / 缓存读取 / 输出）"
        className={`flex items-center gap-1 rounded px-1 py-0.5 hover:bg-slate-100 ${cls}`}
      >
        <span className="text-[11px] text-slate-400">缓存命中</span>
        <span className="font-medium">{reported ? `${hit ?? 0}%` : "未上报"}</span>
      </button>
      <PopOverlay open={open} onClose={() => setOpen(false)} pos={pos} width={380}>
        {/* 头部：图标 + 标题 + 总量，与用户给的参考图同一版式 */}
        <div className="mb-2 flex items-center justify-between border-b border-slate-200 pb-1.5">
          <span className="flex items-center gap-1.5 text-[12px] font-medium text-indigo-600">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
              <ellipse cx="12" cy="6" rx="7" ry="3" fill="currentColor" opacity="0.25" />
              <path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6" fill="none"
                stroke="currentColor" strokeWidth="1.6" />
              <path d="M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6" fill="none"
                stroke="currentColor" strokeWidth="1.6" />
            </svg>
            Token 用量
          </span>
          <span className="text-[12px] font-semibold tabular-nums text-indigo-600">
            {tok(u.total_tokens)} tok
          </span>
        </div>
        <table className="w-full text-[11px]">
          <tbody>
            {rows.map(([k, v, tip]) => (
              <tr key={k} title={tip}>
                <td className="py-1 text-slate-500">{k}</td>
                <td className={`py-1 text-right font-medium tabular-nums ${k === "缓存命中" ? cls : "text-slate-700"}`}>
                  {v}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!!lr && (
          <>
            <div className="mt-2 border-t border-slate-200 pt-1.5 text-[11px] font-medium text-slate-600">
              最近一轮 · {lr.agent_label}
              {lr.model ? ` · ${lr.model}` : ""}
            </div>
            <table className="w-full text-[11px]">
              <tbody>
                {[
                  ["输入", `${tok(lr.prompt_tokens ?? 0)} tok`],
                  ["其中缓存读取", lr.cache_reported === false
                    ? "未上报"
                    : `${tok(lr.cached_tokens ?? 0)} tok`],
                  ["输出", `${tok(lr.completion_tokens)} tok`],
                  ["总耗时", `${(lr.latency_ms / 1000).toFixed(1)} 秒`
                    + (lr.ttft_ms ? `（首字 ${fmtTtft(lr.ttft_ms)}）` : "")],
                ].map(([k, v]) => (
                  <tr key={k}>
                    <td className="py-0.5 text-slate-400">{k}</td>
                    <td className="py-0.5 text-right tabular-nums text-slate-600">{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {u.context_by_agent.length > 1 && (
          <>
            <div className="mt-2 border-t border-slate-200 pt-1.5 text-[11px] font-medium text-slate-600">
              按角色（各取最近{" "}
              {Math.max(...u.context_by_agent.map((c) => c.rounds || 0))} 轮内）
            </div>
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-slate-400">
                  <th className="py-0.5 text-left font-normal">角色</th>
                  <th className="py-0.5 text-right font-normal">输入</th>
                  <th className="py-0.5 text-right font-normal">缓存</th>
                  <th className="py-0.5 text-right font-normal">输出</th>
                </tr>
              </thead>
              <tbody>
                {u.context_by_agent.map((c) => (
                  <tr key={c.agent_key}>
                    <td className="py-0.5 text-slate-600">{c.agent_label}</td>
                    <td className="py-0.5 text-right tabular-nums text-slate-600">{tok(c.prompt_tokens ?? 0)}</td>
                    <td className="py-0.5 text-right tabular-nums text-slate-600">
                      {c.cache_reported_rounds ? tok(c.cached_tokens ?? 0) : "未上报"}
                    </td>
                    <td className="py-0.5 text-right tabular-nums text-slate-600">{tok(c.completion_tokens ?? 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        <div className="mt-2 rounded bg-slate-50 px-2 py-1 text-[11px] leading-relaxed text-slate-500">
          {reported
            ? "命中率 = 缓存读取 ÷ 输入 token。前缀缓存只对「和上一轮完全相同的前缀」生效，"
              + "所以同一会话连着聊命中率会很高、换了角色或改了系统提示词就会掉下来。"
            : "这个模型端点没返回缓存字段，命中率无法计算——不是 0%，是不知道。"
              + "DeepSeek 看 prompt_cache_hit_tokens、OpenAI 看 prompt_tokens_details.cached_tokens，"
              + "中转不转发这两个字段时这里就一直显示「未上报」。"}
        </div>
      </PopOverlay>
    </span>
  );
}

/** 速度那一格（点击才展开明细）：顶栏只留「瞬时速度」，首字/调用/动作/累计执行/平均速度全收进弹层。
 *
 * 用户 2026-11-14 要求：这几格常驻顶栏太吵，合并成一个入口，点开才看明细；
 * 默认仍显示瞬时速度——它是唯一回答「现在快不快、换模型有没有生效」的数字。
 * 弹层里再补一格「平均（本会话）」作对照：瞬时和平均并排，才看得出这轮比平时快还是慢。
 * 平均值不能当主显示：全历史含重试与压缩，跑过上百轮后几乎不动，看起来像卡住了（2026-11-12 实测）。
 */
function SpeedChip({ u }: { u: Usage }) {
  const { open, setOpen, pos, btnRef, toggle } = useAnchorPop();
  const lr = u.last_round;
  const instant = u.tokens_per_sec;
  const avg = u.tokens_per_sec_avg ?? null;
  // 后端在拿不到瞬时值时会用平均兜底：这种必须标明，别把历史平均说成「这一轮的速度」
  const realInstant = !!(lr && lr.completion_tokens > 0 && lr.latency_ms > 0);
  const ttft = u.ttft_ms ?? 0;
  const ttftMed = u.ttft_median_ms ?? 0;
  const act = u.activity;
  const ttftCls = !ttft && !u.ttft_calls ? "text-slate-400"
    : (ttft || ttftMed) >= 5000 ? "text-orange-600"
    : (ttft || ttftMed) >= 2000 ? "text-amber-600" : "text-emerald-600";
  const ttftTip = [
    ttft ? `最近一轮首字耗时 ${fmtTtft(ttft)}（${lr?.agent_label ?? "未知角色"}`
      + `${lr?.model ? ` · ${lr.model}` : ""}）` : "最近一轮没测到首字耗时",
    ttftMed ? `本会话中位数 ${fmtTtft(ttftMed)}（共 ${u.ttft_calls ?? 0} 轮测到）` : "",
    "从请求发出到模型吐出第一个字（含思考）的时间：这段时间你在干等，界面没有任何新字。",
    (u.ttft_calls ?? 0) === 0
      ? "非流式响应拿不到首字时间；端点支持流式（多数中转都支持）时这里才有数。"
      : "",
  ].filter(Boolean).join("\n");
  const rows: [string, string, string?][] = [
    ["首字耗时", ttft ? fmtTtft(ttft) : u.ttft_calls ? fmtTtft(ttftMed) : "未测到", ttftTip],
    ["首字中位数", ttftMed ? fmtTtft(ttftMed) : "—",
      `${u.ttft_calls ?? 0} 轮测到过；用中位数是因为偶发排队/限流重试会把平均拉爆`],
    ["最近一轮耗时", lr
      ? `${(lr.latency_ms / 1000).toFixed(1)} 秒`
        + (lr.ttft_ms ? `（首字 ${fmtTtft(lr.ttft_ms)} + 吐字 ${((lr.latency_ms - lr.ttft_ms) / 1000).toFixed(1)} 秒）` : "")
      : "—",
      "该轮从发请求到收完最后一个字的墙钟时间（含首字等待）"],
    ["动作", act ? `${act.total.toLocaleString()} 次` : "—", act
      ? `运行命令 ${act.commands} · 读取 ${act.reads} · 修改 ${act.writes} · 其它 ${act.other}\n`
        + `整个会话 ${u.runs} 个任务累计（分桶口径与执行轨迹那行摘要一致）`
      : "还没有工具动作"],
    ["累计执行", act ? fmtDur(act.run_seconds) : "—", act
      ? `${u.runs} 个任务从发起到结束的墙钟时间求和；`
        + `同时并行跑的任务会各算一份，所以它不等于你在屏幕前等了多久`
      : ""],
  ];

  return (
    <span className="relative flex items-center">
      <button
        ref={btnRef}
        onClick={toggle}
        title={"点开看明细：瞬时 / 平均速度 · 首字 · 调用 · 动作 · 累计执行"}
        className="flex items-center gap-1 rounded px-1 py-0.5 text-slate-600 hover:bg-slate-100"
      >
        <span className="text-[11px] text-slate-400">速度</span>
        <span className="font-medium">{instant != null ? `${instant} tok/s` : "—"}</span>
        <svg viewBox="0 0 24 24" className="h-2.5 w-2.5 text-slate-400" aria-hidden="true">
          <path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="2.4"
            strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <PopOverlay open={open} onClose={() => setOpen(false)} pos={pos} width={400}>
        <div className="mb-2 flex items-center justify-between border-b border-slate-200 pb-1.5">
          <span className="flex items-center gap-1.5 text-[12px] font-medium text-brand-600">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden="true">
              <path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5z" fill="currentColor" opacity="0.9" />
            </svg>
            生成速度
          </span>
          <span className="text-[11px] text-slate-400" title={`本会话 ${u.runs} 个任务、${u.calls} 次 LLM 往返`}>
            {u.calls} 次调用 · {u.runs} 个任务
          </span>
        </div>

        {/* 两格并排：瞬时（这轮）对平均（全历史）。分开摆才看得出「这轮比平时快还是慢」。
            底色只用 bg-slate-50 / border-slate-200（深色主题里有对应覆写），
            别用 indigo-50 那种只在浅色下成立的色块，否则深色下会变成两块白板。 */}
        <div className="mb-2 grid grid-cols-2 gap-2">
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5">
            <div className="flex items-center gap-1 text-[10px] font-medium text-brand-600">
              <span className="h-1.5 w-1.5 rounded-full bg-current" />
              瞬时 · 最近一轮
            </div>
            <div className="text-[15px] font-semibold leading-tight tabular-nums text-brand-600">
              {instant != null ? instant : "—"}
              {instant != null && <span className="ml-0.5 text-[10px] font-normal">tok/s</span>}
            </div>
            <div className="truncate text-[10px] text-slate-500"
              title={realInstant && lr
                ? `${lr.agent_label}${lr.model ? ` · ${lr.model}` : ""}：输出 ${tok(lr.completion_tokens)} tok ÷ ${(lr.latency_ms / 1000).toFixed(1)} 秒`
                : ""}>
              {realInstant && lr
                ? `${lr.agent_label}${lr.model ? ` · ${lr.model}` : ""}`
                : instant != null ? "这轮没测到，用会话平均顶替" : "还没有 LLM 往返"}
            </div>
            <div className="truncate text-[10px] tabular-nums text-slate-400">
              {realInstant && lr
                ? `输出 ${tok(lr.completion_tokens)} tok ÷ ${(lr.latency_ms / 1000).toFixed(1)} 秒`
                : ""}
            </div>
          </div>
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5">
            <div className="flex items-center gap-1 text-[10px] font-medium text-slate-500">
              <span className="h-1.5 w-1.5 rounded-full bg-current" />
              平均 · 本会话
            </div>
            <div className="text-[15px] font-semibold leading-tight tabular-nums text-slate-700">
              {avg != null ? avg : "—"}
              {avg != null && <span className="ml-0.5 text-[10px] font-normal text-slate-500">tok/s</span>}
            </div>
            <div className="truncate text-[10px] text-slate-500">全历史（含重试/压缩）</div>
            <div className="truncate text-[10px] tabular-nums text-slate-400">
              {avg != null
                ? `输出 ${tok(u.completion_tokens)} tok ÷ ${(u.latency_ms / 60000).toFixed(1)} 分往返`
                : ""}
            </div>
          </div>
        </div>

        <table className="w-full text-[11px]">
          <tbody>
            {rows.map(([k, v, tip]) => (
              <tr key={k} title={tip}>
                <td className="py-1 text-slate-500">{k}</td>
                <td className={`py-1 text-right font-medium tabular-nums ${
                  k === "首字耗时" ? ttftCls : "text-slate-700"
                }`}>
                  {v}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="mt-2 rounded bg-slate-50 px-2 py-1 text-[11px] leading-relaxed text-slate-500">
          瞬时 = 最近一轮输出 token ÷ 该轮总耗时（含首字等待），换模型后立刻会变；
          平均 = 全会话输出 ÷ 累计往返耗时（含重试与历史），会被过去拖住，只作对照。
        </div>
      </PopOverlay>
    </span>
  );
}

export default function SessionUsage({ sessionId, refreshKey = 0, compact = false }: {
  sessionId: string;
  refreshKey?: number;
  /** 顶栏窄的时候只留「总 token / 上下文」，其余（缓存命中、速度）让位给右侧操作按钮 */
  compact?: boolean;
}) {
  const [u, setU] = useState<Usage | null>(null);

  useEffect(() => {
    if (!sessionId) {
      setU(null);
      return;
    }
    let alive = true;
    api
      .sessionUsage(sessionId)
      .then((r: any) => alive && setU(r))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [sessionId, refreshKey]);

  if (!u || !u.calls) return null;
  const item = (label: string, value: string, title?: string, cls = "text-slate-600") => (
    <span className="flex items-center gap-1" title={title}>
      <span className="text-[11px] text-slate-400">{label}</span>
      <span className={`font-medium ${cls}`}>{value}</span>
    </span>
  );

  return (
    <div className="flex items-center gap-2.5 text-[11px] px-2 py-1 rounded border border-slate-200 bg-slate-50 shrink-0">
      {item("本会话 token", fmtTokens(u.total_tokens),
        `输入 ${u.prompt_tokens.toLocaleString()} + 输出 ${u.completion_tokens.toLocaleString()}`)}
      {u.context ? (
        <ContextChip c={u.context} compressAt={u.compress_at_pct} />
      ) : (
        item("上下文", "—", "还没有 LLM 往返记录")
      )}
      {!compact && <CacheChip u={u} />}
      {!compact && <SpeedChip u={u} />}
    </div>
  );
}
