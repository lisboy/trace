import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";

export default function Login() {
  const setAuth = useAuth((s) => s.setAuth);
  const [mode, setMode] = useState<"login" | "setup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // null=未知；true=全新安装（还没任何账号）。全新安装不能预填任何「默认账号」，
  // 否则用户对着一个根本不存在的账号登录，只会收到「邮箱或密码错误」。
  const [fresh, setFresh] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .authStatus()
      .then((s) => {
        if (!alive) return;
        setFresh(!s.initialized);
        if (!s.initialized) setMode("setup");
      })
      .catch(() => {
        if (alive) setFresh(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function submit() {
    setErr("");
    setBusy(true);
    try {
      const data =
        mode === "login"
          ? await api.login(email, password)
          : await api.setup({ token, email, display_name: name, password });
      const teams = await api.teams();
      setAuth(data.access_token, data, teams);
    } catch (e: any) {
      setErr(e?.message || "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-screen flex items-center justify-center bg-slate-100">
      <div className="w-80 bg-white rounded-lg shadow p-6 space-y-3">
        <h1 className="text-xl font-bold text-brand-700">循迹 Trace</h1>
        <p className="text-xs text-slate-500">
          团队协作型多角色游戏开发智能体
        </p>

        {fresh && (
          <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded p-2 leading-relaxed">
            首次使用：还没有任何账号。请复制启动窗口（双击 start.cmd 打开的黑框）里打印的
            「引导 token」，在下方「首次初始化」里创建第一个 owner 账号。
          </div>
        )}

        <div className="flex text-sm border-b border-slate-200">
          <button
            className={mode === "login" ? "pb-1 border-b-2 border-brand-600" : "pb-1 text-slate-400"}
            onClick={() => setMode("login")}
          >
            登录
          </button>
          <button
            className={`ml-3 pb-1 ${mode === "setup" ? "border-b-2 border-brand-600" : "text-slate-400"}`}
            onClick={() => setMode("setup")}
          >
            首次初始化
          </button>
        </div>

        {mode === "setup" && (
          <input
            className="w-full border border-slate-200 rounded px-2 py-1 text-sm"
            placeholder="引导 token（启动日志 / GA_BOOTSTRAP_TOKEN）"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        )}
        <input
          className="w-full border border-slate-200 rounded px-2 py-1 text-sm"
          placeholder="邮箱"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        {mode === "setup" && (
          <input
            className="w-full border border-slate-200 rounded px-2 py-1 text-sm"
            placeholder="显示名"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        )}
        <input
          className="w-full border border-slate-200 rounded px-2 py-1 text-sm"
          type="password"
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        {err && <div className="text-xs text-red-600">{err}</div>}
        <button
          className="ga-btn ga-btn-primary w-full"
          onClick={submit}
          disabled={busy}
        >
          {busy ? "请稍候…" : mode === "login" ? "登录" : "创建首个 owner"}
        </button>
        {mode === "setup" && (
          <p className="text-[11px] text-slate-400">
            首次启动若未设置 GA_BOOTSTRAP_TOKEN，服务端会在日志打印一次性 token。
          </p>
        )}
      </div>
    </div>
  );
}
