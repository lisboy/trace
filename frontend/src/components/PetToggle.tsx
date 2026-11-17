import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

/**
 * 顶栏「桌宠」开关：点一下把桌面上的小白狐「霜尾」叫出来 / 收回去。
 *
 * 为什么要有这个组件：桌宠是独立 Electron 进程（透明置顶窗），
 * 浏览器页面自己开不出来，所以开关放在这里，真正动作由后端
 * /api/pet/start|stop 负责拉起/结束进程。
 *
 * 状态以服务端为准（每 8s 轮询一次）：用户可能在系统托盘/菜单里
 * 直接把桌宠关掉，前端只记本地 state 会显示错。
 */
export default function PetToggle() {
  const [running, setRunning] = useState(false);
  const [available, setAvailable] = useState(true);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState("");
  const hintTimer = useRef<number | null>(null);

  const flash = useCallback((text: string) => {
    setHint(text);
    if (hintTimer.current) window.clearTimeout(hintTimer.current);
    hintTimer.current = window.setTimeout(() => setHint(""), 2600);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const s = await api.petStatus();
      setRunning(!!s.running);
      setAvailable(!!s.available);
      setReason(s.reason || "");
    } catch {
      // 后端老版本没有 /pet/status 时静默降级，不打扰用户
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 8000);
    return () => {
      window.clearInterval(id);
      if (hintTimer.current) window.clearTimeout(hintTimer.current);
    };
  }, [refresh]);

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (running) {
        await api.petStop();
        setRunning(false);
        flash("桌宠已收起");
      } else {
        const r = await api.petStart();
        setRunning(true);
        flash(r?.already ? "桌宠已经在桌面上了" : "桌宠已放到桌面");
      }
    } catch (e: any) {
      flash(e?.message || "桌宠启动失败");
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const disabled = busy || !available;
  const title = !available
    ? reason || "桌宠环境未就绪（缺少 Electron）"
    : running
      ? "收起桌面上的霜尾"
      : "把霜尾放到桌面上（可切换成女 / 少女 / 兽形）";

  return (
    <div className="relative flex items-center">
      <button
        type="button"
        className={
          "ga-btn ga-btn-sm " + (running ? "ga-btn-primary" : "ga-btn-secondary")
        }
        onClick={toggle}
        disabled={disabled}
        title={title}
        aria-pressed={running}
      >
        <span aria-hidden="true" className="mr-1">
          {running ? "🦊" : "🐾"}
        </span>
        {busy ? "…" : running ? "收起霜尾" : "霜尾"}
      </button>
      {hint && (
        <span className="absolute right-0 top-full mt-1 whitespace-nowrap rounded bg-slate-800/90 px-2 py-1 text-[11px] text-white shadow">
          {hint}
        </span>
      )}
    </div>
  );
}
