import { create } from "zustand";
import type { Team, User } from "./types";

const KEY = "ga_profile";

/**
 * 本机身份（没有登录）：只是「界面怎么称呼我」+ 当前工作区的缓存。
 * 没有 token / 会话 / 登出——服务就是本机这一个用户，刷新页面也不需要重新认证。
 */
interface AuthState {
  user: User | null;
  teams: Team[];
  currentTeamId: string | null;
  setIdentity: (user: User, teams: Team[]) => void;
  setUser: (user: User) => void;
  setTeams: (teams: Team[]) => void;
}

function load(): Partial<AuthState> {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return {};
}

function persist(s: AuthState) {
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({ user: s.user, teams: s.teams, currentTeamId: s.currentTeamId })
    );
  } catch {
    /* 隐私模式/配额满：只是少了首屏缓存，不影响使用 */
  }
}

const initial = load();
// 历史数据可能存了空串，统一归为「未选择」再回退到第一个工作区
// （个人版：一台机器一个工作区，teams 里只会有 ≥1 条；字段名沿用 team 是为了不动后端的分片 id）
const initialTeamId = initial.currentTeamId || initial.teams?.[0]?.id || null;

export const useAuth = create<AuthState>((set, get) => ({
  user: initial.user ?? null,
  teams: initial.teams ?? [],
  currentTeamId: initialTeamId,

  setIdentity: (user, teams) => {
    const next = { user, teams, currentTeamId: teams[0]?.id ?? null };
    persist({ ...get(), ...next });
    set(next);
  },
  setTeams: (teams) => {
    const cur = get();
    const next = {
      ...cur,
      teams,
      currentTeamId: cur.currentTeamId || teams[0]?.id || null,
    };
    persist(next);
    set(next);
  },
  setUser: (user) => {
    const cur = get();
    const next = { ...cur, user };
    persist(next);
    set(next);
  },
}));
