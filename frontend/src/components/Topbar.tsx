import { useState } from "react";
import { useAuth } from "../store";
import { navigateKeep } from "../router";
import { notifyEnabled, setNotifyEnabled, notifyPermission } from "../notify";
import Avatar from "./Avatar";

export default function Topbar() {
  const { user } = useAuth();
  const [notifyOn, setNotifyOn] = useState(notifyEnabled());
  const [notifyPerm, setNotifyPerm] = useState(notifyPermission());

  return (
    <header className="h-12 shrink-0 bg-white border-b border-slate-200 flex items-center px-3 gap-3 shadow-sm">
      <div className="flex items-center gap-2 shrink-0">
        {/* 内联 SVG 品牌图标：不走 /favicon.svg 网络请求，避免旧包/缓存里 favicon 变更后
            列表页仍显示「加载失败」（打包版多次出现过 header 图标 broken-image）。
            图形与 packaging/exe/icon-src/trace-icon.svg 母版一致（圆角方块 + 白手柄），
            改母版时记得同步这里。 */}
        <svg className="h-5 w-5 shrink-0" viewBox="0 0 1024 1024" aria-hidden="true">
          <defs>
            <linearGradient id="ga-brand-tile" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor="#3B8BFF" />
              <stop offset="0.55" stopColor="#2F7DDF" />
              <stop offset="1" stopColor="#19C2A8" />
            </linearGradient>
            <linearGradient id="ga-brand-gloss" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#ffffff" stopOpacity="0.22" />
              <stop offset="0.45" stopColor="#ffffff" stopOpacity="0.04" />
              <stop offset="1" stopColor="#0b1a35" stopOpacity="0.10" />
            </linearGradient>
            <linearGradient id="ga-brand-pad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#ffffff" />
              <stop offset="1" stopColor="#eef4ff" />
            </linearGradient>
          </defs>
          <rect x="48" y="48" width="928" height="928" rx="210" fill="url(#ga-brand-tile)" />
          <rect x="48" y="48" width="928" height="928" rx="210" fill="url(#ga-brand-gloss)" />
          <rect x="48" y="48" width="928" height="928" rx="210" fill="none"
            stroke="#ffffff" strokeOpacity="0.20" strokeWidth="6" />
          <rect x="236" y="352" width="552" height="320" rx="160" fill="url(#ga-brand-pad)" />
          <g fill="#1E5FC8">
            <rect x="386" y="404" width="76" height="216" rx="26" />
            <rect x="316" y="474" width="216" height="76" rx="26" />
          </g>
          <circle cx="650" cy="512" r="66" fill="#1E5FC8" />
        </svg>
        <div className="ga-brand font-bold">循迹 Trace</div>
      </div>

      {/* 个人版没有「团队」概念：工作区就是当前账号自己的，不做切换。 */}
      <nav className="flex items-center gap-1 text-sm">
        <button
          className="px-2 py-1 rounded hover:bg-slate-100"
          onClick={() => navigateKeep("chat")}
        >
          对话
        </button>
        <button
          className="px-2 py-1 rounded hover:bg-slate-100"
          onClick={() => navigateKeep("assets")}
        >
          素材库
        </button>
        <button
          className="px-2 py-1 rounded hover:bg-slate-100"
          onClick={() => navigateKeep("usage")}
        >
          用量
        </button>
      </nav>

      <div className="ml-auto flex items-center gap-3 text-sm">
        <button
          className={`ga-notify ${notifyOn ? "on" : ""} p-1 rounded hover:bg-slate-100`}
          aria-label={notifyOn ? "关闭完成提醒" : "开启完成提醒"}
          aria-pressed={notifyOn}
          title={
            notifyOn
              ? `提示已开启：任务完成/需要回答时响铃并弹系统通知（桌面通知：${
                  notifyPerm === "granted" ? "已授权" : notifyPerm === "denied" ? "被浏览器拒绝，仅提示音" : "未授权，开启时已申请"
                }）。点击关闭`
              : "提示已关闭：任务完成/需要回答时不会有提醒。点击开启（会立刻试响一声 + 弹一条测试通知）"
          }
          onClick={() => {
            const v = !notifyOn;
            setNotifyEnabled(v);
            setNotifyOn(v);
            setNotifyPerm(notifyPermission());
            // 权限申请是异步的：稍后再读一次，让提示里的「已授权/被拒绝」尽快变成真状态
            if (v) window.setTimeout(() => setNotifyPerm(notifyPermission()), 800);
          }}
        >
          {notifyOn ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8.7 3A6 6 0 0 1 18 8c0 2.1.4 3.8.9 5.2" />
              <path d="M18 8c0 7 3 9 3 9H6.3" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
              <line x1="2" y1="2" x2="22" y2="22" />
            </svg>
          )}
        </button>
        <button
          className="flex items-center gap-2 rounded-full pl-2 pr-0.5 py-0.5 hover:bg-slate-100"
          onClick={() =>
            window.dispatchEvent(new CustomEvent("ga-open-settings", { detail: { tab: "profile" } }))
          }
          title="个人设置（头像 / 名称 / 密码 / 外观 / 模型 / 项目记忆 / 系统日志）"
          aria-label="打开设置"
        >
          <span className="text-slate-600 leading-tight text-right">
            <span className="block text-xs font-medium max-w-[140px] truncate">
              {user?.display_name || "未命名"}
            </span>
            <span className="block text-[10px] text-slate-400">{user?.email || "未登录"}</span>
          </span>
          <Avatar src={user?.avatar} name={user?.display_name} size={28} className="ring-1 ring-slate-200" />
        </button>
      </div>
    </header>
  );
}
