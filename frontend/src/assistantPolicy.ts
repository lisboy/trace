import { create } from "zustand";
import { api } from "./api";
import type { PermissionScope } from "./types";

/**
 * 助手行为设置：新会话的默认权限作用域 + 指导项。
 * - 默认权限决定「新建会话」初始用哪一种（已存在的会话各自记着自己的）。
 * - 指导项是给模型看的操作要点，由后端注入每一轮的系统提示。
 * 登录后拉一次到全局 store，输入框/设置页都读同一份。
 */
export interface GuidanceItem {
  id: string;
  title: string;
  text: string;
  enabled: boolean;
}

interface PolicyState {
  permission_scope: PermissionScope;
  guidance: GuidanceItem[];
  defaults: GuidanceItem[];
  loaded: boolean;
  load: () => Promise<void>;
  save: (patch: { permission_scope?: PermissionScope; guidance?: GuidanceItem[] }) => Promise<void>;
}

const LS_DEFAULT_SCOPE = "ga_default_scope";

export const usePolicy = create<PolicyState>((set, get) => ({
  permission_scope: (() => {
    try {
      return (localStorage.getItem(LS_DEFAULT_SCOPE) as PermissionScope) || "workspace";
    } catch {
      return "workspace";
    }
  })(),
  guidance: [],
  defaults: [],
  loaded: false,
  load: async () => {
    try {
      const r: any = await api.assistantPolicy();
      const scope: PermissionScope = r?.permission_scope === "full" ? "full" : "workspace";
      try { localStorage.setItem(LS_DEFAULT_SCOPE, scope); } catch { /* ignore */ }
      set({
        permission_scope: scope,
        guidance: Array.isArray(r?.guidance) ? r.guidance : [],
        defaults: Array.isArray(r?.defaults) ? r.defaults : [],
        loaded: true,
      });
    } catch {
      set({ loaded: true });
    }
  },
  save: async (patch) => {
    const next = {
      permission_scope: patch.permission_scope ?? get().permission_scope,
      guidance: patch.guidance ?? get().guidance,
    };
    const r: any = await api.saveAssistantPolicy(next);
    const scope: PermissionScope = r?.permission_scope === "full" ? "full" : "workspace";
    try { localStorage.setItem(LS_DEFAULT_SCOPE, scope); } catch { /* ignore */ }
    set({
      permission_scope: scope,
      guidance: Array.isArray(r?.guidance) ? r.guidance : next.guidance,
    });
  },
}));

/** 新会话默认权限（同步读，避免输入框首帧闪一下） */
export function defaultScope(): PermissionScope {
  try {
    return (localStorage.getItem(LS_DEFAULT_SCOPE) as PermissionScope) || "workspace";
  } catch {
    return "workspace";
  }
}
