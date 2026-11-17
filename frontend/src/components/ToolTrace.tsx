/** 工具调用追踪的公共类型与耗时格式化。

RunTraceInline 的实时轨迹条只用到这里的 ToolStep 与 fmtDuration；
原先那套「按 agent 分组」的折叠视图（TOOL_META / buildToolGroups / StepRow /
ToolGroupBlock）没有任何调用方，已删除。
 */

export type ToolStep = {
  key: number;
  tool: string;
  args?: Record<string, unknown>;
  result?: string;
  /** 工具调用发起时间（ISO）：显示「HH:MM:SS 开始」用 */
  at?: string;
  /** 本步执行耗时（毫秒）：优先来自 tool_result 事件 payload.duration_ms（精确计时） */
  durationMs?: number;
  /** durationMs 是否只是按事件时间戳估算（老数据/未重启时）：渲染成「≈Ns」以示区别 */
  durationEstimated?: boolean;
};

/** 毫秒 → 人类可读耗时：<1s 用 XXXms，随后 X.Xs，超过 1 分钟用 Xm Ys。 */
export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  return `${m}m ${rs}s`;
}
