import { create } from "zustand";
import type { Team, User } from "./types";

const KEY = "ga_auth";

interface AuthState {
  token: string | null;
  user: User | null;
  teams: Team[];
  currentTeamId: string | null;
  setAuth: (token: string, user: User, teams: Team[]) => void;
  setUser: (user: User) => void;
  setTeams: (teams: Team[]) => void;
  setTeam: (id: string) => void;
  logout: () => void;
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
  localStorage.setItem(
    KEY,
    JSON.stringify({
      token: s.token,
      user: s.user,
      teams: s.teams,
      currentTeamId: s.currentTeamId,
    })
  );
}

const initial = load();
// 历史数据可能存了空串，统一归一化为「未选择」再回退到第一个团队
const initialTeamId = initial.currentTeamId || initial.teams?.[0]?.id || null;

export const useAuth = create<AuthState>((set, get) => ({
  token: initial.token ?? null,
  user: initial.user ?? null,
  teams: initial.teams ?? [],
  currentTeamId: initialTeamId,

  setAuth: (token, user, teams) => {
    const next = {
      token,
      user,
      teams,
      currentTeamId: teams[0]?.id ?? null,
    };
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
  setTeam: (id) => {
    const cur = get();
    const next = { ...cur, currentTeamId: id || cur.teams[0]?.id || null };
    persist(next);
    set(next);
  },
  logout: () => {
    localStorage.removeItem(KEY);
    set({ token: null, user: null, teams: [], currentTeamId: null });
  },
}));

export function currentRole(): string | null {
  const { teams, currentTeamId } = useAuth.getState();
  return teams.find((t) => t.id === currentTeamId)?.role ?? null;
}
