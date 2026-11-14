/** 存储空间：查看数据目录占用、更换数据存储位置并把已有数据迁过去。
 *  数据（SQLite 库 / 日志 / 上传 / 项目工作区）默认在用户主目录的 .trace 下，
 *  时间久了会占满系统盘；这里可以改到别的盘，迁移完成后重启生效。
 *
 *  两个体验要点：
 *   1. 选目录走**系统原生**文件夹窗口（资源管理器那种），弹不出来才回退内置浏览器；
 *   2. 迁移是大目录整份复制（动辄几分钟），必须有进度条——否则用户以为卡死直接关窗口。 */
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import DirPicker from "./DirPicker";

interface StorageInfo {
  data_dir: string;
  default_dir: string;
  is_default: boolean;
  used_human: string;
  disk_free_human: string;
  disk_total_bytes: number;
  pending_switch_to: string;
  /** 非空 = 数据目录被环境变量 GA_DATA_DIR 钉死，这里改不动（后端会拒绝迁移） */
  env_override_dir?: string;
  /** 「浏览…」能否弹系统原生窗口（false → 用内置目录浏览器） */
  picker_native?: boolean;
}

export interface MigrateProgress {
  running: boolean;
  phase: "idle" | "scan" | "copy" | "db" | "done" | "error";
  percent: number | null;
  done_human: string;
  total_human: string;
  files_done: number;
  files_total: number;
  current: string;
  error: string;
  elapsed_s: number;
  speed_human: string;
  eta_s: number | null;
}

const PHASE_LABEL: Record<string, string> = {
  scan: "正在统计要迁移的数据…",
  copy: "正在复制文件…",
  db: "正在备份数据库…",
  done: "迁移完成",
  error: "迁移失败",
};

function fmtDuration(s: number | null | undefined): string {
  if (s == null || !isFinite(s) || s < 0) return "";
  const sec = Math.round(s);
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  const r = sec % 60;
  return r ? `${m} 分 ${r} 秒` : `${m} 分`;
}

export default function StorageTab() {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [pickOpen, setPickOpen] = useState(false);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [prog, setProg] = useState<MigrateProgress | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function refresh() {
    api.storageInfo().then((r: StorageInfo) => setInfo(r)).catch((e: any) => setErr(e?.message || "读取失败"));
  }
  useEffect(refresh, []);

  // 进度轮询：迁移中每 400ms 拉一次；结束（done/error）就停。
  // 单独写成 effect，是为了「刷新页面/重开设置页」时也能接上正在跑的迁移。
  useEffect(() => {
    const stop = () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
    const tick = () =>
      api
        .storageProgress()
        .then((p: MigrateProgress) => {
          setProg(p);
          if (!p.running && p.phase !== "idle") stop();
        })
        .catch(() => {});
    tick();
    pollRef.current = setInterval(tick, 400);
    return stop;
  }, [busy]);

  async function browse() {
    setErr("");
    setMsg("");
    setPicking(true);
    try {
      const r = await api.storagePickDir("选择数据存放位置");
      if (r?.path) setTarget(r.path);
      // 用户点了取消（cancelled=true）就什么都不做，别把他刚填的路径清掉
    } catch (e: any) {
      // 501 = 这台机器弹不出原生窗口（无桌面 / 非 Windows 无 GUI）→ 回退内置目录浏览器
      if (e?.status === 501 || e?.status === 500) {
        setPickOpen(true);
      } else {
        setErr(e?.message || "打开文件夹选择窗口失败");
      }
    } finally {
      setPicking(false);
    }
  }

  async function doMigrate(dir: string) {
    if (!dir) { setErr("请先选择目标文件夹"); return; }
    setBusy(true);
    setMsg("");
    setErr("");
    try {
      const r: any = await api.storageMigrate(dir);
      setMsg(r?.message || "迁移完成，重启后生效");
      refresh();
    } catch (e: any) {
      setErr(e?.message || "迁移失败");
    } finally {
      setBusy(false);
    }
  }

  const running = busy || !!prog?.running;
  const pct = prog?.percent ?? null;
  const indeterminate = running && (pct == null);

  return (
    <div className="space-y-4 max-w-2xl">
      <div>
        <h3 className="text-sm font-semibold text-slate-800">数据存储位置</h3>
        <p className="text-xs text-slate-500 mt-1 leading-relaxed">
          对话记录、生成产物、项目工作区都存放在这里。数据量大时不要放在系统盘（C 盘），
          迁移到其他磁盘可避免占满系统盘。
        </p>
      </div>

      {err && <div className="text-xs text-rose-600">{err}</div>}
      {msg && <div className="text-xs text-emerald-700">{msg}</div>}

      {info && (
        <div className="rounded border border-slate-200 bg-slate-50 p-3 space-y-2">
          <div className="flex items-start gap-2 text-[13px]">
            <span className="text-slate-500 shrink-0 w-20">当前目录</span>
            <span className="mono text-slate-800 break-all flex-1">{info.data_dir}</span>
            {info.is_default && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600 shrink-0">默认</span>
            )}
          </div>
          <div className="flex items-center gap-2 text-[13px]">
            <span className="text-slate-500 shrink-0 w-20">已用空间</span>
            <span className="text-slate-800">{info.used_human}</span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-500">所在盘剩余 {info.disk_free_human}</span>
          </div>
        </div>
      )}

      {info?.pending_switch_to && (
        <div className="rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800">
          已迁移到 <span className="mono break-all">{info.pending_switch_to}</span>，
          <b>重启应用后生效</b>。重启时会自动清理旧目录。
        </div>
      )}

      {info?.env_override_dir && (
        <div className="rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800">
          数据目录被环境变量 <span className="mono">GA_DATA_DIR</span> 固定为{" "}
          <span className="mono break-all">{info.env_override_dir}</span>，这里的切换不会生效。
          请先删掉 <span className="mono">.env</span>（或启动脚本）里的 GA_DATA_DIR，再来迁移。
        </div>
      )}

      <div className="rounded border border-slate-200 p-3 space-y-2">
        <div className="text-[13px] font-medium text-slate-700">更换存储位置</div>
        <div className="flex items-center gap-2">
          <input
            className="flex-1 min-w-0 border rounded px-2 py-1.5 text-xs mono bg-white"
            placeholder="选择一个新的空文件夹（如 D:\TraceData）"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            disabled={running}
          />
          <button
            className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
            onClick={browse}
            disabled={running || picking}
            title="打开系统文件夹窗口选择"
          >
            {picking ? "等待选择…" : "浏览…"}
          </button>
          <button
            className="ga-btn ga-btn-primary ga-btn-sm shrink-0"
            disabled={running || !target || !!info?.env_override_dir}
            onClick={() => doMigrate(target)}
          >
            {running ? "迁移中…" : "迁移到此处"}
          </button>
        </div>

        {running && (
          <div className="rounded border border-sky-200 bg-sky-50/70 p-2.5 space-y-1.5">
            <div className="flex items-center justify-between text-[11px] text-slate-600">
              <span>{PHASE_LABEL[prog?.phase || "scan"] || "迁移中…"}</span>
              <span className="mono">
                {indeterminate
                  ? prog?.total_human
                    ? `已统计 ${prog.total_human}`
                    : ""
                  : `${(pct ?? 0).toFixed(1)}%`}
              </span>
            </div>
            <div className="h-2 rounded bg-slate-200 overflow-hidden">
              {indeterminate ? (
                <div className="h-full w-1/3 bg-sky-500 ga-progress-indeterminate" />
              ) : (
                <div
                  className="h-full bg-sky-500 transition-all duration-300"
                  style={{ width: `${Math.max(1, Math.min(100, pct ?? 0))}%` }}
                />
              )}
            </div>
            <div className="flex items-center justify-between text-[11px] text-slate-500 gap-2">
              <span className="truncate mono" title={prog?.current || ""}>
                {prog?.done_human || "0 B"}
                {prog?.total_human ? ` / ${prog.total_human}` : ""}
                {prog?.files_total ? ` · ${prog.files_done}/${prog.files_total} 个文件` : ""}
              </span>
              <span className="shrink-0">
                {prog?.speed_human ? `${prog.speed_human} · ` : ""}
                {prog?.eta_s != null ? `剩余约 ${fmtDuration(prog.eta_s)}` : "计算剩余时间…"}
              </span>
            </div>
            {prog?.current && (
              <div className="text-[11px] text-slate-400 truncate mono" title={prog.current}>
                {prog.current}
              </div>
            )}
          </div>
        )}

        {!running && prog?.phase === "error" && prog.error && (
          <div className="text-[11px] text-rose-600">上次迁移失败：{prog.error}</div>
        )}

        <p className="text-[11px] text-slate-400 leading-relaxed">
          迁移会把当前全部数据复制到新位置，<b>旧数据在成功前不会删除</b>；迁移完成后需重启应用生效，
          重启时自动清理旧目录。目标文件夹必须为空。
        </p>
      </div>

      <DirPicker
        open={pickOpen}
        onClose={() => setPickOpen(false)}
        onPick={(p) => { setTarget(p); setPickOpen(false); }}
      />
    </div>
  );
}
