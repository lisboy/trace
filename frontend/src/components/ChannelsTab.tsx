import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";

/** 通道能力档位（与后端 CHANNELS 对齐；后端返回为准，这里只做兜底文案） */
type ChannelMeta = {
  key: string;
  label: string;
  can_push: boolean;
  can_inbound: boolean;
  note: string;
};

type LinkRow = {
  id: string;
  channel: string;
  channel_label: string;
  external_id: string;
  display_name: string | null;
  status: string;
  can_push: boolean;
  can_inbound: boolean;
  notify_done: boolean;
  notify_question: boolean;
  notify_failed: boolean;
  inbound_enabled: boolean;
  default_project_id: string | null;
  credentials_ready: Record<string, boolean>;
  linked_at: string | null;
  last_notified_at: string | null;
};

type MsgRow = {
  id: string;
  channel: string;
  kind: string;
  title: string | null;
  body: string;
  status: string;
  error: string | null;
  created_at: string;
};

type Project = { id: string; name: string };

/** 每个通道的填写草稿 */
type Draft = { webhook: string; secret: string; ext: string; gateway: string };
const EMPTY: Draft = { webhook: "", secret: "", ext: "", gateway: "" };

const KIND_LABEL: Record<string, string> = {
  done: "任务完成", question: "待你回答", failed: "任务失败", test: "通道自检",
};

/** 「消息通道」设置页：把任务完成提醒推到钉钉 / 企业微信 / 自定义 Webhook。
 *
 * 交互只有两步，界面按这个顺序自上而下排：
 *   ① 在钉钉/企微群里加一个「群机器人」，把 Webhook 地址粘进来 → ② 点「发测试消息」当场确认通了。
 * **没有扫码、不需要公网地址**：提醒是本机主动出站推送，内网 / NAT 后面照样能发。
 * 个人微信没有官方开放接口，所以「微信」那张卡会如实说明「平台无法直接推送到个人微信」，
 * 只在用户自备第三方网关时提供填地址的入口，而不是假装能推。
 */
export default function ChannelsTab() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [channels, setChannels] = useState<ChannelMeta[]>([]);
  const [links, setLinks] = useState<LinkRow[]>([]);
  const [appCfg, setAppCfg] = useState<any>({});
  const [projects, setProjects] = useState<Project[]>([]);
  const [messages, setMessages] = useState<MsgRow[]>([]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [cfgDraft, setCfgDraft] = useState<Record<string, string>>({});
  const [publicUrl, setPublicUrl] = useState("");

  const refresh = useCallback(() => {
    api.channels().then((r: any) => {
      setChannels(r.channels || []);
      setLinks(r.links || []);
      setAppCfg(r.app_config || {});
      setPublicUrl(r.app_config?.public_base_url || "");
    }).catch((e: any) => setMsg(e?.message || "加载失败"));
    api.channelMessages().then((r: any) => setMessages(r.items || [])).catch(() => {});
  }, []);

  useEffect(refresh, [refresh]);

  useEffect(() => {
    // 默认项目下拉：从通道发来的新话题落到哪个项目
    const teamId = useAuth.getState().currentTeamId;
    if (!teamId) return;
    api.projects(teamId).then((ps: any) => {
      setProjects((ps || []).map((p: any) => ({ id: p.id, name: p.name })));
    }).catch(() => {});
  }, [currentTeamId]);

  const draftOf = (ch: string): Draft => drafts[ch] || EMPTY;
  const setDraft = (ch: string, patch: Partial<Draft>) =>
    setDrafts((d) => ({ ...d, [ch]: { ...(d[ch] || EMPTY), ...patch } }));

  async function savePublicUrl() {
    setBusy("public");
    setMsg("");
    try {
      await api.saveChannelAppConfig({ public_base_url: publicUrl.trim() });
      setMsg("公网地址已保存");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    } finally {
      setBusy("");
    }
  }

  async function saveAppCfg() {
    setBusy("cfg");
    setMsg("");
    try {
      await api.saveChannelAppConfig(cfgDraft);
      setCfgDraft({});
      setMsg("应用凭证已保存");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    } finally {
      setBusy("");
    }
  }

  async function bindChannel(ch: string) {
    const d = draftOf(ch);
    if (ch === "wechat") {
      if (!d.gateway.trim()) { setMsg("请先填写你自备网关的地址"); return; }
    } else if (!d.webhook.trim()) {
      setMsg("请先填写群机器人 Webhook 地址");
      return;
    }
    setBusy("bind-" + ch);
    setMsg("");
    try {
      const body: any = {};
      if (d.webhook.trim()) body.webhook = d.webhook.trim();
      if (d.secret.trim()) body.webhook_secret = d.secret.trim();
      if (d.gateway.trim()) body.gateway_url = d.gateway.trim();
      if (d.ext.trim()) body.external_id = d.ext.trim();
      const r: any = await api.channelBind(ch, body);
      setDraft(ch, EMPTY);
      setMsg(r?.message || "已关联");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "关联失败");
    } finally {
      setBusy("");
    }
  }

  async function unlink(id: string) {
    setBusy(id);
    try {
      await api.unlinkChannel(id);
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "解绑失败");
    } finally {
      setBusy("");
    }
  }

  async function patch(id: string, body: any) {
    try {
      await api.patchChannel(id, body);
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    }
  }

  async function testPush(id: string) {
    setBusy("test-" + id);
    setMsg("");
    try {
      const r: any = await api.testChannel(id);
      setMsg(r.ok ? "测试消息已发出，请到钉钉/企微里确认收到" : `发送失败：${r.error || "未知原因"}`);
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "发送失败");
    } finally {
      setBusy("");
    }
  }

  const linksOf = (ch: string) => links.filter((l) => l.channel === ch);
  const box = "bg-white rounded border border-slate-200 p-4";
  const input = "border border-slate-300 rounded px-2 py-1.5 text-sm w-full";
  const btn = "px-3 py-1.5 rounded text-sm bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50";
  const btn2 = "px-2.5 py-1 rounded text-sm border border-slate-300 hover:bg-slate-50";

  /** 单条绑定的开关区（多个绑定共用，群提醒 / 定向个人各一条） */
  const renderLink = (link: LinkRow, meta: ChannelMeta) => (
    <div key={link.id} className="mt-2 rounded border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-center gap-2 flex-wrap text-xs text-slate-600">
        <span className="text-green-700 font-medium">已关联：{link.display_name || link.external_id}</span>
        {link.credentials_ready?.webhook && <span className="px-1.5 py-0.5 rounded bg-white border border-slate-200">群机器人</span>}
        {link.credentials_ready?.gateway_url && <span className="px-1.5 py-0.5 rounded bg-white border border-slate-200">第三方网关</span>}
      </div>
      <div className="flex items-center gap-4 flex-wrap text-sm text-slate-700 mt-2">
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={link.notify_done}
            onChange={(e) => patch(link.id, { notify_done: e.target.checked })} />
          任务完成提醒
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={link.notify_question}
            onChange={(e) => patch(link.id, { notify_question: e.target.checked })} />
          需要我回答时
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={link.notify_failed}
            onChange={(e) => patch(link.id, { notify_failed: e.target.checked })} />
          失败告警
        </label>
        {meta.can_inbound && (
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={link.inbound_enabled}
              onChange={(e) => patch(link.id, { inbound_enabled: e.target.checked })} />
            允许在这里回复继续对话（需公网回调）
          </label>
        )}
      </div>
      <div className="flex items-center gap-2 flex-wrap mt-2">
        <label className="text-xs text-slate-500">新话题落到的项目</label>
        <select className="border border-slate-300 rounded px-2 py-1 text-sm"
          value={link.default_project_id || ""}
          onChange={(e) => patch(link.id, { default_project_id: e.target.value })}>
          <option value="">（自动选第一个）</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button className={btn2} disabled={busy === "test-" + link.id} onClick={() => testPush(link.id)}>
          发测试消息
        </button>
        <button className="px-2.5 py-1 rounded text-sm text-red-600 border border-red-200 hover:bg-red-50"
          disabled={busy === link.id} onClick={() => unlink(link.id)}>
          解绑
        </button>
      </div>
    </div>
  );

  return (
    <div className="space-y-4 max-w-4xl">
      <div>
        <h3 className="font-medium text-slate-800">消息通道</h3>
        <p className="text-xs text-slate-500 mt-1">
          任务跑完 / 需要你回答 / 失败时，把提醒推送到钉钉或企业微信群。
          <span className="text-slate-700">纯出站推送，不需要公网地址、不需要扫码</span>——
          在群里加个机器人，把 Webhook 地址粘进来就行。
        </p>
      </div>

      {msg && <div className="text-xs text-brand-700 bg-brand-50 border border-brand-100 rounded px-3 py-2">{msg}</div>}

      {/* ① 各通道：直填地址/凭证 */}
      {channels.filter((c) => c.key !== "webhook").map((c) => {
        const dt = c.key === "dingtalk";
        const wc = c.key === "wecom";
        const wx = c.key === "wechat";
        const d = draftOf(c.key);
        const myLinks = linksOf(c.key);
        return (
          <div key={c.key} className={box}>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium text-slate-800">{c.label}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${c.can_push ? "bg-green-50 text-green-700" : "bg-amber-50 text-amber-700"}`}>
                {c.can_push ? "可推送提醒" : "官方无推送接口"}
              </span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${c.can_inbound ? "bg-green-50 text-green-700" : "bg-slate-100 text-slate-500"}`}>
                {c.can_inbound ? "可回复对话" : "仅外发"}
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-1">{c.note}</p>

            {/* 个人微信：不给「填个地址就能推」的假入口，如实说明 */}
            {wx && (
              <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
                个人微信（你手机里那个微信）<b>无法设置</b>：微信官方没有对个人号开放消息推送接口，
                任何「扫码绑微信然后自动提醒」的方案都依赖第三方非官方客户端（有封号风险）。
                想收到微信侧的提醒，现实做法是：<b>企业微信群机器人</b>（在微信里能收到企业微信消息），
                或你自备网关（wechaty 之类）后填下面对应地址。
              </div>
            )}

            {!wx && (
              <div className="mt-3 grid grid-cols-2 gap-2">
                <label className="text-xs text-slate-600 col-span-2">
                  <span>{dt ? "钉钉群机器人 Webhook" : "企微群机器人 Webhook"}</span>
                  <input className={input + " mt-1"}
                    placeholder={dt ? "https://oapi.dingtalk.com/robot/send?access_token=..." : "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."}
                    value={d.webhook} onChange={(e) => setDraft(c.key, { webhook: e.target.value })} />
                </label>
                {dt && (
                  <label className="text-xs text-slate-600">
                    <span>加签密钥（选填）</span>
                    <input className={input + " mt-1"} placeholder="机器人安全设置选「加签」时填 SEC 开头那串"
                      value={d.secret} onChange={(e) => setDraft(c.key, { secret: e.target.value })} />
                  </label>
                )}
                <label className={dt ? "text-xs text-slate-600" : "text-xs text-slate-600 col-span-2"}>
                  <span>{dt ? "接收人 userid（选填）" : "接收人 userid / 手机号（选填）"}</span>
                  <input className={input + " mt-1"} placeholder="留空=只发到群里；填了就定向发给这个人"
                    value={d.ext} onChange={(e) => setDraft(c.key, { ext: e.target.value })} />
                </label>
              </div>
            )}

            {wx && (
              <div className="mt-3">
                <label className="text-xs text-slate-600">
                  <span>自备网关地址（选填，仅在你已有 wechaty 等网关时填）</span>
                  <input className={input + " mt-1"} placeholder="http://127.0.0.1:8788/send"
                    value={d.gateway} onChange={(e) => setDraft(c.key, { gateway: e.target.value })} />
                </label>
              </div>
            )}

            <div className="mt-2 flex items-center gap-2 flex-wrap">
              <button className={btn} disabled={busy === "bind-" + c.key} onClick={() => bindChannel(c.key)}>
                {wx ? "关联网关" : "保存并关联"}
              </button>
              <span className="text-[11px] text-slate-400">
                {dt ? "钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook" : ""}
                {wc ? "企微群 → 群设置 → 群机器人 → 添加 → 复制 Webhook" : ""}
              </span>
            </div>

            {/* 应用凭证（定向单聊推送需要；群机器人不需要） */}
            {(dt || wc) && (
              <details className="mt-3">
                <summary className="text-xs text-slate-600 cursor-pointer">
                  企业内部应用凭证（可选：想定向私聊推送才需要）
                </summary>
                <div className="grid grid-cols-2 gap-2 mt-2">
                  {(dt ? [
                    ["dingtalk_app_key", "AppKey", appCfg.dingtalk_app_key],
                    ["dingtalk_app_secret", "AppSecret", appCfg.dingtalk_app_secret],
                    ["dingtalk_agent_id", "AgentId", appCfg.dingtalk_agent_id],
                  ] : [
                    ["wecom_corp_id", "企业 ID (CorpId)", appCfg.wecom_corp_id],
                    ["wecom_corp_secret", "应用 Secret", appCfg.wecom_corp_secret],
                    ["wecom_agent_id", "AgentId", appCfg.wecom_agent_id],
                  ]).map(([k, label, ready]: any) => (
                    <label key={k} className="text-xs text-slate-600">
                      <span className="flex items-center gap-1">
                        {label}
                        {ready && <span className="text-[10px] text-green-700">已配置</span>}
                      </span>
                      <input className={input + " mt-1"} value={cfgDraft[k] ?? ""}
                        placeholder={ready ? "••••••（留空表示不修改）" : "未配置"}
                        onChange={(e) => setCfgDraft({ ...cfgDraft, [k]: e.target.value })} />
                    </label>
                  ))}
                </div>
                <button className={btn + " mt-2"} disabled={busy === "cfg"} onClick={saveAppCfg}>保存凭证</button>
              </details>
            )}

            {myLinks.map((l) => renderLink(l, c))}
          </div>
        );
      })}

      {/* ② 自定义 Webhook */}
      <div className={box}>
        <div className="text-sm font-medium text-slate-800">自定义 Webhook</div>
        <p className="text-xs text-slate-500 mt-1">
          任意能接收 POST JSON 的地址（自建服务、飞书、Slack 等），只做外发提醒。
          注意：这里的格式是通用的 <code className="mono">{"{title,text}"}</code>，
          不要拿它填钉钉/企微的群机器人地址（格式不符会被拒），那两个请用上面的卡片。
        </p>
        <div className="flex gap-2 mt-2">
          <input className={input} placeholder="https://example.com/hook"
            value={draftOf("webhook").webhook}
            onChange={(e) => setDraft("webhook", { webhook: e.target.value })} />
          <button className={btn} disabled={busy === "bind-webhook"} onClick={() => bindChannel("webhook")}>绑定</button>
        </div>
        {linksOf("webhook").map((l) => renderLink(l, channels.find((c) => c.key === "webhook") || {
          key: "webhook", label: "自定义 Webhook", can_push: true, can_inbound: false, note: "",
        }))}
      </div>

      {/* ③ 推送记录 */}
      <div className={box}>
        <div className="text-sm font-medium text-slate-800">最近的推送记录</div>
        <p className="text-xs text-slate-500 mt-1">「说好会提醒我，什么都没收到」时看这里——失败原因都记下来了。</p>
        <div className="mt-2 max-h-56 overflow-auto">
          {messages.length === 0 && <div className="text-xs text-slate-400">还没有推送记录</div>}
          {messages.map((m) => (
            <div key={m.id} className="text-xs py-1.5 border-b border-slate-100 last:border-0">
              <span className={`px-1.5 py-0.5 rounded mr-2 ${
                m.status === "sent" ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>
                {m.status === "sent" ? "已送达" : "失败"}
              </span>
              <span className="text-slate-500 mr-2">{m.channel} · {KIND_LABEL[m.kind] || m.kind}</span>
              <span className="text-slate-700">{m.title}</span>
              {m.error && <div className="text-red-600 mt-0.5">{m.error}</div>}
              {/* 正文可展开：失败时用户最想知道「本来要发什么」，成功时也能确认内容对不对 */}
              {m.body && (
                <details className="mt-1">
                  <summary className="text-slate-400 cursor-pointer select-none">
                    查看推送内容
                    <span className="text-slate-300 ml-1">({(m.body || "").length} 字)</span>
                  </summary>
                  <pre className="whitespace-pre-wrap break-words text-slate-600 bg-slate-50 rounded p-2 mt-1 max-h-96 overflow-auto">
                    {m.body}
                  </pre>
                </details>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* ④ 进阶：公网回调（默认不需要） */}
      <details className={box}>
        <summary className="text-sm font-medium text-slate-800 cursor-pointer">
          进阶（可选）：公网回调地址 —— 只有想在钉钉里直接回复对话时才需要
        </summary>
        <p className="text-xs text-slate-500 mt-2">
          任务提醒是出站推送，<b>不需要</b>这一项。只有当你希望「在钉钉/企微里回一句话就让平台继续跑任务」时，
          才需要一个通道服务器能访问到的公网地址（内网穿透 / 域名），并填好应用凭证里的 Secret 做验签。
        </p>
        <div className="flex gap-2 mt-2">
          <input className={input} placeholder="https://your-domain.com" value={publicUrl}
            onChange={(e) => setPublicUrl(e.target.value)} />
          <button className={btn} disabled={busy === "public"} onClick={savePublicUrl}>保存</button>
        </div>
      </details>
    </div>
  );
}
