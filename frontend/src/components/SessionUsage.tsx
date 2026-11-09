/** 会话级用量条：总消耗 token / 缓存命中率 / 生成速度 / 调用次数 / 上下文窗口占用。
 *
 * 数据源 GET /api/sessions/{id}/usage（后端按会话聚合 llm_calls，落库持久化）——
 * 所以「下次进来这个会话」还能看到累计值，不只是本次 run。
 * 上下文占用取该角色最近一次往返的 prompt_tokens（就是压缩后模型真正看到的量），
 * 与档位窗口的 75% 预算线比较 → 用户能判断「是否已经开始丢历史了」。
 */
import { useEffect, useRef, useState } from "react";
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

/** 占用比例 → 文案与颜色：越过预算线后模型的历史真的在被裁，必须显眼。 */
function ctxTone(pct: number, willCompress: boolean) {
  if (willCompress) return { cls: "text-orange-600", bar: "bg-orange-500", tag: "压缩中" };
  if (pct >= 60) return { cls: "text-amber-600", bar: "bg-amber-400", tag: "接近阈值" };
  return { cls: "text-slate-600", bar: "bg-emerald-400", tag: "余量充足" };
}

function ContextChip({ c, compressAt }: { c: ContextOccupancy; compressAt: number }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      if (next && btnRef.current) {
        // 弹层用 fixed + portal 挂到 body：绝对定位会被消息列表盖住（祖先层叠上下文）/裁剪，
        // portal 到根节点后谁也压不住它（2026-10-29 用户实测弹窗被轨迹区遮挡）。
        const r = btnRef.current.getBoundingClientRect();
        setPos({ top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) });
      }
      return next;
    });
  };
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
      {open &&
        createPortal(
          <>
            {/* 半透明遮罩：点空白处关闭，同时把背后花花的信息流压暗，弹层一眼可辨 */}
            <span
              className="fixed inset-0 z-40 bg-slate-900/30 backdrop-blur-[1px]"
              onClick={() => setOpen(false)}
            />
            <div
              className="fixed z-50 w-[440px] rounded-xl border border-slate-300 bg-white p-3 text-left shadow-2xl ring-1 ring-black/5"
              style={{ top: pos?.top ?? 52, right: pos?.right ?? 8 }}
            >
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
            </div>
          </>,
          document.body,
        )}
    </span>
  );
}

export default function SessionUsage({ sessionId, refreshKey = 0 }: {
  sessionId: string;
  refreshKey?: number;
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
  const hit = u.cache_hit_rate;
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
      {hit !== null
        ? item("缓存命中", `${hit}%`,
            `命中 ${u.cached_tokens.toLocaleString()} / 输入 ${u.prompt_tokens.toLocaleString()}`,
            hit >= 50 ? "text-emerald-600" : hit > 0 ? "text-slate-600" : "text-slate-400")
        : item("缓存命中", "未上报",
            "当前模型端点没返回缓存字段（DeepSeek prompt_cache_hit_tokens / OpenAI cached_tokens），无法计算命中率",
            "text-slate-400")}
      {u.tokens_per_sec !== null &&
        item("速度", `${u.tokens_per_sec} tok/s`, "输出 token / 总耗时（含重试）")}
      {item("调用", `${u.calls} 次`, `${u.runs} 个任务`)}
      {!!u.activity && u.activity.total > 0 && (
        <>
          {item("动作", `${u.activity.total.toLocaleString()} 次`,
            `运行命令 ${u.activity.commands} · 读取 ${u.activity.reads} · ` +
            `修改 ${u.activity.writes} · 其它 ${u.activity.other}\n` +
            `整个会话 ${u.runs} 个任务累计（分桶口径与执行轨迹那行摘要一致）`)}
          {item("累计执行", fmtDur(u.activity.run_seconds),
            `${u.runs} 个任务从发起到结束的墙钟时间求和；` +
            `同时并行跑的任务会各算一份，所以它不等于你在屏幕前等了多久`)}
        </>
      )}
    </div>
  );
}
