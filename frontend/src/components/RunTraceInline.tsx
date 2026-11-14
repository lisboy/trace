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
  activity?: { commands: number; reads: number; writes: number; other: number; total: number; subagents?: number };
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

/** 摘要头文案：运行了 3 条命令，读取了 5 个文件，修改了 1 个文件，派了 4 个子agent 并行核对。
 *  展开态（数行）与收起态（数后端分桶）共用这段排版，收起→点开数字不会跳。 */
function bucketText(a: { commands: number; reads: number; writes: number; other: number; subagents?: number }): string {
  const parts: string[] = [];
  if (a.commands) parts.push(`运行了 ${a.commands} 条命令`);
  if (a.reads) parts.push(`读取了 ${a.reads} 个文件`);
  if (a.writes) parts.push(`修改了 ${a.writes} 个文件`);
  if (a.other) parts.push(`调用了 ${a.other} 次其它工具`);
  if (a.subagents) parts.push(`派了 ${a.subagents} 个子agent 并行核对`);
  return parts.join("，");
}

type Ev = {
  seq: number; type: string; agent?: string | null; payload?: any; created_at: string;
  /** 产生这条事件的 task（子agent 各有自己的 task_id）：前端靠它把行归进对应子agent 分组 */
  task_id?: string | null;
};

/** 子agent 分组：task_start(role=sub) 开一组，组内收该 task 的全部行，task_done 收口。
 *  渲染成一行可点击的「子agent 卡」：头像 + 名字 + 任务 + 状态 + 耗时，点开弹窗看它
 *  内部的完整执行轨迹（思考 + 每次工具调用）。 */
type SubGroup = {
  taskId: string;
  /** spawn_agents 派发时给的子任务名（如「查物料状态Map」），行标题用 */
  title: string;
  /** 派发时的问题摘要（title 缺省时显示它；弹窗里也展示完整问题） */
  question?: string;
  agent: string;
  rows: Row[];
  /** 还没收口 = 还在跑（黄点） */
  open: boolean;
  /** task_done 的终态（done/failed/cancelled/question…）；还没收口时为空 */
  status?: string;
  /** task_done 带回来的错误摘要（子agent 失败时给用户看它到底报了什么） */
  error?: string;
  /** 子agent 自己的收尾结论（task_done.summary；老 run 从 task.result_json.text 回填）：
   *  它就是交给主agent 的那段文字，用户以前在弹窗里只看得到工具流水、看不到结论。 */
  summary?: string;
  /** 是否已点过「停止」（本地在途标记，等后端 task_done(cancelled) 回来） */
  stopping?: boolean;
  /** 子agent 任务的起止时刻（task_start / task_done 的 created_at），算耗时用 */
  startedAt?: string;
  finishedAt?: string;
  key: string;
  /** 这组的编号（第几个子agent），行头显示「1」「2」… */
  index: number;
};

/** context_compacted 事件的载荷：一次全局摘要压缩的现场（合并了多少历史、摘要多大） */
type CompactInfo = {
  mergedMsgs: number; mergedTokens: number; digestTokens: number;
  before: number; after: number; window: number; budget: number;
  /** 档位没配 context_window：按 DEFAULT_CONTEXT_WINDOW 估算的窗口（压得可能偏早） */
  windowDefault?: boolean;
  /** 上一轮端点实报的 prompt_tokens：触发看的是待发估算，两者差额=上一轮后新增内容 */
  prevPromptTokens?: number;
  /** endpoint_overflow = 端点拒收（HTTP 400 超长）后按半预算重压：端点实际上限比配置窗口小 */
  source?: string;
  overflowError?: string;
};

/** notes_saved 事件的载荷：本轮收尾判断「值得记」后沉淀的项目改动经验 */
type NotesInfo = { entries: number; files: string[]; titles: string[]; reason: string };

/* llm_retry（端点 503/限流等可恢复失败后的退避重试）不再进界面：用户要求这种自动恢复
   的情况后台静默处理，别每一步都提醒出来。事件本身仍由后端照常落库（导出 md / 排查时可见）。 */

type Row =
  | { kind: "think"; agent: string; text: string; round: number; key: string; taskId?: string }
  | { kind: "compact"; agent: string; round: number; key: string; compact: CompactInfo; taskId?: string }
  | { kind: "notes"; agent: string; round: number; key: string; notes: NotesInfo; taskId?: string }
  | { kind: "steer"; agent: string; round: number; key: string; steer: SteerNote }
  | { kind: "reply"; agent: string; round: number; key: string; reply: ReplyNote }
  | {
      kind: "spawn"; agent: string; round: number; key: string; args?: Record<string, unknown>;
      /** spawn_agents 的 tool_result：成功是「已并行派出 N 个…」汇总；失败（少于3条被拒）是一条
       *  错误文本。以前不回填 → 界面只画「⇗ 派出 N 个」，看着像派了其实一个都没建。 */
      result?: string;
    }
  | {
      kind: "tool";
      agent: string;
      step: ToolStep;
      round: number;
      key: string;
      /** 产生这行的 task（子agent 分组归并键）：主 agent 没有，子agent 各有各的 */
      taskId?: string;
      /** 工具调用发起时间：用来在「执行中…」后面显示已经等了多少秒 */
      at?: string;
      /** 连续重复的同一条命令合并成一行，count = 实际执行次数 */
      count?: number;
      /** 这条 report_to_user 之后还有工具调用 = 中途进度（不是最终交付） */
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
  /** steer=用户中途插话；answer=回答了助手的提问（同一 run 内暂停后作答）。 */
  kind?: "steer" | "answer";
}

/** 主 agent 在执行中途落的一段答复（不是最终交付）：和插话一样按发生时刻嵌进轨迹，
 *  而不是作为独立气泡排在消息流末尾——它逻辑上属于这次执行的过程。
 *  interim=等子agent/插话期间的过渡答复；stage=途中交的一份完整阶段报告。 */
export interface ReplyNote {
  id: string;
  text: string;
  at: string;
  kind: "interim" | "stage";
  /** stage 专属：还有多少项没做完（前端标「还有 N 项没做完，继续执行」） */
  openCount?: number;
}

function mapEv(e: any): Ev {
  return {
    seq: e.seq,
    type: e.type,
    agent: e.agent ?? e.payload?.agent_key ?? null,
    payload: e.payload,
    task_id: e.task_id ?? null,
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
 *  key 用事件 seq（稳定），避免插入新行时按下标重建导致整段重挂载（视觉上会「闪」）。
 *  replies 是主 agent 中途落的答复（临时回复/进度），和插话一起按时间戳归位。 */
function buildRows(events: Ev[], steers: SteerNote[] = [], replies: ReplyNote[] = []): Row[] {
  const rows: Row[] = [];
  // 插话是「这一轮跑到一半说的话」：它前面的动作在上、后面的在下，整块贴到轨迹上面或
  // 下面都不对（那样刚说的话看起来像插进了过去）。按时间戳归位。
  // 中途答复同理：它发生在执行过程中，不该悬在消息流末尾。
  type Pending = { at: string; steer?: SteerNote; reply?: ReplyNote };
  const pending: Pending[] = [
    ...steers.map((s): Pending => ({ at: s.at, steer: s })),
    ...replies.map((r): Pending => ({ at: r.at, reply: r })),
  ].sort((a, b) => (a.at < b.at ? -1 : 1));
  const flush = (upto: number) => {
    while (pending.length && parseTs(pending[0].at) <= upto) {
      const p = pending.shift()!;
      if (p.steer) {
        rows.push({ kind: "steer", agent: "user", round: -1, key: `s${p.steer.id}`, steer: p.steer });
      } else if (p.reply) {
        rows.push({ kind: "reply", agent: "agent", round: -1, key: `p${p.reply.id}`, reply: p.reply });
      }
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
      // 只合并「同一 task 内」的连续思考：按 task_id 相等判，而不是 !last.taskId——
      // 主 agent 的思考现在也带 task_id，用旧条件会导致主 agent 连续思考不再合并、刷成一堆行。
      if (last && last.kind === "think" && last.agent === agent
          && (last.taskId || null) === (e.task_id || null)) {
        last.text = last.text ? `${last.text}\n\n${text}` : text;
        last.round = round;
      } else {
        rows.push({ kind: "think", agent, text, round, key: `t${e.seq}`, taskId: e.task_id || undefined });
      }
    } else if (e.type === "tool_call") {
      const tool = String(p.tool || "");
      // spawn_agents 派发行单独建行（它下面的子agent 分组都挂在它后面）。
      // 注意：早期后端主 agent 的 tool_call 不带 task_id，后来带上了——所以这里**不能**
      // 再用 `!e.task_id` 判断（否则新版后端下这根「⇗ 派出」行列永远不出现，子agent 卡片
      // 前面的派发线就丢了）。spawn_agents 只有主 agent 白名单里有，sub 调不了，直接按键名认。
      if (tool === "spawn_agents") {
        rows.push({ kind: "spawn", agent, round, key: `sp${e.seq}`, args: p.args });
        continue;
      }
      const argsKey = JSON.stringify(p.args ?? {});
      const last = rows[rows.length - 1];
      // 同一条命令连续重复（超时重试、反复试探环境）：并进上一行计数，别把轨迹刷成瀑布。
      // 只在「同一 task 内」合并（按 task_id 相等）：主 agent 现在也带 task_id，旧条件
      // !last.taskId 会失效；同时仍保证不跨 task（不把子agent 的行并进主agent 的）。
      if (
        last && last.kind === "tool" && last.agent === agent && last.round === round &&
        (last.taskId || null) === (e.task_id || null) &&
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
        taskId: e.task_id || undefined,
        step: { key: rows.length, tool, args: p.args, at: e.created_at },
      });
    } else if (e.type === "context_compacted") {
      rows.push({
        kind: "compact", agent, round, key: `k${e.seq}`,
        taskId: e.task_id || undefined,
        compact: {
          mergedMsgs: Number(p.merged_msgs || 0),
          mergedTokens: Number(p.merged_tokens || 0),
          digestTokens: Number(p.digest_tokens || 0),
          before: Number(p.before || 0), after: Number(p.after || 0),
          window: Number(p.window || 0), budget: Number(p.budget || 0),
          windowDefault: !!p.window_default,
          prevPromptTokens: Number(p.prev_prompt_tokens || 0) || undefined,
          source: p.source ? String(p.source) : undefined,
          overflowError: p.overflow_error ? String(p.overflow_error) : undefined,
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
      // 后台静默：不生成轨迹行、不弹提醒。重试会自己恢复，把 (n/5)、延迟、被 Cloudflare
      // 拦这类失败原因铺在轨迹上只会让用户以为出事了。细节仍留在事件流里备查。
    } else if (e.type === "tool_result") {
      // 结果按发生顺序回填最早那个「还没结果」的同名调用：
      // 从后往前找会让重复命令的结果错配，早先那行永远挂着黄点。
      // task_id 必须一致：主 agent 和子 agent 可能同时跑同名工具（并行 grep），
      // 跨 task 回填会把结果挂到别人的行上。
      for (const r of rows) {
        if (r.kind !== "tool" && r.kind !== "spawn") continue;
        if (r.agent !== agent || (r.kind === "tool" && r.step.tool !== String(p.tool || ""))) continue;
        if (r.kind === "spawn" && String(p.tool || "") !== "spawn_agents") continue;
        if (r.kind === "tool" && r.step.result !== undefined) continue;
        if (r.kind === "spawn" && r.result !== undefined) continue;
        if (r.kind === "spawn") {
          // spawn 行只需要结果（成功汇总 / 被拒原因）。spawn_agents 只有主 agent 能调，
          // 所以按「同名工具 + 同 agent」匹配即可，不比 task_id（老后端可能没有/带主任务 id）。
          r.result = String(p.result || "");
          break;
        }
        if ((r.taskId || null) !== (e.task_id || null)) continue;
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
  // 汇报后还有工具的 report_to_user 行 = 中途进度（模型先同步了一句又接着干）。
  // 最终的那次汇报之后没有工具调用，保持「汇报」不改。用户正是看到「✓汇报」下面
  // 还挂着一串动作才困惑，这里明确标成「进度」，不再像是已经交付完了。
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.kind !== "tool" || r.step.tool !== "report_to_user") continue;
    const hasAfter = rows.slice(i + 1).some((x) => x.kind === "tool");
    if (hasAfter) r.interimReport = true;
  }
  return rows;
}

/** 子agent 分组的归并：spawn 行之后的子 agent 行（带 taskId）折进各自的组里。
 *
 *  为什么按 task_start/task_done 事件而不是 spawn 行边界：子 agent 并行跑，
 *  主 agent 的行（更新计划、后续动作）会插在它们中间——按行边界切会把主 agent
 *  的动作错分进某个子任务。task_id 是后端落库的事实，按它归并不会错。
 *
 *  版面：同一批派出的子agent 组**紧贴 spawn 行连续排列**（按派发顺序），主agent 的行
 *  让到组块之后。以前按「每组第一行事件」的时间插入，主agent 在中间干一次活就把
 *  6 个子agent 切成 3+3 两段（用户截图：顺序混乱，看不出是一次派出的）。
 */
function groupRows(
  rows: Row[],
  events: Ev[],
  windowed = false,
  taskResults?: Record<string, string>,
): Array<Row | { kind: "subgroup"; group: SubGroup; key: string }> {
  // task_id → 组元数据（来自 task_start / task_done 事件）
  const meta = new Map<string, {
    title: string; question?: string; agent: string; startedAt?: string; status?: string;
    finishedAt?: string; error?: string; summary?: string;
  }>();
  const seenRows = new Set<string>(); // 已有行的 sub 任务（group 挂在它们中间）
  for (const e of events) {
    const p = e.payload || {};
    if (e.type === "task_start" && p.role === "sub" && p.task_id) {
      const tid = String(p.task_id);
      meta.set(tid, {
        title: String(p.title || ""),
        question: p.question ? String(p.question) : undefined,
        agent: "sub",
        startedAt: e.created_at,
      });
    } else if (e.type === "task_done" && p.task_id && p.role === "sub") {
      const tid = String(p.task_id);
      const cur = meta.get(tid) || { title: "", agent: "sub" };
      meta.set(tid, { ...cur, status: String(p.status || "done"),
                      finishedAt: e.created_at,
                      error: p.error ? String(p.error) : cur.error,
                      summary: p.summary ? String(p.summary) : cur.summary });
    }
  }
  // 老 run 的 task_done 没有 summary 字段：用 run 详情里的 task.result_json.text 回填，
  // 让历史会话也能看到每个子agent 的结论（与主agent 收到的文字同源）。
  if (taskResults) {
    for (const [tid, text] of Object.entries(taskResults)) {
      const cur = meta.get(tid);
      if (cur && !cur.summary) cur.summary = text;
    }
  }
  if (!meta.size) return rows; // 没有子 agent：原样返回，零开销
  const out: Array<Row | { kind: "subgroup"; group: SubGroup; key: string }> = [];
  const groups = new Map<string, SubGroup>();
  // 编号按派发顺序（task_start 事件的先后），而不是「谁先产出第一行」：
  // 子agent 是并行跑的，按产出排序会让编号每次刷新都可能变。
  const order = new Map<string, number>();
  for (const tid of meta.keys()) order.set(tid, order.size);
  // 当前这批「⇗ 派出」行在 out 里的位置：它的子agent 组都贴着它排
  let spawnAt = -1;
  let subsAfter = 0;
  for (const r of rows) {
    if (r.kind === "spawn") {
      out.push(r);
      spawnAt = out.length - 1;
      subsAfter = 0;
      continue;
    }
    const tid = (r.kind === "steer" || r.kind === "reply") ? undefined : r.taskId;
    const m = tid ? meta.get(tid) : undefined;
    if (m && tid) {
      let g = groups.get(tid);
      if (!g) {
        g = { taskId: tid, title: m.title, question: m.question, agent: m.agent,
              rows: [], open: !m.finishedAt, status: m.status, error: m.error,
              summary: m.summary,
              startedAt: m.startedAt, finishedAt: m.finishedAt,
              key: `g${tid}`, index: order.get(tid) ?? 0 };
        groups.set(tid, g);
        const item = { kind: "subgroup" as const, group: g, key: g.key };
        if (spawnAt >= 0) {
          // 插进本批组块里「派发顺序」对应的位置，而不是末尾——即使第 4 个先产出，
          // 也仍排在 1、2、3 之后。
          let at = spawnAt + 1;
          while (at < spawnAt + 1 + subsAfter) {
            const cur = out[at];
            if (cur.kind !== "subgroup" || cur.group.index > g.index) break;
            at++;
          }
          out.splice(at, 0, item);
          subsAfter++;
        } else {
          // 开窗切片把 spawn 行切掉了：没有锚点，退回按首次产出的位置插
          out.push(item);
        }
      }
      g.rows.push(r);
      seenRows.add(tid);
    } else {
      out.push(r);
    }
  }
  // 兜底：派出去但还没产出任何行的子agent（还在首轮思考，或一启动就失败）——以前它们
  // 因为「组头按需创建」而完全隐形，用户只看到一行「派出 N 个」却没有下文。这里把没有行
  // 的 sub 任务也补成一条独立行（放在末尾），至少能看到「N 个 · 运行中/失败」并能点开。
  // 仅在非开窗模式补：开窗切片会把这行排到最前、时间对不上，交给「显示更早的 N 行」。
  if (!windowed) {
    for (const [tid, m] of meta) {
      if (groups.has(tid) || seenRows.has(tid)) continue;
      const g: SubGroup = { taskId: tid, title: m.title, question: m.question, agent: m.agent,
        rows: [], open: !m.finishedAt, status: m.status, error: m.error,
        summary: m.summary,
        startedAt: m.startedAt, finishedAt: m.finishedAt, key: `g${tid}`,
        index: order.get(tid) ?? 0 };
      out.push({ kind: "subgroup", group: g, key: g.key });
    }
  }
  return out;
}

/** 子agent 行的头像：按序号取一个稳定的柔和渐变（不依赖后端头像资源），像截图那样
 *  一眼区分「这是哪一个子agent」。 */
const SUB_AVATAR_GRADS = [
  "from-sky-200 to-sky-400", "from-violet-200 to-violet-400",
  "from-amber-200 to-amber-400", "from-pink-200 to-pink-400",
  "from-emerald-200 to-emerald-400", "from-orange-200 to-orange-400",
  "from-rose-200 to-rose-400", "from-teal-200 to-teal-400",
];

/** 子agent 的状态文案：终态优先，否则「运行中」。 */
function subState(g: SubGroup): { text: string; tone: string; dot: string } {
  if (g.stopping) return { text: "停止中…", tone: "text-amber-600", dot: "bg-amber-400 animate-pulse" };
  if (g.open || !g.status) return { text: "运行中", tone: "text-amber-600", dot: "bg-amber-400 animate-pulse" };
  switch (g.status) {
    case "done": return { text: "已完成", tone: "text-slate-400", dot: "bg-emerald-500" };
    case "failed": return { text: "失败", tone: "text-red-600", dot: "bg-red-500" };
    case "cancelled": return { text: "已停止", tone: "text-slate-500", dot: "bg-slate-400" };
    case "question": return { text: "提问", tone: "text-sky-600", dot: "bg-sky-500" };
    default: return { text: g.status, tone: "text-slate-500", dot: "bg-slate-400" };
  }
}

/** 子agent 独立行：头像 + 名字 + 任务摘要 + 状态 + 耗时；点一行弹窗看内部执行轨迹。 */
function SubAgentRow({ group, runId, wait }: {
  group: SubGroup; runId: string;
  /** 这个子agent 当前「等模型」的秒数（心跳来自它自己的 runtime）；没有则不显示 */
  wait?: { sec: number; at: number };
}) {
  const [open, setOpen] = useState(false);
  const [stopping, setStopping] = useState(false);
  // 标题里后端自带「子agent · 」前缀：卡片本身已经用头像/状态说明了身份，
  // 这里剥掉前缀，否则会出现「子agent · xxx · 子 Agent 已完成」两遍「子agent」。
  const label = (group.title || group.question || "子agent")
    .replace(/^\s*子\s*agent\s*[·:：\-]\s*/i, "").trim() || "子agent";
  const toolRows = group.rows.filter((r) => r.kind === "tool");
  const steps = toolRows.length;
  const doneSteps = steps - toolRows.filter((r) => r.step.result === undefined).length;
  const st = subState({ ...group, stopping: stopping && group.open });
  // 心跳有 15s 保鲜期：最后一个心跳之后如果一直没新事件，宁可回到「运行中」，
  // 也不让一行「等模型 9s」永远挂着（看起来像卡死）。
  const waitLive = group.open && wait && Date.now() - wait.at < 15000 ? wait.sec : 0;
  // 行内给一句结论预览：用户不点开也能知道这个子agent 查出了什么（完整版在弹窗里）。
  // 允许更短（48 字）：状态和耗时是右对齐的硬信息，描述长了会把它们挤到裁掉。
  const preview = !group.open && group.summary
    ? oneLine(group.summary.replace(/\s+/g, " ").replace(/[#*`>]/g, "").replace(/^[·\-\s]+/, ""), 48) : "";
  const ms = group.startedAt && group.finishedAt
    ? parseTs(group.finishedAt) - parseTs(group.startedAt) : 0;
  const durText = ms > 0 ? (ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s` : `${Math.round(ms)}ms`) : "";
  const grad = SUB_AVATAR_GRADS[group.index % SUB_AVATAR_GRADS.length];
  // 只有还在跑的子agent 才给「单独停止」：停掉它不影响主agent 和别的子agent
  const canStop = group.open && !stopping;
  async function stop(e: React.MouseEvent) {
    e.stopPropagation();
    if (!canStop) return;
    setStopping(true);
    try {
      await api.cancelTask(runId, group.taskId);
    } catch {
      setStopping(false); // 失败就恢复按钮，别永久禁用
    }
  }
  return (
    <>
      <div className="group my-0.5 flex w-full items-center gap-2.5 rounded-md px-1.5 py-1.5 hover:bg-slate-50">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
          title={group.question || label}
        >
          <span className={`h-6 w-6 shrink-0 rounded-full bg-gradient-to-br ring-1 ring-black/5 ${grad}`} />
          <span className="min-w-0 flex-1 truncate text-[13px] text-slate-700">
            <span className="font-medium text-slate-800">{label}</span>
            {preview && <span className="ml-1.5 text-slate-400">· {preview}</span>}
          </span>
          {/* 状态 + 耗时合成一句「子 Agent 已完成 61s」（对齐参考稿）：
              耗时以前单独占右侧 w-10 一列，跟状态之间隔着一大片空白，
              读起来像两件不相干的事；现在贴着状态写，扫描一行就能读完。 */}
          <span className={`shrink-0 whitespace-nowrap text-[11.5px] ${waitLive ? "text-amber-600" : st.tone}`}>
            {/* 只有还在跑（或等模型）时给个点：终态一律纯文字，和参考稿一样安静。
                判据必须写成 `waitLive > 0`——直接 `group.open || waitLive` 在静止态会得到
                数字 0，React 把 0 当文本渲染出来（界面上真出现过「0子 Agent 已完成」）。 */}
            {(group.open || waitLive > 0) && (
              <span className={`mr-1 inline-block h-1.5 w-1.5 align-middle rounded-full ${
                waitLive ? "animate-pulse bg-amber-400" : st.dot}`} />
            )}
            {waitLive ? (
              <>
                子 Agent 等模型 <span className="tabular-nums">{waitLive}s</span>
              </>
            ) : (
              <>
                子 Agent {st.text}
                {st.text === "运行中" && steps > 0 ? <span className="tabular-nums"> {doneSteps}/{steps}</span> : null}
                {durText && <span className="ml-1 tabular-nums">{durText}</span>}
              </>
            )}
          </span>
        </button>
        {group.open && (
          <button
            type="button"
            onClick={stop}
            disabled={stopping}
            className="shrink-0 rounded border border-slate-200 px-1.5 py-0.5 text-[11px] text-slate-500 hover:border-rose-300 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-60"
            title={stopping ? "正在停止这个子agent…" : "只停这个子agent（主agent 和其它子agent 继续跑）"}
          >
            {stopping ? "停止中…" : "停止"}
          </button>
        )}
      </div>
      {open && <SubAgentModal group={group} runId={runId} onClose={() => setOpen(false)} />}
    </>
  );
}

/** 子agent 详情弹窗一次最多渲染多少步（超出的折成「显示更早的 N 步」）。 */
const SUB_MODAL_WINDOW = 200;

/** 子agent 详情弹窗：铺开它的完整执行轨迹（思考 + 每次工具调用，均可再点开看细节）。 */
function SubAgentModal({ group, runId, onClose }: { group: SubGroup; runId: string; onClose: () => void }) {
  const [stopping, setStopping] = useState(false);
  const st = subState({ ...group, stopping: stopping && group.open });
  const label = (group.title || group.question || "子agent")
    .replace(/^\s*子\s*agent\s*[·:：\-]\s*/i, "").trim() || "子agent";
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => { if (ev.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // 子agent 边跑边产出时，详情弹窗自动滚到底部最新一步（打开时也先滚到底，看结论不看完头）。
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [group.rows.length, group.open]);
  const toolRows = group.rows.filter((r) => r.kind === "tool");
  // 这个子agent 自己的耗时（task_start → task_done）：和行头那句「子 Agent 已完成 61s」同源
  const ms = group.startedAt && group.finishedAt
    ? parseTs(group.finishedAt) - parseTs(group.startedAt) : 0;
  const durText = ms > 0 ? (ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s` : `${Math.round(ms)}ms`) : "";
  // 弹窗里同样开窗：一个跑很久的子agent 能有几百步，全量挂 DOM 点开就卡；
  // 默认只渲染最后 SUB_MODAL_WINDOW 步，更早的点按钮再铺开（与主轨迹同样的取舍）。
  const [showAll, setShowAll] = useState(false);
  const winStart = !showAll && group.rows.length > SUB_MODAL_WINDOW
    ? group.rows.length - SUB_MODAL_WINDOW : 0;
  const shownRows = winStart > 0 ? group.rows.slice(winStart) : group.rows;
  const canStop = group.open && !stopping;
  async function stop() {
    if (!canStop) return;
    setStopping(true);
    try {
      await api.cancelTask(runId, group.taskId);
    } catch {
      setStopping(false);
    }
  }
  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-white rounded-lg border border-slate-200 w-[720px] max-w-[92vw] max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 px-4 py-2.5 border-b border-slate-100">
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">{label}</span>
          <span className={`shrink-0 text-[11.5px] ${st.tone}`}>
            {group.open && <span className={`mr-1 inline-block h-1.5 w-1.5 align-middle rounded-full ${st.dot}`} />}
            子 Agent {st.text}
            {durText && <span className="ml-1 tabular-nums">{durText}</span>}
          </span>
          {group.open && (
            <button
              className="shrink-0 rounded border border-slate-200 px-2 py-0.5 text-[11.5px] text-slate-500 hover:border-rose-300 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-60"
              onClick={stop}
              disabled={stopping}
              title={stopping ? "正在停止这个子agent…" : "只停这个子agent（主agent 和其它子agent 继续跑）"}
            >
              {stopping ? "停止中…" : "停止这个子agent"}
            </button>
          )}
          <button className="text-slate-400 hover:text-slate-600 text-lg leading-none shrink-0" onClick={onClose}>×</button>
        </div>
        {group.question && group.question !== label && (
          <div className="px-4 py-2 text-[12px] text-slate-500 border-b border-slate-100 whitespace-pre-wrap break-words">
            {group.question}
          </div>
        )}
        {group.status === "failed" && group.error && (
          <div className="px-4 py-2 text-[12px] text-red-600 bg-red-50/60 border-b border-red-100 break-words">
            出错：{group.error}
          </div>
        )}
        {group.summary && (
          <div className="border-b border-emerald-100 bg-emerald-50/40 px-4 py-2.5">
            <div className="mb-1 text-[11px] font-medium text-emerald-700">
              子agent 的结论（已交给主agent）
            </div>
            {/* 结论按 Markdown 渲染：子agent 的交付格式就是 Markdown（标题/清单/代码块），
                以前只把它当纯文本塞进任务记录，用户在界面上根本看不到。 */}
            <div className="max-h-56 overflow-auto">
              <Markdown text={group.summary} className="text-[12.5px] text-slate-700" />
            </div>
          </div>
        )}
        <div ref={scrollRef} className="p-3 overflow-auto">
          <div className="mb-2 text-[11px] text-slate-400">
            共 {group.rows.length} 步 · 工具调用 {toolRows.length} 次
            {!group.open && group.finishedAt ? " · 已结束" : " · 进行中"}
          </div>
          {group.rows.length === 0 ? (
            <div className="py-6 text-center text-[12px] text-slate-400">
              {group.open ? "这个子agent 还在首轮思考，暂时没有产出……" : "这个子agent 没有留下执行记录（可能一启动就失败/取消）。"}
            </div>
          ) : (
            <>
              {winStart > 0 && (
                <button
                  type="button"
                  onClick={() => setShowAll(true)}
                  className="mb-1 w-full rounded px-1 py-[3px] text-left text-[12px] text-slate-500 hover:bg-slate-50 hover:text-slate-800"
                  title="这个子agent 的轨迹很长，先只显示最近的部分；点这里把更早的也铺开"
                >
                  ⌃ 显示更早的 {winStart} 步（共 {group.rows.length} 步）
                </button>
              )}
              {shownRows.map((r) =>
                r.kind === "think" ? (
                  <ThinkRow key={r.key} agent={r.agent} text={r.text} />
                ) : r.kind === "tool" ? (
                  // runId 必须透传：子agent 的截图/素材（ShotThumbs）要靠它去
                  // /runs/{id}/shots 找图，传空串时弹窗里永远是「没有图」。
                  <ToolRow key={r.key} agent={r.agent} step={r.step} runId={runId} live={group.open} />
                ) : r.kind === "compact" ? (
                  <CompactRow key={r.key} info={r.compact} />
                ) : null)}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** 汇总一个 spawn_agents 派发行的状态与汇总条数（供 SpawnRow 用）。 */
function spawnSummary(groups: SubGroup[] | undefined) {
  if (!groups || !groups.length) return null;
  let done = 0, failed = 0, running = 0;
  for (const g of groups) {
    const s = subState(g).text;
    if (s === "运行中") running++;
    else if (s === "失败") failed++;
    else done++;
  }
  return { total: groups.length, done, failed, running };
}

function actionSummary(rows: Row[], subTaskIds?: Set<string>): string {
  let cmd = 0, read = 0, write = 0, other = 0;
  const subTasks = new Set<string>();
  const isSub = (tid?: string) => !!tid && !!subTaskIds && subTaskIds.has(tid);
  for (const r of rows) {
    if (r.kind === "tool") {
      const n = r.count ?? 1;
      const t = r.step.tool;
      if (t === "run_command") cmd += n;
      else if (t === "read_file" || t === "grep" || t === "glob" || t === "list_dir") read += n;
      else if (t === "write_file" || t === "edit_file" || t === "delete_file") write += n;
      else other += n;
      // 只把「子agent 的 task」算进「派了 N 个子agent」：主 agent 现在也带 task_id，
      // 直接看 r.taskId 会把主任务当成一个子agent（永远多算 1 个）。
      if (isSub(r.taskId)) subTasks.add(r.taskId!);
    } else if (r.kind === "think" && isSub(r.taskId)) {
      subTasks.add(r.taskId!);
    }
  }
  return bucketText({ commands: cmd, reads: read, writes: write, other, subagents: subTasks.size });
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
  // 只读调研子agent：紫标，与轨迹里的「子agent 分组」同色系
  sub: "bg-violet-50 text-violet-700 ring-violet-200",
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
 *  以前压缩只在适配层做、压完就丢，界面上完全看不出来发生过。
 *  行内写「发送前 → 压缩后」：只写「合并 49.7k tok」会被读成「上下文只有 49.7k」，
 *  误以为没到阈值就压了（实际 49.7k 只是被并进摘要的那一段，尾部回合是原文留下的）。 */
function CompactRow({ info }: { info: CompactInfo }) {
  const k = (n: number) => (n >= 1000 ? `${Math.round(n / 100) / 10}k` : `${n}`);
  const overflow = info.source === "endpoint_overflow";
  return (
    <div
      className={`my-1 flex items-center gap-2 px-1 text-[11.5px] ${
        overflow ? "text-amber-600" : "text-slate-400"}`}
      title={
        (overflow
          ? `端点判定请求超长（${info.overflowError || "HTTP 400"}），按半预算重压后重发。\n`
            + "这说明端点实际允许的长度比配置的窗口小——请核对「模型配置」里该模型的 context_window。\n"
          : "") +
        `模型窗口 ${k(info.window)} tok${info.windowDefault
          ? "（档位没填 context_window，按默认值估算；在「模型配置」里给该模型填真实窗口可避免过早压缩）"
          : ""}，发送预算 ${k(info.budget)} tok（越过就压缩）\n` +
        (info.prevPromptTokens
          ? `上一轮端点实报 ${k(info.prevPromptTokens)} tok；触发看的是本轮待发估算` +
            `（含上一轮之后新增的工具结果），两者差额就是新增内容的量级\n`
          : "") +
        `把较早的 ${info.mergedMsgs} 条历史（约 ${k(info.mergedTokens)} tok）合并成 ` +
        `${k(info.digestTokens)} tok 的要点摘要，最近回合保留原文\n` +
        `这一轮发出去的请求：${k(info.before)} → ${k(info.after)} tok（估算，含工具定义）`
      }
    >
      <span className="h-px flex-1 bg-slate-200" />
      <span className="shrink-0">
        {overflow && <>✂ 端点判定请求超长 · </>}
        {info.before > 0 && info.after > 0
          ? <>▤ 已压缩上下文 · 发送前约 {k(info.before)} tok → {k(info.after)} tok
            （合并 {info.mergedMsgs} 条历史 · 摘要 {k(info.digestTokens)} tok）</>
          : <>▤ 已压缩上下文 · 合并 {info.mergedMsgs} 条历史（{k(info.mergedTokens)} tok）
            → 摘要 {k(info.digestTokens)} tok</>}
        {info.windowDefault ? ` · 窗口按默认 ${k(info.window)}` : ""}
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

/** spawn_agents 派发行：一句话说明派了几个只读子agent、各自查什么（子 agent 的事件
 *  全部折进下面的分组行里，不再平铺混排——用户之前看到的是一串相同 sub 标签的瀑布）。
 *  关键：结果回来后要分清「确实派出去了」和「被后端拒了」——只有 items≥2 才真派；少于
 *  2 条后端直接报错、一个子agent 都不建。以前不看 result，被拒也画成「⇗ 派出 2 个」，
 *  用户对着一条根本没有下文的线找「可展开的子agent」，怎么都找不到。 */
function SpawnRow({ agent, args, result, summary }: {
  agent: string; args?: Record<string, unknown>; result?: string;
  /** 这次派出后各子agent 的汇总状态（完成/失败/运行中）；用于在这条分隔线上给个总览 */
  summary?: { total: number; done: number; failed: number; running: number } | null;
}) {
  const items = Array.isArray(args?.items) ? (args!.items as any[]) : [];
  const names = items.map((it) => String(it?.name || "")).filter(Boolean);
  // 失败判定：结果文本以「工具错误」开头 = 后端拒绝派发（少于2条 / 参数非法），
  // 这种情况下不存在任何子agent，不能显示成「已派出」。
  const rejected = typeof result === "string" && result.trim().startsWith("工具错误");
  const pending = result === undefined; // 结果还没回来
  if (rejected) {
    const reason = String(result || "").replace(/^工具错误[：:]\s*/, "");
    return (
      <div className="my-1 flex items-start gap-2 px-1 text-[11.5px] text-amber-600"
        title={reason}>
        <span className="mt-2 h-px flex-1 bg-amber-100" />
        <span className="shrink-0">
          ⇗ 未派出子agent（请求 {items.length || "?"} 个，需≥2）· {oneLine(reason, 60)}
        </span>
        <span className="mt-2 h-px flex-1 bg-amber-100" />
      </div>
    );
  }
  // 汇总文案：已经全部收口时给「完成 X · 失败 Y」，还在跑时给「运行中 Z/Y」。
  let agg = "";
  if (summary && summary.total) {
    const bits: string[] = [];
    if (summary.running) bits.push(`运行中 ${summary.running}/${summary.total}`);
    if (summary.done) bits.push(`完成 ${summary.done}`);
    if (summary.failed) bits.push(`失败 ${summary.failed}`);
    agg = bits.join(" · ");
  }
  return (
    <div className="my-1 flex items-center gap-2 px-1 text-[11.5px] text-violet-600"
      title={names.length ? `子任务：${names.join("、")}` : undefined}>
      <span className="h-px flex-1 bg-violet-100" />
      <span className="shrink-0">
        {pending ? "⇗ 正在派出" : "⇗ 派出"} {items.length || "?"} 个只读子agent 并行核对
        {pending ? "…" : ""}
        {agg ? ` · ${agg}` : ""}
      </span>
      <span className="h-px flex-1 bg-violet-100" />
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
      <div className="text-[11px] text-slate-400">
        {note.kind === "answer" ? "你 · 回答提问" : "你 · 插话"}{hm ? ` · ${hm}` : ""}
      </div>
      <div className="mt-0.5 inline-block max-w-xl rounded-lg bg-brand-600 px-3 py-2 text-left text-[12.5px] leading-relaxed whitespace-pre-wrap text-white [overflow-wrap:anywhere]">
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

/** 主 agent 中途落的答复（临时回复 / 进度）：按发生时刻嵌在轨迹里。
 *  样式沿用消息流里的对应气泡（临时回复虚线框、进度琥珀色），但作为轨迹的一行。 */
function ReplyRow({ note }: { note: ReplyNote }) {
  const hm = localClock(note.at || "");
  const isStage = note.kind === "stage";
  return (
    <div className="my-1.5">
      <div className="text-[11px] text-slate-400">
        助手{isStage ? " · 进度" : " · 临时回复"}
        {isStage && note.openCount != null && note.openCount > 0
          ? ` · 还有 ${note.openCount} 项没做完` : ""}
        {hm ? ` · ${hm}` : ""}
      </div>
      <div className={`mt-0.5 rounded-lg border px-3 py-2 text-[12.5px] leading-relaxed [overflow-wrap:anywhere] ${
        isStage
          ? "border-amber-200/70 bg-amber-50/50"
          : "border-dashed border-slate-300 bg-slate-50/60"
      }`}>
        <Markdown text={note.text} className="text-slate-700" />
      </div>
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
        <span className="text-slate-700">{oneLine(text, long ? THINK_COLLAPSE : 600)}</span>
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
  /** report_to_user 之后还有动作：这是中途进度，不是最终交付 */
  interimReport?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const verb = step.tool === "report_to_user" && interimReport ? "进度" : (TOOL_VERB[step.tool] || step.tool);
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
            {/* 时刻/耗时用 slate-500 而不是 400：这是用户逐条核对「几点跑的、跑了多久」
                的数据，白底 2.7:1 的 slate-400 看不清（用户反馈「默认淡色看不清」）；
                深色模式由 index.css 统一重映射，不受影响。 */}
            {localClock(step.at || at) && (
              <span className="ml-1 text-[11px] text-slate-500 mono"
                title="这条命令开始执行的时间">
                {localClock(step.at || at)}
              </span>
            )}
            {!pending && typeof step.durationMs === "number"
              && (!step.durationEstimated || step.durationMs >= 1000) && (
              <span className="ml-1 text-[11px] text-slate-500 mono"
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
            {/* 命令/路径是这行的主要信息，用 slate-700 与动词同级——
                之前 slate-600 比动词还淡一档，长命令在浅色下发灰看不清 */}
            <span className="mono text-slate-700">{b}</span>
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

/** 实时思考：一行显示最新输出，点开看全文并自动跟随。
 *  `text` 为空时是**占位态**：仍在跑的 run 把这一行钉在原地（内容 visibility:hidden），
 *  高度与有文字时完全一致。以前它随「这一轮思考是否已落库」反复挂载/卸载，
 *  每来回一次就把下面整块内容顶上去/拉下来一行（用户反馈的「鼠标放上去看思考过程，
 *  后面的内容一直抖」），这里用常驻占位把这个位移彻底消掉。 */
function LiveRowBase({ text, agent = "" }: { text: string; agent?: string }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text, open]);
  const tail = oneLine(text.slice(-400), 170);
  // 占位态：不给点击（disabled 后不可聚焦，tab 不会停在一条看不见的空行上）
  const blank = text.trim().length === 0;
  return (
    <div className={blank ? "invisible" : undefined} aria-hidden={blank || undefined}>
      <Line
        glyph="✦"
        glyphClass="text-violet-500"
        lead={<RoleTag agent={agent} />}
        onToggle={blank ? undefined : () => setOpen((o) => !o)}
        mark={blank ? undefined : "pending"}
      >
        <span className="font-medium text-violet-700">思考中</span>
        <span className="text-slate-400"> · </span>
        <span className="text-slate-700">{tail}</span>
      </Line>
      {!blank && open && (
        <Detail tone="think">
          <div className="mb-1 flex items-center">
            <CopyButton
              get={() => text}
              label="复制思考"
              title="复制目前为止这段思考的文字（仍在生成，复制到手的是已到达的部分）"
              className="ml-auto text-[11px] text-violet-500 hover:text-violet-800"
            />
          </div>
          <div ref={boxRef} className="max-h-72 overflow-y-auto whitespace-pre-wrap [overflow-wrap:anywhere]">
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
      const ev = (e as CustomEvent).detail as { type?: string; payload?: any; task_id?: string | null };
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
const EMPTY_REPLIES: ReplyNote[] = [];

export default function RunTraceInline({
  runId, steers = EMPTY_STEERS, replies = EMPTY_REPLIES, stat, statPending,
}: {
  runId: string;
  steers?: SteerNote[];
     /** 主 agent 中途落的答复（临时回复/进度）：按发生时刻嵌进轨迹 */
  replies?: ReplyNote[];
  /** 该 run 的现成摘要（会话 run 列表顺带算好的）：收起态靠它做到「一条事件不拉也有完整一行」 */
  stat?: RunStat;
  /** 会话 run 列表还没回来：先别急着拉事件，免得给一条早就跑完的 run 白拉 1.5MB */
  statPending?: boolean;
}) {
  const [events, setEvents] = useState<Ev[]>([]);
  /** 子agent 结论回填表（task_id → result_json.text）：老 run 的 task_done 没有 summary，
   *  从 run 详情里的 task 记录补齐；新 run 也有，双保险。 */
  const [taskResults, setTaskResults] = useState<Record<string, string>>({});
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
  // 已等待秒数 + agent_key），让用户看到模型还在跑，而不是「一直不动」。收到任何落库事件即清空。
  const [liveNote, setLiveNote] = useState<string>("");
  // 子agent 各自的「等模型」状态（task_id 归一化 → {秒数, 收到时刻}）。
  // 以前不区分是谁的心跳：5 个子agent 各发各的「已等待 N 秒」，几路数字交替顶同一行，
  // 加上子agent 的工具事件又把这行清掉 → 底部提示一直跳（用户实测）。现在主agent 的心跳
  // 只进上面这一行，子agent 的显示在各自那一行上，互不干扰。
  const [subWaits, setSubWaits] = useState<Record<string, { sec: number; at: number }>>({});
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
        // 子agent 的收尾结论：run 详情里每条 task 都带 result_json（老 run 的
        // task_done 没有 summary 字段，靠它回填）。只在有新内容时更新，避免 3s 轮询重建。
        const tr: Record<string, string> = {};
        for (const t of r.tasks || []) {
          const text = t?.result_json?.text;
          if (t?.id && t?.agent_key === "sub" && typeof text === "string" && text.trim()) {
            tr[String(t.id).replace(/-/g, "")] = text;
            tr[String(t.id)] = text;
          }
        }
        if (Object.keys(tr).length) setTaskResults((cur) => ({ ...cur, ...tr }));
        // 权威暂停态：run 详情里带当前在等回答的问题（刷新/重连后也能正确显示）。
        if (r.awaiting_question) setPaused((cur) => cur ?? { q: String(r.awaiting_question) });
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
    const tidKey = (v?: string | null) => String(v || "").replace(/-/g, "").toLowerCase();
    function onEv(e: Event) {
      const ev = (e as CustomEvent).detail as { type?: string; payload?: any; task_id?: string | null; agent?: string | null };
      if (!ev?.type) return;
      const p = ev.payload || {};
      const who = String(ev.agent || p.agent_key || p.agent || "");
      if (ev.type === "agent_thinking_delta") {
        // 「思考中」实时行只跟主 agent（main）。子 agent 是并行独立跑、round 各自从 0
        // 编号：把它们的增量并进来会在主/子之间来回整段顶掉，视觉上就是「思考中」内容
        // 一秒一换、有时有有时没有。子 agent 的思考照样会落库（agent_thinking 整轮版），
        // 分组里能看到，这里不实时播。agent_key 缺省时按旧行为照收（兼容老数据）。
        if (who && who !== "main") return;
        const rnd = Number(p.round ?? 0);
        const add = String(p.text || "");
        setLiveNote("");  // 真的在吐思考增量了，撤掉「等待模型」心跳
        setLive((cur) => (cur.round === rnd
          ? { round: rnd, agent: who || cur.agent, text: cur.text + add }
          : { round: rnd, agent: who || cur.agent, text: add }));  // 换轮：丢掉上一轮残留
        return;
      }
      if (ev.type === "status") {
        // 等待模型心跳：只更新提示行，不触发整表重拉（它 10s 一条，重拉会白拉事件流）
        if (p.waiting) {
          if (who === "sub") {
            // 子agent 的心跳只落在它自己那一行（「等模型 Ns」）；不再顶主轨迹的提示行
            const k = tidKey(ev.task_id);
            if (!k) return;
            setSubWaits((cur) => ({ ...cur, [k]: { sec: Number(p.waited || 0), at: Date.now() } }));
            return;
          }
          setLiveNote(String(p.note || ""));
          return;
        }
      }
      // 子agent 有新动静（工具调用/结果/思考/收工）：撤掉它那行的「等模型」。
      // 主agent 的事件不应该清子agent 的状态，反之亦然。
      if (who === "sub" && ev.task_id) {
        const k = tidKey(ev.task_id);
        setSubWaits((cur) => {
          if (!(k in cur)) return cur;
          const next = { ...cur };
          delete next[k];
          return next;
        });
      }
      // 提问暂停态的实时切换：问到 → 暂停；已回答 / 任何后续事件（工具又动了）→ 继续。
      if (ev.type === "question_asked") {
        setPaused({ q: String(ev.payload?.question || "") });
      } else if (ev.type === "question_answered" || ev.type === "tool_call"
                 || ev.type === "agent_thinking" || ev.type === "run_done") {
        setPaused(null);
      }
      // 落库事件到达：立刻拉一次事件流。这里**不要**先把实时文本清空——
      // 清了之后到事件真正落进列表之间会空一拍，视觉上就是「思考完闪一下再出现下面的行」。
      // 实时行什么时候让位，交给渲染层按「本轮是否已落库」判断（见 liveCovered）。
      // 只有主agent（或 run 级）事件才撤掉「等待模型」提示：子agent 的事件以前会把主agent
      // 的等待提示一并清掉，子agent 一多这行就疯狂出没（用户实测「一直跳」）。
      if (who !== "sub") setLiveNote("");
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
  // 同一 run 内「等你回答」暂停态：收到 question_asked → 暂停，question_answered / 任何
  // 后续工具进展 → 继续。此前提问是把 run 落 blocked 结束本轮（界面显示「等你回答」、
  // 计划清单整个消失）；现在暂停只是这一轮里的一个停顿，仍属同一次执行。
  const [paused, setPaused] = useState<{ q: string } | null>(null);
  useEffect(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.type === "question_answered") { setPaused(null); return; }
      if (e.type === "question_asked") { setPaused({ q: String(e.payload?.question || "") }); return; }
    }
    setPaused(null);
  }, [events]);
  const rows = useMemo(() => buildRows(events, steers, replies), [events, steers, replies]);
  // 子agent 的 task_id 集合（来自 task_start role=sub）：把「主 agent 的 task」与「真子agent
  // 的 task」区分开——主 agent 事件现在也带 task_id，不能再靠「有没有 task_id」判主/子。
  const subTaskIds = useMemo(() => {
    const s = new Set<string>();
    for (const e of events) {
      const p = e.payload || {};
      if (e.type === "task_start" && p.role === "sub" && p.task_id) s.add(String(p.task_id));
    }
    return s;
  }, [events]);
  // 子 agent 分组：把带 taskId 的行折进各自的组（见 groupRows）。
  // 注意分组前先算 actionSummary——它吃扁平 rows，数字口径才和收起态一致。
  const view = useMemo(() => groupRows(rows, events, false, taskResults), [rows, events, taskResults]);
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
  // 一旦这一轮（或更新的轮次）的思考已经落库到列表里，实时行就立刻让位——
  // 时间点上与落库那行重合，不会出现「先消失、下一拍再出现」的闪动。
  // 只认主 agent 的**思考行**（think 且不属于任何子agent task）：
  // - 子agent 的 think 行属于某个 sub task、round 独立编号，不能算；
  // - 主 agent 现在也带 task_id，所以按「是否属于 sub 任务」判，而不是「有没有 task_id」；
  // - 过程事件（context_compacted / llm_retry）虽然带 round，但它们发生在本轮
  //   思考**之前**（压缩/重试都发生在拿到回复前），round == 当前流式 round 时会把
  //   实时行误判成「已被覆盖」→ 压掉 → 下一拍 delta 到达又恢复，正是闪动来源。
  const liveCovered = rows.some((r) =>
    r.kind === "think" && !(r.taskId && subTaskIds.has(r.taskId)) && r.round >= live.round,
  );
  const liveText = running && !liveCovered ? live.text : "";
  // 实时行要不要占位：只认「本次 run 确实产生过思考」（有过流式增量，或已有主 agent 的思考行）。
  // 这个条件在一次 run 里最多从 false 翻到 true 一次，之后恒为 true —— 所以占位不会来回抽动；
  // 反过来，完全不思考的 run（纯工具型/一句话答完）不会在底部平白多挂一行空白。
  const hadMainThink = rows.some((r) =>
    r.kind === "think" && !(r.taskId && subTaskIds.has(r.taskId)),
  );
  const liveSlot = running && (hadMainThink || !Number.isNaN(live.round));
  // 端点重试（llm_retry）后台静默：标题行不再提「重试模型请求 (n/5)」，收起态就和正常执行一样。
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
  // 开窗切扁平 rows，再分组：组元数据取自全部 task_start 事件，所以窗外开的组
  // 也能正常渲染组头（只是组内只有窗内的行）。
  const viewRows = groupRows(winStart < 0 ? rows : rows.slice(winStart), events, winStart >= 0,
                             taskResults);
  const hiddenRows = winStart < 0 ? 0 : winStart;
  // 子agent 汇总（给「⇗ 派出 N 个」分隔线显示 完成/失败/运行中 概览）；多条派出时
  // 无法把组精确归到某一次派出，就不硬套汇总，避免张冠李戴。
  const allSubGroups: SubGroup[] = viewRows
    .filter((r) => r.kind === "subgroup")
    .map((r) => (r as { kind: "subgroup"; group: SubGroup }).group);
  const subSummary = spawnSummary(allSubGroups);
  const spawnRowCount = viewRows.filter((r) => r.kind === "spawn").length;

  // 摘要数字：已经跑完的用它自己那份分桶（和收起态同一个数，点开不会跳变），
  // 还在跑的按事件实时数出来。
  const summary = status && TERMINAL.has(status) && stat?.activity
    ? bucketText(stat.activity)
    : actionSummary(rows, subTaskIds);
  const agentKeys = Array.from(new Set(
    (rows.length ? rows.filter((r) => r.kind !== "steer" && r.kind !== "reply")
      .map((r) => r.agent) : stat?.agents || [])
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
        {running && !paused && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-400" />}
        {paused && <span className="shrink-0 text-[11px]">⏸</span>}
        <span className="truncate">
          <span
            className={
              paused
                ? "font-medium text-amber-700"
                : running
                ? "font-medium text-slate-800"
                : status === "failed"
                ? "font-medium text-red-600"
                : "font-medium text-slate-800"
            }
          >
            {paused ? "等你回答" : stateWord}
          </span>
          {waitNote && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-amber-600">{waitNote}</span>
            </>
          )}
          {paused && paused.q && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-amber-700">{oneLine(paused.q, 60)}</span>
            </>
          )}
          {summary && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-700">{summary}</span>
            </>
          )}
          {startClock && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-700" title={`本次执行开始于 ${startClock}`}>
                {startClock} 开始
              </span>
            </>
          )}
          {baseSec > 0 && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-700">
                <RunElapsed baseSec={baseSec} startedAt={elapsedBase.at}
                  ticking={running && elapsedBase.at > 0} running={running} />
              </span>
            </>
          )}
          {agentLabel && (
            <>
              <span className="text-slate-400"> · </span>
              <span className="text-slate-700">{agentLabel}</span>
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
            <div key={r.key}>
              {r.kind === "subgroup" ? (
                <SubAgentRow group={r.group} runId={runId}
                  wait={subWaits[r.group.taskId.replace(/-/g, "").toLowerCase()]} />
              ) : r.kind === "think" ? (
                <ThinkRow agent={r.agent} text={r.text} />
              ) : r.kind === "compact" ? (
                <CompactRow info={r.compact} />
              ) : r.kind === "notes" ? (
                <NotesRow info={r.notes} />
              ) : r.kind === "steer" ? (
                <SteerRow note={r.steer} />
              ) : r.kind === "reply" ? (
                <ReplyRow note={r.reply} />
              ) : r.kind === "spawn" ? (
                <SpawnRow agent={r.agent} args={r.args} result={r.result}
                  summary={spawnRowCount === 1 ? subSummary : null} />
              ) : (
                <ToolRow agent={r.agent} step={r.step} runId={runId}
                  count={r.count} at={r.at} live={running} awaiting={awaiting}
                  interimReport={r.interimReport} />
              )}
              {r.kind === "tool" && (["image", "audio", "video"] as const).map((m) => {
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
          {/* 运行期间的「状态脚注」：固定高度。以前它是一条普通行，出现/消失会把整块轨迹
              的高度反复顶开又收回（子agent 一多、心跳一来一去，看着就是「一直跳」）。
              现在位置和高度都钉死，只有里面的提示文字在原地更新。 */}
          {/* 运行期间的「实时思考行」：常驻占位（条件 liveSlot 见上）。
              以前它是 `liveText.trim() && <LiveRow/>`，流式 delta 一到就挂载、这一轮思考一落库
              就卸载，每个 LLM 往返走一遍，下面整块内容就跟着上下抽 26px
              （用户反馈的「鼠标放上去看思考过程，后面的内容一直抖」）。
              现在只要本次 run 产生过思考、且还在跑，这一行就固定占着：没文字时 invisible
              占位（高度与有文字时一致），有文字时原地更新，全程不改变轨迹高度。 */}
          {liveSlot && (
            <LiveRow key="live-think" text={liveText}
              agent={live.agent || (agentKeys.length === 1 ? agentKeys[0] : "")} />
          )}
          {/* 等待心跳放最后：它是「此刻」的状态，不是历史事件，不该插在思考行上面 */}
          {running && (
            <div className="flex h-[26px] items-center gap-1.5 overflow-hidden px-1.5 text-[12px] text-amber-700">
              {waitNote && (
                <>
                  <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-400" />
                  <span className="truncate rounded-full bg-amber-50/80 px-2 py-[1px] ring-1 ring-amber-200/60">
                    {waitNote}
                  </span>
                </>
              )}
            </div>
          )}
          {/* 改动卡片已挪到消息末尾（RunChanges 独立展示），这里不再重复 */}
        </div>
      )}
    </div>
  );
}
