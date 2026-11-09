import { useEffect, useRef, useState } from "react";
import { useAuth } from "./store";
import { navigateKeep, paramsOf, parseHash, type Route } from "./router";
import { initTheme, usePrefs } from "./theme";
import { usePolicy } from "./assistantPolicy";
import { api } from "./api";
import Login from "./pages/Login";
import Chat from "./pages/Chat";
import Projects from "./pages/Projects";
import RunDetail from "./pages/RunDetail";
import Assets from "./pages/Assets";
import Config from "./pages/Config";
import Team from "./pages/Team";
import Topbar from "./components/Topbar";
import ErrorBoundary from "./components/ErrorBoundary";

/** 全局背景壁纸层：外链图片 / 上传的图片或视频 / Wallpaper Engine 工坊文件。
 *  fixed + z-0 垫底，业务界面以 relative z-10 盖在上面（开背景时面板转半透明毛玻璃）。 */
function BackgroundLayer() {
  const p = usePrefs((s) => s.prefs);
  const [blob, setBlob] = useState("");
  useEffect(() => {
    if (p.bgKind !== "we" || !p.bgSrc) {
      setBlob("");
      return;
    }
    let dead = false;
    let url = "";
    api.weFileBlob(p.bgSrc)
      .then((u) => {
        if (dead) URL.revokeObjectURL(u);
        else {
          url = u;
          setBlob(u);
        }
      })
      .catch(() => {
        if (!dead) setBlob("");
      });
    return () => {
      dead = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [p.bgKind, p.bgSrc]);

  if (p.bgKind === "none" || !p.bgSrc) return null;
  if (p.bgKind === "we" && !blob) return null; // 工坊文件还在取
  const isVideo = p.bgVideo && p.bgKind !== "url";
  const src = p.bgKind === "we" ? blob : p.bgSrc;
  return (
    <div className="ga-bg-layer" aria-hidden style={{ opacity: p.bgOpacity / 100 }}>
      {isVideo ? (
        <video className="ga-bg-media" src={src} autoPlay loop muted playsInline />
      ) : (
        <img className="ga-bg-media" src={src} alt="" />
      )}
      <div
        className="absolute inset-0"
        style={{
          backdropFilter: p.bgBlur ? `blur(${p.bgBlur}px)` : undefined,
          background: p.bgDim ? `rgba(8, 12, 22, ${p.bgDim / 100})` : undefined,
        }}
      />
    </div>
  );
}

export default function App() {
  const token = useAuth((s) => s.token);
  const [route, setRoute] = useState<Route>(parseHash());
  // 「配置」不再占一整页，而是盖在当前页上的设置弹窗。hash 仍然是入口
  // （所以 F5／后退能恢复），但底下渲染的页面保持打开弹窗前那一个。
  const lastView = useRef(route.view === "config" ? "chat" : route.view);
  const [settingsOpen, setSettingsOpen] = useState(route.view === "config");
  const [settingsTab, setSettingsTab] = useState<string | undefined>(undefined);

  useEffect(() => {
    initTheme();
  }, []);

  // 登录态恢复后同步一次个人资料（换设备/改过头像时，本地缓存可能是旧的）
  useEffect(() => {
    if (!token) return;
    api.me().then((r: any) => {
      if (r?.user) useAuth.getState().setUser(r.user);
    }).catch(() => {});
    // 助手行为设置（默认权限 / 指导项）：输入框草稿与设置页共用这份
    usePolicy.getState().load();
  }, [token]);

  useEffect(() => {
    const onHash = () => {
      const r = parseHash();
      setRoute(r);
      if (r.view === "config") {
        setSettingsTab(undefined);
        setSettingsOpen(true);
      } else lastView.current = r.view;
    };
    // 顶栏头像/用户名直接开设置弹窗（不写 hash，关闭后仍停在原页）；可指定初始分类
    const onOpenSettings = (e: Event) => {
      const tab = (e as CustomEvent).detail?.tab;
      setSettingsTab(typeof tab === "string" ? tab : undefined);
      setSettingsOpen(true);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("ga-open-settings", onOpenSettings);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("ga-open-settings", onOpenSettings);
    };
  }, []);

  if (!token)
    return (
      <>
        <BackgroundLayer />
        <div className="relative z-10 h-screen">
          <Login />
        </div>
      </>
    );

  // 弹窗盖住时，底下渲染的仍是打开前那一页（含它自己的参数）
  const baseRoute: Route = route.view === "config"
    ? { view: lastView.current, params: paramsOf(lastView.current) }
    : route;
  const closeSettings = () => {
    setSettingsOpen(false);
    navigateKeep(lastView.current);
  };

  return (
    <>
      <BackgroundLayer />
      <div className="ga-app-root relative z-10 h-screen flex flex-col bg-slate-100 text-slate-800">
        <Topbar />
        <div className="flex-1 overflow-hidden">
          {/* 页面级渲染兜底：单个页面崩了只替换内容区，不会把整站炸成黑屏 */}
          <ErrorBoundary resetKey={baseRoute.view}>
            {baseRoute.view === "projects" && <Projects />}
            {baseRoute.view === "assets" && <Assets route={baseRoute} />}
            {baseRoute.view === "team" && <Team />}
            {baseRoute.view === "runs" &&
              (baseRoute.params.id ? (
                <RunDetail runId={baseRoute.params.id} initialFile={baseRoute.params.file}
                           initialAsset={baseRoute.params.asset} />
              ) : (
                <div className="h-full flex flex-col items-center justify-center gap-2 text-slate-400 text-sm">
                  <span>未指定运行记录。</span>
                  <span className="text-xs">出于安全考虑，运行 ID 不进 URL——请在「对话」页右侧的运行面板里查看。</span>
                </div>
              ))}
            {baseRoute.view === "chat" && <Chat route={baseRoute} />}
          </ErrorBoundary>
        </div>
      </div>
      {settingsOpen && (
        <Config
          key={settingsTab || "default"}
          onClose={closeSettings}
          initialTab={(settingsTab as any) || undefined}
        />
      )}
    </>
  );
}
