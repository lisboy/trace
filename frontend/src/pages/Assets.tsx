import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";
import { navigate, type Route } from "../router";
import type { Artifact } from "../types";
import ImageGenPanel from "../components/ImageGenPanel";
import VoiceGenPanel from "../components/VoiceGenPanel";
import VideoGenPanel from "../components/VideoGenPanel";
import SearchSelect from "../components/SearchSelect";
import { copyToClipboard } from "../clipboard";

type Source = {
  id: string;
  driver_key: string;
  label: string;
  kind: string;
  enabled: boolean;
  config_json?: Record<string, any>;
};

const KIND_LABEL: Record<string, string> = {
  image: "图片", icon: "图标", audio: "音频", music: "音乐", voice: "语音", sfx: "音效",
  video: "视频",
};

/** 素材类型筛选：值对应 Artifact.asset_kind */
const TYPE_FILTERS: { v: string; label: string }[] = [
  { v: "all", label: "全部类型" },
  { v: "image", label: "图片" },
  { v: "icon", label: "图标" },
  { v: "video", label: "视频" },
  { v: "audio", label: "音频" },
  { v: "voice", label: "语音" },
  { v: "music", label: "音乐" },
  { v: "sfx", label: "音效" },
];

const isImageKind = (k: string) => k === "image" || k === "icon";
const isVideoKind = (k: string) => k === "video";

const srcLabelOf = (a: Artifact) =>
  a.provider === "import" ? "导入" : a.provider?.startsWith("external:") ? "外置" : null;

    /** 单个素材卡片。抽成独立组件，与网格分页解耦，便于列表整块重排。 */
const AssetCard = function AssetCard({
  a,
  onRegen,
  onRerecord,
  onRevideo,
  onDelete,
  onCopy,
  onDownload,
  deleting,
}: {
  a: Artifact;
  onRegen: (a: Artifact) => void;
  onRerecord: (a: Artifact) => void;
  onRevideo: (a: Artifact) => void;
  onDelete: (a: Artifact) => void;
  onCopy: (a: Artifact) => Promise<boolean>;
  onDownload: (a: Artifact) => Promise<void>;
  deleting: boolean;
}) {
  const isImg = isImageKind(a.asset_kind);
  const isVideo = isVideoKind(a.asset_kind);
  const srcLabel = srcLabelOf(a);
  // 卡片内的临时反馈：复制/下载都是异步（要拉原图字节），没有「进行中/已复制」就像点了没反应
  const [copyState, setCopyState] = useState<"" | "ok" | "fail" | "busy">("");
  const [downloading, setDownloading] = useState(false);
  return (
    <div className="bg-white rounded border border-slate-200 p-3">
      {isVideo ? (
        <video
          controls
          preload="metadata"
          src={api.artifactFile(a.id, a.size ?? undefined)}
          className="h-28 w-full object-contain rounded bg-black mb-2"
        />
      ) : isImg ? (
        <a href={api.artifactFile(a.id, a.size ?? undefined)} target="_blank" rel="noreferrer">
          <img
            src={api.artifactThumb(a.id, 320, a.size ?? undefined)}
            alt={a.slug}
            loading="lazy"
            decoding="async"
            className="h-28 w-full object-contain rounded bg-slate-50 mb-2"
            onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
          />
        </a>
      ) : (
        <div className="h-28 w-full rounded bg-slate-50 mb-2 flex items-center justify-center">
          {a.is_placeholder ? (
            <span className="text-[11px] text-slate-400">占位音频（未生成真实配音）</span>
          ) : (
            <audio controls preload="none" src={api.artifactFile(a.id, a.size ?? undefined)} className="w-[90%] h-8" />
          )}
        </div>
      )}
      <div className="text-xs mono truncate" title={a.slug}>{a.slug}</div>
      <div className="text-[11px] text-slate-400 truncate">
        {KIND_LABEL[a.asset_kind] || a.asset_kind} · {a.rel_path}
      </div>
      {a.session_title && (
        <div className="text-[11px] text-slate-400 truncate" title={`来源会话：${a.session_title}`}>
          来源：{a.session_title}
        </div>
      )}
      <div className="mt-1 flex items-center gap-1">
        {a.is_placeholder && (
          <span className="px-1 rounded bg-orange-100 text-orange-700 text-[11px]">占位</span>
        )}
        {srcLabel && (
          <span className="px-1 rounded bg-sky-100 text-sky-700 text-[11px]">{srcLabel}</span>
        )}
        <div className="ml-auto flex flex-wrap gap-1.5 justify-end">
          <a
            className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
            href={api.artifactFile(a.id, a.size ?? undefined)}
            target="_blank"
            rel="noreferrer"
          >
            查看
          </a>
          {/* 复制只对图片有意义（剪贴板不认音频/视频字节），音频/视频给下载 */}
          {isImg && (
            <button
              className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50 disabled:opacity-50"
              disabled={copyState === "busy"}
              title="把这张图复制到剪贴板，可直接粘进聊天/文档/画布"
              onClick={async () => {
                setCopyState("busy");
                let ok = false;
                try {
                  ok = await onCopy(a);
                } finally {
                  setCopyState(ok ? "ok" : "fail");
                  window.setTimeout(() => setCopyState(""), 1400);
                }
              }}
            >
              {copyState === "ok" ? "已复制" : copyState === "fail" ? "复制失败" : copyState === "busy" ? "复制中…" : "复制"}
            </button>
          )}
          <button
            className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50 disabled:opacity-50"
            disabled={downloading}
            title="下载原文件到本机"
            onClick={async () => {
              setDownloading(true);
              try {
                await onDownload(a);
              } finally {
                setDownloading(false);
              }
            }}
          >
            {downloading ? "下载中…" : "下载"}
          </button>
          {isVideo ? (
            <button
              className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
              onClick={() => onRevideo(a)}
              title="视频改到「视频生成」里重新生成"
            >
              重新生成
            </button>
          ) : isImg ? (
            <button
              className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
              onClick={() => onRegen(a)}
              title="用原图作参考重新生成"
            >
              重画
            </button>
          ) : (
            <button
              className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
              onClick={() => onRerecord(a)}
              title="音频不是「重画」，改到语音生成里重新配音"
            >
              重新配音
            </button>
          )}
          <button
            className="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-50"
            onClick={() => onDelete(a)}
            disabled={deleting}
            title="从素材库删除这张（含工作区文件）"
          >
            {deleting ? "删除中…" : "删除"}
          </button>
        </div>
      </div>
    </div>
  );
};

export default function Assets({ route }: { route?: Route }) {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const projectId = route?.params.project || "";
  const [items, setItems] = useState<Artifact[]>([]);
  const [filter, setFilter] = useState<"all" | "placeholder">("all");
  const [sessionFilter, setSessionFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [msg, setMsg] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [showSrc, setShowSrc] = useState(false);
  const [showGen, setShowGen] = useState(false);
  const [showVoice, setShowVoice] = useState(false);
  const [showVideo, setShowVideo] = useState(false);
  // 素材分页：一页一页拉，滚到底自动续。筛选（类型/会话/占位）全在服务端做，
  // 否则只是「在已加载的一页里筛」，看起来就像素材变少了。
  const PAGE = 60;
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [clearing, setClearing] = useState(false);
  const hasMoreRef = useRef(false);
  const loadingRef = useRef(false);
  const [sessionSources, setSessionSources] = useState<{ session_id: string; title: string | null }[]>([]);
  const reqSeq = useRef(0);

  function refreshProjects() {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps) => {
      const list = ps.map((p: any) => ({ id: p.id, name: p.name }));
      setProjects(list);
      // 没带项目参数进来时默认选第一个（选过的会留在 sessionStorage，不覆盖）
      if (!route?.params.project && list.length) {
        navigate("assets", { project: list[0].id });
      }
    });
  }
  useEffect(refreshProjects, [currentTeamId]);

  // 第一页：项目或任一筛选变化都从头拉（页码/游标重置）
  const refresh = useCallback(() => {
    if (!projectId) {
      setItems([]);
      setHasMore(false);
      hasMoreRef.current = false;
      return;
    }
    const seq = ++reqSeq.current;
    setLoadingMore(true);
    api
      .artifacts({
        project_id: projectId,
        asset_kind: typeFilter,
        session_id: sessionFilter,
        placeholder: filter === "placeholder",
        limit: PAGE,
      })
      .then((res: any) => {
        if (seq !== reqSeq.current) return;
        const list: Artifact[] = Array.isArray(res) ? res : (res?.artifacts ?? []);
        setItems(list);
        const more = Array.isArray(res) ? false : !!res?.has_more;
        setHasMore(more);
        hasMoreRef.current = more;
      })
      .catch(() => {})
      .finally(() => {
        if (seq === reqSeq.current) setLoadingMore(false);
      });
  }, [projectId, typeFilter, sessionFilter, filter]);
  useEffect(refresh, [refresh]);

  // 续页：按游标往更早的一页追加
  const loadMore = useCallback(async () => {
    const last = items[items.length - 1];
    if (!projectId || !last || !hasMoreRef.current || loadingRef.current) return;
    loadingRef.current = true;
    setLoadingMore(true);
    const seq = reqSeq.current;
    try {
      const res: any = await api.artifacts({
        project_id: projectId,
        asset_kind: typeFilter,
        session_id: sessionFilter,
        placeholder: filter === "placeholder",
        limit: PAGE,
        before: last.created_at ?? undefined,
        before_id: last.id,
      });
      if (seq !== reqSeq.current) return; // 期间切换了筛选/项目：丢弃
      const list: Artifact[] = Array.isArray(res) ? res : (res?.artifacts ?? []);
      setItems((prev) => {
        const have = new Set(prev.map((x) => x.id));
        const add = list.filter((x) => !have.has(x.id));
        return add.length ? [...prev, ...add] : prev;
      });
      const more = Array.isArray(res) ? false : !!res?.has_more;
      setHasMore(more);
      hasMoreRef.current = more;
    } catch {
      /* 下次滚到底再试 */
    } finally {
      loadingRef.current = false;
      setLoadingMore(false);
    }
  }, [projectId, typeFilter, sessionFilter, filter, items]);

  // 会话下拉项：单独一个轻量接口（分页后手里只有一页素材，不能再靠它去重出全部会话）
  useEffect(() => {
    if (!projectId) {
      setSessionSources([]);
      return;
    }
    api
      .artifactSessions(projectId)
      .then((rows: any[]) => setSessionSources(Array.isArray(rows) ? rows : []))
      .catch(() => {});
  }, [projectId]);

  // 切项目后旧项目的会话筛选会命中不到任何素材，直接复位，避免看到莫名其妙的空列表
  useEffect(() => {
    setSessionFilter("all");
    setTypeFilter("all");
    setFilter("all");
  }, [projectId]);


  function refreshSources() {
    if (!currentTeamId) return;
    api
      .providers()
      .then((ps: Source[]) => setSources(ps.filter((p) => p.driver_key === "external_http")))
      .catch(() => {});
  }
  useEffect(refreshSources, [currentTeamId]);

  async function regen(a: Artifact) {
    setMsg("重画中…");
    try {
      const r = await api.regenerate(a.id);
      setMsg(r?.placeholder
        ? (r?.note ? `已回退占位图重画（${r.note}）` : "已用占位图重画（配置 ComfyUI 后会用真实模型）。")
        : "已用 ComfyUI 重新生成真实图片。");
      refresh();
    } catch (e: any) {
      setMsg("重画失败：" + (e?.message || e));
    }
  }

  async function importFiles(files: FileList | null, kind: string) {
    if (!files?.length || !projectId) return;
    setMsg("导入中…");
    try {
      const r = await api.importAssets(projectId, Array.from(files), kind);
      const n = r.imported?.length ?? 0;
      const sk = r.skipped?.length ?? 0;
      setMsg(`已导入 ${n} 个素材${sk ? `，跳过 ${sk} 个（类型不识别或超 20MB）` : ""}。`);
      refresh();
    } catch (e: any) {
      setMsg("导入失败：" + (e?.message || e));
    }
  }

  async function downloadOne(a: Artifact) {
    try {
      await api.downloadArtifact(a);
      setMsg(`已开始下载「${a.slug}」。`);
    } catch (e: any) {
      setMsg("下载失败：" + (e?.message || e));
    }
  }

  // 复制图片到剪贴板：剪贴板只认位图/PNG，得先把原图取成 dataURL 再让 clipboard.ts 转一道。
  // 顺带带上 slug 文本，粘到文档里能看见是哪张素材。
  async function copyOne(a: Artifact): Promise<boolean> {
    try {
      const dataUrl = await api.artifactDataUrl(a);
      const res = await copyToClipboard({ text: a.slug, images: [dataUrl] });
      const ok = res === "full";
      setMsg(ok
        ? `已复制「${a.slug}」到剪贴板。`
        : res === "text"
          ? `已复制「${a.slug}」的文字，图片没能写进剪贴板。`
          : "复制失败：浏览器拒绝了剪贴板写入。");
      return ok || res === "text";
    } catch (e: any) {
      setMsg("复制失败：" + (e?.message || e));
      return false;
    }
  }

  async function deleteOne(a: Artifact) {
    if (!confirm(`确定删除「${a.slug}」？\n将从素材库移除，并删除工作区里的文件，不可恢复。`)) return;
    setDeletingId(a.id);
    setMsg("删除中…");
    try {
      await api.deleteArtifact(a.id);
      // 本地先移除，列表立即有反馈；再拉一页补齐（可能把后面的一页提前）
      setItems((prev) => prev.filter((x) => x.id !== a.id));
      setMsg(`已删除「${a.slug}」。`);
      refresh();
    } catch (e: any) {
      setMsg("删除失败：" + (e?.message || e));
    } finally {
      setDeletingId("");
    }
  }

  async function clearAll() {
    if (!projectId) return;
    const scope =
      filter !== "all" || typeFilter !== "all" || sessionFilter !== "all"
        ? "当前筛选条件下的"
        : "";
    const n = shown.length;
    if (
      !confirm(
        `确定清空${scope}素材库？\n将删除${scope}全部 ${n}${
          hasMore ? "+" : ""
        } 个素材（含工作区文件），不可恢复。`
      )
    )
      return;
    setClearing(true);
    setMsg("正在清空…");
    try {
      const r = await api.clearArtifacts({
        project_id: projectId,
        asset_kind: typeFilter,
        session_id: sessionFilter,
        placeholder: filter === "placeholder",
      });
      setMsg(`已清空 ${r.deleted} 个素材。`);
      setItems([]);
      setHasMore(false);
      hasMoreRef.current = false;
      refresh();
    } catch (e: any) {
      setMsg("清空失败：" + (e?.message || e));
    } finally {
      setClearing(false);
    }
  }

  // 会话筛选项：来自专用轻量接口（按素材实际来源会话去重，导入素材没有会话）
  const sessionOptions = useMemo(() => {
    return sessionSources.map((s) => ({
      id: s.session_id,
      label: (s.title || "").trim() || `会话 ${s.session_id.slice(0, 8)}`,
    }));
  }, [sessionSources]);

  // 分页后服务端已经按类型/会话/占位过滤，这里直接用，不再二次筛（否则只会筛当前页）
  const shown = items;

  // 素材网格：整列直接渲染，不做虚拟滚动。
  // 素材是分页追加的（每页 60 条，滚到底再续下一页），DOM 规模本来就不大；
  // 之前那版虚拟滚动要拿「网格上方的工具栏/AI 面板高度」当 scrollMargin，
  // 一旦估算行高和卡片真实高度对不上，行就会整段错位、滑过首屏后整片空白
  //（用户看到的「往下滑素材就消失」），已去掉。gridRef 只用作滚动容器。
  const gridRef = useRef<HTMLDivElement>(null);
  // 网格起点：各生成面板比网格还高（AI 生图展开后大预览能顶到 900px 以上），
  // 展开后网格会被整个推到视口下方，屏幕上只剩一片空白、看着像「素材没了」。
  // 面板展开/收起后把网格起始处滚进视口，素材立刻回到视野。
  const gridAnchorRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const scroller = gridRef.current;
    const anchor = gridAnchorRef.current;
    if (!scroller || !anchor) return;
    const reveal = () => {
      const gridTop =
        anchor.getBoundingClientRect().top -
        scroller.getBoundingClientRect().top +
        scroller.scrollTop;
      if (scroller.scrollTop > gridTop) {
        scroller.scrollTop = Math.max(0, gridTop - 8);
      }
    };
    reveal();
  }, [showGen, showVoice, showVideo, showSrc, projectId]);

  function onRerecord(a: Artifact) {
    setShowVoice(true);
    setMsg(`音频素材请在「语音生成」里重新配音：${a.slug}`);
  }

  function onRevideo(a: Artifact) {
    setShowVideo(true);
    setMsg(`视频素材请在「视频生成」里重新生成：${a.slug}`);
  }

  return (
    <div
      ref={gridRef}
      className="h-full overflow-auto p-5"
      onScroll={(e) => {
        const el = e.currentTarget;
        // 滚到底部附近就续下一页
        if (el.scrollHeight - el.scrollTop - el.clientHeight <= 240 && hasMoreRef.current) loadMore();
      }}
    >
      <div className="flex items-center mb-4 gap-3 flex-wrap">
        <h2 className="text-lg font-semibold">素材库</h2>
        <SearchSelect
          className="text-sm border border-slate-200 rounded px-2 py-1 w-[160px]"
          value={projectId}
          title="选择项目"
          searchPlaceholder="搜索项目…"
          options={[{ value: "", label: "选择项目" }, ...projects.map((p) => ({ value: p.id, label: p.name, keywords: p.name }))]}
          onChange={(v) => navigate("assets", { project: v })}
        />
        <select
          className="text-sm border border-slate-200 rounded px-2 py-1"
          value={filter}
          onChange={(e) => setFilter(e.target.value as any)}
          title="只看占位素材（还没换成真实美术的）"
        >
          <option value="all">全部素材</option>
          <option value="placeholder">仅占位</option>
        </select>
        {/* 会话筛选：同一轮对话产出的图/音常常要一起找，单独按会话收窄 */}
        <SearchSelect
          className="text-sm border border-slate-200 rounded px-2 py-1 w-[220px]"
          value={sessionFilter}
          title="按产出素材的会话筛选"
          searchPlaceholder="搜索会话…"
          emptyText="没有匹配的会话"
          options={[
            { value: "all", label: "全部会话" },
            { value: "__none__", label: "无会话（导入/外置）" },
            ...sessionOptions.map((s) => ({ value: s.id, label: s.label, keywords: s.label })),
          ]}
          onChange={setSessionFilter}
        />
        {/* 类型筛选：图片/图标/音频分开找 */}
        <select
          className="text-sm border border-slate-200 rounded px-2 py-1"
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          title="按素材类型筛选"
        >
          {TYPE_FILTERS.map((t) => (
            <option key={t.v} value={t.v}>
              {t.label}
            </option>
          ))}
        </select>
        <div className="ml-auto flex items-center gap-2">
          <ImportButtons projectId={projectId} onImport={importFiles} />
          <button
            className={`ga-btn ga-btn-primary ${showGen ? "ring-2 ring-brand-300" : ""}`}
            onClick={() => setShowGen((v) => !v)}
          >
            AI 生图
          </button>
          <button
            className={`ga-btn ga-btn-primary ${showVoice ? "ring-2 ring-brand-300" : ""}`}
            onClick={() => setShowVoice((v) => !v)}
          >
            语音生成
          </button>
          <button
            className={`ga-btn ga-btn-primary ${showVideo ? "ring-2 ring-brand-300" : ""}`}
            onClick={() => setShowVideo((v) => !v)}
          >
            视频生成
          </button>
          <button
            className={`ga-btn ga-btn-primary ${showSrc ? "ring-2 ring-brand-300" : ""}`}
            onClick={() => setShowSrc((v) => !v)}
            title="管理外置素材源（开放平台 / 自建目录）"
          >
            素材源（{sources.length}）
          </button>
          <button
            className="ga-btn border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-50"
            onClick={clearAll}
            disabled={clearing || !projectId || shown.length === 0}
            title={
              filter !== "all" || typeFilter !== "all" || sessionFilter !== "all"
                ? "清空当前筛选条件下的素材（含工作区文件）"
                : "清空本项目全部素材（含工作区文件）"
            }
          >
            {clearing ? "清空中…" : "清空素材库"}
          </button>
        </div>
      </div>

      {msg && <div className="text-xs text-slate-500 mb-2">{msg}</div>}

      {showGen && <ImageGenPanel projectId={projectId} onImported={refresh} />}

      {showVoice && <VoiceGenPanel projectId={projectId} onImported={refresh} />}

      {showVideo && <VideoGenPanel projectId={projectId} onImported={refresh} />}

      {!projectId && (
        <div className="text-sm text-slate-400">请先选择一个项目。</div>
      )}

      {showSrc && (
        <SourcePanel
          sources={sources}
          projectId={projectId}
          refreshSources={refreshSources}
          refresh={refresh}
          onMsg={setMsg}
        />
      )}

      {projectId && shown.length === 0 && (
        <div className="text-sm text-slate-400">
          {(filter !== "all" || typeFilter !== "all" || sessionFilter !== "all")
            ? "当前筛选条件下没有素材，换个筛选试试。"
            : "本项目暂无素材。"}
        </div>
      )}

      {/* 网格：普通 grid 布局，卡片按顺序排满，不依赖虚拟行定位 */}
      <div ref={gridAnchorRef} className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        {shown.map((a) => (
          <AssetCard
            key={a.id}
            a={a}
            onRegen={regen}
            onRerecord={onRerecord}
            onRevideo={onRevideo}
            onDelete={deleteOne}
            onCopy={copyOne}
            onDownload={downloadOne}
            deleting={deletingId === a.id}
          />
        ))}
      </div>
      {projectId && shown.length > 0 && (
        <div className="flex justify-center py-3">
          {hasMore ? (
            <button
              className="text-xs text-slate-500 hover:text-slate-700 px-2 py-1 rounded hover:bg-slate-100 disabled:opacity-60"
              onClick={loadMore}
              disabled={loadingMore}
            >
              {loadingMore ? "正在加载更多…" : "加载更多"}
            </button>
          ) : (
            <span className="text-[11px] text-slate-400">已到底 · 共 {shown.length} 个（当前筛选）</span>
          )}
        </div>
      )}
    </div>
  );
}

function ImportButtons({
  projectId,
  onImport,
}: {
  projectId: string;
  onImport: (files: FileList | null, kind: string) => void;
}) {
  const [kind, setKind] = useState("auto");
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <>
      {/* 「自动识别」只作用于旁边的「导入素材」：说明白它是导入归类，别让人以为是全站开关 */}
      <span className="text-xs text-slate-500" title="导入时如何归类素材">导入归类</span>
      <select
        className="text-xs border border-slate-200 rounded px-1 py-1.5 bg-white"
        value={kind}
        onChange={(e) => setKind(e.target.value)}
        title="导入归类：自动按文件扩展名识别；也可以强制归为图标 / 图片 / 音频"
      >
        <option value="auto">自动识别</option>
        <option value="icon">归为图标</option>
        <option value="image">归为图片</option>
        <option value="audio">归为音频</option>
      </select>
      <button
        className="ga-btn ga-btn-primary"
        disabled={!projectId}
        onClick={() => fileRef.current?.click()}
        title="从本机选择图片 / 音频导入素材库"
      >
        导入素材
      </button>
      <input
        ref={fileRef}
        type="file"
        multiple
        accept="image/*,audio/*"
        className="hidden"
        onChange={(e) => {
          onImport(e.target.files, kind);
          e.target.value = "";
        }}
      />
    </>
  );
}

function SourcePanel({
  sources,
  projectId,
  refreshSources,
  refresh,
  onMsg,
}: {
  sources: Source[];
  projectId: string;
  refreshSources: () => void;
  refresh: () => void;
  onMsg: (s: string) => void;
}) {
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState("image");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiStyle, setApiStyle] = useState("json");
  const [urlTpl, setUrlTpl] = useState("");
  const [queries, setQueries] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState("");

  async function add() {
    onMsg("");
    if (!label.trim() || !baseUrl.trim()) {
      onMsg("请填写素材源名称与 Base URL。");
      return;
    }
    try {
      await api.createProvider({
        driver_key: "external_http",
        label: label.trim(),
        kind,
        enabled: true,
        config_json: {
          base_url: baseUrl.trim(),
          api_style: apiStyle,
          ...(apiStyle === "template" ? { url_template: urlTpl.trim() } : {}),
        },
      });
      setLabel("");
      setBaseUrl("");
      setUrlTpl("");
      refreshSources();
      onMsg("素材源已添加。");
    } catch (e: any) {
      onMsg("添加失败：" + (e?.message || e));
    }
  }

  async function pull(src: Source) {
    if (!projectId) {
      onMsg("请先选择项目再拉取素材。");
      return;
    }
    setBusyId(src.id);
    onMsg(`正在从「${src.label}」拉取素材…`);
    try {
      const r = await api.fetchAssets(projectId, {
        source_id: src.id,
        query: queries[src.id] || "",
        limit: 6,
      });
      const n = r.imported?.length ?? 0;
      const sk = r.skipped?.length ?? 0;
      onMsg(
        r.message || `拉取完成：入库 ${n} 个${sk ? `，失败 ${sk} 个` : ""}。`
      );
      refresh();
    } catch (e: any) {
      onMsg("拉取失败：" + (e?.message || e));
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="bg-white rounded border border-slate-200 p-4 mb-4">
      <h3 className="font-medium mb-2 text-sm">外置素材源（开放平台 / 自建目录）</h3>
      <p className="text-[11px] text-slate-400 mb-2">
        支持任意返回 JSON 列表（{"{"}items: [{"{"}name, url{"}"}]{"}"} 或数组）的开放平台，以及按
        URL 模板编号的静态目录；可配鉴权请求头。添加后在下方输入关键词拉取入库。
      </p>
      <div className="space-y-1.5 mb-3">
        {sources.map((s) => (
          <div key={s.id} className="flex items-center gap-2 text-xs border border-slate-100 rounded px-2 py-1.5">
            <span className="font-medium">{s.label}</span>
            <span className="px-1 rounded bg-slate-100 text-slate-500">{KIND_LABEL[s.kind] || s.kind}</span>
            <span className="mono text-slate-400 truncate max-w-[220px]" title={s.config_json?.base_url}>
              {s.config_json?.base_url || "（未配置 base_url）"}
            </span>
            <input
              className="border border-slate-200 rounded px-2 py-1 w-40"
              placeholder="关键词（可空）"
              value={queries[s.id] || ""}
              onChange={(e) => setQueries({ ...queries, [s.id]: e.target.value })}
            />
            <button
              className="px-2 py-1 rounded bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
              disabled={busyId === s.id}
              onClick={() => pull(s)}
            >
              {busyId === s.id ? "拉取中…" : "拉取"}
            </button>
            <button
              className="ml-auto text-red-600 hover:underline"
              onClick={async () => {
                if (!confirm(`删除素材源「${s.label}」？`)) return;
                await api.deleteProvider(s.id);
                refreshSources();
              }}
            >
              删除
            </button>
          </div>
        ))}
        {sources.length === 0 && (
          <div className="text-xs text-slate-400">暂无素材源，用下方表单添加。</div>
        )}
      </div>
      <div className="flex items-center gap-2 text-xs flex-wrap border-t border-slate-100 pt-3">
        <input className="border rounded px-2 py-1 w-32" placeholder="名称"
               value={label} onChange={(e) => setLabel(e.target.value)} />
        <select className="border rounded px-2 py-1" value={kind} onChange={(e) => setKind(e.target.value)}>
          {Object.entries(KIND_LABEL).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <input className="border rounded px-2 py-1 w-56" placeholder="Base URL（列表接口或目录根）"
               value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        <select className="border rounded px-2 py-1" value={apiStyle} onChange={(e) => setApiStyle(e.target.value)}>
          <option value="json">JSON 列表接口</option>
          <option value="template">URL 模板</option>
        </select>
        {apiStyle === "template" && (
          <input className="border rounded px-2 py-1 w-64 mono" placeholder="https://host/img/{query}/{n}.png"
                 value={urlTpl} onChange={(e) => setUrlTpl(e.target.value)} />
        )}
        <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={add}>
          添加素材源
        </button>
      </div>
    </div>
  );
}
