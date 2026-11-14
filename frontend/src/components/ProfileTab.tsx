import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";
import Avatar from "./Avatar";

/** 本机资料设置：只是「界面怎么称呼我」——名称与头像。
 *  这个版本没有账号/密码/登录，所以这里没有改密码、没有退出登录。
 *  保存后同步更新全局身份缓存（顶栏/头像即时生效）。 */
export default function ProfileTab() {
  const user = useAuth((s) => s.user);
  const setUser = useAuth((s) => s.setUser);
  const fileRef = useRef<HTMLInputElement>(null);

  const [name, setName] = useState(user?.display_name || "");
  const [avatar, setAvatar] = useState<string | null>(user?.avatar || null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setName(user?.display_name || "");
    setAvatar(user?.avatar || null);
  }, [user?.display_name, user?.avatar]);

  async function saveProfile() {
    setMsg("");
    setErr("");
    const trimmed = name.trim();
    if (!trimmed) {
      setErr("名称不能为空");
      return;
    }
    setBusy(true);
    try {
      const r = await api.updateMe({ display_name: trimmed, avatar });
      setUser(r.user);
      setMsg("已保存");
    } catch (e: any) {
      setErr(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function pickAvatar(file: File | null | undefined) {
    if (!file) return;
    setMsg("");
    setErr("");
    if (!file.type.startsWith("image/")) {
      setErr("请选择图片文件");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      setErr("头像不能超过 8MB");
      return;
    }
    try {
      const r = await api.uploadAvatar(file);
      setAvatar(r.avatar);
      // 后端上传时已经落库，但名称可能还没保存：这里只刷新头像
      const me = await api.me();
      if (me?.user) setUser(me.user);
      setMsg("头像已更新");
    } catch (e: any) {
      setErr(e?.message || "头像上传失败");
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function removeAvatar() {
    setMsg("");
    setErr("");
    try {
      const r = await api.updateMe({ avatar: "" });
      setAvatar(null);
      setUser(r.user);
      setMsg("已恢复默认头像");
    } catch (e: any) {
      setErr(e?.message || "操作失败");
    }
  }

  return (
    <div className="max-w-2xl space-y-4">
      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="text-sm font-medium mb-3">个人资料</div>
        <div className="flex items-center gap-4">
          <div className="relative group">
            <Avatar src={avatar} name={name} size={72} className="ring-2 ring-slate-200" />
            <button
              type="button"
              className="absolute inset-0 rounded-full bg-slate-900/45 text-white text-[11px] opacity-0 group-hover:opacity-100 transition-opacity"
              onClick={() => fileRef.current?.click()}
              title="更换头像"
            >
              更换
            </button>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="text-xs px-2.5 py-1.5 rounded border border-slate-200 hover:bg-slate-50"
                onClick={() => fileRef.current?.click()}
              >
                上传新头像
              </button>
              {avatar && (
                <button
                  type="button"
                  className="text-xs px-2.5 py-1.5 rounded border border-slate-200 hover:bg-slate-50 text-slate-500"
                  onClick={removeAvatar}
                >
                  恢复默认
                </button>
              )}
            </div>
            <div className="text-[11px] text-slate-400">支持 JPG / PNG / WebP / GIF，不超过 8MB</div>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => pickAvatar(e.target.files?.[0])}
            />
          </div>
        </div>

        <div className="mt-4 grid grid-cols-[88px_1fr] gap-2 items-center text-sm">
          <label className="text-slate-500 text-xs">名称</label>
          <input
            className="border rounded px-2 py-1.5 max-w-sm"
            value={name}
            maxLength={120}
            onChange={(e) => setName(e.target.value)}
            placeholder="你的显示名称"
          />
        </div>

        <div className="mt-3 flex items-center gap-2">
          <button className="ga-btn ga-btn-primary" disabled={busy} onClick={saveProfile}>
            {busy ? "保存中…" : "保存资料"}
          </button>
          {msg && <span className="text-xs text-green-700">{msg}</span>}
          {err && <span className="text-xs text-red-600">{err}</span>}
        </div>
      </div>

      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="text-sm font-medium mb-1">无需登录</div>
        <div className="text-[11px] text-slate-400 leading-relaxed">
          本版本没有账号、密码，也没有登录/登出：打开页面即可使用，数据全部保存在本机数据目录里
          （可在「设置 / 存储空间」查看并迁移到别的磁盘）。
        </div>
      </div>
    </div>
  );
}
