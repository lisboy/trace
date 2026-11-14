import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { createPortal } from "react-dom";
import { api } from "../api";
import { useAuth } from "../store";
import { navigate, type Route } from "../router";
import SessionList from "../components/SessionList";
import ModelPicker, { DEFAULT_MODEL_CHOICE, type ModelChoice } from "../components/ModelPicker";
import RunPanel from "../components/RunPanel";
import Markdown from "../components/Markdown";
import { ProjectScriptButtons } from "../components/ProjectScriptButtons";
import RunTraceInline, { type SteerNote, type ReplyNote, type RunStat } from "../components/RunTraceInline";
import AskQuestion from "../components/AskQuestion";
import PlanChecklist from "../components/PlanChecklist";
import SessionUsage from "../components/SessionUsage";
import SystemMetrics from "../components/SystemMetrics";
import RunChanges from "../components/RunChanges";
import CopyButton from "../components/CopyButton";
import { escapeHtml, type CopyPayload } from "../clipboard";
import { ProjectSocket } from "../ws";
import { fireNotify, truncBody } from "../notify";
import { loadQueueImages, saveQueueImages } from "../queueMedia";
import { ROLE_LABEL, type Message, type PermissionScope, type ProjectSummary, type Session } from "../types";
import { defaultScope } from "../assistantPolicy";

/** UUID 归一比较：runs.session_id 存的是 32 位无横线串，sessions.id 带横线，
 *  直接 === 永远不相等（会导致「正在执行的会话」永远认不出来）。 */
function sameId(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false;
  return a.replace(/-/g, "").toLowerCase() === b.replace(/-/g, "").toLowerCase();
}

/** 没有插话时返回同一个空数组：每次新建一个会让轨迹里的行缓存整段作废、全量重算。 */
const NO_STEERS: SteerNote[] = [];
/** 同理：没有中途答复时返回同一个空数组。 */
const NO_REPLIES: ReplyNote[] = [];

/** 首屏消息分页大小：大会话（几百条 + 大量附图）只先拉最近一屏，
 *  往上翻到顶再按 created_at cursor 拉更早的，避免一次吐全量把页面撑爆。 */
const MSG_PAGE = 60;

/** 这条消息是否「接管」它所属 run 的执行轨迹/改动卡片（只有助手与系统消息接管）。
 *  插话落的 user 消息也带 run_id（见 runs.py 的 steer_run），若把它算作「这条 run 已落消息」，
 *  底部那块实时轨迹会在插话瞬间被自己抹掉，而 user 气泡又不画轨迹 → 两段话中间的内容整段消失。
 *  另外，**中途进度消息**（meta.kind==='stage_report'）虽也是 agent 消息、也带 run_id，
 *  但它只是途中一句进度同步，不接管轨迹——轨迹必须挂在最后那条收尾消息上，否则长任务里
 *  每来一次进度消息，整段执行轨迹就会「搬」到这条中间消息上，看着像跑到了过去。 */
function ownsRunUi(m: Message): boolean {
  // 提问消息（kind==='question'）不算接管：它由输入框上方的提问面板统一渲染，
  // 且提问是「同一 run 内暂停」，轨迹应挂在底部实时块/后续动作上，不该被一条问题气泡牵走。
  // 临时回复（kind==='interim_reply'）同理：等子agent/插话期间的一段过渡答复，
  // 不是这次执行的最终交付，轨迹必须继续留在底部实时块直到收尾消息出现。
  return !!m.run_id && m.author_type !== "user"
    && m.meta?.kind !== "stage_report" && m.meta?.kind !== "question"
    && m.meta?.kind !== "interim_reply";
}

/** 会话里的图片可能是 dataURL，也可能是服务端 URL；剪贴板只认 dataURL，统一转一道。
 *  带缓存：反复点复制不该反复拉同一张图。 */
const dataUrlCache = new Map<string, string>();
async function toDataUrl(src: string): Promise<string> {
  if (!src) return "";
  if (src.startsWith("data:")) return src;
  const hit = dataUrlCache.get(src);
  if (hit) return hit;
  try {
    const blob = await (await fetch(src)).blob();
    const u = await new Promise<string>((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res(String(fr.result));
      fr.onerror = () => rej(new Error("读取图片失败"));
      fr.readAsDataURL(blob);
    });
    dataUrlCache.set(src, u);
    return u;
  } catch {
    return ""; // 拿不到图的就把文字复制走，别整条失败
  }
}

async function msgImages(m: Message): Promise<string[]> {
  const meta = m.meta as any;
  const urls: string[] = (Array.isArray(meta?.image_urls) && meta.image_urls.length
    ? meta.image_urls
    : (Array.isArray(meta?.images) ? meta.images : [])
  ).filter(Boolean);
  return (await Promise.all(urls.map(toDataUrl))).filter(Boolean);
}

/** 消息附图在页面上的显示 src：优先服务端按需 URL（不把 1MB base64 塞进每次轮询的 JSON），
 *  老数据没有 image_urls 时退回内联 dataURL。 */
function msgImageSrcs(m: Message): string[] {
  const meta = m.meta as any;
  const urls = Array.isArray(meta?.image_urls) ? meta.image_urls.filter(Boolean) : [];
  const inline = Array.isArray(meta?.images) ? meta.images.filter(Boolean) : [];
  return urls.length ? urls : inline;
}

/** 附图缩略图 src：服务端 URL 加 thumb=320（列表里只下小图），dataURL 原样。
 *  点开灯箱/复制仍用原图（msgImageSrcs），缩略图只负责列表里不卡。 */
function msgImageThumbSrcs(m: Message): string[] {
  return msgImageSrcs(m).map((u) =>
    u.startsWith("data:") ? u : `${u}${u.includes("?") ? "&" : "?"}thumb=320`,
  );
}

function whoLabel(m: Message): string {
  return m.author_type === "user" ? "我" : (ROLE_LABEL[m.author_type] || m.author_type);
}

/** 单条消息：文字 + 附图一起进剪贴板（HTML 内联 dataURL，另附一份 PNG）。 */
async function msgCopyPayload(m: Message): Promise<CopyPayload> {
  const images = await msgImages(m);
  const text = (m.content || "").trim() || (images.length ? "（本条只有图片）" : "");
  const html = [
    ...images.map((u) => `<img src="${u}" style="max-width:520px">`),
    `<div>${escapeHtml(text).replace(/\n/g, "<br>")}</div>`,
  ].join("");
  return { text, html, images };
}

/** 整段会话：按顺序把每条消息的文字和图片拼成一份可粘贴的记录。 */
async function sessionCopyPayload(list: Message[]): Promise<CopyPayload> {
  const parts: string[] = [];
  const htmlParts: string[] = [];
  let firstImage = "";
  for (const m of list) {
    const images = await msgImages(m);
    const who = whoLabel(m);
    const text = (m.content || "").trim();
    if (text) {
      parts.push(`【${who}】\n${text}`);
      htmlParts.push(`<p><b>${escapeHtml(who)}</b>：${escapeHtml(text).replace(/\n/g, "<br>")}</p>`);
    }
    if (images.length) {
      if (!firstImage) firstImage = images[0];
      parts.push(`【${who}·附图 ${images.length} 张】`);
      htmlParts.push(`<p><b>${escapeHtml(who)}（附图）</b>：<br>${images
        .map((u) => `<img src="${u}" style="max-width:520px">`).join("<br>")}</p>`);
    }
  }
  return { text: parts.join("\n\n"), html: htmlParts.join(""), images: firstImage ? [firstImage] : [] };
}

/** 排队消息（执行中发的那几条）按会话存档。
 *  原来队列只活在组件 state 里：刷新页面、切项目/切会话都会让它凭空消失，
 *  而界面上一秒还写着「1 排队中」——用户看到的就是「上一轮做完了，我排队说的话没人接着做」。
 *  注意：这里只管**同一条会话内**的顺序；不同会话各有各的队列，互不排队（跨会话并行靠后端 worker 池）。 */
const QUEUE_KEY = "ga_msg_queue";
const QUEUE_MAX_ITEM = 12000; // 单条序列化上限：base64 图片动辄几 MB，写进去会挤爆其他会话的队列
const QUEUE_MAX_KEEP = 10;

interface QueuedFile { path: string; name: string; size: number }
/** imageCount 只在「从 localStorage 读回来」时有值：文字/附件走 localStorage，图片本体走
 *  IndexedDB（见 queueMedia.ts），刷新后由 loadQueueImages 还原；还原不到时才提示重新附上。 */
interface QueueItem { id: string; content: string; images: string[]; files: QueuedFile[]; imageCount?: number; modelProfile?: string; reasoningEffort?: string; scope?: PermissionScope }

function queueArchive(): Record<string, QueueItem[]> {
  try {
    const all = JSON.parse(localStorage.getItem(QUEUE_KEY) || "{}");
    return all && typeof all === "object" ? all as Record<string, QueueItem[]> : {};
  } catch {
    return {};
  }
}

function loadQueue(sessionId?: string | null): QueueItem[] {
  if (!sessionId || sessionId === "__new__") return [];
  const items = queueArchive()[sessionId];
  if (!Array.isArray(items)) return [];
  return items
    .filter((q) => q && typeof q.content === "string")
    .slice(-QUEUE_MAX_KEEP)
    .map((q) => {
      const images = Array.isArray(q.images) ? q.images : [];
      return {
        id: String(q.id || crypto.randomUUID()),
        content: q.content,
        images,
        files: Array.isArray(q.files) ? q.files : [],
        imageCount: Math.max(Number(q.imageCount) || 0, images.length),
        modelProfile: typeof q.modelProfile === "string" ? q.modelProfile : "",
        reasoningEffort: typeof q.reasoningEffort === "string" ? q.reasoningEffort : "",
        scope: q.scope === "full" ? "full" : "workspace",
      };
    });
}

function saveQueue(sessionId: string | null | undefined, items: QueueItem[]): void {
  if (!sessionId || sessionId === "__new__") return;
  const all = queueArchive();
  // localStorage 只有 ~5MB，图片 dataURL 不往里写：这里只留张数，图片本体由
  // saveQueueImages 存 IndexedDB（queueMedia.ts），刷新后 loadQueueImages 再还原。
  // 以前整条带图的排队消息会因为超过 QUEUE_MAX_ITEM 被**整条丢掉**，现在文字/附件照常保留。
  const keep = items
    .map((q) => ({
      id: q.id, content: q.content, files: q.files,
      images: [] as string[],
      imageCount: q.images.length || (q.imageCount || 0),
      modelProfile: q.modelProfile || "",
      reasoningEffort: q.reasoningEffort || "",
      scope: q.scope,
    }))
    .filter((q) => q.content.trim() || q.imageCount || q.files.length)
    .filter((q) => JSON.stringify(q).length <= QUEUE_MAX_ITEM);
  if (keep.length) all[sessionId] = keep;
  else delete all[sessionId];
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(all));
  } catch {
    /* 存不下就退回纯内存队列（至少这一次会话内还能自动续发） */
  }
}

function fmtSize(n: number): string {
  if (!n || n < 0) return "";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  if (n >= 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}

function fileKindLabel(kind?: string): string {
  return kind === "excel" ? "表" : kind === "word" ? "文" : kind === "pdf" ? "PDF"
    : kind === "text" ? "TXT" : "文件";
}

/** 系统自动续跑那条「续跑说明」：整段是给组长的指令，几百上千字，
 *  照原样贴在气泡里会把对话冲成一堵墙（用户以为自己发了篇长文）。
 *  收起成一行说明，展开才看原文。 */
function AutoContinueNote({ text, count }: { text: string; count: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="ga-auto-continue mx-auto max-w-2xl rounded-lg border border-amber-200 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12.5px] text-amber-800">
          ⏩ 系统自动续跑：上一轮清单还有 {count || "若干"} 项没做完，已排队接着做
        </span>
        <button
          className="shrink-0 text-[11.5px] text-amber-700 underline underline-offset-2 hover:text-amber-900"
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "收起续跑说明" : "看续跑说明"}
        </button>
      </div>
      {open && (
        <div className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere] border-t border-amber-200 pt-2 text-[11.5px] leading-relaxed text-amber-900">
          {text}
        </div>
      )}
    </div>
  );
}

/** 单条消息的整块渲染（气泡 + 附图 + 轨迹 + 改动）。
 *  从主组件的 map 里抽出来：虚拟列表只需要为「视口附近」的消息调用它，
 *  不再一次把几百条消息的 Markdown/图片/轨迹全挂到 DOM 上。 */
function MessageItem({
  m, mi, role, traceHostIdxOfRun, steerHasHost, steersOf, repliesOf, runStatById,
  runsLoaded, runningRunId, usageKey, lastMsgIdxOfRun, setZoom,
}: {
  m: Message; mi: number; role: (m: Message) => string;
  traceHostIdxOfRun: Record<string, number>; steerHasHost: (id: string) => boolean;
  steersOf: (id: string) => SteerNote[]; repliesOf: (id: string) => ReplyNote[];
  runStatById: Record<string, RunStat>;
  runsLoaded: boolean; runningRunId: string | null; usageKey: number;
  lastMsgIdxOfRun: Record<string, number>;
  setZoom: (z: { src: string; alt: string } | null) => void;
}) {
  // 附图缩略图加载失败：可能只是网络抖动 / 服务端一时没起来，别急着判「文件已清理」。
  // 失败先自动重试两次（带 cache-bust），仍不行才显示一张中性占位卡，点它还可手动再试或开原图。
  // 真正文件不在时（换机器/被清理）原图和缩略图都会 404，这时才需要提醒；但「刚发的图」绝不
  // 应该因为一次请求失败就盖成「已失效」。
  const [imgErr, setImgErr] = useState<Record<number, boolean>>({});
  const [imgRetry, setImgRetry] = useState<Record<number, number>>({});
  const [imgReason, setImgReason] = useState<Record<number, string>>({});
  // 重试耗尽后探一次真实状态，把「为什么失败」说清楚，而不是笼统的「加载失败」：
  // 401=登录过期（重新登录就好） / 404=文件真的不在（换机器/被清理） / 其他=网络或服务问题
  const probeReason = async (full: string, i: number) => {
    try {
      const r = await fetch(full, { method: "HEAD" });
      const msg = r.status === 401 ? "登录已过期，请重新登录"
        : r.status === 404 ? "文件已不在（可能被清理或换机器）"
        : r.status === 403 ? "没有查看权限"
        : r.status === 0 ? "网络不通，服务可能没启动"
        : `服务异常（HTTP ${r.status}）`;
      setImgReason((e) => ({ ...e, [i]: msg }));
    } catch {
      setImgReason((e) => ({ ...e, [i]: "网络不通，服务可能没启动" }));
    }
  };
  const bumpRetry = (i: number, full: string) => {
    const n = imgRetry[i] || 0;
    if (n < 2) {
      setImgRetry((e) => ({ ...e, [i]: n + 1 }));
    } else {
      setImgErr((e) => ({ ...e, [i]: true }));
      probeReason(full, i);
    }
  };
  // 插话/回答统一并入所属 run 的轨迹按时间显示（带图也一起）：留在消息流会出现
  // 「我明明在后面插话，却显示到前面去」。带附件的插话除外（文件芯片要显示）。
  // 没有轨迹宿主时也原位兜底显示，绝不能让插话/回答凭空消失。
  if (m.author_type === "user" && m.run_id && steerHasHost(m.run_id)
    && !m.meta?.files?.length) return null;
  // 提问消息：由输入框上方的提问面板（AskQuestion）统一渲染成选项卡片，
  // 消息流里不再重复一条原始问题气泡——这既是「样式」修复，也避免同一句问题出现两次。
  if (m.author_type !== "user" && m.meta?.kind === "question") return null;
  // 主 agent 中途落的答复（临时回复/进度）：有轨迹宿主时按发生时刻嵌进轨迹（见
  // repliesOf → RunTraceInline），消息流里不再重复一条气泡——否则它会悬在消息流末尾，
  // 看起来像「跑到了最上面/最下面」而不是过程的一部分。没有宿主时原位兜底显示。
  if (m.author_type !== "user" && m.run_id && steerHasHost(m.run_id)
    && (m.meta?.kind === "interim_reply" || m.meta?.kind === "stage_report")) return null;
  // 系统自动续跑的「续跑说明」：折成一条提示，不把整段指令贴成用户气泡
  //（点「继续」的手动续跑不再发消息，所以这里只会命中自动续跑）
  if (m.author_type === "user" && m.meta?.auto_continue_from)
    return (
      <div className="ml-auto text-right">
        <div className="text-[11px] text-slate-400">系统 · 自动续跑</div>
        <AutoContinueNote
          text={m.meta?.brief || m.content}
          count={Number(m.meta?.open_count) || 0}
        />
      </div>
    );
  // 进度消息：run 还在跑、模型自己决定发的一句话（meta.kind==='stage_report'，沿用旧键）。
  // 它不是一份格式化的汇报，只是让用户知道进度到哪了；外观更淡，并标明「还有 N 项没做完」。
  // 它不接管执行轨迹（见 ownsRunUi）。
  const isStage = m.author_type !== "user" && m.meta?.kind === "stage_report";
  // 临时回复：run 还在跑时主agent 对插话/进度的即时答复（不等子agent 收工、也不等收尾）。
  // 用虚线框 + 「执行中」小标，和最终汇报区分开——省得用户把过渡答复当成已交付。
  const isInterim = m.author_type !== "user" && m.meta?.kind === "interim_reply";
  return (
    <div className={m.author_type === "user" ? "ml-auto text-right" : ""}>
      <div className="text-[11px] text-slate-400">
        {role(m)}
        {isStage && (
          <span className="ml-1.5 rounded bg-amber-50 px-1 py-[1px] text-[10px] font-medium text-amber-700 ring-1 ring-amber-200">
            进度{Number(m.meta?.open_count) > 0 ? ` · 还有 ${Number(m.meta.open_count)} 项没做完` : ""}
          </span>
        )}
        {isInterim && (
          <span className="ml-1.5 rounded bg-sky-50 px-1 py-[1px] text-[10px] font-medium text-sky-700 ring-1 ring-sky-200">
            执行中 · 临时回复
          </span>
        )}
      </div>
      {m.author_type !== "user" && m.run_id && traceHostIdxOfRun[m.run_id] === mi && (
        <>
          {/* 待办清单已统一挪到输入框上方（见下方 planRunId 区块） */}
          <RunTraceInline runId={m.run_id} steers={steersOf(m.run_id)}
            replies={repliesOf(m.run_id)}
            stat={runStatById[m.run_id]} statPending={!runsLoaded} />
        </>
      )}
      <div
        className={`text-sm rounded-lg px-3 py-2 text-left ${
          m.author_type === "user"
            ? "inline-block max-w-2xl bg-brand-600 text-white whitespace-pre-wrap"
            : isStage
            ? "block w-full bg-amber-50/50 border border-amber-200/70"
            : isInterim
            ? "block w-full bg-slate-50/60 border border-dashed border-slate-300"
            : "block w-full bg-white border border-slate-200"
        }`}
      >
        {msgImageSrcs(m).length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {msgImageThumbSrcs(m).map((u, i) => {
              const full = msgImageSrcs(m)[i];
              // 自动重试期间给缩略图 URL 加 cache-bust，强制浏览器重新请求
              const retry = imgRetry[i] || 0;
              const src = retry ? `${u}${u.includes("?") ? "&" : "?"}_r=${retry}` : u;
              if (imgErr[i]) {
                return (
                  <button
                    key={i}
                    type="button"
                    className="flex h-40 w-[220px] flex-col items-center justify-center gap-1 rounded border border-dashed border-slate-300 bg-slate-50 text-[11px] text-slate-500 hover:border-slate-400"
                    title="点击重试加载，或打开原图"
                    onClick={() => {
                      setImgErr((e) => ({ ...e, [i]: false }));
                      setImgRetry((e) => ({ ...e, [i]: 0 }));
                    }}
                  >
                    <span className="text-xs font-medium">附图{i + 1} 暂时无法加载</span>
                    {imgReason[i] && (
                      <span className="text-[10px] text-amber-600 px-2 text-center">{imgReason[i]}</span>
                    )}
                    <span className="text-[10px] text-slate-400">点击重试</span>
                  </button>
                );
              }
              return (
                <img
                  key={`${i}-${retry}`}
                  src={src}
                  loading="lazy"
                  decoding="async"
                  alt={`附图${i + 1}`}
                  className="h-40 w-[220px] rounded border border-black/10 object-contain bg-black/5 cursor-zoom-in hover:opacity-90"
                  onClick={() => setZoom({ src: full, alt: `附图${i + 1}` })}
                  onError={() => bumpRetry(i, full)}
                  title="点击放大查看"
                />
              );
            })}
          </div>
        )}
        {!!m.meta?.files?.length && (
          <div className="flex flex-wrap gap-1.5 mb-1">
            {m.meta.files.map((f: any, i: number) => (
              <span
                key={i}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-white/15 text-[11px] max-w-[240px]"
                title={`${f.path}（${f.size ?? 0} 字节）`}
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                  <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
                </svg>
                <span className="truncate">{f.original_name || f.name || f.path}</span>
              </span>
            ))}
          </div>
        )}
        <Markdown
          text={m.content}
          className={m.author_type === "user" ? "md-invert" : ""}
        />
      </div>
      {/* 用户气泡：文字 + 附图一并复制（容器是 text-right，按钮自然贴右） */}
      {m.author_type === "user" &&
        (!!m.content?.trim() || msgImageSrcs(m).length > 0) && (
        <div className="mt-1 flex justify-end">
          <CopyButton
            get={() => msgCopyPayload(m)}
            title="复制这条消息（含附图）"
            className="text-[11px] text-slate-400 hover:text-brand-600"
          />
        </div>
      )}
      {/* 对话消息的底部操作条：只保留复制（原「🔊 朗读」及情绪设置已按需求移除） */}
      {m.author_type !== "user" &&
        (!!m.content?.trim() || msgImageSrcs(m).length > 0) && (
        <div className="mt-1 flex items-center gap-2">
          <CopyButton
            get={() => msgCopyPayload(m)}
            title="复制这条回复（含附图）"
            className="text-[11px] text-slate-400 hover:text-brand-600"
          />
        </div>
      )}
      {/* 本次改动了几个文件：放在整条消息（含轨迹）的最末尾——
          以前插在气泡和消息操作条之间，一旦轨迹在气泡之后继续增长，卡片就被夹在
          「编辑」和后面那些「读取 / 思考」中间（用户截图 2026-11-02）。
          进度消息不挂改动卡片：那时活儿还没干完，改动清单属于最后那条收尾消息。 */}
      {m.author_type !== "user" &&
        !isStage &&
        m.run_id &&
        lastMsgIdxOfRun[m.run_id as string] === mi && (
          <div className="mt-1.5">
            <RunChanges
              runId={m.run_id as string}
              defaultOpen
              lazy={m.run_id !== runningRunId}
              // 只有还在跑的那条才跟着用量刷新：历史 run 的改动早就定下来了，
              // 每条每 8 秒重拉一次纯属白烧（一个 19 条 run 的会话 = 每轮 19 个请求）
              refreshKey={m.run_id === runningRunId ? usageKey : undefined}
            />
          </div>
        )}
    </div>
  );
}

/** 附图灯箱：点缩略图看原图。原图取不回（文件被收尾清理 / 服务端拒预览）时别只留一片黑，
 *  退化成「用缩略图看 + 明确说明」，并保留「下载原图」露出真实地址。
 *  以前这里 img 没挂 onError：一 400/404 就是整块黑屏，看着像「点开失效」——
 *  其实有两个来源：① 过程截图/合图在 run 收尾被清理；② >2MB 的手机截图/高清附图
 *  撞上后端 2MB 预览上限（缩略图能出、原图 400）。现在两处都堵上，且失败有兜底。 */
function ImageLightbox({
  zoom, onClose,
}: {
  zoom: { src: string; alt: string };
  onClose: () => void;
}) {
  const [failed, setFailed] = useState(false);
  const [thumbFailed, setThumbFailed] = useState(false);
  // 原图取不回时的退路：同源缩略图（小图基本都在，清理掉的除外）
  const fallback = zoom.src.startsWith("data:")
    ? zoom.src
    : `${zoom.src}${zoom.src.includes("?") ? "&" : "?"}thumb=1280`;
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/80 p-6"
      onClick={onClose}
    >
      {failed ? (
        <div
          className="flex max-w-lg flex-col items-center gap-2 rounded-lg bg-white/95 px-6 py-5 text-center text-sm text-slate-700"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="text-base font-medium">原图暂时加载失败</div>
          <div className="text-xs text-slate-500">
            可能是网络或服务一时没响应。下面是缩略图预览，也可以点「重试」再加载一次。
          </div>
          {!thumbFailed && (
            <img
              src={fallback}
              alt={zoom.alt}
              className="mt-1 max-h-[60vh] max-w-full rounded object-contain"
              onError={() => setThumbFailed(true)}
            />
          )}
          <div className="mt-1 flex items-center gap-3 text-xs">
            <button
              className="rounded border border-slate-300 px-3 py-1 text-slate-600 hover:bg-slate-50"
              onClick={() => {
                setFailed(false);
                setThumbFailed(false);
              }}
            >
              重试
            </button>
            <a
              href={zoom.src}
              download={zoom.alt}
              className="rounded border border-slate-300 px-3 py-1 text-slate-600 hover:bg-slate-50"
            >
              下载原图
            </a>
            <button
              className="rounded border border-slate-300 px-3 py-1 text-slate-600 hover:bg-slate-50"
              onClick={onClose}
            >
              关闭（Esc）
            </button>
          </div>
        </div>
      ) : (
        <>
          <img
            src={zoom.src}
            alt={zoom.alt}
            className="max-h-[86vh] max-w-[92vw] rounded object-contain shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            onError={() => setFailed(true)}
          />
          <div
            className="mt-3 flex items-center gap-3 text-xs text-white/80"
            onClick={(e) => e.stopPropagation()}
          >
            <a
              href={zoom.src}
              download={zoom.alt}
              className="rounded border border-white/30 px-3 py-1 hover:bg-white/10"
            >
              下载原图
            </a>
            <button
              className="rounded border border-white/30 px-3 py-1 hover:bg-white/10"
              onClick={onClose}
            >
              关闭（Esc）
            </button>
          </div>
        </>
      )}
    </div>,
    document.body,
  );
}

export default function Chat({ route }: { route: Route }) {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  // 工作区列表到位了吗：没到位之前不能判定「没有项目」去自动建默认工作区，
  // 否则首帧的空数组会被误解成「工作区下确实没项目」，每次进对话都白建一个。
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  // 每个工作区各允许一次「进对话即自动建默认工作区」：按 teamId 记账，
  // 换工作区时还能再补建一次，但同一工作区不会循环重试（失败也不重试）。
  const autoWsRef = useRef<string>("");
  const projectId = route.params.project || projects[0]?.id || "";
  const [session, setSession] = useState<Session | null>(
    route.params.session && route.params.session !== "__new__"
      ? ({ id: route.params.session, title: "", created_at: "" } as Session)
      : null
  );
  const [messages, setMessages] = useState<Message[]>([]);
  // 当前 messages 属于哪个会话：切会话时接口没回来之前，messages 还是上一个会话的，
  // 不能拿它去猜「要显示哪条 run 的待办清单」（否则就把上个会话的计划贴过来了）
  const [msgSid, setMsgSid] = useState<string>("");
  const msgSidRef = useRef<string>("");
  const [msgLoading, setMsgLoading] = useState(false);
  void msgLoading; // 加载态只用于内部判定，消息区不铺骨架屏（切会话时闪一下更显卡）
  const [msgErr, setMsgErr] = useState("");
  // 「加载更早」分页状态：首屏只拉最近 MSG_PAGE 条，hasMore 表明服务端还有更早的。
  const [hasMore, setHasMore] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const loadingOlderRef = useRef(false);
  const hasMoreRef = useRef(false);
  // 用户是否已经手动往上加载过更早的消息：一旦加载过，"还有没有更早"以本地
  // loadOlder 的结果为准，不能被 4s 轮询的「最近一页 has_more」覆盖回去。
  const extendedRef = useRef(false);
  // 长会话虚拟滚动：一个会话可能几百条消息，每条都带 Markdown/图片/轨迹。
  // 以前只渲染最近 40 条 + 「显示更早」，既看不到全部又容易一次挂太多节点。
  // 现在用虚拟列表：DOM 里只保留视口附近的几十条，滚动到哪渲染到哪，
  // 几百上千条也只占一屏的节点，切会话/往上翻都不卡。
  // 本会话自己的 run（新→旧）。用 /sessions/{id}/runs 而不是 /projects/{id}/runs：
  // 项目 run 列表只给最近 50 条，老会话的 run 会被挤出窗口 → 认不出「上次跑成什么样」
  const [sessionRuns, setSessionRuns] = useState<any[]>([]);
  // 本会话的 run 列表到位了吗：轨迹「收起态」的摘要数字就从这里带回来。
  // 没到位之前先别急着展开拉事件——一条 run 能有 1400+ 事件（1.5MB），
  // 十几条一起拉就是切换会话那一下的卡顿（见 RunTraceInline 的 statPending）。
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [imgs, setImgs] = useState<string[]>([]);
  const [files, setFiles] = useState<{ path: string; name: string; size: number; kind?: string; pending?: boolean }[]>([]);
  const filesRef = useRef(files);
  const draftVersion = useRef(0);
  const uploadBusy = useRef(false);
  const [uploading, setUploading] = useState(false);
  const sendBusy = useRef(false);
  const [sending, setSending] = useState(false);
  function updateFiles(next: typeof files) {
    filesRef.current = next;
    setFiles(next);
  }
  const fileRef = useRef<HTMLInputElement>(null);
  const [runningRunId, setRunningRunId] = useState<string | null>(null);
  // 「停止」按钮的在途标记：取消是异步的（后端看门狗约 1s 掐断），期间先禁用别连点
  const [stopping, setStopping] = useState(false);
  // 底部轨迹条要展示的 run（含已取消/失败但未落消息的 run）——插话取消后思考过程不丢失
  const [traceRunIds, setTraceRunIds] = useState<string[]>([]);
  const [err, setErr] = useState("");
  const [panelW, setPanelW] = useState(360);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  // 插话在途集合：连点「插话」时第二次点击直接忽略（同一排队项只允许发一次）。
  // 之前没有这个锁，快速双击会把同一条消息发两遍，会话里出现两条一模一样的插话。
  const steerBusyRef = useRef<Set<string>>(new Set());
  const [steerBusyIds, setSteerBusyIds] = useState<string[]>([]);
  // 排队入口同步锁：执行中点「排队」时输入框要等下一拍才清空，双击会读到同一份草稿
  // 并入队两次（run 结束后两条一模一样发出去）。锁在事件循环内同步生效，500ms 后释放。
  const queueLockRef = useRef(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // 消息区滚动：进会话默认贴底（最新内容），用户自己往上翻时不再硬拽下来
  const listRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const lastSidRef = useRef<string | null>(null);
  const [showJump, setShowJump] = useState(false);
  // 会话用量条刷新信号（run 结束/新任务开始时 +1，触发 SessionUsage 重新拉聚合值）
  const [usageKey, setUsageKey] = useState(0);
  // 顶栏读数区的「让位等级」：0=全显示 1=收起本机余量 2=用量条只留 token/上下文 3=只留右侧操作按钮。
  // 顶栏读数区的「让位等级」：0=全显示 1=用量条收起速度/缓存命中 2=再收起本机余量
  // 3=再收起整条用量条 4=只留右侧操作按钮。
  // 右上角面板可拖宽（最窄时中栏只剩 ~820px），读数三条加起来放不下会把「复制会话/导出记录」
  // 挤出容器或裁掉「速度」的值。这里靠 ResizeObserver 实测 scrollWidth>clientWidth 逐级让位，
  // 不写死像素阈值——面板宽度、字体、项目名长度都变了也不会失配（用户 2026-11-14 指图要求）。
  const topbarRef = useRef<HTMLDivElement>(null);
  const readWrapRef = useRef<HTMLDivElement>(null);
  const [readLevel, setReadLevel] = useState(0);
  const [qKey, setQKey] = useState(0); // 提问面板刷新信号
  // 会话挂载技能（渐进式披露）：null=跟随项目默认；[]=明确不挂；数组=手动指定的清单
  const [attachSkills, setAttachSkills] = useState<string[] | null>(null);
  const [skillPickOpen, setSkillPickOpen] = useState(false);
  const [skillPool, setSkillPool] = useState<{ name: string; description: string; scope: string }[]>([]);
  // 技能弹层同运行选项：portal 到 body，用按钮屏幕坐标自定位。开壁纸后祖先的
  // backdrop-filter 会把它困在局部包含块里（点别处关不掉），同时让半透明白底糊住
  // 底下对话（截图里技能名和聊天文字叠在一起）——两者都靠 portal 跳出祖先解决。
  const skillBtnRef = useRef<HTMLButtonElement>(null);
  const skillPanelRef = useRef<HTMLDivElement>(null);
  const [skillPos, setSkillPos] = useState<{ left: number; bottom: number } | null>(null);
  // 运行选项（会话权限）：从顶栏挪到输入区，平时收起，别一直占着工具栏
  const [runOptsOpen, setRunOptsOpen] = useState(false);
  // 运行选项弹层改为 portal 到 body：开壁纸后祖先的 backdrop-filter 会把 fixed 定位的
  // 遮罩困在局部包含块里，点到别处关不掉。portal 后要用按钮的屏幕坐标自己定位。
  const runOptsBtnRef = useRef<HTMLButtonElement>(null);
  const runOptsPanelRef = useRef<HTMLDivElement>(null);
  const [runOptsPos, setRunOptsPos] = useState<{ left: number; bottom: number } | null>(null);
  // 运行中改运行选项 → 立刻推给正在跑的 run（下一轮生效），而不是只改本地状态。
  // 后端把 permission_scope 写回 run 选项并推进 steering 邮箱，runtime
  // 下一轮开头重算权限作用域（以前只在开跑时读一次，中途改了不生效）。
  function pushSettingsToRun(s: { permission_scope?: string }) {
    if (!runningRunId) return;
    api.steerRun(runningRunId, "", [], [], "", undefined, s)
      .then((r: any) => {
        if (r && r.delivered === false) {
          setErr("本轮执行刚刚结束，运行选项将在下一条消息生效");
        }
      })
      .catch((e: any) => setErr(e?.message || "运行选项同步失败"));
  }
  function toggleRunOpts() {
    if (runOptsOpen) { setRunOptsOpen(false); return; }
    const r = runOptsBtnRef.current?.getBoundingClientRect();
    if (r) {
      const W = 320, M = 12;
      setRunOptsPos({
        left: Math.max(M, Math.min(r.left, window.innerWidth - W - M)),
        bottom: Math.max(M, window.innerHeight - r.top + 8),
      });
    }
    setRunOptsOpen(true);
  }
  // 打开时：Esc / 窗口缩放 / 任何滚动 → 关掉（弹层定位是打开那一刻的快照，滚动后会错位）
  useEffect(() => {
    if (!runOptsOpen) return;
    const close = () => setRunOptsOpen(false);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    // 点弹层与按钮之外：用捕获阶段自己判，比整屏透明遮罩更可靠（壁纸模式下祖先
    // backdrop-filter 会裁剪/错位 fixed 遮罩，导致点别处关不掉——用户实测的第二个问题）。
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (!t) return;
      if (runOptsPanelRef.current?.contains(t)) return;   // 点面板内部：不动
      if (runOptsBtnRef.current?.contains(t)) return;     // 点按钮：交给 onClick 切换
      close();
    };
    // 面板本身可滚（max-h-[75vh] overflow-auto）：滚面板内容不能当成「底层滚动」关掉
    const onScroll = (e: Event) => {
      const t = e.target as Node | null;
      if (t && (t === runOptsPanelRef.current || runOptsPanelRef.current?.contains(t))) return;
      close();
    };
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    document.addEventListener("mousedown", onDown, true);
    return () => {
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("mousedown", onDown, true);
    };
  }, [runOptsOpen]);
  // 顶栏读数区让位：容器一溢出就升一级（少显示一组读数），直到放得下。
  // 反方向不自动降级（否则会在两级之间来回跳），只有顶栏明显变宽时才重置重测。
  useEffect(() => {
    const bar = topbarRef.current;
    const wrap = readWrapRef.current;
    if (!bar || !wrap) return;
    let raf = 0;
    const measure = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const overflow = wrap.scrollWidth - wrap.clientWidth;
        if (overflow > 1) {
          setReadLevel((lv) => (lv < 4 ? lv + 1 : lv));
        }
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(bar);
    ro.observe(wrap);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [readLevel, session?.id, projects, projectId]);

  // 顶栏变宽（面板收起/窗口放大）时重置，重新从「全显示」开始测
  useEffect(() => {
    const bar = topbarRef.current;
    if (!bar) return;
    let lastW = bar.clientWidth;
    const ro = new ResizeObserver(() => {
      const w = bar.clientWidth;
      if (w > lastW + 160) setReadLevel(0);
      lastW = w;
    });
    ro.observe(bar);
    return () => ro.disconnect();
  }, []);

  // 模型选择（图三/图四）：输入框右下角胶囊，按会话记住；空=跟随角色绑定档位
  const [modelChoice, setModelChoice] = useState<ModelChoice>(DEFAULT_MODEL_CHOICE);
  const modelKeyRef = useRef<string>("ga_model_choice:draft");
  useEffect(() => {
    const key = session && session.id !== "__new__" ? `ga_model_choice:${session.id}` : "ga_model_choice:draft";
    if (modelKeyRef.current === key) return;
    modelKeyRef.current = key;
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const p = JSON.parse(raw);
        setModelChoice({ profile: String(p.profile || ""), effort: String(p.effort || "") });
      } else {
        const draft = key !== "ga_model_choice:draft"
          ? localStorage.getItem("ga_model_choice:draft") : null;
        if (draft) {
          localStorage.setItem(key, draft);
          localStorage.removeItem("ga_model_choice:draft");
          const p = JSON.parse(draft);
          setModelChoice({ profile: String(p.profile || ""), effort: String(p.effort || "") });
        } else {
          setModelChoice(DEFAULT_MODEL_CHOICE);
        }
      }
    } catch {
      setModelChoice(DEFAULT_MODEL_CHOICE);
    }
  }, [session?.id]);
  function setModelPick(v: ModelChoice, silent?: boolean) {
    setModelChoice(v);
    try { localStorage.setItem(modelKeyRef.current, JSON.stringify(v)); } catch { /* ignore */ }
    // 手点切换 = 同时改「设置 → 智能体 → 助手」的默认：默认模型对没单独选过模型的会话生效，
    // 设置页也就跟这里看到的一致（以前两处各管各的，聊天里换了模型设置页纹丝不动）。
    // 选「跟随默认」（profile 为空）时不动默认值——否则等于把默认清空，会掉回 .env 全局端点。
    if (!silent) {
      const patch: Record<string, any> = { params_json: { reasoning_effort: v.effort || "" } };
      if (v.profile) patch.model_profile_key = v.profile;
      api.updateAgent("main", patch)
        .catch(() => { /* 没有改配置权限：按会话生效即可，不打断对话 */ });
    }
    // 执行中用户手点改模型 → 直接切这一轮后续的 LLM（不排队、不新开 run）。
    // silent=true 是程序化校正（档位被删→清回跟随默认），不该触发切换。
    if (!silent && runningRunId) {
      api.steerRun(runningRunId, "", [], [], "", { profile: v.profile, effort: v.effort })
        .then((r: any) => {
          if (r && r.delivered === false) {
            setErr("本轮执行刚刚结束，所选模型将在下一条消息生效");
          }
        })
        .catch((e: any) => setErr(e?.message || "切换模型失败"));
    }
  }
  // 会话权限作用域：仅工作区 / 全部权限。
  // 已有会话读它自己记着的值（后端 sessions.permission_scope）；
  // 新会话草稿跟「设置 → 助手行为」里的默认值走（defaultScope() 同步读本地缓存）。
  const [scope, setScope] = useState<PermissionScope>(() => defaultScope());
  const scopeKeyRef = useRef<string>("__draft__");
  // 记住当前打开会话的后端权限值：切权限 / 发送后要同步回 session 对象，
  // 但 session 对象可能已被新的列表覆盖（例如轮询刷新），所以单独存一份最新值。
  const sessScopeRef = useRef<PermissionScope | null>(null);
  useEffect(() => {
    const sid = session && session.id !== "__new__" ? session.id : "";
    if (sid) {
      // 同一个会话也会走到这里：列表刷新 / 别的标签页改了权限 / 发送后重拉会话，
      // 后端记着的值才是事实源。以前只在「换会话」时才同步，于是在 A 会话切到
      // 全部权限、切去 B、再切回 A，选择器还停在 B 的值（或发送时的旧快照），
      // 显示与实际生效的权限不一致。
      const v = (session?.permission_scope as PermissionScope) || defaultScope();
      const fresh = scopeKeyRef.current !== sid || sessScopeRef.current !== v;
      scopeKeyRef.current = sid;
      sessScopeRef.current = v;
      if (fresh) setScope(v);
    } else {
      scopeKeyRef.current = "";
      sessScopeRef.current = null;
      setScope(defaultScope());
    }
  }, [session?.id, session?.permission_scope]);
  // 设置里的默认值变了：草稿态直接跟随；已打开的会话保留自己的值
  useEffect(() => {
    const onChanged = () => {
      if (!scopeKeyRef.current) setScope(defaultScope());
    };
    window.addEventListener("ga-policy-changed", onChanged);
    return () => window.removeEventListener("ga-policy-changed", onChanged);
  }, []);
  async function changeScope(v: PermissionScope) {
    if (v === scope) return;
    setScope(v);
    // 执行中切权限 → 立刻推给正在跑的 run（下一轮起工具路径守卫按新作用域放行/拦截）
    pushSettingsToRun({ permission_scope: v });
    const sid = session && session.id !== "__new__" ? session.id : "";
    if (!sid) return; // 新会话草稿：发送时随 chat 带上
    sessScopeRef.current = v;
    try {
      // 已有会话：立即落库。这样「继续 / 自动续跑」即使不经过 chat 接口，
      // 也按新权限执行（orchestrator 读会话已存值）。
      await api.updateSession(sid, { permission_scope: v });
      setSession((cur) => (cur && sameId(cur.id, sid) ? { ...cur, permission_scope: v } : cur));
      window.dispatchEvent(new Event("ga-sessions-changed"));
    } catch (e: any) {
      setErr(e?.message || "切换权限失败");
    }
  }
  // LLM 就绪自检：一眼看出「这轮能不能调到真实大模型」。程序永远走真实模型，
  // 唯一变量是「有没有配好供应商」——没配就在输入框上方提醒去配置。
  const [llmHealth, setLlmHealth] = useState<{
    ready: boolean; model?: string;
    provider?: string | null; profile_ok: boolean; profile_issue?: string | null;
    issues: string[]; hints: string[];
  } | null>(null);
  const [llmBannerDismissed, setLlmBannerDismissed] = useState(false);
  useEffect(() => {
    let dead = false;
    const load = () => api.doctorLlm()
      .then((r: any) => { if (!dead) setLlmHealth(r); })
      .catch(() => { /* 自检失败不打断聊天 */ });
    load();
    const t = window.setInterval(load, 30000);
    return () => { dead = true; window.clearInterval(t); };
  }, []);
  // 右栏（上下文请求 / 统计）收起状态：按用户习惯记住，切会话/刷新都保持
  const [panelCollapsed, setPanelCollapsed] = useState(() => {
    try { return localStorage.getItem("ga_run_panel_collapsed") === "1"; } catch { return false; }
  });
  function toggleRunPanel(collapsed: boolean) {
    setPanelCollapsed(collapsed);
    try { localStorage.setItem("ga_run_panel_collapsed", collapsed ? "1" : "0"); } catch { /* ignore */ }
  }

  function resetTa() {
    if (taRef.current) taRef.current.style.height = "auto";
  }

  const refreshProjects = useCallback(() => {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps: ProjectSummary[]) => {
      setProjects(ps);
      setProjectsLoaded(true);
      const cur = route.params.project;
      // 当前工作区被删除（归档）后不要停在空白页：自动切到第一个工作区
      if (cur && ps.length && !ps.some((p) => p.id === cur)) {
        setSession(null);
        setMessages([]);
        navigate("chat", { project: ps[0].id });
      }
    }).catch(() => {});
  }, [currentTeamId, route.params.project]);

  useEffect(refreshProjects, [refreshProjects]);

  // 「对话」是默认落地页，必须开箱就能说话：还没有任何工作区时，
  // 自动建一个默认工作区并切过去，用户不用先跑去建项目才能发第一条消息。
  // 只在列表确实加载完、且确实为空时做一次；建失败就停下，让用户手动新建。
  useEffect(() => {
    if (!projectsLoaded || !currentTeamId || projects.length > 0) return;
    if (autoWsRef.current === currentTeamId) return;
    autoWsRef.current = currentTeamId;
    (async () => {
      try {
        const slug = `workspace-${Date.now().toString(36)}`;
        const r: any = await api.createProject(currentTeamId, {
          name: "默认工作区", slug, repo_path: `.agent/projects/${slug}`, project_type: "python",
        });
        const ps: ProjectSummary[] = await api.projects(currentTeamId);
        setProjects(ps);
        if (r?.id) {
          setSession(null);
          setMessages([]);
          navigate("chat", { project: r.id });
        }
      } catch {
        // 失败不重试（autoWsRef 已记账），避免每次渲染都打一遍接口
      }
    })();
  }, [projectsLoaded, currentTeamId, projects.length]);

  // 消息拉取的请求序号：连点会话列表时会有多个请求在飞，只允许「最后一次」的结果落地。
  // 否则先发的慢请求（老会话）后到，会把刚点开的新会话内容盖掉/盖成空。
  const msgReqRef = useRef(0);
  // 上次落到 state 的消息指纹：4s 轮询兜底时内容没变就跳过 setMessages，
  // 否则每轮都新建数组 → 整个消息树重渲染（切大会话更明显）。
  const messagesSigRef = useRef("");

  const refreshMessages = useCallback(() => {
    const sid = session && session.id !== "__new__" ? session.id : "";
    if (!sid) {
      setMessages([]);
      setMsgSid("");
      setMsgLoading(false);
      setMsgErr("");
      return;
    }
    const seq = ++msgReqRef.current;
    // 切到另一个会话：先清掉上一个会话的消息再拉。否则切进大会话时，
    // React 会先用旧消息的 DOM 渲染一遍新会话，等接口回来再整页换掉——就是那一下「严重卡顿」。
    if (!sameId(sid, msgSidRef.current)) {
      setMessages([]);
      setMsgSid(sid);
      msgSidRef.current = sid;
      messagesSigRef.current = "";
      extendedRef.current = false;
      setHasMore(false);
      hasMoreRef.current = false;
    }
    setMsgLoading(true);
    api
      .messages(sid, { limit: MSG_PAGE })
      .then((res: any) => {
        if (seq !== msgReqRef.current) return; // 已被更晚的请求取代：丢弃
        // 分页返回 { messages, has_more }；老后端/老会话仍可能返回纯数组，两种都兼容。
        const list: Message[] = Array.isArray(res) ? res : (res?.messages ?? []);
        const more = Array.isArray(res) ? false : !!res?.has_more;
        // 轮询兜底每 4s 拉一次：内容没变就别 setMessages（新数组会让整棵消息树重渲染）。
        const sig = list.map((m) => `${m.id}\u0001${m.content || ""}`).join("\u0000");
        if (sig !== messagesSigRef.current) {
          messagesSigRef.current = sig;
          // 轮询只拉最近一页；如果用户已经往上翻过（本地消息比一页多），
          // 用最新一页覆盖会把历史截断。所以只在「首屏/没有更早历史」时整体替换，
          // 否则把新出现的尾部消息追加到现有列表后面。
          setMessages((prev) => {
            if (prev.length <= MSG_PAGE) return list;
            const have = new Set(prev.map((m) => m.id));
            const tail = list.filter((m) => !have.has(m.id));
            return tail.length ? [...prev, ...tail] : prev;
          });
        }
        // 轮询只反映「最近一页之后是否还有更早」，用户一旦向上加载过历史，
        // 「还有没有更早」以 loadOlder 的结果为准，别被轮询覆盖回去（否则按钮会反复跳）。
        if (!extendedRef.current) {
          setHasMore(more);
          hasMoreRef.current = more;
        }
        setMsgSid(sid);
        msgSidRef.current = sid;
        setMsgErr("");
      })
      .catch((e: any) => {
        if (seq !== msgReqRef.current) return;
        // 只有「会话确实不存在」才回退到新对话。网络抖动 / 超时 / 5xx 一律保留现有内容并提示重试——
        // 之前这里对任何错误都 setSession(null)+setMessages([])，连点几下对话列表就能把对话点成空白。
        if (e?.status === 404) {
          setSession(null);
          setMessages([]);
          setMsgSid("");
          navigate("chat", { project: projectId });
          return;
        }
        setMsgErr(e?.message ? `加载失败：${e.message}` : "加载失败，请重试");
      })
      .finally(() => {
        if (seq === msgReqRef.current) setMsgLoading(false);
      });
  }, [session?.id, projectId]);

  useEffect(refreshMessages, [refreshMessages]);

  // 向上翻到顶时按 created_at cursor 拉更早的一页，插到列表最前面。
  // 虚拟列表会换掉行 key → DOM 重排，所以先记住"离底部有多远"，插完把同样的距离还回去，
  // 视觉上就像内容往上长、当前位置不动（这是无限上翻不跳的常用手法）。
  const loadOlder = useCallback(async () => {
    const sid = session && session.id !== "__new__" ? session.id : "";
    const el = listRef.current;
    if (!sid || !el || loadingOlderRef.current || !hasMoreRef.current) return;
    // 数组按 created_at 升序，第一条就是当前最老的一条；用 (created_at, id) 复合作游标，
    // 同一秒并列的多条也能正好接上（只用时间做 < 会漏掉并列的）。
    const oldestMsg = messages.find((m) => m && m.created_at);
    if (!oldestMsg) return;
    const oldest = oldestMsg.created_at;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    const distToBottom = el.scrollHeight - el.scrollTop;
    try {
      const res: any = await api.messages(sid, { limit: MSG_PAGE, before: oldest, before_id: oldestMsg.id });
      if (!sameId(sid, msgSidRef.current)) return; // 中途切了会话：丢弃
      const older: Message[] = (Array.isArray(res) ? res : (res?.messages ?? []))
        .filter((m: Message) => m && m.id);
      const more = Array.isArray(res) ? false : !!res?.has_more;
      if (older.length) {
        messagesSigRef.current = ""; // 强制下一次 setMessages 生效
        setMessages((prev) => {
          const have = new Set(prev.map((m) => m.id));
          const add = older.filter((m) => !have.has(m.id));
          return add.length ? [...add, ...prev] : prev;
        });
      }
      setHasMore(more);
      hasMoreRef.current = more;
      extendedRef.current = true;
      requestAnimationFrame(() => {
        const e2 = listRef.current;
        if (e2) e2.scrollTop = e2.scrollHeight - distToBottom;
      });
    } catch {
      /* 失败就保持原状，下次滚到顶会再试 */
    } finally {
      loadingOlderRef.current = false;
      setLoadingOlder(false);
    }
  }, [session?.id, messages]);

  // 「执行中」跟踪的镜像 ref：ws 回调里判断 run_done 是否属于当前跟踪的 run
  const runningRef = useRef<string | null>(null);
  // 桌面通知点击的落点（跳到那条会话）。通知的 onclick 跑在 WS 回调里，而 WS 回调是
  // 「建连那一刻」的闭包——用户中途切过会话，那里读到的 session/projectId 就是旧的。
  // 所以统一经 ref 取最新的一份（在下面的 effect 里每帧更新）。
  const jumpToSessionRef = useRef<(projectOfSession: string, sid: string) => void>(() => {});
  // 是否已经从服务端确认过「这个会话现在有没有 run 在跑」；没确认过之前不发排队消息
  const runsChecked = useRef(false);
  useEffect(() => {
    runningRef.current = runningRunId;
  }, [runningRunId]);

  function addTrace(id: string | null | undefined) {
    if (!id) return;
    setTraceRunIds((cur) => (cur.includes(id) ? cur : [...cur.slice(-5), id]));
  }

  // 订阅项目事件：对话全链路 WebSocket 驱动（无轮询）——
  // message_new 刷消息流；run_done 结束「执行中」跟踪并兜底刷新；
  // run 事件转发给聊天流内嵌轨迹（RunTraceInline 实时显示思考流式与工具执行）
  // 系统提示：run_done / question_asked 触发提示音+桌面通知（跨会话都提醒；
  // 已提醒过的 run/提问记集合去重，断线重连补拉时不重复响）
  const notifiedDone = useRef<Set<string>>(new Set());
  const notifiedQ = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!projectId) return;
    const socket = new ProjectSocket(projectId);
    socket.on((e) => {
      if (e.run_id) {
        window.dispatchEvent(new CustomEvent(`ga-run-ev-${e.run_id}`, { detail: e }));
      }
      const mine = sameId(e.payload?.session_id, session?.id);
      if ((e.type === "message_new" || e.type === "question_asked"
           || e.type === "question_answered") && mine) {
        setQKey((k) => k + 1); // 新提问到达 / 已回答 → 刷新提问面板
        refreshMessages();
      }
      // 会话列表自动刷新：新消息 / run 结束都要让侧栏的标题与「执行中」转圈跟上来。
      // 以前只在本地操作（归档/删除）时刷新，后台在跑也看不到任何变化。
      if (e.type === "message_new" || e.type === "run_done" || e.type === "question_asked") {
        window.dispatchEvent(new Event("ga-sessions-changed"));
      }
      if (e.type === "run_done") {
        const st = e.payload?.status;
        // 这条 run 属于哪条会话：后端在 run_done 载荷里带上了 session_id（事件通道按
        // 项目扇出，不带就不知道点开该跳哪条会话）。老数据/老服务没有这个字段时
        // jump 为 undefined —— 通知照常弹、点了只把窗口拉到前面，不会跳到错的地方。
        const sid = String(e.payload?.session_id || "");
        const jump = sid ? () => jumpToSessionRef.current(projectId, sid) : undefined;
        if (e.run_id && !notifiedDone.current.has(e.run_id)) {
          notifiedDone.current.add(e.run_id);
          if (st === "succeeded")
            fireNotify("done", "任务完成", "运行已成功结束 · 点击跳到这条会话", jump);
          else if (st === "failed")
            fireNotify("done", "任务失败",
                       (truncBody(e.payload?.error) || "运行失败，详情见对话页") + " · 点击跳到这条会话",
                       jump);
          else if (st === "cancelled")
            fireNotify("done", "任务已取消", "运行被停止 · 点击跳到这条会话", jump);
        }
      }
      if (e.type === "question_asked") {
        const qid = e.payload?.id || e.payload?.message_id;
        if (qid && !notifiedQ.current.has(qid)) {
          notifiedQ.current.add(qid);
          const sid = String(e.payload?.session_id || "");
          fireNotify("question", "需要你的回答",
                     (truncBody(e.payload?.question) || "智能体在等待你的回复") + " · 点击跳到这条会话",
                     sid ? () => jumpToSessionRef.current(projectId, sid) : undefined);
        }
      }
      if (e.type === "run_done" && sameId(e.run_id, runningRef.current)) {
        setRunningRunId(null); // 终态：结束跟踪（排队队列自动续发）
        refreshMessages(); // 确保 AI 回复/失败提示落进聊天流
        setUsageKey((k) => k + 1); // 用量条刷新（总 token / 缓存命中率 / 速度）
      }
    });
    socket.onConnection((ok) => {
      if (ok) refreshMessages(); // 断线重连后补拉一次，弥补离线期间错过的事件
    });
    socket.start();
    return () => socket.close();
  }, [projectId, session?.id, refreshMessages]);

  // 进入历史会话时恢复最近一次 run，让运行结果卡片（含变更报告）可见；
  // 若该 run 仍在执行，同时恢复 runningRunId —— 接上 4s 轮询兜底刷新与消息排队
  useEffect(() => {
    if (!session || session.id === "__new__" || !projectId) {
      setSessionRuns([]);
      setRunsLoaded(false);
      return;
    }
    let alive = true;
    const sid = session.id;
    // 切会话先把上一条会话的 run 摘要清掉：否则新会话第一帧拿旧摘要去算轨迹收起态，
    // 会一批批展开又收起，既闪又卡。
    setSessionRuns([]);
    setRunsLoaded(false);
    api
      .sessionRuns(sid)
      .then((res: any) => {
        const rs: any[] = Array.isArray(res) ? res : (res?.runs ?? res?.items ?? []);
        if (!alive) return;
        setSessionRuns(rs);
        setRunsLoaded(true);
        const mine = rs[0]; // 新→旧，第一条就是本会话最近一次执行
        if (!mine) return;
        setActiveRunId(mine.id);
        addTrace(mine.id); // 失败/取消未落消息的 run 也能看到轨迹（有消息时渲染层自动隐藏）
        if (!["succeeded", "failed", "cancelled", "blocked"].includes(mine.status)) {
          setRunningRunId((cur) => cur ?? mine.id);
        }
      })
      // 列表拉不到也放行：轨迹该显示还得显示，不能因为摘要没拿到就一直空着
      .catch(() => { if (alive) setRunsLoaded(true); });
    return () => {
      alive = false;
    };
  }, [session?.id, projectId]);

  /** 贴底判定：留 80px 宽容度，避免"差一像素就判定用户翻页了"。 */
  const nearBottom = (el: HTMLDivElement) => el.scrollHeight - el.scrollTop - el.clientHeight <= 80;

  // 程序化滚动标记：我们自己调的 scrollTop 会触发 onScroll，不能让它被当成
  // 「用户往上翻」把 stickRef 关掉（否则内容一变高就再也不贴底了）。
  const autoScrollRef = useRef(false);
  const applyBottom = (el: HTMLDivElement) => {
    autoScrollRef.current = true;
    el.scrollTop = el.scrollHeight;
    requestAnimationFrame(() => { autoScrollRef.current = false; });
  };

  const jumpToBottom = (smooth = false) => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    stickRef.current = true;
    setShowJump(false);
  };

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const sid = session?.id ?? "";
    stickRef.current = true;
    setShowJump(false);
    if (lastSidRef.current !== sid) {
      lastSidRef.current = sid;
      // 上一个会话的临时错误（图片超限…）不跟着这个会话显示
      setErr("");
    }
    applyBottom(el);
    const r1 = requestAnimationFrame(() => {
      applyBottom(el);
      requestAnimationFrame(() => applyBottom(el));
    });
    const t = window.setTimeout(() => applyBottom(el), 150);
    return () => {
      cancelAnimationFrame(r1);
      clearTimeout(t);
    };
  }, [session?.id]);

  // 新消息/轨迹增量：只有本来就贴在底部时才跟着滚（用户正在看历史就别动）
  useEffect(() => {
    const el = listRef.current;
    if (!el || !stickRef.current) return;
    applyBottom(el);
    const t = window.setTimeout(() => {
      if (stickRef.current) applyBottom(el);
    }, 160);
    return () => clearTimeout(t);
  }, [messages, traceRunIds]);

  // 内容二次撑高（图片解码、Markdown 重排、轨迹行异步展开）时，如果本来就贴底就继续贴底。
  // 以前只在 messages 变化的那一瞬滚一次：图片晚到把内容顶高，视图停在倒数第二屏，
  // 而且那次被自己的 scroll 事件判成「用户翻页了」——进会话后右下角一直挂着「回到最新」。
  // 注意：虚拟列表下消息行在 inner 容器里（absolute 定位），要观察 inner 容器本身的
  // 高度变化，而不是 listRef 的直接子节点（那是 virtualizer 的定位容器，高度由它自己撑）。
  useEffect(() => {
    const el = listRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const inner = el.firstElementChild;
    if (!inner) return;
    const ro = new ResizeObserver(() => {
      if (stickRef.current) applyBottom(el);
    });
    ro.observe(inner);
    return () => ro.disconnect();
  }, [messages, traceRunIds, session?.id]);

  // 执行态自愈：每 4s 用服务端事实校准「本会话是否有 run 在跑」。
  // 覆盖刷新页面、别的标签页发起的 run、WS 丢事件导致的"看着像没在跑"。
  useEffect(() => {
    runsChecked.current = false; // 换会话/换项目：先重新确认服务端在跑什么，再决定要不要发排队消息
    if (!session || session.id === "__new__" || !projectId) return;
    let alive = true;
    let lastLive = false;
    const tick = async () => {
      try {
        // 按会话查（不受项目 50 条窗口限制），顺手刷新「本会话最近一次执行」的状态，
        // 让看门狗超时/取消这类外部终态 4s 内就能在聊天区给出提示
        const res: any = await api.sessionRuns(session.id);
        const rs: any[] = Array.isArray(res) ? res : (res?.runs ?? res?.items ?? []);
        if (!alive) return;
        setSessionRuns(rs);
        setRunsLoaded(true);
        runsChecked.current = true;
        const live = rs.filter(
          (r) => sameId(r.session_id, session.id) &&
            !["succeeded", "failed", "cancelled", "blocked"].includes(r.status),
        );
        // 服务端是唯一事实源：有在跑的就对齐到最新的那条（覆盖「卡在已结束的旧 run 上」），
        // 没有就清空——否则刷新页面后可能永远停在一条早已结束的 run 上。
        if (live.length) {
          addTrace(live[0].id);
          setRunningRunId(live[0].id);
        } else {
          setRunningRunId((cur) => (cur ? null : cur));
        }
        lastLive = live.length > 0;
        // 消息流兜底：每 4s 都补拉一次（不只在执行中）。WS 丢事件/半断时，
        // 新消息最多晚 4s 出现，而不是「很久才自动刷新」。内容指纹会挡掉无变化的 setMessages。
        // 页面隐藏（切走标签页/最小化）时跳过本轮轮询，省流量与 CPU；回到前台立刻补一次。
        if (document.hidden) return;
        refreshMessages();
      } catch {
        /* 网络抖动忽略，下个周期再校准 */
      }
    };
    tick();
    const t = window.setInterval(tick, 4000);
    const onVis = () => { if (!document.hidden) tick(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVis);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id, projectId]);

  // 执行中：每 8s 刷新用量条，token 数/缓存命中率随调用增长（不轮询消息流本身）
  useEffect(() => {
    if (!runningRunId) return;
    const t = window.setInterval(() => setUsageKey((k) => k + 1), 8000);
    return () => clearInterval(t);
  }, [runningRunId]);

  /** 清空输入区（切项目/切会话/新建对话）。排队队列**不**跟着清：
   *  它按会话存在存档里，切回那条会话时会原样接上并继续自动发。 */
  function clearDraft() {
    draftVersion.current += 1;
    setInput("");
    setImgs([]);
    updateFiles([]);
    setQueue([]);
  }

  async function pickProject(id: string) {
    clearDraft();
    setActiveRunId(null);
    setRunningRunId(null);
    setTraceRunIds([]);
    setSession(null);
    navigate("chat", { project: id });
  }

  function onPickSession(projectOfSession: string, s: Session) {
    // 重复点同一条会话：什么都不做（不必重建 WebSocket、不必重拉消息）。
    // 连点会话列表本来就不该让对话内容发生变化。
    if (s.id !== "__new__" && session && sameId(s.id, session.id)) return;
    clearDraft();
    setActiveRunId(null);
    setRunningRunId(null);
    setTraceRunIds([]);
    setSessionRuns([]);
    setRunsLoaded(false);
    setMsgErr("");
    // 先把上一个会话的重消息区清掉再切换：否则切进新会话的第一帧还要把上一个会话的
    // 全部 Markdown/图片/轨迹重新渲染一遍，这就是「切换会话严重卡顿」的主要来源。
    setMessages([]);
    setMsgSid("");
    if (s.id === "__new__") {
      setSession(null);
      setMessages([]);
      navigate("chat", { project: projectOfSession });
      return;
    }
    setSession(s);
    // 进入已有会话时同步它挂载的技能（null=跟随项目默认）
    setAttachSkills(Array.isArray(s.skills) ? s.skills : null);
    // 权限作用域也跟随该会话（见上面的 scope effect）
    navigate("chat", { project: projectOfSession, session: s.id });
  }

  // 桌面通知点击的落点：正好是当前会话就滚到最新（新回复在底部，别让人自己找），
  // 是别的会话就走「点会话列表」同一套流程（清草稿、重置跟踪、切项目+会话）。
  // 每帧重挂一次，保证闭包里的 session/projectId 是最新的。
  useEffect(() => {
    jumpToSessionRef.current = (projectOfSession, sid) => {
      if (!sid) return;
      if (session && sameId(sid, session.id)) {
        jumpToBottom(true);
        return;
      }
      onPickSession(projectOfSession || projectId,
                    { id: sid, title: "", created_at: "" } as Session);
    };
  });

  async function addFiles(list: FileList | File[] | null) {
    if (!list?.length || !projectId || sendBusy.current) return;
    if (uploadBusy.current) {
      setErr("文件正在上传，请稍候再添加");
      return;
    }
    uploadBusy.current = true;
    setUploading(true);
    const version = draftVersion.current;
    try {
    const arr = Array.from(list);
    const imgFiles = arr.filter((f) => f.type.startsWith("image/"));
    const docFiles = arr.filter((f) => !f.type.startsWith("image/"));
    setErr("");
    const dataUrls: string[] = [];
    for (const f of imgFiles) {
      if (f.size > 6 * 1024 * 1024) {
        setErr(`${f.name} 超过 6MB，请压缩后再上传`);
        continue;
      }
      try {
        const url = await new Promise<string>((resolve, reject) => {
          const r = new FileReader();
          r.onload = () => resolve(String(r.result));
          r.onerror = () => reject(r.error);
          r.readAsDataURL(f);
        });
        dataUrls.push(url);
      } catch {
        /* 单张读取失败跳过 */
      }
    }
    if (version !== draftVersion.current) return;
    if (dataUrls.length) {
      setImgs((cur) => {
        const fresh = dataUrls.filter((u) => !cur.includes(u));
        const room = 4 - cur.length;
        if (fresh.length > room) setErr("最多附 4 张图片");
        return [...cur, ...fresh.slice(0, Math.max(0, room))];
      });
    }
    // 其他文件：先上传落工作区（.trace/uploads/），成功后挂到草稿；失败的直接报错不占位
    for (const f of docFiles) {
      if (f.size > 30 * 1024 * 1024) {
        setErr(`${f.name} 超过 30MB，请压缩后再传`);
        continue;
      }
      if (filesRef.current.length >= 6) {
        setErr("最多带 6 个附件");
        break;
      }
      const localId = crypto.randomUUID();
      updateFiles([...filesRef.current, { path: localId, name: f.name, size: f.size, pending: true }]);
      try {
        const r: any = await api.uploadProjectFile(projectId, f);
        if (version !== draftVersion.current) return;
        updateFiles(filesRef.current.map((x) => (x.path === localId
          ? { path: r.path, name: r.name || f.name, size: r.size ?? f.size, kind: r.kind } : x)));
      } catch (e: any) {
        if (version !== draftVersion.current) return;
        updateFiles(filesRef.current.filter((x) => x.path !== localId));
        setErr(`${f.name} 上传失败：${e?.message || "未知错误"}`);
      }
    }
    } finally {
      uploadBusy.current = false;
      setUploading(false);
    }
  }

  // 粘贴截图/图片：全局监听（焦点在页面任意位置都能 Ctrl+V）
  useEffect(() => {
    function onPaste(e: ClipboardEvent) {
      const dt = e.clipboardData;
      if (!dt) return;
      const files = Array.from(dt.files || []).filter((f) => f.type.startsWith("image/"));
      if (!files.length) {
        // 部分浏览器把截图放在 items 而非 files
        for (const it of Array.from(dt.items || [])) {
          if (it.kind === "file" && it.type.startsWith("image/")) {
            const f = it.getAsFile();
            if (f) files.push(f);
          }
        }
      }
      if (files.length) {
        e.preventDefault(); // 只拦截图片，不影响正常文本粘贴
        addFiles(files);
      }
    }
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [projectId]);

  // 拖拽图片/文件到聊天区域直接放入
  function onDrop(e: React.DragEvent) {
    const dropped = Array.from(e.dataTransfer?.files || []);
    if (dropped.length) {
      e.preventDefault();
      addFiles(dropped);
    }
  }
  function onDragOver(e: React.DragEvent) {
    if (Array.from(e.dataTransfer?.items || []).some((i) => i.kind === "file")) {
      e.preventDefault(); // 仅对文件拖入允许放置，不影响文本拖拽
    }
  }

  async function doSend(content: string, imgList: string[], fileList: { path: string; name: string; size: number }[] = [], modelOverride?: ModelChoice, scopeOverride?: PermissionScope) {
    if ((!content.trim() && !imgList.length && !fileList.length) || !projectId || sendBusy.current) return false;
    sendBusy.current = true;
    setSending(true);
    const version = draftVersion.current;
    const chosenModel = modelOverride ?? modelChoice;
    const chosenScope = scopeOverride ?? scope;
    setErr("");
    try {
      const res = await api.chat(
        projectId,
        {
          content,
          // 模型选择：空=跟随角色绑定，不改变现有行为
          ...(chosenModel.profile ? { model_profile_key: chosenModel.profile } : {}),
          ...(chosenModel.effort ? { reasoning_effort: chosenModel.effort } : {}),
          images: imgList,
          files: fileList,
          // 挂载技能：没动过选择器就不传（跟随项目默认）；新会话/老会话都按这份清单生效
          ...(attachSkills !== null ? { skills: attachSkills } : {}),
          // 权限作用域：只有与「会话已存值」不一致（或新会话）时才带上。
          // 正常发送不改变已存权限；切了权限会先落库，这里只是双保险。
          ...(chosenScope !== (sessScopeRef.current ?? chosenScope)
            ? { permission_scope: chosenScope } : {}),
          // 已有会话则续聊（同一会话历史上下文），新对话/占位符不传
          ...(session && session.id !== "__new__" ? { session_id: session.id } : {}),
        },
        crypto.randomUUID()
      );
      if (version !== draftVersion.current) return true;
      const sid = res.session_id;
      sessScopeRef.current = chosenScope;
      setSession({ id: sid, title: content.slice(0, 40) || "（附件消息）", created_at: "",
                   permission_scope: chosenScope });
      setActiveRunId(res.run_id);
      setRunningRunId(res.run_id);
      addTrace(res.run_id);
      navigate("chat", { project: projectId, session: sid });
      refreshMessages();
      // 新会话/新 run 立刻反映到左侧会话树（不等下一次轮询）
      window.dispatchEvent(new Event("ga-sessions-changed"));
      // 自己发的消息一定拉到底（哪怕刚才正在往上翻历史）
      stickRef.current = true;
      setShowJump(false);
      requestAnimationFrame(() => jumpToBottom());
      return true;
    } catch (e: any) {
      if (version === draftVersion.current) setErr(e?.message || "发送失败");
      return false;
    } finally {
      sendBusy.current = false;
      setSending(false);
    }
  }

  async function send() {
    if (sendBusy.current || (!input.trim() && !imgs.length && !files.length) || !projectId) return;
    if (uploadBusy.current || files.some((f) => f.pending)) {
      setErr("附件还在上传中，请稍候再发送");
      return;
    }
    const version = draftVersion.current;
    const content = input;
    const imgList = imgs;
    const fileList = files.map(({ path, name, size }) => ({ path, name, size }));
    if (runningRunId) {
      // 同一条会话里已有 run 在跑：仍按序排队（这是会话内的顺序，不是跨会话限制）。
      // 跨会话不受影响——别的会话各有各的 runningRunId 与队列，后端 worker 池并行执行。
      // 连点「排队」：同步锁挡住第二次，避免同一条草稿入队两遍（run 结束后会发两次）。
      if (queueLockRef.current) return;
      queueLockRef.current = true;
      setQueue((q) => [...q, { id: crypto.randomUUID(), content, images: imgList, files: fileList,
                                modelProfile: modelChoice.profile, reasoningEffort: modelChoice.effort,
                                scope }]);
      window.setTimeout(() => { queueLockRef.current = false; }, 500);
    } else if (!await doSend(content, imgList, fileList)) {
      return;
    }
    if (version !== draftVersion.current) return;
    setInput("");
    setImgs([]);
    updateFiles([]);
    resetTa();
  }

  /** 打开技能选择器：每次都重拉清单（技能可能刚在配置页增删过） */
  function openSkillPicker() {
    if (skillPickOpen) { setSkillPickOpen(false); return; }
    const r = skillBtnRef.current?.getBoundingClientRect();
    if (r) {
      const W = 320, M = 12;
      setSkillPos({
        left: Math.max(M, Math.min(r.left, window.innerWidth - W - M)),
        bottom: Math.max(M, window.innerHeight - r.top + 8),
      });
    }
    setSkillPickOpen(true);
    api
      .skills(projectId)
      .then((r: any) => setSkillPool(r.skills || []))
      .catch(() => setSkillPool([]));
  }
  // 技能弹层打开时：Esc / 窗口缩放 / 底层滚动 → 关掉（定位是打开那刻的快照，滚动后会错位）。
  // 点弹层与按钮之外：捕获阶段自己判，比整屏透明遮罩可靠——壁纸模式下祖先 backdrop-filter
  // 会裁剪/错位 fixed 遮罩，造成「点别处关不掉」（用户实测）。
  // 注意：scroll 用捕获阶段能捕获任意可滚容器，但技能列表自身也是可滚容器——滚列表内容
  // 触发的 scroll target 会落在面板内部，此时不能关（否则技能多时滚一下选中框就消失）。
  useEffect(() => {
    if (!skillPickOpen) return;
    const close = () => setSkillPickOpen(false);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (!t) return;
      if (skillPanelRef.current?.contains(t)) return;   // 点面板内部：不动
      if (skillBtnRef.current?.contains(t)) return;     // 点按钮：交给 onClick 切换
      close();
    };
    const onScroll = (e: Event) => {
      const t = e.target as Node | null;
      if (t && (t === skillPanelRef.current || skillPanelRef.current?.contains(t))) return; // 滚列表：不动
      close();
    };
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    document.addEventListener("mousedown", onDown, true);
    return () => {
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("mousedown", onDown, true);
    };
  }, [skillPickOpen]);

  function toggleSkill(name: string) {
    setAttachSkills((cur) => {
      const base = cur ?? []; // 从「跟随默认」勾第一个起转为手动指定模式
      return base.includes(name) ? base.filter((x) => x !== name) : [...base, name].slice(0, 12);
    });
  }

  /** 停止当前执行（原来在右栏 RunPanel 的「取消」按钮上——执行中想停手还得先转头找右栏）。 */
  async function stopRun() {
    if (!runningRunId || stopping) return;
    setStopping(true);
    setErr("");
    try {
      await api.cancelRun(runningRunId);
    } catch (e: any) {
      setErr(e?.message || "停止失败");
    }
    // 终态由 WS/轮询推回来（runningRunId 自动清空）；8s 兜底清按钮状态，不让它永久禁用
    window.setTimeout(() => setStopping(false), 8000);
  }

  // 执行中输入框的状态决定主按钮：空 → 「停止」，有草稿 → 「排队」（仅本会话内排队；
  // 别的会话各自独立执行，不受这里影响）
  const hasDraft = !!input.trim() || imgs.length > 0 || files.length > 0;

  // 「插话」：把这条排队消息并进**正在执行**的那一轮（不取消、不重开）。
  // 以前这里是先 cancel 再重开一个 run —— 用户实测反馈「我是插话，不是取消」：
  // 组长会从零重来、已探过的路重走。现在只是往当前对话追加一条 user 消息。
  async function sendNow(id: string) {
    // 连点保护：同一个排队项一次只发一条（双击/手抖不会出现两条重复插话）
    if (steerBusyRef.current.has(id)) return;
    const item = queue.find((q) => q.id === id);
    if (!item) return;
    steerBusyRef.current.add(id);
    setSteerBusyIds((cur) => (cur.includes(id) ? cur : [...cur, id]));
    try {
      if (!runningRunId) {
        if (await doSend(item.content, item.images, item.files,
          { profile: item.modelProfile || "", effort: item.reasoningEffort || "" }, item.scope)) {
          setQueue((q) => q.filter((x) => x.id !== id));
        }
        return;
      }
      // 正常情况下图片已从 IndexedDB 还原；只有存储不可用/被清理时才走到这里，不能悄悄丢图
      if (!item.images.length && (item.imageCount || 0) > 0) {
        setErr("这条消息的图片在刷新后失效了：点「编辑」重新附上再插话");
        return;
      }
      setErr("");
      try {
        const r: any = await api.steerRun(runningRunId, item.content, item.images, item.files, id);
        if (r?.delivered === false) {
          // 这个 run 其实已经结束了：当成普通消息发出去
          setRunningRunId(null);
          if (await doSend(item.content, item.images, item.files,
            { profile: item.modelProfile || "", effort: item.reasoningEffort || "" }, item.scope)) {
            setQueue((q) => q.filter((x) => x.id !== id));
          }
          return;
        }
      } catch (e: any) {
        // 送不进去就留在队列里，别丢消息
        setErr(e?.message || "插话失败，已留在队列");
        return;
      }
      setQueue((q) => q.filter((x) => x.id !== id));
      refreshMessages();
    } finally {
      steerBusyRef.current.delete(id);
      setSteerBusyIds((cur) => cur.filter((x) => x !== id));
    }
  }

  /** 「编辑」排队消息：连同图片/附件一起退回输入框，改完重新排队或插话。
   *  以前排队项只能移除重写，带图的连移除都等于白拍（图片只存在内存里）。 */
  function editQueued(id: string) {
    const item = queue.find((q) => q.id === id);
    if (!item) return;
    setQueue((q) => q.filter((x) => x.id !== id));
    draftVersion.current += 1;
    setInput(item.content);
    setImgs(item.images);
    updateFiles(item.files.map((f) => ({ ...f })));
    setErr(!item.images.length && (item.imageCount || 0) > 0
      ? "这条消息的图片在刷新后失效了，请重新附上再发送" : "");
    requestAnimationFrame(() => taRef.current?.focus());
  }

  // run 结束后自动发送排队消息：**只要「没在跑」成立就反复试队首**，而不是只在
  // runningRunId 变空的那一刻试一次。那一刻只要没发出去（发送锁还没释放、网络报错、
  // 切项目时 projectId 还没就绪、WS 掉了 run_done 而 4s 校准还没到），旧写法就再也不会
  // 重试，排队消息永久躺在队列里——用户看到的就是「上一轮做完了，排队的没接着做」。
  useEffect(() => {
    if (!queue.length) return;
    let dead = false;
    const tryHead = () => {
      // 服务端有没有在跑的 run 还没校准过就先别发：否则刷新页面后第一时间会把
      // 排队消息直接开成新任务，抢掉原本该「插进这一轮」的话。
      if (runningRunId || !runsChecked.current || sendBusy.current) return;
      const first = queue[0];
      // 正在「插话」发送中的条目不要被自动发送抢跑（否则会在插话成功后又被当成普通消息发一遍）
      if (steerBusyRef.current.has(first.id)) return;
      // 图片在刷新后失效的消息不自动发：宁可停在这儿（行上有「点编辑重新附上」），
      // 也不能把用户发的截图悄悄扔掉。
      if (!first.images.length && (first.imageCount || 0) > 0) return;
      doSend(first.content, first.images, first.files,
        { profile: first.modelProfile || "", effort: first.reasoningEffort || "" }, first.scope).then((sent) => {
        if (sent && !dead) setQueue((q) => q.filter((x) => x.id !== first.id));
      });
    };
    tryHead();
    const t = window.setInterval(tryHead, 2000);
    return () => {
      dead = true;
      window.clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue, runningRunId, projectId]);

  // 排队队列按会话存档/回读（切会话那一次渲染里 queue 还是上一个会话的值，不能覆盖存档）。
  // 文字/附件走 localStorage，图片走 IndexedDB（见 queueMedia.ts）：刷新后图片也能原样回来。
  // 注意：必须等 IndexedDB 里的图片救回来之后才允许回写存档，否则首屏那次「空图队列」
  // 会把库里刚存好的图片覆盖掉（实测刷新一次图就没了）。
  const queueHydratedRef = useRef<string | null>(null);
  useEffect(() => {
    const sid = session?.id;
    queueHydratedRef.current = null;
    setQueue(loadQueue(sid));
    if (!sid || sid === "__new__") {
      queueHydratedRef.current = "";
      return;
    }
    let dead = false;
    loadQueueImages(sid).then((map) => {
      if (dead) return;
      setQueue((cur) => cur.map((q) => (
        !q.images.length && map[q.id]?.length ? { ...q, images: map[q.id] } : q
      )));
      queueHydratedRef.current = sid;
    });
    return () => { dead = true; };
  }, [session?.id]);
  const queueSidRef = useRef<string | null>(null);
  useEffect(() => {
    const sid = session?.id ?? null;
    if (!sid || sid === "__new__") return;
    if (queueSidRef.current !== sid) {
      queueSidRef.current = sid;
      return;
    }
    if (queueHydratedRef.current !== sid) return; // 图片还没救回来，先别覆盖存档
    saveQueue(sid, queue);
    void saveQueueImages(sid, queue.map((q) => ({ id: q.id, images: q.images })));
  }, [queue, session?.id]);

  // 输入框上方「待办清单」显示哪条 run 的计划：
  // 正在执行的优先 → 否则最近一条助手消息 → 否则还没落消息的实时轨迹 run
  // 注意 messages 必须确认属于当前会话（切会话时它会短暂残留上一个会话的内容）
  const planRunId = useMemo(() => {
    if (runningRunId) return runningRunId;
    const msgMine = !!session?.id && sameId(msgSid, session.id);
    if (msgMine) {
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].run_id) return messages[i].run_id as string;
      }
    }
    for (let i = traceRunIds.length - 1; i >= 0; i--) {
      if (!(msgMine && messages.some((m) => ownsRunUi(m) && m.run_id === traceRunIds[i]))) return traceRunIds[i];
    }
    return null;
  }, [runningRunId, messages, msgSid, session?.id, traceRunIds]);

  // 执行轨迹挂在「这条 run 的一条消息」上——同一 run 往往同时有助手消息和
  // 「本次会话已结束」等系统消息，两条都带 run_id。以前逐条渲染，整条轨迹连同
  // 每批素材被原样画两遍（用户反馈「上面的图又被渲染到下面」）。这里每个 run
  // 只选一个宿主：优先最后一条助手消息；只有系统消息的（失败/取消）退回最后一条。
  const traceHostIdxOfRun = useMemo(() => {
    const map: Record<string, number> = {};
    messages.forEach((m, i) => {
      if (!ownsRunUi(m)) return;
      const prev = map[m.run_id as string];
      if (prev === undefined || m.author_type === "agent") map[m.run_id as string] = i;
    });
    return map;
  }, [messages]);

  // 改动卡片挂在哪个宿主上：
  // - 有轨迹的 run → 轨迹宿主（同一块里按顺序排：消息 → 轨迹 → 改动卡片）；
  // - 没轨迹的 run（纯聊天）但落了助手消息 → 最后一条助手消息；
  // - 只有系统消息的（失败/取消）→ 最后一条，卡片本来就没有文件会自己隐藏。
  // 以前一律取「最后一条助手消息」，可助手消息是运行中途落的（见 _post_reply），
  // 轨迹还在往后长：卡片就被夹在「编辑」和后面那些「读取 / 思考」中间（用户截图 2026-11-02）。
  const lastMsgIdxOfRun = useMemo(() => {
    const map: Record<string, number> = {};
    const traced = new Set(traceRunIds);
    const fallback: Record<string, number> = {};
    messages.forEach((m, i) => {
      if (!ownsRunUi(m)) return;
      const rid = m.run_id as string;
      if (traced.has(rid)) {
        if (traceHostIdxOfRun[rid] === i) map[rid] = i;
      } else {
        fallback[rid] = i;
      }
    });
    Object.entries(fallback).forEach(([rid, i]) => { map[rid] = i; });
    return map;
  }, [messages, traceRunIds, traceHostIdxOfRun]);

  // 底部还在实时生长的轨迹块（run 已开跑但还没落助手消息）；只渲染最近 3 条
  const pendingTraceIds = useMemo(
    () =>
      traceRunIds
        .filter((id) => !messages.some((m) => ownsRunUi(m) && m.run_id === id))
        .slice(-3),
    [traceRunIds, messages],
  );

  // ---- 消息虚拟列表：DOM 里只保留视口附近的若干条，几百上千条也只占一屏节点 ----
  // 虚拟项 = 每条消息 + 末尾「实时轨迹块」一项（下标 messages.length）。
  // measureElement 自适应行高（Markdown/图片/轨迹高度不一），过扫描 6 屏避免滚动抖动。
  const virtRows = messages.length + pendingTraceIds.length;
  const rowVirtualizer = useVirtualizer({
    count: virtRows,
    getScrollElement: () => listRef.current,
    estimateSize: () => 180,
    overscan: 6,
    getItemKey: (i) =>
      i < messages.length ? messages[i].id : `__pending_${pendingTraceIds[i - messages.length] ?? i}`,
  });
  // 新消息到达：如果本来就贴底，滚到更新后的最后一项（虚拟列表只渲染视口内，
  // 单纯改 scrollTop 到 scrollHeight 时最后一条可能还没测量、高度按估值算 → 会差一截）。
  useEffect(() => {
    if (!stickRef.current) return;
    const last = virtRows - 1;
    if (last >= 0) rowVirtualizer.scrollToIndex(last, { align: "end" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [virtRows, session?.id]);

  /** 插话按所属 run 归组，交给那一轮的轨迹块按「事件发生时刻」插进中间：
   *  用户中途说的话，前面已跑的动作在上、之后跑的动作在下——这才是它真实的位置。
   *  如果留在消息流里，它只能排在「整轮结束才落库的助手消息」之前（甚至排在
   *  整块正在执行的轨迹上面），看起来就像「我刚插的话跑到前面去了」。
   *  带图插话同样进轨迹（SteerRow 会渲染图片）；轨迹收起时由 RunTraceInline
   *  底部统一补渲染，图不会消失。带附件的插话仍走消息流（SteerRow 不画附件）。 */
  const steersByRun = useMemo(() => {
    const map = new Map<string, SteerNote[]>();
    const seen = new Set<string>();
    messages.forEach((m) => {
      if (m.author_type !== "user" || !m.run_id) return;
      // 「回答提问」也是同一 run 内用户说的话：按发生时刻插进轨迹（和插话一样），
      // 不再作为独立气泡另起一段——它就是这一轮里的一个动作。
      const isAnswer = m.meta?.kind === "answer";
      if (m.meta?.files?.length) return; // 带附件的插话走普通消息气泡，文件芯片要看得见
      if (!m.content?.trim() && !msgImageSrcs(m).length) return; // 空插话不占行
      // 历史脏数据/连点会留下同 run 同内容同秒的两条插话：展示层去重，只显示一条
      const dedupKey = `${m.run_id}|${m.content || ""}|${m.created_at || ""}`;
      if (seen.has(dedupKey)) return;
      seen.add(dedupKey);
      const list = map.get(m.run_id) ?? [];
      list.push({ id: m.id, text: m.content, at: m.created_at, images: msgImageSrcs(m),
        kind: isAnswer ? "answer" : "steer" });
      map.set(m.run_id, list);
    });
    return map;
  }, [messages]);

  /** 主 agent 中途落的答复（临时回复/进度）按 run 归组，交给那一轮的轨迹块按发生
   *  时刻插进中间——它们逻辑上属于这次执行的过程，不是一条独立气泡。
   *  和插话同源同位置：有轨迹宿主时嵌进轨迹、消息流里不重复渲染；没有宿主时原位兜底。 */
  const repliesByRun = useMemo(() => {
    const map = new Map<string, ReplyNote[]>();
    messages.forEach((m) => {
      if (m.author_type === "user" || !m.run_id) return;
      const kind = m.meta?.kind;
      if (kind !== "interim_reply" && kind !== "stage_report") return;
      if (!m.content?.trim()) return; // 空答复不占行
      const note: ReplyNote = {
        id: m.id,
        text: m.content,
        at: m.created_at,
        kind: kind === "stage_report" ? "stage" : "interim",
        openCount: m.meta?.open_count != null ? Number(m.meta.open_count) : undefined,
      };
      const list = map.get(m.run_id) ?? [];
      list.push(note);
      map.set(m.run_id, list);
    });
    return map;
  }, [messages]);

  /** run id → 轨迹收起态那一行的全部说辞（状态 / 动作分桶 / 角色 / 起止时间）。
   *  这些数据 /sessions/{id}/runs 一次就带回来了，切会话时不必每条 run 各自去拉全量事件。 */
  const runStatById = useMemo(() => {
    const m: Record<string, RunStat> = {};
    (sessionRuns as any[]).forEach((r) => {
      if (!r?.id) return;
      m[r.id] = {
        status: r.status,
        activity: r.activity,
        agents: r.agents,
        created_at: r.created_at,
        finished_at: r.finished_at ?? null,
      };
    });
    return m;
  }, [sessionRuns]);

  const steersOf = (runId: string): SteerNote[] => steersByRun.get(runId) ?? NO_STEERS;
  const repliesOf = (runId: string): ReplyNote[] => repliesByRun.get(runId) ?? NO_REPLIES;
  // 轨迹块还在不在（run 没落消息、轨迹列表被截断时可能没了）：
  // 没地方可插就原位显示，绝不能让插话凭空消失
  const steerHasHost = (runId: string) =>
    pendingTraceIds.includes(runId) || lastMsgIdxOfRun[runId] !== undefined;

  // 最近一次执行已经终止（失败/取消/超时/重启中断/服务端崩溃）→ 自动给「重试」入口。
  // 以前这种情况界面上什么都不弹，用户只能干等或以为卡死（实测反馈：「中断后点继续没反应」）。
  const deadRun = useMemo(() => {
    const r = sessionRuns[0];
    if (!r) return null;
    return ["failed", "cancelled", "orphaned"].includes(r.status) ? r : null;
  }, [sessionRuns]);

  // 该 run 有没有留下任何消息来解释（没有 = 聊天区会只剩用户那句话，看起来像空白）
  const deadRunSilent = useMemo(
    () => !!deadRun && !messages.some((m) => ownsRunUi(m) && sameId(m.run_id, deadRun.id)),
    [deadRun, messages],
  );

  // 重试用：最后一条用户消息（内容 + 附图），点重试就按原样再发一次
  const lastUserMsg = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].author_type === "user") return messages[i];
    }
    return null;
  }, [messages]);
  const [retrying, setRetrying] = useState(false);
  // 手动关掉失败浮层（按 run id 记，换 run 自动重新出现）
  const [deadDismissed, setDeadDismissed] = useState<string>("");

  async function retryLast() {
    if (retrying) return;
    setRetrying(true);
    setErr("");
    try {
      // 失败/中断/取消：**从断点继续**，而不是把上一条消息重发一遍。
      // 上次若是 ask_user 的回答（「【回答】…」），重发会把回答当成新需求，组长会从头再问一遍
      // （实测 bug）。continueRun 会把原需求原文、原图、已改文件、计划进度、最后动作一起带给组长。
      if (deadRun) {
        const res = await api.continueRun(deadRun.id, undefined,
          { profile: modelChoice.profile, effort: modelChoice.effort });
        setSession({ id: res.session_id, title: session?.title || "继续任务", created_at: "" });
        setActiveRunId(res.run_id);
        setRunningRunId(res.run_id);
        addTrace(res.run_id);
        setDeadDismissed(deadRun.id);
        refreshMessages();
        stickRef.current = true;
        setShowJump(false);
        requestAnimationFrame(() => jumpToBottom());
        return;
      }
      if (!lastUserMsg) return;
      const retryImages = await msgImages(lastUserMsg);
      await doSend(lastUserMsg.content || "", retryImages,
        Array.isArray(lastUserMsg.meta?.files) ? lastUserMsg.meta.files : [],
        modelChoice);
    } catch (e: any) {
      setErr(e?.message || "继续失败，请稍后再试");
    } finally {
      setRetrying(false);
    }
  }

  // 附图放大看：data: URL 用 window.open 会被浏览器拦掉（点了没反应），改成页面内灯箱
  const [zoom, setZoom] = useState<{ src: string; alt: string } | null>(null);
  useEffect(() => {
    if (!zoom) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") setZoom(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoom]);

  // 会话记录打包下载（zip：给人读的 conversation.md + 全量 session.json）
  const [exporting, setExporting] = useState(false);
  const [exportErr, setExportErr] = useState("");
  async function onExportSession() {
    if (!session || session.id === "__new__" || exporting) return;
    setExporting(true);
    setExportErr("");
    try {
      await api.exportSession(session.id, session.title || "");
    } catch (e: any) {
      setExportErr(e?.message || "导出失败");
    } finally {
      setExporting(false);
    }
  }

  // 还没有工作区：不再把整页挡死（「请到项目页创建」——可项目入口早就收进
  // 左侧会话面板了）。这里照常渲染会话面板 + 输入区，面板里的「+」可新建工作区；
  // 上面那段 effect 通常已经自动建好默认工作区，这里是建失败/建好前的兜底。
  // 输入区此时禁用（projectId 为空），不会发出没有归属的任务。
  if (!projectId) {
    return (
      <div className="flex h-full">
        <SessionList
          teamId={currentTeamId || ""}
          projects={projects}
          projectId=""
          activeId={null}
          onPick={onPickSession}
          onPickProject={pickProject}
          onProjectsChanged={refreshProjects}
        />
        <div className="flex-1 flex flex-col min-w-0">
          <div className="h-12 shrink-0 border-b border-slate-200 bg-white flex items-center gap-2 px-3">
            <span className="text-sm font-medium text-slate-700">未选择工作区</span>
            <div className="ml-auto flex items-center gap-2">
              <SystemMetrics />
            </div>
          </div>
          <div className="flex-1 min-h-0 flex items-center justify-center px-6">
            <div className="text-center text-sm text-slate-400">
              {!projectsLoaded
                ? "正在加载工作区…"
                : "还没有工作区，正在为你准备一个默认工作区…点左侧「+」也可以手动新建。"}
            </div>
          </div>
          <div className="relative shrink-0 border-t border-slate-200 bg-white p-3">
            {err && <div className="text-xs text-red-600 mb-1">{err}</div>}
            <div className="flex items-end gap-2">
              <textarea
                className="flex-1 border border-slate-200 rounded px-3 py-2 text-sm resize-none min-h-[38px] max-h-48 leading-6 overflow-auto bg-slate-50"
                rows={1}
                placeholder="先在左侧新建一个工作区，再在这里描述需求…"
                disabled
              />
            </div>
          </div>
        </div>
      </div>
    );
  }

  const role = (m: Message) =>
    m.author_type === "user"
      ? "你"
      : m.agent_key
      ? ROLE_LABEL[m.agent_key] || m.agent_key
      : "系统";

  return (
    <div className="flex h-full">
      <SessionList
        teamId={currentTeamId || ""}
        projects={projects}
        projectId={projectId}
        activeId={session?.id ?? null}
        activeIsArchived={session?.status === "archived"}
        onPick={onPickSession}
        onPickProject={pickProject}
        onProjectsChanged={refreshProjects}
      />

      <div
        className="flex-1 flex flex-col min-w-0"
        onDrop={onDrop}
        onDragOver={onDragOver}
      >
        <div ref={topbarRef} className="h-12 shrink-0 border-b border-slate-200 bg-white flex items-center gap-2 px-3">
          {/* 项目切换已移到左侧「工作区」会话树（图二）；这里只显示当前项目名，避免两套入口 */}
          <span className="text-sm font-medium text-slate-700 truncate max-w-[220px] shrink-0" title={projects.find((p) => p.id === projectId)?.name || ""}>
            {projects.find((p) => p.id === projectId)?.name || "未选择项目"}
          </span>
          {projectId && <ProjectScriptButtons projectId={projectId} />}
          {/* 会话级用量：总消耗 token / 缓存命中率 / 速度（落库，重进会话仍在）
              顺序固定为「机器 → 用量 → 操作」；读数区 min-w-0 可收缩，右侧操作按钮 shrink-0
              贴在最右边，读数放不下时按 readLevel 逐组让位，绝不把按钮挤出容器
              （用户 2026-11-14 指图要求：按钮组放最右，不要夹在读数中间）。 */}
          <div ref={readWrapRef} className="ml-auto flex min-w-0 items-center gap-2 overflow-hidden">
             {/* 本机余量：跑批/配音/压上下文时先看它还顶不顶得住，不用翻系统日志 */}
             {readLevel < 2 && <SystemMetrics />}
             {session && session.id !== "__new__" && readLevel < 4 && (
               <SessionUsage sessionId={session.id} refreshKey={usageKey} compact={readLevel >= 1} />
             )}
           </div>
          {session && session.id !== "__new__" && (
            <div className="ml-1 flex shrink-0 items-center gap-2">
              {exportErr && <span className="text-xs text-red-600">{exportErr}</span>}
              <CopyButton
                className="ga-btn ga-btn-secondary ga-btn-sm"
                get={() => sessionCopyPayload(messages)}
                title="把当前会话的文字和图片复制进剪贴板，可直接粘贴到聊天工具或文档"
                label="复制会话"
              />
              <button
                className="ga-btn ga-btn-secondary ga-btn-sm"
                disabled={exporting}
                onClick={onExportSession}
                title="打包下载当前会话记录（markdown 转写 + JSON 全量，含 run 轨迹）"
              >
                {exporting ? "导出中…" : "导出记录"}
              </button>
            </div>
          )}
        </div>

        <div className="relative flex-1 min-h-0">
        <div
          ref={listRef}
          onScroll={(e) => {
            // 自己滚的：不算「用户翻页」，保持贴底
            if (autoScrollRef.current) {
              stickRef.current = true;
              setShowJump(false);
              return;
            }
            const el = e.currentTarget;
            const stick = nearBottom(el);
            stickRef.current = stick;
            setShowJump(!stick);
            // 翻到顶附近就自动补更早的一页（还有历史时）
            if (el.scrollTop <= 120 && hasMoreRef.current) loadOlder();
          }}
          className="ga-scroll-stable h-full overflow-auto p-4 space-y-3"
        >
          {/* 加载/失败/空态：以前这三种情况都是「什么都不画」，看起来就像对话内容丢了 */}
          {msgErr && (
            <div className="flex items-center gap-2 text-xs text-red-600">
              <span>{msgErr}</span>
              <button
                className="ga-btn ga-btn-danger ga-btn-sm"
                onClick={refreshMessages}
              >
                重试
              </button>
            </div>
          )}
          {/* 顶部「更早」：自动上翻到底会补拉，这里再给一个显式入口兜底（触屏/滚轮猛冲） */}
          {(hasMore || loadingOlder) && (
            <div className="flex justify-center pb-1">
              <button
                className="text-xs text-slate-500 hover:text-slate-700 px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-60"
                onClick={() => loadOlder()}
                disabled={loadingOlder}
                title="加载更早的消息"
              >
                {loadingOlder ? "正在加载更早…" : "加载更早的消息"}
              </button>
            </div>
          )}
          {/* 消息虚拟列表：只渲染视口附近的条（含末尾实时轨迹块）。
              空前留白由 virtualizer 的总高度撑开，滚动到哪渲染到哪。 */}
          <div style={{ height: rowVirtualizer.getTotalSize(), width: "100%", position: "relative" }}>
            {rowVirtualizer.getVirtualItems().map((vi) => {
              const isPending = vi.index >= messages.length;
              const m = messages[vi.index];
              return (
                <div
                  key={vi.key}
                  data-index={vi.index}
                  ref={rowVirtualizer.measureElement}
                  className="absolute left-0 top-0 w-full"
                  style={{ transform: `translateY(${vi.start}px)` }}
                >
                  <div className="pb-3">
                    {isPending ? (
                      <RunTraceInline
                        runId={pendingTraceIds[vi.index - messages.length]}
                        steers={steersOf(pendingTraceIds[vi.index - messages.length])}
                        replies={repliesOf(pendingTraceIds[vi.index - messages.length])}
                        stat={runStatById[pendingTraceIds[vi.index - messages.length]]}
                        statPending={!runsLoaded}
                      />
                    ) : (
                      <MessageItem
                        m={m}
                        mi={vi.index}
                        role={role}
                        traceHostIdxOfRun={traceHostIdxOfRun}
                        steerHasHost={steerHasHost}
                        steersOf={steersOf}
                        repliesOf={repliesOf}
                        runStatById={runStatById}
                        runsLoaded={runsLoaded}
                        runningRunId={runningRunId}
                        usageKey={usageKey}
                        lastMsgIdxOfRun={lastMsgIdxOfRun}
                        setZoom={setZoom}
                      />
                    )}
                  </div>
                </div>
              );
            })}
          </div>
          <div ref={bottomRef} />
          {/* 空态/加载态：以前这两种情况一个像素都不画，用户会以为「对话内容丢了」 */}
          {!msgErr &&
            !deadRun &&
            messages.length === 0 &&
            pendingTraceIds.length === 0 && (
              <div className="pt-6 text-center text-sm text-slate-400">
                {msgLoading
                  ? "正在加载对话…"
                  : session && session.id !== "__new__"
                  ? "这个会话还没有任何对话内容（上次执行没有留下回复）。直接发一条消息即可继续。"
                  : "开始一个新对话：在下面描述你要做的内容，回车发送。"}
              </div>
            )}
          </div>
          {/* 用户往上翻、又不影响它时给个回底部的入口 */}
          {showJump && (
            <button
              onClick={() => jumpToBottom(true)}
              className="absolute bottom-4 right-4 z-10 rounded-full border border-slate-300 bg-white/95 px-3 py-1.5 text-xs text-slate-600 shadow-sm hover:bg-white"
              title="跳到最新内容"
            >
              ↓ 回到最新
            </button>
          )}
        </div>

        <div className="relative shrink-0 border-t border-slate-200 bg-white p-3">
          {/* LLM 未就绪横幅：没有可用的模型供应商（没绑档位且 .env 无 Key）——
              发出去也没法调用大模型。可关闭（每会话记一次），但不会自己消失。 */}
          {llmHealth && !llmHealth.ready && !llmBannerDismissed && (
            <div className="mb-2 rounded-lg border border-rose-300 bg-rose-50/95 px-3 py-2 text-xs text-rose-800">
              <button
                className="float-right -mt-0.5 h-5 w-5 rounded text-rose-400 hover:bg-rose-100 hover:text-rose-700"
                onClick={() => setLlmBannerDismissed(true)}
                title="关闭提示（可在「设置 → 模型配置」重新检查）"
              >
                ×
              </button>
              <div className="flex items-start gap-1.5 pr-6">
                <span className="mt-0.5 shrink-0">⚠️</span>
                <div className="leading-5">
                  <span className="font-medium">还没有可用的模型供应商</span>
                  <span className="ml-1">（去「设置 → 模型配置」加一个供应商，并把对话模型绑到它）</span>
                  {llmHealth.issues.length > 0 && (
                    <ul className="mt-1 list-disc pl-4 space-y-0.5">
                      {llmHealth.issues.map((s, i) => <li key={i}>{s}</li>)}
                    </ul>
                  )}
                  {llmHealth.hints.length > 0 && (
                    <div className="mt-1 text-rose-700/90">
                      {llmHealth.hints.map((s, i) => <div key={i}>→ {s}</div>)}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
          {/* 执行失败的浮层提示：贴在输入框上方（不占对话流位置，可关掉）。
              以前放在消息最顶部，用户得往上滚才看得到 */}
          {deadRun && deadDismissed !== deadRun.id && (
            <div className="absolute bottom-full left-1/2 z-20 mb-2 w-[min(94%,560px)] -translate-x-1/2 rounded-lg border border-amber-300 bg-amber-50/95 px-3 py-2 text-xs text-amber-800 shadow-lg">
              <button
                className="absolute right-1.5 top-1.5 h-5 w-5 rounded text-amber-500 hover:bg-amber-100 hover:text-amber-700"
                onClick={() => setDeadDismissed(deadRun.id)}
                title="关闭提示"
              >
                ×
              </button>
              <div className="pr-5 leading-5">
                <span className="font-medium">
                  {deadRun.status === "cancelled" ? "上一次执行已取消" : "上一次执行失败了"}
                </span>
                {deadRun.error ? <span>：{deadRun.error}</span> : null}
                {deadRunSilent ? <span className="ml-1">（本轮没有产生任何回复）</span> : null}
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                <button
                  className="rounded border border-amber-300 bg-white px-2 py-0.5 font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-50"
                  onClick={retryLast}
                  disabled={(!deadRun && !lastUserMsg) || retrying}
                  title="从断点继续上次任务（原需求、图片、已改文件都会带过去）"
                >
                  {retrying ? "继续中…" : "↻ 继续"}
                </button>
                <span className="text-amber-600">
                  {deadRun
                    ? "继续＝接着上次的断点做，不重发消息、不重新提问"
                    : "直接在下面再发一条消息即可"}
                </span>
              </div>
            </div>
          )}
          {/* 待办清单贴在输入框上方（可折叠）：只显示当前这条任务的计划，
              历史任务的计划看「运行详情」页 */}
          {planRunId && <PlanChecklist key={planRunId} runId={planRunId} />}
          {/* 组长拿不准就问：待回答面板 + 已回答记录（都在输入框上方） */}
          {session && session.id !== "__new__" && (
            <AskQuestion
              sessionId={session.id}
              refreshKey={qKey}
              onAnswered={() => {
                setQKey((k) => k + 1);
                refreshMessages();
              }}
            />
          )}
          {err && <div className="text-xs text-red-600 mb-1">{err}</div>}
          {queue.length > 0 && (
            <div className="space-y-1 mb-2">
              {queue.map((q, i) => (
                <div
                  key={q.id}
                  className="flex items-center gap-2 text-xs bg-amber-50 border border-amber-100 rounded px-2 py-1"
                >
                  <span
                    className="text-amber-600 shrink-0 font-medium"
                    title="这一轮跑完会自动接着发送；中途想立刻并进当前执行就点「插话」"
                  >
                    #{i + 1} 排队中
                  </span>
                  <span className="flex-1 min-w-0 flex items-center gap-2">
                    <span className="truncate text-slate-600">
                      {q.content}
                      {q.images.length > 0 && `（图片 ×${q.images.length}）`}
                      {!q.images.length && (q.imageCount || 0) > 0
                        && `（图片 ×${q.imageCount}·刷新后已失效，编辑时重新附上）`}
                      {q.files.length > 0 && `（附件 ×${q.files.length}）`}
                    </span>
                    {q.images.length > 0 && (
                      <span className="flex gap-1 shrink-0">
                        {q.images.map((u, j) => (
                          <img
                            key={j}
                            src={u}
                            alt={`排队图片${j + 1}`}
                            className="h-8 w-8 object-cover rounded border border-amber-200 cursor-zoom-in"
                            title="点击放大（刷新后仍保留）"
                            onClick={() => setZoom({ src: u, alt: `排队图片${j + 1}` })}
                          />
                        ))}
                      </span>
                    )}
                  </span>
                  <button
                    className="text-brand-600 hover:underline shrink-0 disabled:text-slate-400 disabled:no-underline"
                    title="把这条插进当前执行（不取消任务，接着往下做；带图也能插）"
                    onClick={() => sendNow(q.id)}
                    disabled={steerBusyIds.includes(q.id)}
                  >
                    {steerBusyIds.includes(q.id) ? "发送中…" : "插话"}
                  </button>
                  <button
                    className="text-brand-600 hover:underline shrink-0"
                    title="连同图片/附件一起退回输入框，改完再发"
                    onClick={() => editQueued(q.id)}
                  >
                    编辑
                  </button>
                  <button
                    className="text-slate-400 hover:text-red-500 shrink-0"
                    onClick={() => setQueue((s) => s.filter((x) => x.id !== q.id))}
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
          )}
          {imgs.length > 0 && (
            <div className="flex gap-2 mb-2">
              {imgs.map((u, i) => (
                <div key={i} className="relative">
                  <img
                    src={u}
                    alt={`待发送${i + 1}`}
                    className="h-14 w-14 object-cover rounded border border-slate-200 cursor-zoom-in"
                    onClick={() => setZoom({ src: u, alt: `待发送${i + 1}` })}
                    title="点击放大查看"
                  />
                  <button
                    className="absolute -top-1.5 -right-1.5 h-5 w-5 rounded-full bg-slate-700 text-white text-[10px] leading-none flex items-center justify-center hover:bg-red-600"
                    onClick={() => setImgs(imgs.filter((_, j) => j !== i))}
                    title="移除"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
          {files.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2">
              {files.map((f) => (
                <div
                  key={f.path}
                  className={`flex items-center gap-1.5 px-2 py-1 rounded border text-xs max-w-[260px] ${
                    f.pending ? "border-slate-200 bg-slate-50 text-slate-400 animate-pulse" : "border-slate-200 bg-slate-50"
                  }`}
                  title={f.pending ? "上传中…" : `${f.path}（${f.size} 字节）`}
                >
                  <span className="shrink-0 px-1 rounded bg-slate-200 text-slate-600 text-[10px] font-medium">
                    {fileKindLabel(f.kind)}
                  </span>
                  <span className="truncate">{f.name}</span>
                  <span className="shrink-0 text-slate-400">{f.pending ? "上传中…" : fmtSize(f.size)}</span>
                  <button
                    className="shrink-0 text-slate-400 hover:text-red-600"
                    onClick={() => updateFiles(filesRef.current.filter((x) => x.path !== f.path))}
                    disabled={sending}
                    title="移除"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
          {/* 本会话挂载的技能（手动指定模式时显示；点「恢复默认」回到跟随项目） */}
          {attachSkills !== null && (
            <div className="flex flex-wrap items-center gap-1 mb-2 text-xs">
              <span className="text-slate-400">本会话技能：</span>
              {attachSkills.length === 0 ? (
                <span className="text-amber-600">不挂载任何技能</span>
              ) : (
                attachSkills.map((n) => (
                  <span key={n} className="px-1.5 py-0.5 rounded bg-brand-50 text-brand-700">
                    {n}
                  </span>
                ))
              )}
              <button
                className="text-slate-400 hover:text-red-500 underline underline-offset-2"
                onClick={() => setAttachSkills(null)}
                title="清除手动指定，回到跟随项目 .agent/skills 默认"
              >
                恢复默认
              </button>
            </div>
          )}
          <div className="flex gap-2 items-center">
            <div className="relative shrink-0">
              <button
                ref={skillBtnRef}
                className={`h-9 w-9 rounded border flex items-center justify-center ${
                  attachSkills !== null
                    ? "border-brand-500 text-brand-600 bg-brand-50"
                    : "border-slate-200 text-slate-500 hover:bg-slate-50"
                }`}
                title="挂载技能：只注入名称+描述，模型按需加载正文（渐进式披露）"
                aria-expanded={skillPickOpen}
                onClick={openSkillPicker}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 0 1-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 1 0-3.214 3.214c.446.166.855.497.925.968a.979.979 0 0 1-.276.837l-1.61 1.61a2.404 2.404 0 0 1-1.705.707 2.402 2.402 0 0 1-1.704-.706l-1.568-1.568a1.026 1.026 0 0 0-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 1 1-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 0 0-.289-.877l-1.568-1.568A2.402 2.402 0 0 1 1.998 12c0-.617.236-1.234.706-1.704L4.23 8.77c.24-.24.581-.353.917-.303.515.077.877.528 1.073 1.01a2.5 2.5 0 1 0 3.259-3.259c-.482-.196-.933-.558-1.01-1.073-.05-.336.062-.676.303-.917l1.525-1.525A2.402 2.402 0 0 1 12 1.998c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 1 1 3.237 3.237c-.464.18-.894.527-.967 1.02Z" />
                </svg>
                {attachSkills !== null && (
                  <span className="absolute -top-1.5 -right-1.5 h-4 min-w-4 px-0.5 rounded-full bg-brand-600 text-white text-[9px] leading-4 text-center">
                    {attachSkills.length}
                  </span>
                )}
              </button>
              {skillPickOpen && skillPos && createPortal(
                <div
                  ref={skillPanelRef}
                  className="fixed z-[60] w-80 rounded-lg border border-slate-200 bg-white shadow-xl p-2"
                  style={{
                    left: skillPos.left,
                    bottom: skillPos.bottom,
                    backgroundColor: "rgb(var(--ga-panel-rgb))",
                  }}
                >
                    <div className="text-xs font-medium px-1 pb-1">挂载技能（渐进式披露）</div>
                    <label className="flex items-center gap-2 px-1 py-1.5 rounded hover:bg-slate-50 cursor-pointer border-b border-slate-100">
                      <input
                        type="checkbox"
                        checked={attachSkills === null}
                        onChange={() => setAttachSkills(null)}
                      />
                      <span className="text-[12.5px]">跟随项目默认（.agent/skills 全部可用）</span>
                    </label>
                    <div className="max-h-60 overflow-auto mt-1">
                      {skillPool.length === 0 && (
                        <div className="text-[11px] text-slate-400 px-1 py-2">
                          没有可用技能。可在「设置 → 技能」里新建全局技能、添加目录，或往项目 .agent/skills/ 放技能包。
                        </div>
                      )}
                      {skillPool.map((sk) => (
                        <label
                          key={sk.scope + "/" + sk.name}
                          className="flex items-start gap-2 px-1 py-1 rounded hover:bg-slate-50 cursor-pointer"
                          title={sk.description}
                        >
                          <input
                            type="checkbox"
                            className="mt-0.5"
                            checked={attachSkills?.includes(sk.name) ?? false}
                            onChange={() => toggleSkill(sk.name)}
                          />
                          <span className="min-w-0">
                            <span className="text-[12.5px] font-medium">{sk.name}</span>
                            <span className="ml-1.5 text-[10px] text-slate-400">
                              {sk.scope === "project" ? "项目" : sk.scope === "global" ? "全局" : "自定义"}
                            </span>
                            <span className="block text-[11px] text-slate-500 truncate">
                              {sk.description || "（无描述）"}
                            </span>
                          </span>
                        </label>
                      ))}
                    </div>
                    <div className="text-[10px] text-slate-400 px-1 pt-1 border-t border-slate-100 mt-1">
                      勾选任意技能即转为手动指定；清空 = 本会话不挂技能。最多 12 个。
                    </div>
                </div>,
                document.body,
              )}
            </div>
            <div className="relative shrink-0">
              <button
                ref={runOptsBtnRef}
                className={`h-9 w-9 rounded border flex items-center justify-center relative ${
                  scope === "full"
                    ? "border-amber-300 bg-amber-50 text-amber-700"
                    : "border-slate-200 text-slate-500 hover:bg-slate-50"
                }`}
                title={
                  scope === "full"
                    ? "运行选项：会话权限为「全部权限」（可访问工作区外）"
                    : "运行选项（会话权限）"
                }
                aria-label="运行选项"
                aria-expanded={runOptsOpen}
                onClick={toggleRunOpts}
              >
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 7h9M18.5 7H20M4 17h3M12.5 17H20" />
                  <circle cx="15.5" cy="7" r="2.2" />
                  <circle cx="9.5" cy="17" r="2.2" />
                </svg>
                {scope === "full" && (
                  <span className="absolute -top-1 -right-1 h-2 w-2 rounded-full bg-amber-500" />
                )}
              </button>
              {runOptsOpen && runOptsPos && createPortal(
                <>
                  <div className="fixed inset-0 z-50" onClick={() => setRunOptsOpen(false)} />
                  <div
                    ref={runOptsPanelRef}
                    className="fixed z-[60] w-80 max-h-[75vh] overflow-auto rounded-lg border border-slate-200 bg-white shadow-xl p-3"
                    style={{
                      left: runOptsPos.left,
                      bottom: runOptsPos.bottom,
                      backgroundColor: "rgb(var(--ga-panel-rgb))",
                    }}
                  >
                    <div className="text-[11px] font-medium text-slate-500 pb-1">会话权限</div>
                    <label className="flex items-start gap-2 px-1 py-1.5 rounded hover:bg-slate-50 cursor-pointer">
                      <input
                        type="radio"
                        className="mt-1"
                        name="ga-scope"
                        checked={scope === "workspace"}
                        onChange={() => changeScope("workspace")}
                      />
                      <span>
                        <span className="text-[12.5px] font-medium">仅工作区</span>
                        <span className="block text-[11px] text-slate-500 leading-relaxed">
                          读写/解析工具只能碰当前项目工作区内（默认，最安全）。
                        </span>
                      </span>
                    </label>
                    <label className="flex items-start gap-2 px-1 py-1.5 rounded hover:bg-slate-50 cursor-pointer">
                      <input
                        type="radio"
                        className="mt-1"
                        name="ga-scope"
                        checked={scope === "full"}
                        onChange={() => changeScope("full")}
                      />
                      <span>
                        <span className="text-[12.5px] font-medium">全部权限</span>
                        <span className="block text-[11px] text-slate-500 leading-relaxed">
                          允许访问工作区外的本机文件（项目外的脚本、依赖、配置）。系统关键目录仍被保护。
                        </span>
                      </span>
                    </label>
                    <div className="text-[10px] text-slate-400 px-1 pt-0.5">
                      每个会话单独生效；新会话默认值在「设置 → 助手行为」里调。
                    </div>
                  </div>
                </>,
                document.body,
              )}
            </div>
            <button
              className="shrink-0 h-9 w-9 rounded border border-slate-200 text-slate-500 hover:bg-slate-50 flex items-center justify-center"
              title="添加图片或文件（图片最多 4 张、文件最多 6 个，也可粘贴截图/拖入）"
              aria-label="添加图片或文件"
              disabled={!projectId || uploading || sending}
              onClick={() => fileRef.current?.click()}
            >
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
              </svg>
            </button>
            <input
              ref={fileRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <textarea
              ref={taRef}
              className="flex-1 border border-slate-200 rounded px-3 py-2 text-sm resize-none min-h-[38px] max-h-48 leading-6 overflow-auto"
              rows={1}
              placeholder="描述需求，可附图/附文件/粘贴截图/拖入文件，Enter 发送、Shift+Enter 换行"
              disabled={sending}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                const el = e.target;
                el.style.height = "auto";
                el.style.height = Math.min(el.scrollHeight, 192) + "px";
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            {/* 模型选择：输入框右下的「模型名 + 推理等级」胶囊（图三/图四） */}
            <ModelPicker value={modelChoice} onChange={setModelPick} disabled={sending} />
            {/* 执行中：输入框空着 → 主键就是「停止」；有草稿 → 主键「排队」，左边补一个小停止键。
                这里排的是**同一条会话**里的下一条；别的会话各自独立执行，不会被这里挡住。 */}
            {runningRunId && !hasDraft && (
              <button
                className="ga-btn ga-btn-danger ga-btn-lg"
                onClick={stopRun}
                disabled={stopping}
                title="停止当前执行：掐断后续派发与工具调用，已写进工作区的改动保留"
              >
                {stopping ? "停止中…" : "■ 停止"}
              </button>
            )}
            {(!runningRunId || hasDraft) && (
              <div className="flex items-center gap-1.5">
                {runningRunId && (
                  <button
                    className="ga-btn ga-btn-danger"
                    onClick={stopRun}
                    disabled={stopping}
                    title={stopping ? "停止中…" : "停止当前执行（草稿留在输入框，不受影响）"}
                  >
                    ■
                  </button>
                )}
                <button
                  className="ga-btn ga-btn-primary ga-btn-lg"
                  onClick={send}
                  disabled={!hasDraft || uploading || sending}
                  title={
                    runningRunId
                      ? "当前会话正在执行，点击加入本会话的排队队列（其他会话不受影响，可同时执行）"
                      : "发送"
                  }
                >
                  {runningRunId ? "排队" : "发送"}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {activeRunId && (panelCollapsed ? (
        <button
          className="w-9 shrink-0 border-l border-slate-200 bg-slate-50 flex flex-col items-center justify-center gap-2 text-slate-400 hover:text-brand-700 hover:bg-slate-100"
          title="展开运行面板（上下文请求 / 上下文统计）"
          aria-label="展开运行面板"
          onClick={() => toggleRunPanel(false)}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m15 18-6-6 6-6" />
          </svg>
          <span className="text-[11px] tracking-[0.18em] [writing-mode:vertical-rl]">运行面板</span>
          {runningRunId === activeRunId && (
            <span className="h-1.5 w-1.5 rounded-full bg-brand-500 animate-pulse" />
          )}
        </button>
      ) : (
        <>
          <div
            className="w-1 shrink-0 cursor-col-resize hover:bg-brand-300 transition-colors"
            title="拖拽调整面板宽度"
            onMouseDown={(e) => {
              e.preventDefault();
              const startX = e.clientX;
              const startW = panelW;
              function onMove(ev: MouseEvent) {
                setPanelW(Math.min(900, Math.max(300, startW - (ev.clientX - startX))));
              }
              function onUp() {
                window.removeEventListener("mousemove", onMove);
                window.removeEventListener("mouseup", onUp);
              }
              window.addEventListener("mousemove", onMove);
              window.addEventListener("mouseup", onUp);
            }}
          />
          <div className="shrink-0 border-l border-slate-200 bg-slate-50" style={{ width: panelW }}>
            <RunPanel projectId={projectId} runId={activeRunId} onCollapse={() => toggleRunPanel(true)} />
          </div>
        </>
      ))}

      {/* 附图灯箱：点缩略图放大看原图（Esc / 点背景关闭）。portal 到 body：
          开壁纸后祖先的 backdrop-filter 会把 fixed 变成相对面板定位，灯箱会被裁剪 */}
      {zoom && <ImageLightbox zoom={zoom} onClose={() => setZoom(null)} />}
    </div>
  );
}
