import { useEffect, useState } from "react";
import { api } from "../api";

/** 项目脚本快捷按钮：工作区根目录有 run.bat|start.bat 显示「启动」，有 stop.bat 显示「停止」。
 * 脚本由后端分离启动（不等待执行结果），按钮只反馈「命令已发送」。 */
export function ProjectScriptButtons({ projectId }: { projectId: string }) {
  const [scripts, setScripts] = useState<{ run: string | null; stop: string | null } | null>(null);
  const [busy, setBusy] = useState<"run" | "stop" | null>(null);
  const [tip, setTip] = useState("");

  useEffect(() => {
    let alive = true;
    setScripts(null);
    setTip("");
    api.projectScripts(projectId).then((r) => {
      if (alive) setScripts({ run: r.run_script, stop: r.stop_script });
    }).catch(() => {});
    return () => { alive = false; };
  }, [projectId]);

  if (!scripts || (!scripts.run && !scripts.stop)) return null;

  async function exec(action: "run" | "stop") {
    if (busy) return;
    setBusy(action);
    setTip("");
    try {
      const r = await api.runProjectScript(projectId, action);
      setTip(`已执行 ${r.script}（后台运行）`);
    } catch (e: any) {
      setTip(e?.message || "执行失败");
    } finally {
      setBusy(null);
      setTimeout(() => setTip(""), 5000);
    }
  }

  return (
    <div className="flex items-center gap-1">
      {scripts.run && (
        <button
          className="ga-btn ga-btn-secondary ga-btn-sm"
          title={`运行 ${scripts.run}`}
          onClick={() => exec("run")} disabled={busy !== null}>
          {busy === "run" ? "启动中…" : "▶ 启动"}
        </button>
      )}
      {scripts.stop && (
        <button
          className="ga-btn ga-btn-danger ga-btn-sm"
          title={`运行 ${scripts.stop}`}
          onClick={() => exec("stop")} disabled={busy !== null}>
          {busy === "stop" ? "停止中…" : "■ 停止"}
        </button>
      )}
      {tip && <span className="text-[10px] text-slate-400">{tip}</span>}
    </div>
  );
}
