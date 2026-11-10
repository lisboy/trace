/** 聊天流内嵌的运行轨迹（扁平样式，参照 DeepSeek）：
 *   · 一行摘要头：状态 + 动作摘要（运行了 N 条命令，读取了 N 个文件…），点一下整段收起/展开
 *   · 每步一行：`思考 · …` / `运行命令 · dir` / `读取 · path`，点某行展开该行细节
 *   · 运行中由 ws 事件增量拉取事件流；思考增量（agent_thinking_delta）直接推送
 *   · **已经跑完的历史 run 默认收起，而且收起时一条事件都不拉**：摘要数字直接用
 *     /sessions/{id}/runs 带回来的分桶统计（见 RunStat）。一个大会话有十几条 run、
 *     单条 run 最多 1448 个事件（1.5MB），切会话时全展开会一次挂出上万个节点——
 *     那就是用户反馈的「切换会话卡一下」。点开哪条才拉哪条。
 *  没有卡片、没有边框，只有灰字一行行往下排——信息密度高且不抢戏。 */
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import Markdown from "./Markdown";
import CopyButton from "./CopyButton";
import { ROLE_LABEL } from "../types";
import { diffFromToolArgs, diffStats, lineDiff } from "../diff";
import DiffView from "./DiffView";
import ShotThumbs from "./ShotThumbs";
import { parseSources, SourceChips } from "./SearchSources";
import { fmtDuration } from "./ToolTrace";
import type { ToolStep } from "./ToolTrace";

// blocked=组长在等用户回答（本轮已停），不算"还在跑"
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "blocked", "orphaned"]);

/** 收起态摘要头的数据源：/sessions/{id}/runs 顺带算好的每条 run 统计。
 *  没有它（老后端 / 列表还没到位）就不敢收起——收起态那一行也得有数字可说。 */
export type RunStat = {
  status?: string;
  activity?: { commands: number; reads: number; writes: number; other: number; total: number };
  agents?: string[];
  created_at?: string;
  finished_at?: string | null;
};

/** 素材网格/进度条的挂载点：某次媒体工具调用对应的产物时间窗（见 mediaWindows）。 */
type MediaWin = { from?: string; to?: string; latest: boolean };

/** 状态词：收起的轨迹也要说实话——被重启中断的说「已中断」、等回答的说「等你回答」，
 *  不能因为没拉事件流就把它们印成「执行完毕」。 */
function statusWord(status: string | null | undefined): string {
  switch (status) {
    case "failed": return "执行失败";
    case "cancelled": return "已取消";
    case "orphaned": return "已中断";
    case "blocked": return "等你回答";
    case "succeeded": return "执行完毕";
    case null:
    case undefined: return "执行中";
    default: return "执行中";   // queued / claimed / running
  }
}

/** 摘要头文案：运行了 3 条命令，读取了 5 个文件，修改了 1 个文件。
 *  展开态（数行）与收起态（数后端分桶）共用这段排版，收起→点开数字不会跳。 */
function bucketText(a: { commands: number; reads: number; writes: number; other: number }): string {
  const parts: string[] = [];
  if (a.commands) parts.push(`运行了 ${a.commands} 条命令`);
  if (a.reads) parts.push(`读取了 ${a.reads} 个文件`);
  if (a.writes) parts.push(`修改了 ${a.writes} 个文件`);
  if (a.other) parts.push(`调用了 ${a.other} 次其它工具`);
  return parts.join("，");
}

type Ev = { seq: number; type: string; agent?: string | null; payload?: any; created_at: string };

/** context_compacted 事件的载荷：一次全局摘要压缩的现场（合并了多少历史、摘要多大） */
type CompactInfo = {
  mergedMsgs: number; mergedTokens: number; digestTokens: number;
  before: number; after: number; window: number; budget: number;
};

/** notes_saved 事件的载荷：本轮收尾判断「值得记」后沉淀的项目改动经验 */
type NotesInfo = { entries: number; files: string[]; titles: string[]; reason: string };

/** llm_retry 事件的载荷：端点 503/限流等可恢复失败，退避重试中的内联提醒 */
type LlmRetryInfo = {
  attempt: number; max: number; delayMs: number; error: string;
};

type Row =
  | { kind: "think"; agent: string; text: string; round: number; key: string }
  | { kind: "compact"; agent: string; round: number; key: string; compact: CompactInfo }
  | { kind: "notes"; agent: string; round: number; key: string; notes: NotesInfo }
  | { kind: "llm_retry"; agent: string; round: number; key: string; retry: LlmRetryInfo }
  | { kind: "steer"; agent: string; round: number; key: string; steer: SteerNote }
  | {
      kind: "tool";
      agent: string;
      step: ToolStep;
      round: number;
      key: string;
      /** 工具调用发起时间：用来在「执行中…」后面显示已经等了多少秒 */
      at?: string;
      /** 连续重复的同一条命令合并成一行，count = 实际执行次数 */
      count?: number;
      /** 这条 report_to_user 之后还有工具调用 = 阶段汇报（不是最终交付） */
      interimReport?: boolean;
    };

/** 用户在执行中途的插话：按发生时刻插进事件流，而不是堆在整块轨迹的上/下 */
export interface SteerNote {
  id: string;
  text: string;
  at: string;
  /** 插话附带的图片（服务端 URL 或 dataURL）：和普通消息一样要显示出来，
   *  否则「明明带图插话，会话里却看不到图」。 */
  images?: string[];
}

function mapEv(e: any): Ev {
  return {
    seq: e.seq,
    type: e.type,
    agent: e.agent ?? e.payload?.agent_key ?? null,
    payload: e.payload,
    created_at: e.created_at,
  };
}

function fmtElapsed(sec: number): string {
  if (sec >= 60) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
  return `${Math.max(sec, 1)} 秒`;
}

/** 「已运行 / 用时 X」：只有还在跑的那条才自己挂个秒表，
 *  跑完的历史 run 是个静态数字——不给它挂时钟，也就不用整条轨迹每秒重渲染。 */
function RunElapsed({ baseSec, startedAt, ticking, running }: {
  baseSec: number; startedAt: number; ticking: boolean; running: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [ticking]);
  const sec = ticking
    ? baseSec + Math.max(0, Math.round((now - startedAt) / 1000))
    : baseSec;
  return <>{`${running ? "已运行" : "用时"} ${fmtElapsed(sec)}`}</>;
}

/** 后端时间为 UTC（现带 +00:00）；对旧格式（无时区标记）按 UTC 归一化，避免差 8 小时。 */
function parseTs(s: string): number {
  return new Date(
    /[Z+]|-\d{2}:?\d{2}$/.test(s) ? s.replace(" ", "T") : s.replace(" ", "T") + "Z",
  ).getTime();
}

/** 本地「时:分:秒」：后端时间戳是 UTC，直接 slice(11,19) 会把 14:30 显示成 06:30（差 8 小时）。
 *  统一走 parseTs 归一化后再按本地时钟格式化。 */
function localClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = parseTs(iso);
  if (!t || Number.isNaN(t)) return "";
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 中文动词：一行里只显示「动词 · 摘要」，不再堆 JSON。 */
const TOOL_VERB: Record<string, string> = {
  read_file: "读取", write_file: "写入", edit_file: "编辑", delete_file: "删除",
  list_dir: "查看目录", glob: "查找文件", grep: "搜索", run_command: "运行命令",
  start_service: "启动服务", service_logs: "看服务日志", stop_service: "停止服务",
  screenshot_page: "截图验收",
  view_image: "查看图片", view_images: "合并看图",
  load_skill: "加载技能", search_history: "检索历史", parse_document: "解析文档",
  save_env_fact: "记住入口",
  web_search: "联网搜索", web_fetch: "读取网页",
  generate_image: "生成图片", generate_audio: "生成音频", generate_video: "生成视频",
  // 旧版派发工具名（历史 run 轨迹仍会出现）
  dispatch_art: "派发美术", dispatch_audio: "派发配音",
  report_to_user: "汇报", update_plan: "更新计划",
  ask_user: "向你提问", tts: "语音合成", edit_image: "改图",
  generate_sfx: "生成音效", generate_music: "生成音乐",
  save_character_sheet: "保存角色设定", upsert_voice_profile: "记录音色",
};

/** 一行摘要素材：命令行显示命令本身，文件类显示路径，其余压成短 JSON。 */
function brief(tool: string, args?: Record<string, unknown>): string {
  const a = args || {};
  const pick = (k: string) => (typeof a[k] === "string" ? String(a[k]) : "");
  switch (tool) {
    case "read_file":
    case "parse_document":
    case "write_file":
    case "edit_file":
    case "delete_file":
      return pick("path");
    case "run_command":
      return pick("command");
    case "start_service":
      return pick("command");
    case "screenshot_page":
      return pick("url");
    case "view_image":
      return pick("path");
    case "view_images": {
      const ps = Array.isArray(a.paths) ? (a.paths as unknown[]) : [];
      return `${ps.length} 张`;
    }
    case "list_dir":
      return pick("path") || ".";
    case "glob":
    case "grep":
      return pick("pattern");
    case "load_skill":
      return pick("name");
    case "search_history":
      return pick("keyword");
    case "web_search":
      return pick("query");
    case "web_fetch":
      return pick("url");
    case "save_env_fact":
      return pick("key") ? `${pick("key")}=${pick("value")}` : pick("value");
    case "tts":
    case "dispatch_audio":
    case "generate_audio":
      return pick("text") || pick("kind") || pick("voice");
    case "dispatch_art":
    case "generate_image":
      return pick("subject") || pick("prompt") || pick("slug");
    case "generate_video":
      return pick("prompt") || pick("slug");
    case "report_to_user":
      return pick("report");
    case "ask_user":
      return pick("question");
    case "update_plan":
      return `${(a.items as unknown[] | undefined)?.length ?? 0} 项`;
    default: {
      const s = JSON.stringify(a);
      return s === "{}" ? "" : s;
    }
  }
}

const oneLine = (s: string, n = 170): string => {
  const line = (s || "").replace(/\s+/g, " ").trim();
  return line.length > n ? line.slice(0, n) + "…" : line;
};

/** 事件流 → 有序行：连续思考合并成一行，工具调用各占一行，用户插话按发生时刻插在中间。
 *  key 用事件 seq（稳定），避免插入新行时按下标重建导致整段重挂载（视觉上会「闪」）。 */
function buildRows(events: Ev[], steers: SteerNote[] = []): Row[] {
  const rows: Row[] = [];
  // 插话是「这一轮跑到一半说的话」：它前面的动作在上、后面的在下，整块贴到轨迹上面或
  // 下面都不对（那样刚说的话看起来像插进了过去）。按时间戳归位。
  const pending = [...steers].sort((a, b) => (a.at < b.at ? -1 : 1));
  const flush = (upto: number) => {
    while (pending.length && parseTs(pending[0].at) <= upto) {
      const s = pending.shift()!;
      rows.push({ kind: "steer", agent: "user", round: -1, key: `s${s.id}`, steer: s });
    }
  };
  for (const e of events) {
    flush(parseTs(e.created_at));
    const p = e.payload || {};
    const agent = String(e.agent || p.agent_key || "");
    const round = Number(p.round ?? -1);
    if (e.type === "agent_thinking") {
      const text = String(p.reasoning || "");
      // 空思考不占行（带工具调用的那一轮 reasoning 常为空，否则会出现一行空的「思考 ·」）
      if (!text.trim()) continue;
      const last = rows[rows.length - 1];
      if (last && last.kind === "think" && last.agent === agent) {
        last.text = last.text ? `${last.text}\n\n${text}` : text;
        last.round = round;
      } else {
        rows.push({ kind: "think", agent, text, round, key: `t${e.seq}` });
      }
    } else if (e.type === "tool_call") {
      const tool = String(p.tool || "");
      const argsKey = JSON.stringify(p.args ?? {});
      const last = rows[rows.length - 1];
      // 同一条命令连续重复（超时重试、反复试探环境）：并进上一行计数，别把轨迹刷成瀑布
      if (
        last && last.kind === "tool" && last.agent === agent && last.round === round &&
        last.step.tool === tool && JSON.stringify(last.step.args ?? {}) === argsKey &&
        last.step.result !== undefined
      ) {
        last.count = (last.count ?? 1) + 1;
        last.step = { ...last.step, result: undefined, durationMs: undefined }; // 又跑了一遍，重新等这次的结果+耗时
        last.at = e.created_at;                          // 计时也跟着重来
        continue;
      }
      rows.push({
        kind: "tool", agent, round, key: `c${e.seq}`, at: e.created_at,
        step: { key: rows.length, tool, args: p.args, at: e.created_at },
      });
    } else if (e.type === "context_compacted") {
      rows.push({
        kind: "compact", agent, round, key: `k${e.seq}`,
        compact: {
          mergedMsgs: Number(p.merged_msgs || 0),
          mergedTokens: Number(p.merged_tokens || 0),
          digestTokens: Number(p.digest_tokens || 0),
          before: Number(p.before || 0), after: Number(p.after || 0),
          window: Number(p.window || 0), budget: Number(p.budget || 0),
        },
      });
    } else if (e.type === "notes_saved") {
      rows.push({
        kind: "notes", agent, round, key: `n${e.seq}`,
        notes: {
          entries: Number(p.entries || 0),
          files: Array.isArray(p.files) ? p.files.map(String) : [],
          titles: Array.isArray(p.titles) ? p.titles.map(String) : [],
          reason: String(p.reason || ""),
        },
      });
    } else if (e.type === "llm_retry") {
      const info: LlmRetryInfo = {
        attempt: Number(p.attempt || 0),
        max: Number(p.max_attempts || 0),
        delayMs: Number(p.delay_ms || 0),
        error: String(p.error || ""),
      };
      const last = rows[rows.length - 1];
      // 连续重试并成一行，只显示最新一次的 (n/max) 与延迟，别把轨迹刷成瀑布
      if (last && last.kind === "llm_retry" && last.agent === agent) {
        last.retry = info;
        last.round = round;
      } else {
        rows.push({ kind: "llm_retry", agent, round, key: `r${e.seq}`, retry: info });
      }
    } else if (e.type === "tool_result") {
      // 结果按发生顺序回填最早那个「还没结果」的同名调用：
      // 从后往前找会让重复命令的结果错配，早先那行永远挂着黄点。
      for (const r of rows) {
        if (r.kind !== "tool") continue;
        if (r.agent !== agent || r.step.tool !== String(p.tool || "")) continue;
        if (r.step.result !== undefined) continue;
        const precise = typeof p.duration_ms === "number";
        let durationMs = precise ? p.duration_ms : r.step.durationMs;
        let durationEstimated = precise ? false : r.step.durationEstimated;
        if (!precise && !durationMs && r.step.at && e.created_at) {
          // 老数据无精确耗时：用「调用→结果」事件时间戳估算（秒级，前端标 ≈）
          const d = parseTs(e.created_at) - parseTs(r.step.at);
          if (Number.isFinite(d) && d >= 0) { durationMs = d; durationEstimated = true; }
        }
        r.step = {
          ...r.step,
          result: String(p.result || ""),
          durationMs,
          durationEstimated,
        };
        break;
      }
    }
  }
  flush(Number.POSITIVE_INFINITY);
  // 汇报后还有工具的 report_to_user 行 = 阶段汇报（模型先汇报了一句又接着干）。
  // 最终的那次汇报之后没有工具调用，保持「汇报」不改。用户正是看到「✓汇报」下面
  // 还挂着一串动作才困惑，这里明确标成「阶段汇报」，不再像是已经交付完了。
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.kind !== "tool" || r.step.tool !== "report_to_user") continue;
    const hasAfter = rows.slice(i + 1).some((x) => x.kind === "tool");
    if (hasAfter) r.interimReport = true;
  }
  return rows;
}

/** 摘要头用的动作计数：按**实际执行次数**算（合并显示的重复命令要 ×count），
 *  这样和后端按 tool_call 事件数出来的分桶完全一致，收起/展开不会两个数字。 */
function actionSummary(rows: Row[]): string {
  let cmd = 0, read = 0, write = 0, other = 0;
  for (const r of rows) {
    if (r.kind !== "tool") continue;
    const n = r.count ?? 1;
    const t = r.step.tool;
    if (t === "run_command") cmd += n;
    else if (t === "read_file" || t === "grep" || t === "glob" || t === "list_dir") read += n;
    else if (t === "write_file" || t === "edit_file" || t === "delete_file") write += n;
    else other += n;
  }
  return bucketText({ commands: cmd, reads: read, writes: write, other });
}

function toolGlyph(tool: string): string {
  if (tool === "run_command") return "❯";
  if (tool === "read_file" || tool === "list_dir" || tool === "glob" || tool === "grep") return "▤";
  if (tool === "view_image" || tool === "view_images") return "▧";
  if (tool === "write_file" || tool === "edit_file") return "✎";
  if (tool === "delete_file") return "⌫";
  return "◇";
}

const failedResult = (s?: string): boolean =>
  !!s && (s.startsWith("工具错误") || s.startsWith("工具执行异常") || s.includes("不在你的权限白名单内"));

/* ---------------- 行组件 ---------------- */

/** 角色标签：一行里标明这步是谁做的（助手），旧角色 key 统一同色。 */
const ROLE_TONE: Record<string, string> = {
  main: "bg-sky-50 text-sky-700 ring-sky-200",
  // 旧角色 key：同色处理，历史 run 轨迹显示不破
  planner: "bg-sky-50 text-sky-700 ring-sky-200",
  artist: "bg-pink-50 text-pink-700 ring-pink-200",
  voice: "bg-violet-50 text-violet-700 ring-violet-200",
};

function RoleTag({ agent }: { agent: string }) {
  if (!agent) return null;
  const label = ROLE_LABEL[agent] || agent;
  const tone = ROLE_TONE[agent] || "bg-slate-100 text-slate-600 ring-slate-300";
  return (
    <span
      className={`shrink-0 rounded px-1 py-[1px] text-[10px] font-medium leading-4 ring-1 ${tone}`}
      title={`这一步由「${label}」执行`}
    >
      {label}
    </span>
  );
}

/** 一行的外壳：图标 + 角色标签 + 单行文本，点一下展开细节。
 *  `children` 是会被截断的主文本；`trail` 是行尾的次要信息（时刻 / 耗时 / 次数…），
 *  用 shrink-0 固定在右侧——以前这些时间和耗时混在 `children` 里，赶上一条很长的命令文本，
 *  右侧的「时分秒 · 128ms」就被 truncate 一起挤没了（用户截图：13:30:09 那行看不到开始时刻）。 */
function Line(props: {
  glyph: string;
  glyphClass?: string;
  lead?: React.ReactNode;
  onToggle?: () => void;
  mark?: "pending" | "ok" | "fail";
  /** 行尾固定不截断的次要信息：始终停在右侧可见位置 */
  trail?: React.ReactNode;
  children: React.ReactNode;
}) {
  const { glyph, glyphClass = "text-slate-400", lead, onToggle, mark, trail, children } = props;
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={!onToggle}
      className="group w-full flex items-start gap-2 rounded px-1 py-[3px] text-left text-[13px] leading-5 text-slate-700 enabled:hover:bg-slate-50 enabled:hover:text-slate-900"
    >
      <span className={`mt-px w-3 shrink-0 text-center text-[12px] ${glyphClass}`}>{glyph}</span>
      {lead && <span className="mt-px shrink-0">{lead}</span>}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {trail && <span className="mt-px flex shrink-0 items-center">{trail}</span>}
      {mark === "pending" && (
        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-400" />
      )}
      {mark === "ok" && <span className="shrink-0 text-[11px] text-emerald-500">✓</span>}
      {mark === "fail" && <span className="shrink-0 text-[11px] text-red-500">✕</span>}
    </button>
  );
}

/** 展开的细节：铺一层浅底色 + 左侧色条，和外层灰色一行行明确区分开。 */
function Detail({ children, tone = "tool" }: { children: React.ReactNode; tone?: "think" | "tool" }) {
  const skin =
    tone === "think"
      ? "border-violet-300 bg-violet-50/70 text-slate-700"
      : "border-slate-300 bg-slate-100/80 text-slate-700";
  return (
    <div className={`mb-1.5 ml-5 rounded-r border-l-2 px-3 py-2 text-[12.5px] leading-relaxed ${skin}`}>
      {children}
    </div>
  );
}

/** 全局上下文压缩的分隔行：告诉用户「这里之前，较早的历史被合并成摘要了」。
 *  以前压缩只在适配层做、压完就丢，界面上完全看不出来发生过。 */
function CompactRow({ info }: { info: CompactInfo }) {
  const k = (n: number) => (n >= 1000 ? `${Math.round(n / 100) / 10}k` : `${n}`);
  return (
    <div
      className="my-1 flex items-center gap-2 px-1 text-[11.5px] text-slate-400"
      title={
        `模型窗口 ${k(info.window)} tok，发送预算 ${k(info.budget)} tok\n` +
        `把较早的 ${info.mergedMsgs} 条历史（约 ${k(info.mergedTokens)} tok）合并成 ` +
        `${k(info.digestTokens)} tok 的要点摘要，最近回合保留原文\n` +
        `这一轮发出去的请求：${k(info.before)} → ${k(info.after)} tok`
      }
    >
      <span className="h-px flex-1 bg-slate-200" />
      <span className="shrink-0">
        ▤ 已压缩上下文 · 合并 {info.mergedMsgs} 条历史（{k(info.mergedTokens)} tok）
        → 摘要 {k(info.digestTokens)} tok
      </span>
      <span className="h-px flex-1 bg-slate-200" />
    </div>
  );
}

function NotesRow({ info }: { info: NotesInfo }) {
  return (
    <div
      className="my-1 flex items-center gap-2 px-1 text-[11.5px] text-emerald-600"
      title={
        `${info.reason ? info.reason + "\n" : ""}` +
        `写入：${info.files.join("、") || "（未落盘）"}\n` +
        `条目：${info.titles.join("；") || "-"}`
      }
    >
      <span className="h-px flex-1 bg-emerald-100" />
      <span className="shrink-0">
        ✎ 已沉淀 {info.entries} 条项目改动经验（{info.files.map((f) => f.split("/").pop()).join("、")}）
      </span>
      <span className="h-px flex-1 bg-emerald-100" />
    </div>
  );
}

/** 端点 503/限流这类可恢复失败：内联提醒（仿 Qoder 的重试行），点开看重试延迟与失败原因 */
function LlmRetryRow({ info }: { info: LlmRetryInfo }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <Line glyph="↻" glyphClass="text-amber-500" onToggle={() => setOpen((o) => !o)}>
        <span className="font-medium text-amber-600">
          已重试模型请求（{info.attempt}/{info.max}）
        </span>
        <span className="text-slate-400"> · </span>
        <span className="text-slate-600">等待 {(info.delayMs / 1000).toFixed(1)}s 后继续</span>
      </Line>
      {open && (
        <Detail tone="tool">
          <div>重试延迟：{info.delayMs} 毫秒</div>
          <div className="break-all">失败原因：{info.error || "（未记录）"}</div>
        </Detail>
      )}
    </div>
  );
}

/** 用户中途插话：按发生时刻嵌在事件流里（右侧蓝气泡，与消息流同一套皮肤）。
 *  以前整块贴在整个轨迹上面或下面，两种都让刚说的话看起来像插进了过去。
 *  插话附图同样要能点开放大（以前只渲染了 <img>、没挂点击，用户反馈「自己发的图点不开」）。
 *  这里用原图开灯箱，缩略图只负责列表里不卡（thumb=320）。 */
function SteerRow({ note }: { note: SteerNote }) {
  const hm = localClock(note.at || "");
  const images = Array.isArray(note.images) ? note.images.filter(Boolean) : [];
  // 灯箱用原图（不带 thumb）；失败时退回缩略图，别留一片白。
  const [zoom, setZoom] = useState<{ src: string; alt: string } | null>(null);
  const [broken, setBroken] = useState<Record<number, boolean>>({});
  useEffect(() => {
    if (!zoom) return;
    const onKey = (ev: KeyboardEvent) => { if (ev.key === "Escape") setZoom(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);
  return (
    <div className="my-1.5 text-right">
      <div className="text-[11px] text-slate-400">你 · 插话{hm ? ` · ${hm}` : ""}</div>
      <div className="mt-0.5 inline-block max-w-xl rounded-lg bg-brand-600 px-3 py-2 text-left text-[12.5px] leading-relaxed whitespace-pre-wrap text-white">
        {!!images.length && (
          <div className="mb-1 flex flex-wrap gap-1.5">
            {images.map((u, i) => {
              if (broken[i]) {
                return (
                  <span key={i} className="flex h-28 w-[200px] items-center justify-center rounded border border-dashed border-white/40 text-[10px] text-white/70">
                    附图{i + 1} 加载失败
                  </span>
                );
              }
              return (
                <img
                  key={i}
                  src={u.startsWith("data:") ? u : `${u}${u.includes("?") ? "&" : "?"}thumb=320`}
                  loading="lazy"
                  decoding="async"
                  alt={`插话附图${i + 1}`}
                  title="点击放大查看"
                  className="max-h-40 max-w-[220px] rounded border border-white/20 object-contain cursor-zoom-in hover:opacity-90"
                  onClick={() => setZoom({ src: u, alt: `插话附图${i + 1}` })}
                  onError={() => setBroken((b) => ({ ...b, [i]: true }))}
                />
              );
            })}
          </div>
        )}
        {note.text}
      </div>
      {zoom &&
        createPortal(
          <div
            className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/80 p-6"
            onClick={() => setZoom(null)}
          >
            <img
              src={zoom.src}
              alt={zoom.alt}
              className="max-h-[86vh] max-w-[92vw] rounded object-contain shadow-2xl"
              onClick={(e) => e.stopPropagation()}
            />
            <div className="mt-3 flex items-center gap-3 text-xs text-white/80" onClick={(e) => e.stopPropagation()}>
              <a href={zoom.src} download={zoom.alt} className="rounded border border-white/30 px-3 py-1 hover:bg-white/10">
                下载原图
              </a>
              <button type="button" onClick={() => setZoom(null)} className="rounded border border-white/30 px-3 py-1 hover:bg-white/10">
                关闭（Esc）
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

/** 「执行中… N 秒」的秒表：只有这一小块自己每秒醒一次。
 *  以前是整条轨迹每秒重渲染一遍来驱动它——长 run 跑到几百行时，
 *  每秒把几百行全部重算+重排，跑着跑着界面就开始掉帧。 */
function WaitBadge({ at, stalled, waitingYou }: {
  at: string; stalled: boolean; waitingYou: boolean;
}) {
  const counting = !stalled && !waitingYou;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!counting) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [counting]);
  const waited = counting ? Math.max(0, Math.round((now - parseTs(at)) / 1000)) : 0;
  const slow = waited >= 60;
  return (
    <span
      className={`ml-1 text-[11px] font-normal ${
        stalled ? "text-slate-400" : slow ? "text-rose-600" : "text-amber-600"}`}
      title={
        stalled
          ? "任务已经停止，这条没等到结果。注意：同一轮并行的多个工具调用，结果要等最慢那条"
          + "跑完才一起落库——像「查看目录」这种毫秒级的调用其实早就执行完了，只是当时被"
          + "「生成图片」拖在同一批里，任务一停就再没有结果记录。"
          : waitingYou
          ? "助手停在这条提问上，正在等你回答"
          : slow
          ? "已经等了一分钟以上。递归扫描（Get-ChildItem -Recurse 等）会被搜索时限终止；找文件用 glob、找内容用 grep"
          : "正在等这条命令跑完"
      }
    >
      {stalled ? "· 无返回"
        : waitingYou ? "· 等你回答"
        : `执行中…${waited >= 2 ? ` ${fmtElapsed(waited)}` : ""}`}
    </span>
  );
}

function ThinkRowBase({ agent, text }: { agent: string; text: string }) {
  const [open, setOpen] = useState(false);
  const who = ROLE_LABEL[agent] || agent;
  // 长思考折叠：摘要只显示前 200 字，避免一行把整段思考全铺开；细节点开再看全文。
  const THINK_COLLAPSE = 200;
  const long = text.length > THINK_COLLAPSE * 1.6;
  return (
    <div>
      <Line
        glyph="✦"
        glyphClass="text-violet-400"
        lead={<RoleTag agent={agent} />}
        onToggle={() => setOpen((o) => !o)}
      >
        <span className="font-medium text-violet-700">思考</span>
        <span className="text-slate-400"> · </span>
        <span className="text-slate-600">{oneLine(text, long ? THINK_COLLAPSE : 600)}</span>
      </Line>
      {open && (
        <Detail tone="think">
          <div className="mb-1 flex items-center gap-2">
            {who && <div className="text-[11px] font-medium text-violet-700">{who}的思考</div>}
            <CopyButton
              get={() => text}
              label="复制思考"
              title="复制这段思考过程的文字"
              className="ml-auto text-[11px] text-violet-500 hover:text-violet-800"
            />
          </div>
          <div className="max-h-72 overflow-y-auto">
            <Markdown text={text} />
          </div>
        </Detail>
      )}
    </div>
  );
}

/** 思考行只吃两个字符串，父级每批事件重建行数组时不必跟着重渲染 */
const ThinkRow = memo(ThinkRowBase);

function ToolRowBase({ agent, step, runId, count, at, live, awaiting, interimReport }: {
  agent: string; step: ToolStep; runId: string; count?: number; at?: string;
  /** 这条 run 还在跑吗？只有还在跑才给「执行中…」配秒数计时 */
  live?: boolean;
  /** run 停在「向你提问」上（status=blocked）：ask_user 没结果是在等回答，不是中断 */
  awaiting?: boolean;
  /** report_to_user 之后还有动作：这是阶段汇报，不是最终交付 */
  interimReport?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const verb = step.tool === "report_to_user" && interimReport ? "阶段汇报" : (TOOL_VERB[step.tool] || step.tool);
  const b = oneLine(brief(step.tool, step.args), 400);
  const pending = step.result === undefined;
  // 任务已终止而这条没等到结果：它不会再有结果了，标成「无返回」而不是「执行中」
  const dead = pending && live === false;
  // 组长停在「向你提问」上时，ask_user 没有结果是在等你回答，不是中断
  const waitingYou = dead && awaiting === true && step.tool === "ask_user";
  const stalled = dead && !waitingYou;
  const bad = failedResult(step.result);
  const who = ROLE_LABEL[agent] || agent;
  const hasDetail = true;
  // 写文件类工具：先算好前后差异，收起态就能看到「+a −b」
  const dp = useMemo(() => diffFromToolArgs(step.tool, step.args), [step.tool, step.args]);
  const rows = useMemo(() => (dp ? lineDiff(dp.before, dp.after) : []), [dp]);
  const st = useMemo(() => diffStats(rows), [rows]);
  // 联网工具：返回里带一段结构化来源清单，剥出来单独渲染成可点击芯片
  const isWeb = step.tool === "web_search" || step.tool === "web_fetch";
  const parsed = useMemo(() => (isWeb ? parseSources(step.result) : { body: step.result || "", sources: [] }),
    [isWeb, step.result]);
  return (
    <div>
      <Line
        glyph={toolGlyph(step.tool)}
        glyphClass={
          stalled ? "text-slate-300"
            : pending ? "text-amber-500"
            : bad ? "text-red-500"
            : "text-slate-400"
        }
        lead={<RoleTag agent={agent} />}
        mark={stalled ? undefined : pending ? "pending" : bad ? "fail" : "ok"}
        onToggle={hasDetail ? () => setOpen((o) => !o) : undefined}
        trail={
          <>
            {count && count > 1 ? (
              <span
                className="ml-1 rounded bg-rose-50 px-1 text-[11px] font-medium text-rose-600 ring-1 ring-rose-200"
                title={`同一条命令连续执行了 ${count} 次（通常是超时重试或反复试探环境）`}
              >
                ×{count}
              </span>
            ) : null}
            {/* 每条命令各自的开始时刻 + 执行耗时：用户要能逐条看清"这条几点跑的、跑了多久"。
                放在 trail（shrink-0）里而不是主文本里——命令很长时主文本会被 truncate，
                时间和耗时若跟着排在里面就一起被截没了（用户截图：13:30:09 那行的时刻看不见）。
                耗时来自后端 tool_result.duration_ms（真实计时，非按事件时间戳估算）；
                老数据没有精确值时按事件时间戳估算（秒级），只在 ≥1s 时显示并标 ≈。 */}
            {localClock(step.at || at) && (
              <span className="ml-1 text-[11px] text-slate-400 mono"
                title="这条命令开始执行的时间">
                {localClock(step.at || at)}
              </span>
            )}
            {!pending && typeof step.durationMs === "number"
              && (!step.durationEstimated || step.durationMs >= 1000) && (
              <span className="ml-1 text-[11px] text-slate-400 mono"
                title={step.durationEstimated ? "按事件时间戳估算的耗时（秒级）" : "这条命令的执行耗时"}>
                {step.durationEstimated ? "≈" : ""}{fmtDuration(step.durationMs)}
              </span>
            )}
            {pending && at !== undefined && (
              <WaitBadge at={at} stalled={stalled} waitingYou={waitingYou} />
            )}
          </>
        }
      >
        <span className={bad ? "font-medium text-red-600" : "font-medium text-slate-700"}>{verb}</span>
        {/* 改动行数紧跟在「编辑」动词后面，而不是甩到整行最右端：
            它属于这次编辑本身，读的时候要能「编辑 +3 −0 · 文件路径」连起来看
            （用户截图 2026-11-02：+3 −0 被排到了命令后面、贴着时间和耗时）。 */}
        {dp && (
          <span className="ml-1 shrink-0 mono">
            <span className="text-emerald-600">+{st.added}</span>
            <span className="ml-0.5 text-rose-500">−{st.deleted}</span>
          </span>
        )}
        {b && (
          <>
            <span className="text-slate-400"> · </span>
            <span className="mono text-slate-600">{b}</span>
          </>
        )}
      </Line>
      {/* 截图/回灌图：直接给缩略图，点开看大图——用户要能核对「模型到底看见了什么」 */}
      <ShotThumbs runId={runId} text={step.result} />
      {open && (
        <Detail tone="tool">
          {who && <div className="mb-1 text-[11px] font-medium text-slate-600">{who}</div>}
          {dp && (
            <div className="mb-1">
              <DiffView rows={rows} title={dp.path} maxHeight={340} />
            </div>
          )}
          {step.result !== undefined && (
            <pre
              className={`mono max-h-60 overflow-auto whitespace-pre-wrap break-all rounded bg-white/70 p-1.5 text-[11.5px] ${
                bad ? "text-red-600" : "text-slate-700"
              }`}
            >
              {(isWeb ? parsed.body : step.result) || "（无输出）"}
            </pre>
          )}
          {isWeb && parsed.sources.length > 0 && <SourceChips sources={parsed.sources} />}
          {step.args && Object.keys(step.args).length > 0 && (
            <>
              <div className="mt-1 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setRaw((v) => !v)}
                  className="text-[10.5px] text-slate-400 hover:text-slate-600"
                >
                  {raw ? "收起参数" : "查看参数 JSON"}
                </button>
                <CopyButton
                  get={() => JSON.stringify(step.args, null, 2)}
                  label="复制参数"
                  title="复制这次工具调用的完整参数 JSON"
                  className="text-[10.5px] text-slate-400 hover:text-slate-600"
                />
              </div>
              {raw && (
                <pre className="mono mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-all rounded bg-white/70 p-1.5 text-[11.5px] text-slate-700">
                  {JSON.stringify(step.args, null, 2)}
                </pre>
              )}
            </>
          )}
        </Detail>
      )}
    </div>
  );
}

/** 工具行按内容比较：buildRows 每来一批事件就会把整段行对象重建一遍，
 *  逐字段比才能真的跳过没变的行（长 run 里这就是「事件一到全页重算」的主因）。
 *  args 直接引用事件载荷里的同一个对象，比引用就够了。 */
const ToolRow = memo(ToolRowBase, (a, b) =>
  a.agent === b.agent && a.runId === b.runId && a.count === b.count && a.at === b.at &&
  a.live === b.live && a.awaiting === b.awaiting &&
  a.step.tool === b.step.tool && a.step.args === b.step.args &&
  a.step.result === b.step.result && a.step.at === b.step.at &&
  a.step.durationMs === b.step.durationMs && a.step.durationEstimated === b.step.durationEstimated);

/** 实时思考：一行显示最新输出，点开看全文并自动跟随。 */
function LiveRowBase({ text, agent = "" }: { text: string; agent?: string }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text, open]);
  const tail = oneLine(text.slice(-400), 170);
  return (
    <div>
      <Line
        glyph="✦"
        glyphClass="text-violet-500"
        lead={<RoleTag agent={agent} />}
        onToggle={() => setOpen((o) => !o)}
        mark="pending"
      >
        <span className="font-medium text-violet-700">思考中</span>
        <span className="text-slate-400"> · </span>
        <span className="text-slate-600">{tail}</span>
      </Line>
      {open && (
        <Detail tone="think">
          <div className="mb-1 flex items-center">
            <CopyButton
              get={() => text}
              label="复制思考"
              title="复制目前为止这段思考的文字（仍在生成，复制到手的是已到达的部分）"
              className="ml-auto text-[11px] text-violet-500 hover:text-violet-800"
            />
          </div>
          <div ref={boxRef} className="max-h-72 overflow-y-auto whitespace-pre-wrap">
            {text}
            <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-violet-400 align-middle" />
          </div>
        </Detail>
      )}
    </div>
  );
}

// 流式思考行每秒都在长字，父组件其它部分不再陪它重渲染
const LiveRow = memo(LiveRowBase);

/* ---------------- 进度条：批量出图（art_progress 事件驱动） ---------------- */

type ArtProg = {
  total: number; done: number; current: string;
  real: number; placeholder: number; failed: number;
  phase: string; note?: string; error?: string; media?: string;
  /** 这条进度事件发生时间：用来把它挂回真正属于它的那一批工具行 */
  at?: string;
};

/** 进度事件是否落在某次媒体工具的产物时间窗里（同一批才开始、下一批之前结束）。 */
function inMediaWin(at: string | undefined, w: MediaWin): boolean {
  if (!at) return true; // 老事件没带时间时保持旧行为
  const t = parseTs(at);
  const from = w.from ? parseTs(w.from) : Number.NEGATIVE_INFINITY;
  const to = w.to ? parseTs(w.to) : Number.POSITIVE_INFINITY;
  return t >= from && t < to;
}

function ArtProgressBar({ prog, running }: { prog: ArtProg; running: boolean }) {
  const pct = prog.total > 0 ? Math.min(100, Math.round((prog.done / prog.total) * 100)) : 0;
  const finished = prog.phase === "batch_done";
  const active = running && !finished;
  const isAudio = prog.media === "audio";
  const isVideo = prog.media === "video";
  // 当前这张已出的秒数：一张大图要 3~6 分钟，没有计时的话「正在出 xxx」看着像卡死
  const [curSince, setCurSince] = useState(0);
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    setCurSince(prog.current ? Date.now() : 0);
  }, [prog.current]);
  useEffect(() => {
    if (!active || !prog.current) return;
    const t = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active, prog.current]);
  const curSec = active && curSince ? Math.max(0, Math.round((nowTick - curSince) / 1000)) : 0;
  const curText = `${Math.floor(curSec / 60)}:${String(curSec % 60).padStart(2, "0")}`;
  return (
    <div className="my-1 rounded border border-slate-200 bg-white/70 px-2.5 py-2">
      <div className="flex items-center gap-2 text-[12.5px] leading-5">
        <span className={active ? "font-medium text-slate-800" : "font-medium text-slate-600"}>
          {isVideo ? `🎬 生成视频 ${prog.done}/${prog.total}`
            : isAudio ? `🔊 配音出音 ${prog.done}/${prog.total}`
            : `🎨 美术出图 ${prog.done}/${prog.total}`}
        </span>
        {active && prog.current && (
          <span className="mono truncate text-slate-500" title={prog.current}>
            {isVideo ? "正在出片 " : isAudio ? "正在念 " : "正在出 "}{prog.current}
            {curSec > 0 && <span className="ml-1 text-slate-400">{curText}</span>}
          </span>
        )}
        {active && !prog.current && prog.note && (
          <span className="truncate text-amber-700">{prog.note}</span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-2 text-[11px]">
          {(prog.real > 0 || finished) && <span className="text-emerald-600">真实 {prog.real}</span>}
          {(prog.placeholder > 0 || finished) && (
            <span className={prog.placeholder > 0 ? "text-amber-600" : "text-slate-400"}>
              占位 {prog.placeholder}
            </span>
          )}
          {prog.failed > 0 && <span className="text-red-500">失败 {prog.failed}</span>}
          {active && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />}
        </span>
      </div>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            finished && prog.placeholder > 0 && prog.real === 0
              ? "bg-amber-400"
              : finished
              ? "bg-emerald-500"
              : "bg-brand-500"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {prog.error && (
        <div className="mt-1 text-[11px] text-rose-500" title={prog.error}>
          最近一次失败：{oneLine(prog.error, 160)}
        </div>
      )}
    </div>
  );
}

/* ---------------- 实时素材条：生成一张就显示一张（artifact 事件驱动） ---------------- */

function LiveArtifacts({ runId, media, from, to, collapse = false }: {
  runId: string; media?: "image" | "audio" | "video"; from?: string; to?: string; collapse?: boolean;
}) {
  const [arts, setArts] = useState<any[] | null>(null);
  const [broken, setBroken] = useState<Record<string, boolean>>({});
  const [zoom, setZoom] = useState<any | null>(null);
  // 收起态（历史 run）默认只铺一屏缩略图，避免十几条历史 run 各自把几十上百张图全挂上来
  const [expand, setExpand] = useState(false);
  const fetchRef = useRef<(() => void) | null>(null);

  const fetchArts = () => {
    api
      .artifacts({ run_id: runId })
      .then((rows: any[]) => {
        let list = Array.isArray(rows) ? rows : [];
        if (from || to) {
          // 时间窗：同一次 run 第二次生成时，产物各归各行——
          // 第一次生成的挂第一次的行，重生成的新产物挂最后一次的行，
          // 否则旧产物全堆在最新一行下面，看起来像「串了跑」（2026-10-29 用户标注）。
          const f = from ? parseTs(from) : Number.NEGATIVE_INFINITY;
          const t = to ? parseTs(to) : Number.POSITIVE_INFINITY;
          list = list.filter((a) => {
            const ts = a?.created_at ? parseTs(a.created_at) : 0;
            return ts >= f && ts < t;
          });
        }
        list = [...list].sort((x, y) =>
          (x?.created_at || "") < (y?.created_at || "") ? -1 : 1);
        setArts(list);
      })
      .catch(() => {});
  };
  fetchRef.current = fetchArts;

  useEffect(() => {
    fetchArts();
    function onEv(e: Event) {
      const ev = (e as CustomEvent).detail as { type?: string };
      if (ev?.type === "artifact") {
        // 出完一张立刻拉一次（事件会连发，交给浏览器合并也没问题，量小）
        fetchRef.current?.();
      }
    }
    window.addEventListener(`ga-run-ev-${runId}`, onEv);
    return () => window.removeEventListener(`ga-run-ev-${runId}`, onEv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (!zoom) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") setZoom(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);

  if (!arts || arts.length === 0) return null;
  // media 分流：图只挂在「生成图片」行下面，音频只挂在「生成音频」行下面，视频只挂在「生成视频」行下面。
  // 以前不分流、整块永远钉在轨迹最末尾——出完 20 张图还得滚到所有思考/搜索行底下才看见。
  const isImageKind = (k: string) => k === "image" || k === "icon";
  const images = media === "audio" || media === "video"
    ? [] : arts.filter((a) => isImageKind(a.asset_kind));
  // 占位音频是静音 wav：以前直接过滤掉，用户只看到「什么都没有」，无法判断是没生成还是没声音。
  // 现在保留并打「静音占位」红标（title 说明原因），真实音频正常展示；
  // 音效/BGM 由内置合成器产出时 provider=synth，is_placeholder=0，正常走播放器。
  const videos = media === "video" ? arts.filter((a) => a.asset_kind === "video") : [];
  const audios = media === "image" || media === "video"
    ? [] : arts.filter((a) => !isImageKind(a.asset_kind) && a.asset_kind !== "video");
  if (images.length === 0 && audios.length === 0 && videos.length === 0) return null;
  // 收起态（历史 run）：默认最多铺 CAP 张，避免一次挂几百个 <img>
  const CAP = 12;
  const limited = collapse && !expand && images.length > CAP;
  const shownImages = limited ? images.slice(0, CAP) : images;
  return (
    <div className="my-1 space-y-1.5">
      {shownImages.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {shownImages.map((a) => (
            <div key={a.id} className="relative w-36">
              {broken[a.id] ? (
                <div className="flex h-24 w-36 items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 px-2 text-center text-[10px] text-slate-400">
                  文件已移动/删除
                </div>
              ) : (
                <img
                  src={api.artifactThumb(a.id, 320, a.size)}
                  alt={a.slug}
                  loading="lazy"
                  decoding="async"
                  title={`${a.rel_path}（点击看大图）`}
                  className="h-24 w-36 rounded-lg border border-slate-200 object-contain cursor-zoom-in bg-slate-50"
                  onClick={() => setZoom(a)}
                  onError={() => setBroken((b) => ({ ...b, [a.id]: true }))}
                />
              )}
              {a.is_placeholder && (
                <span className="absolute left-1 top-1 text-[10px] px-1 py-0.5 rounded bg-slate-700/70 text-white">
                  占位
                </span>
              )}
              {!collapse && (
                <div className="w-36 truncate text-[10px] text-slate-400" title={a.rel_path}>{a.rel_path}</div>
              )}
            </div>
          ))}
          {limited && (
            <button
              type="button"
              onClick={() => setExpand(true)}
              className="flex h-24 w-36 flex-col items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 text-[11px] text-slate-500 hover:border-brand-400 hover:text-brand-600"
              title="展开这条历史 run 的全部素材"
            >
              还有 {images.length - CAP} 张
              <span className="text-[10px] text-slate-400">点此展开</span>
            </button>
          )}
        </div>
      )}
      {videos.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {videos.map((a) => (
            <div key={a.id} className="relative w-[320px]">
              <video
                controls
                preload="metadata"
                src={api.artifactFile(a.id, a.size)}
                title={`${a.rel_path}（首帧静态图；点播放看动态）`}
                className="h-40 w-[320px] rounded-lg border border-slate-200 bg-black object-contain"
              />
              {a.is_placeholder && (
                <span className="absolute left-1 top-1 text-[10px] px-1 py-0.5 rounded bg-slate-700/70 text-white">
                  占位
                </span>
              )}
              <div className="w-[320px] truncate text-[10px] text-slate-400" title={a.rel_path}>
                {a.rel_path}
              </div>
            </div>
          ))}
        </div>
      )}
      {audios.map((a) => {
        // 配音产物显示「念的台词」而不是文件路径（用户反馈：一堆 .mp3 路径看不出哪句是哪句）。
        // 音效/BGM 没有台词（params 里没有 text）时退回显示路径。
        const say = String(a.text || "").trim();
        const label = say || a.rel_path || a.slug;
        return (
          <div key={a.id} className="flex items-center gap-2 text-xs text-slate-500">
            <audio controls preload="none" src={api.artifactFile(a.id)} className="h-8 max-w-[260px]" />
            {a.is_placeholder && (
              <span
                className="shrink-0 rounded bg-rose-50 px-1.5 py-0.5 text-[10px] text-rose-600"
                title="这是静音占位文件（真实配音生成失败后的回退产物），不是可听的音频"
              >
                静音占位
              </span>
            )}
            <span
              className={`min-w-0 flex-1 truncate ${say ? "text-slate-700" : "text-slate-500"}`}
              title={say ? `${say}\n${a.rel_path}` : a.rel_path}
            >
              {oneLine(label, say ? 80 : 100)}
            </span>
          </div>
        );
      })}
      {zoom &&
        createPortal(
        <div
          className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/80 p-6"
          onClick={() => setZoom(null)}
        >
          <img
            src={api.artifactFile(zoom.id, zoom.size)}
            alt={zoom.slug}
            className="max-h-[86vh] max-w-[92vw] rounded object-contain shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
          <div className="mt-3 text-xs text-white/80" onClick={(e) => e.stopPropagation()}>
            {zoom.rel_path} ·{" "}
            <a href={api.artifactFile(zoom.id, zoom.size)} target="_blank" rel="noreferrer" className="underline">
              原图
            </a>{" "}
            · 按 Esc 关闭
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

/* ---------------- 主组件 ---------------- */

/** 展开后一屏最多渲染多少行：一条长 run 能有几百上千行，全塞进 DOM 的话点开那一下也卡。
 *  插话行不参与开窗——用户说的话不能被窗口藏到「更早」里去。 */
const ROW_WINDOW = 240;
const EMPTY_STEERS: SteerNote[] = [];

export default function RunTraceInline({
  runId, steers = EMPTY_STEERS, stat, statPending,
}: {
  runId: string;
  steers?: SteerNote[];
  /** 该 run 的现成摘要（会话 run 列表顺带算好的）：收起态靠它做到「一条事件不拉也有完整一行」 */
  stat?: RunStat;
  /** 会话 run 列表还没回来：先别急着拉事件，免得给一条早就跑完的 run 白拉 1.5MB */
  statPending?: boolean;
}) {
  const [events, setEvents] = useState<Ev[]>([]);
  // 实时思考：按「轮」缓存。后端 agent_thinking_delta 带 round（第几轮 LLM 往返），
  // 轮次变了就整段换掉——否则新一轮还没吐出增量时，行里会残留上一轮的文本。
  const [live, setLive] = useState<{ round: number; agent: string; text: string }>(
    { round: Number.NaN, agent: "", text: "" });
  const [elapsedBase, setElapsedBase] = useState<{ sec: number; at: number }>({ sec: 0, at: 0 });
  // 一个 status 串取代 failed/cancelled/awaiting/running 四个布尔：
  // 收起时只有 stat.status、展开后以 runDetail 为准，两种来源走同一套状态词判断，
  // 不会出现「收起写执行完毕、点开写已中断」这种自己打自己脸的数。
  const [liveStatus, setLiveStatus] = useState<string | null>(null);
  // 「等待模型」心跳：后端在模型处理期间每 ~10s 推一条 ephemeral status（带 waiting=true +
  // 已等待秒数），让用户看到模型还在跑，而不是「一直不动」。收到任何落库事件即清空。
  const [liveNote, setLiveNote] = useState<string>("");
  // 本次执行开始时刻（run.created_at）：摘要头里显示「HH:MM:SS 开始」，
  // 让用户一眼知道这条是什么时候跑起来的（历史会话、并行多条时尤其有用）。
  // 展开态从 runDetail 拿；收起态直接用 run 列表里的 stat.created_at。
  const [startIso, setStartIso] = useState<string | null>(null);
  const status = liveStatus ?? stat?.status ?? null;
  const running = status ? !TERMINAL.has(status) : true;
  // 组长停在「向你提问」上：这时 ask_user 没有结果不是中断，是在等用户回答
  const awaiting = status === "blocked";
  // 展开状态：null=跟着默认（已经跑完的收起、还在跑的展开），用户点过就以用户为准
  const [openOverride, setOpenOverride] = useState<boolean | null>(null);
  // 本次挂载期间这条 run 真的跑过 → 它结束时不要当着用户的面突然收起
  const wasLiveRef = useRef(false);
  // 敢收起的前提是摘要数字已经在手（否则收起态那一行就没话可说，还不如老老实实展开）
  const collapsible = !!stat?.activity && !!status && TERMINAL.has(status) && !wasLiveRef.current;
  const open = openOverride ?? !collapsible;
  const [showOlder, setShowOlder] = useState(false);
  const [graceExpired, setGraceExpired] = useState(false);
  const eventsRef = useRef<Ev[]>([]);
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);
  const fetchRef = useRef<(() => Promise<void>) | null>(null);

  // run 列表迟迟没到位（接口失败/老后端没有这个字段）也不能把轨迹憋没：宽限 3s 后照常拉
  useEffect(() => {
    if (!statPending || stat || graceExpired) return;
    const t = window.setTimeout(() => setGraceExpired(true), 3000);
    return () => clearTimeout(t);
  }, [statPending, !!stat, graceExpired]);

  // 只在「该拉」的时候拉：收起的历史 run 一条事件都不拉，点开才拉
  const mayFetch = open && (!statPending || !!stat || graceExpired);

  // 一次性加载 + ws 事件驱动的增量拉取（无定时轮询）
  useEffect(() => {
    if (!mayFetch) return;
    let alive = true;
    async function fetchOnce() {
      try {
        const lastSeq = eventsRef.current.length ? eventsRef.current[eventsRef.current.length - 1].seq : 0;
        const [evs, r] = await Promise.all([
          api.events(runId, lastSeq) as Promise<any[]>,
          api.runDetail(runId) as Promise<any>,
        ]);
        if (!alive) return;
        if (evs.length) {
          setEvents((prev) => {
            const known = new Set(prev.map((e) => e.seq));
            const fresh = evs.filter((e) => !known.has(e.seq)).map(mapEv);
            return [...prev, ...fresh].sort((a, b) => a.seq - b.seq);
          });
        }
        // 后端时间为 UTC，parseTs 统一归一化，避免差 8 小时
        const t0 = parseTs(r.created_at);
        const t1 = r.finished_at ? parseTs(r.finished_at) : Date.now();
        if (!Number.isNaN(t0) && !Number.isNaN(t1)) {
          setElapsedBase({ sec: Math.round((t1 - t0) / 1000), at: Date.now() });
        }
        if (!TERMINAL.has(r.status)) wasLiveRef.current = true;
        setStartIso(r.created_at || null);
        setLiveStatus(r.status);
      } catch {
        /* 忽略：等下一个 ws 事件触发重试 */
      }
    }
    fetchRef.current = fetchOnce;
    fetchOnce();
    return () => {
      alive = false;
      fetchRef.current = null;
    };
  }, [runId, mayFetch]);

  // 实时事件（Chat 页 ws 广播）：流式思考增量直达；工具/整轮事件到达即拉取一次
  useEffect(() => {
    let timer: number | undefined;
    function onEv(e: Event) {
      const ev = (e as CustomEvent).detail as { type?: string; payload?: any };
      if (!ev?.type) return;
      if (ev.type === "agent_thinking_delta") {
        const p = ev.payload || {};
        const rnd = Number(p.round ?? 0);
        const who = String(p.agent_key || p.agent || "");
        const add = String(p.text || "");
        setLiveNote("");  // 真的在吐思考增量了，撤掉「等待模型」心跳
        setLive((cur) => (cur.round === rnd
          ? { round: rnd, agent: who || cur.agent, text: cur.text + add }
          : { round: rnd, agent: who || cur.agent, text: add }));  // 换轮：丢掉上一轮残留
        return;
      }
      if (ev.type === "status") {
        const p = ev.payload || {};
        // 等待模型心跳：只更新提示行，不触发整表重拉（它 10s 一条，重拉会白拉事件流）
        if (p.waiting) {
          setLiveNote(String(p.note || ""));
          return;
        }
      }
      // 落库事件到达：立刻拉一次事件流。这里**不要**先把实时文本清空——
      // 清了之后到事件真正落进列表之间会空一拍，视觉上就是「思考完闪一下再出现下面的行」。
      // 实时行什么时候让位，交给渲染层按「本轮是否已落库」判断（见 liveCovered）。
      setLiveNote("");
      if (timer) clearTimeout(timer);
      timer = window.setTimeout(() => {
        fetchRef.current?.();
      }, 120);
    }
    window.addEventListener(`ga-run-ev-${runId}`, onEv);
    return () => {
      window.removeEventListener(`ga-run-ev-${runId}`, onEv);
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  // WS 丢事件兜底：还在跑的时候每 3s 补拉一次事件流。
  // 以前完全依赖 WS——连接半断（浏览器不触发 close）时轨迹会停住不动，
  // 用户看到的就是「对话一直不刷新，很久才更新」。
  useEffect(() => {
    if (!running || !mayFetch) return;
    const t = window.setInterval(() => { fetchRef.current?.(); }, 3000);
    return () => window.clearInterval(t);
  }, [running, mayFetch]);

  // 只有「还在跑」才在基准时长上叠加本地时钟；已终态必须冻结在基准值，
  // 否则「执行完毕」之后用时还会一直往上涨（后端 finished_at 已给出最终时长）。
  // 收起的历史 run 从没拉过 runDetail，用 run 列表里的起止时间算同一个数。
  const statSec = stat?.created_at && stat?.finished_at
    ? Math.max(0, Math.round((parseTs(stat.finished_at) - parseTs(stat.created_at)) / 1000))
    : 0;
  const baseSec = elapsedBase.sec || statSec;
  const rows = useMemo(() => buildRows(events, steers), [events, steers]);
  // 批量出图进度（art_progress 事件，取最新一条）——轨迹行里只看得到"生成图片 执行中"，
  // 细粒度的第几张/真实还是占位全靠这条事件驱动进度条。
  // 渲染位置：紧贴在「生成图片」那一行下面（用户实测反馈：浮在轨迹顶部不对，要跟着工具行走）。
  const progs = useMemo<Record<string, ArtProg>>(() => {
    const out: Record<string, ArtProg> = {};
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.type !== "art_progress" || !e.payload) continue;
      const p = e.payload;
      // 老事件没有 media 字段 → 一律按美术处理（历史 run 的进度条不能因此消失）
      const media = p.media === "audio" ? "audio" : p.media === "video" ? "video" : "image";
      if (out[media]) continue; // 每种只取最新一条
      out[media] = {
        total: Number(p.total || 0), done: Number(p.done || 0),
        current: String(p.current || ""), phase: String(p.phase || ""),
        real: Number(p.real || 0), placeholder: Number(p.placeholder || 0),
        failed: Number(p.failed || 0), note: p.note, error: p.error, media,
        at: e.created_at,
      };
    }
    return out;
  }, [events]);
  // 进度条与素材网格该插到哪一行后面：对应的媒体工具行。
  // 只按行找、不依赖进度事件——单张生成不发 art_progress，
  // 用事件反查会让素材网格整个不显示。
  // 兼容三代工具名：直连 generate_image/generate_audio（现行）、旧派发 dispatch_*、更早的单条 tts。
  // 同一 run 有多次生成时，素材按时间窗各归各行（window = 本行开始 → 下一次媒体行开始）：
  // 重生成只把新产物挂在自己这行，不把第一次的旧产物一起拽下来（2026-10-29 用户标注）。
  // 索引按行 key（不是行号）：展开时只渲染最近一段，行号会跟全量对不上。
  const mediaWindows = useMemo<Record<string, Map<string, MediaWin>>>(() => {
    const want: Record<string, string[]> = {
      image: ["generate_image", "dispatch_art"],
      audio: ["generate_audio", "dispatch_audio", "tts"],
      video: ["generate_video"],
    };
    const out: Record<string, Map<string, MediaWin>> = {};
    Object.entries(want).forEach(([m, tools]) => {
      const hits = rows.filter(
        (r): r is Extract<Row, { kind: "tool" }> =>
          r.kind === "tool" && tools.includes(r.step.tool),
      );
      if (!hits.length) return;
      const atOf = (i: number) => hits[i].at;
      const map = new Map<string, MediaWin>();
      hits.forEach((r, k) => map.set(r.key, {
        // 本行只认「本行开始 → 下一次媒体行开始」这段里产出的素材。
        // 之前 from 取的是上一行开始、to 取的是下一行开始，窗口跨了两行，
        // 结果每一批图都会在「自己这行」和「下一行」各渲染一次（用户截图红框：
        // 上一批 3 张又被下一行拖下来一遍）。首行 from 留空只为了兜住首行之前
        // 就已经存在的素材（上一次会话复用/上传），不会影响后续行的归属。
        from: k === 0 ? undefined : atOf(k),
        to: k < hits.length - 1 ? atOf(k + 1) : undefined,
        latest: k === hits.length - 1,
      }));
      out[m] = map;
    });
    return out;
  }, [rows]);
  // 实时思考只显示「当前这一轮」的流式增量（后端按 round 标记）：
  // 一旦这一轮（或更新的轮次）已经落库到列表里，实时行就立刻让位——
  // 时间点上与落库那行重合，不会出现「先消失、下一拍再出现」的闪动。
  const liveCovered = rows.some((r) =>
    r.kind === "think" ? r.round >= live.round || r.round === live.round : r.round >= live.round,
  );
  const liveText = running && !liveCovered ? live.text : "";
  // 最近一次端点重试提到标题行：收起轨迹也能看到「重试模型请求 (n/5)」，不必展开找。
  // 注意：所有 Hook 必须写在下面的早退 return 之前——否则「先返回 null、下一拍再算这个
  // useMemo」会让本组件两次渲染的 Hook 数量不一致，React 直接抛 #310 把整棵树卸载（黑屏）。
  const lastRetry = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === "llm_retry") return events[i].payload || null;
    }
    return null;
  }, [events]);
  // 等待模型心跳只在「还在跑」时显示；一落库/一结束就撤。
  const waitNote = running && liveNote ? liveNote : "";
  // 一行都还没有：什么都没发生过就别亮一行空的「执行中」（老逻辑）。
  // 但收起的历史 run 是「有 stat、故意没拉事件」，它必须把这行摘要留住。
  const fetched = liveStatus !== null;
  if (fetched ? (!events.length && !liveText && !running) : !stat) return null;

  // 开窗：只渲染最近 ROW_WINDOW 行，更早的折起来（几百行一次性挂 DOM 的话，
  // 点开一条长 run 同样会卡一下）。注意不要把早于窗口的插话强行拽进可视区——
  // 那样「很早以前的一句插话」会排在一大串最近动作的最上面，看起来就是它跑到了前面。
  // 被窗口藏住的插话会在「显示更早的 N 行」里，点开即可看到；收起态另有单独渲染。
  const winStart = rows.length > ROW_WINDOW && !showOlder ? rows.length - ROW_WINDOW : -1;
  const viewRows = winStart < 0 ? rows : rows.slice(winStart);
  const hiddenRows = rows.length - viewRows.length;

  // 摘要数字：已经跑完的用它自己那份分桶（和收起态同一个数，点开不会跳变），
  // 还在跑的按事件实时数出来。
  const summary = status && TERMINAL.has(status) && stat?.activity
    ? bucketText(stat.activity)
    : actionSummary(rows);
  const agentKeys = Array.from(new Set(
    (rows.length ? rows.filter((r) => r.kind !== "steer").map((r) => r.agent) : stat?.agents || [])
      .filter(Boolean),
  ));
  const agentLabel = agentKeys.length
    ? agentKeys.map((a) => ROLE_LABEL[a] || a).join(" / ")
    : "";
  const stateWord = statusWord(status);
  // 开始时刻：展开态用 runDetail.created_at，收起态用 run 列表的 stat.created_at。
  const startClock = localClock(startIso || stat?.created_at || "");
  return (
    <div className="ga-trace mb-2 space-y-0">
      <button
        type="button"
        onClick={() => setOpenOverride(!open)}
        className="flex w-full items-center gap-1.5 rounded px-1 py-[3px] text-left text-[13px] text-slate-700 hover:bg-slate-50 hover:text-slate-900"
        title={open ? "收起这次执行的轨迹" : "展开这次执行的轨迹（点开才会拉完整事件流）"}
      >
        <span className="w-3 shrink-0 text-center text-[11px] text-slate-500">{open ? "⌄" : "›"}</span>
        {running && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-400" />}
        <span className="truncate">
          <span
            className={
              running
                ? "font-medium text-slate-800"
                : status === "failed"
                ? "font-medium text-red-600"
                : "font-medium text-slate-800"
            }
          >
            {stateWord}
          </span>
          {running && lastRetry && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="font-medium text-amber-600">
                重试模型请求（{Number(lastRetry.attempt || 0)}/{Number(lastRetry.max_attempts || 0)}）
              </span>
            </>
          )}
          {waitNote && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-amber-600">{waitNote}</span>
            </>
          )}
          {summary && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-600">{summary}</span>
            </>
          )}
          {startClock && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-600" title={`本次执行开始于 ${startClock}`}>
                {startClock} 开始
              </span>
            </>
          )}
          {baseSec > 0 && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-600">
                <RunElapsed baseSec={baseSec} startedAt={elapsedBase.at}
                  ticking={running && elapsedBase.at > 0} running={running} />
              </span>
            </>
          )}
          {agentLabel && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-600">{agentLabel}</span>
            </>
          )}
          {!open && rows.length === 0 && (
            <span className="text-slate-400"> · 点开看完整过程</span>
          )}
        </span>
      </button>
      {/* 收起态只保留上面这一行摘要：不再外挂素材网格和插话（用户实测反馈：收起后
          下方仍堆着一长串音频/图片播放器和插话气泡，既占屏、又和「收起=看个结论」相悖）。
          这些内容全部随展开恢复——素材挂在各自「生成图片/生成音频」工具行下、插话按
          发生时刻嵌回轨迹（见下方 open 分支）。收起时也就不必再为本 run 拉 /artifacts，
          历史会话里十几条收起 run 各拉一遍的请求随之省掉。 */}
      {open && (
        <div className="ml-4 mt-0.5 space-y-0">
          {hiddenRows > 0 && (
            <button
              type="button"
              onClick={() => setShowOlder(true)}
              className="w-full rounded px-1 py-[3px] text-left text-[12px] text-slate-500 hover:bg-slate-50 hover:text-slate-800"
              title="这段执行的过程很长，先只显示最近的部分；点这里把更早的也铺开"
            >
              ⌃ 显示更早的 {hiddenRows} 行（共 {rows.length} 行）
            </button>
          )}
          {viewRows.map((r) => (
            <div key={r.key || r.kind}>
              {r.kind === "think" ? (
                <ThinkRow agent={r.agent} text={r.text} />
              ) : r.kind === "compact" ? (
                <CompactRow info={r.compact} />
              ) : r.kind === "notes" ? (
                <NotesRow info={r.notes} />
              ) : r.kind === "llm_retry" ? (
                <LlmRetryRow info={r.retry} />
              ) : r.kind === "steer" ? (
                <SteerRow note={r.steer} />
              ) : (
                <ToolRow agent={r.agent} step={r.step} runId={runId}
                  count={r.count} at={r.at} live={running} awaiting={awaiting}
                  interimReport={r.interimReport} />
              )}
              {(["image", "audio", "video"] as const).map((m) => {
                const w = mediaWindows[m]?.get(r.key);
                if (!w) return null;
                return (
                  <div key={m}>
                    {/* 进度条只挂在最新一次生成的行上（旧生成行早已结束），
                        且必须真的是这一批的进度事件（否则会拿上一批的进度条挂在
                        刚起步的单张行上，文字对不上） */}
                    {w.latest && progs[m] && inMediaWin(progs[m].at, w) && (
                      <ArtProgressBar prog={progs[m]} running={running} />
                    )}
                    {/* 本 run 已生成的素材：出一张显示一张，紧跟在把它做出来的工具行下面 */}
                    <LiveArtifacts runId={runId} media={m} from={w.from} to={w.to} />
                  </div>
                );
              })}
            </div>
          ))}
          {/* 没有任何媒体工具行的素材（素材库上传等）：退回末尾显示 */}
          {!mediaWindows.image && <LiveArtifacts runId={runId} media="image" />}
          {!mediaWindows.audio && <LiveArtifacts runId={runId} media="audio" />}
          {!mediaWindows.video && <LiveArtifacts runId={runId} media="video" />}
          {/* 注意：不要在这里补「更早批次总览」。长 run 的旧媒体行跟随「显示更早的 N 行」
              一起展开即可；额外汇总会把上面的图又铺到最下面，正是用户反馈的问题。 */}
          {waitNote && (
            <div className="flex items-center gap-1.5 rounded px-1 py-[3px] text-[12px] text-amber-700">
              <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-400" />
              <span>{waitNote}</span>
            </div>
          )}
          {liveText.trim().length > 0 && (
            <LiveRow text={liveText} agent={live.agent || (agentKeys.length === 1 ? agentKeys[0] : "")} />
          )}
          {/* 改动卡片已挪到消息末尾（RunChanges 独立展示），这里不再重复 */}
        </div>
      )}
    </div>
  );
}
