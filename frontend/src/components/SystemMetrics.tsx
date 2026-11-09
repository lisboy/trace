/** 本机资源读数：CPU / 内存 / GPU 占用挂在会话顶栏上。
 *
 * 为什么放顶栏而不只放系统日志：跑批出图、配音、压上下文的时候，
 * 「这台机器现在还有没有余量」是决定要不要再发一个任务的第一现场，
 * 不该为了看一眼去翻日志弹窗。
 * 取数在服务端按需 + 4s 缓存（见 core/sysinfo.py），所以这里 5s 一轮不会把机器压垮；
 * 页面切到后台就停轮询——没人看的时候不该一直 spawn nvidia-smi。
 */
import { useEffect, useState } from "react";
import { api } from "../api";
import type { SystemMetrics as Metrics } from "../types";

const POLL_MS = 5000;

function fmtMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)}G` : `${Math.round(mb)}M`;
}

/** 占用高低决定颜色：接近打满就该显眼，不然看着一片灰以为没事。 */
function tone(pct: number): string {
  if (pct >= 90) return "text-red-600";
  if (pct >= 70) return "text-amber-600";
  return "text-slate-600";
}

export default function SystemMetrics() {
  const [m, setM] = useState<Metrics | null>(null);

  useEffect(() => {
    let alive = true;
    let timer = 0;
    async function pull() {
      try {
        const r = await api.systemMetrics();
        // 空对象 = 这个后端还没有该接口（旧进程没重启）：整条不显示，别挂个「—」骗人
        if (alive) setM(r && Object.keys(r).length ? r : null);
      } catch {
        /* 一次失败等下一轮，不打扰用户 */
      }
    }
    function schedule() {
      window.clearTimeout(timer);
      if (document.hidden) {
        timer = window.setTimeout(schedule, POLL_MS);
        return;
      }
      pull().finally(() => {
        timer = window.setTimeout(schedule, POLL_MS);
      });
    }
    schedule();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);

  if (!m || m.mem_pct === undefined) return null;
  const gpu = (m.gpus || [])[0];
  const cpu = Math.round(m.cpu_pct || 0);
  const mem = Math.round(m.mem_pct || 0);
  const title = [
    `CPU ${cpu}%（${m.cores} 核）`,
    `内存 ${fmtMb(m.mem_used_mb || 0)} / ${fmtMb(m.mem_total_mb || 0)}（${mem}%）`,
    `虚拟内存 ${Math.round(m.swap_pct || 0)}%（${fmtMb(m.swap_used_mb || 0)} / ${fmtMb(m.swap_total_mb || 0)}）`,
    `本服务占用 ${fmtMb(m.proc_mem_mb || 0)} · CPU ${Math.round(m.proc_cpu_pct || 0)}%`,
    gpu
      ? `GPU ${gpu.name}：核心 ${gpu.util_pct}% · 显存 ${fmtMb(gpu.mem_used_mb)} / ${fmtMb(gpu.mem_total_mb)} · ${gpu.temp_c}℃`
      : "GPU：没读到 nvidia-smi（非 N 卡或驱动没就绪）",
    (m.gpus || []).length > 1 ? `另有 ${m.gpus!.length - 1} 张卡未显示` : "",
  ].filter(Boolean).join("\n");

  return (
    <div
      className="flex items-center gap-2.5 text-[11px] px-2 py-1 rounded border border-slate-200 bg-slate-50 shrink-0"
      title={title}
    >
      <span className="text-[11px] text-slate-400">机器</span>
      <span className="flex items-center gap-1">
        <span className="text-[11px] text-slate-400">CPU</span>
        <span className={`font-medium tabular-nums ${tone(cpu)}`}>{cpu}%</span>
      </span>
      <span className="flex items-center gap-1">
        <span className="text-[11px] text-slate-400">内存</span>
        <span className={`font-medium tabular-nums ${tone(mem)}`}>{mem}%</span>
      </span>
      {gpu && (
        <span className="flex items-center gap-1">
          <span className="text-[11px] text-slate-400">GPU</span>
          <span className={`font-medium tabular-nums ${tone(gpu.util_pct)}`}>{gpu.util_pct}%</span>
          <span className="text-slate-400 tabular-nums">
            {fmtMb(gpu.mem_used_mb)}/{fmtMb(gpu.mem_total_mb)}
          </span>
        </span>
      )}
    </div>
  );
}
