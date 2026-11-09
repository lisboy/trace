export interface Route {
  view: string;
  params: Record<string, string>;
}

/**
 * 路由参数不进 URL（避免把会话/项目/run 等 ID 暴露在地址栏）：
 * - hash 只保留页面名（#/chat、#/runs …）
 * - 页面参数存内存 + sessionStorage（按 view 记录最近一次）
 * - F5 / 后退可恢复当前位置；直接粘贴带 ID 的旧 URL 会被忽略 query
 */
const ROUTE_KEY = "ga_route_map";

let paramsMap: Record<string, Record<string, string>> = (() => {
  try {
    return JSON.parse(sessionStorage.getItem(ROUTE_KEY) || "{}");
  } catch {
    return {};
  }
})();

export function parseHash(): Route {
  const h = location.hash.replace(/^#\/?/, "");
  const view = (h.split("/")[0] || "").split("?")[0] || "chat";
  return { view, params: paramsMap[view] || {} };
}

/** 取某页记住的参数。设置弹窗盖在页面上时，底下那页仍要按自己的参数渲染。 */
export function paramsOf(view: string): Record<string, string> {
  return paramsMap[view] || {};
}

export function navigate(view: string, params: Record<string, string> = {}) {
  paramsMap[view] = { ...params };
  try {
    sessionStorage.setItem(ROUTE_KEY, JSON.stringify(paramsMap));
  } catch {
    /* 隐私模式下忽略 */
  }
  _go(view);
}

/** 回到某页但不覆盖该页已存的参数（保留最近会话/项目等状态）。 */
export function navigateKeep(view: string) {
  _go(view);
}

function _go(view: string) {
  const target = `#/${view}`;
  if (location.hash === target) {
    // 同页重复导航（仅更新参数，如素材页切换项目）→ 手动触发，让 App 重新解析
    window.dispatchEvent(new Event("hashchange"));
  } else {
    location.hash = target;
  }
}
