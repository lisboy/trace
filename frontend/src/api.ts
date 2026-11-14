import { useAuth } from "./store";

const BASE = "/api";

class ApiError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

async function request<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const teamId = useAuth.getState().currentTeamId;
  const headers = new Headers(opts.headers);
  if (teamId) headers.set("X-Team-Id", teamId);
  if (opts.body && !(opts.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(BASE + path, { ...opts, headers });
  if (!res.ok) {
    let msg = `${res.status}`;
    try {
      const j = await res.json();
      msg = j.detail || msg;
    } catch {
      /* ignore */
    }
    throw new ApiError(msg, res.status);
  }
  if (res.status === 204) return null as T;
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  // ---- 本机资料（没有登录：这里只是昵称/头像） ----
  me: () => request("/auth/me"),
  updateMe: (body: { display_name?: string; avatar?: string | null }) =>
    request<{ ok: boolean; user: any }>("/auth/me", { method: "PATCH", body: JSON.stringify(body) }),
  uploadAvatar: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ ok: boolean; avatar: string }>("/auth/me/avatar", { method: "POST", body: fd });
  },

  // ---- teams / projects ----
  teams: () => request("/teams"),
  projects: (teamId: string, archived = false) =>
    request(`/teams/${teamId}/projects?archived=${archived}`),
  archiveProject: (id: string) =>
    request(`/projects/${id}/archive`, { method: "POST" }),
  restoreProject: (id: string) =>
    request(`/projects/${id}/restore`, { method: "POST" }),
  project: (id: string) => request(`/projects/${id}`),
  /** 在本机文件管理器里打开项目工作区目录（仅 lite 模式） */
  revealProject: (id: string) =>
    request(`/projects/${id}/reveal`, { method: "POST" }),
  createProject: (teamId: string, body: any) =>
    request(`/teams/${teamId}/projects`, { method: "POST", body: JSON.stringify(body) }),
  /** 目录浏览：新建工作区时选文件夹（lite 模式可浏览本机任意目录） */
  fsDirs: (path = "") =>
    request(`/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ""}`),

  // ---- sessions / runs ----
  sessions: (projectId: string, archived = false) =>
    request(`/projects/${projectId}/sessions?archived=${archived}`),
  updateSession: (id: string, patch: { status?: "active" | "archived"; permission_scope?: "workspace" | "full" }) =>
    request(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteSession: (id: string) => request(`/sessions/${id}`, { method: "DELETE" }),
  /** 会话消息。带 limit 时返回 {messages, has_more, oldest}（最近 limit 条 / 更早一页）；
   *  不带 limit 时返回全部消息数组（老行为）。before/before_id=(上页最老一条的 created_at, id)
   *  复合游标 → 取它之前的一页（同一秒并列的多条不会被漏掉）。 */
  messages: (sessionId: string, opts?: { limit?: number; before?: string; before_id?: string }) => {
    const q = new URLSearchParams();
    if (opts?.limit) q.set("limit", String(opts.limit));
    if (opts?.before) q.set("before", opts.before);
    if (opts?.before_id) q.set("before_id", opts.before_id);
    const qs = q.toString();
    return request(`/sessions/${sessionId}/messages${qs ? `?${qs}` : ""}`);
  },
  /** 该会话自己的 run 列表（新→旧）：不受「项目最近 50 条」窗口限制，
   *  老会话也能认出上一次执行是成功/失败，避免聊天页看起来一片空白 */
  sessionRuns: (sessionId: string) => request(`/sessions/${sessionId}/runs`),
  /** 会话级用量：总 token / 缓存命中率 / 生成速度 */
  sessionUsage: (sessionId: string) => request(`/sessions/${sessionId}/usage`),
  /** 上下文统计：请求构成占比、逐轮占用趋势、工具调用分布（runId 为空=整个会话） */
  contextStats: (sessionId: string, runId?: string | null) =>
    request(`/sessions/${sessionId}/context-stats${runId ? `?run_id=${runId}` : ""}`),
  /** 本次运行的逐轮「上下文请求」清单（不含正文，点开某轮再取全文） */
  runTranscripts: (runId: string) => request(`/runs/${runId}/transcripts`),
  /** 单轮往返现场：发给模型什么 + 模型返回了什么 */
  transcriptDetail: (id: string) => request(`/llm-transcripts/${id}`),
  chat: (projectId: string, body: any, idempotencyKey: string) =>
    request(`/projects/${projectId}/chat`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  // 聊天附件：先传到项目工作区 .trace/uploads/，chat 时按返回的 path 引用
  uploadProjectFile: (projectId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request(`/projects/${projectId}/uploads`, { method: "POST", body: fd });
  },
  runs: (projectId: string) => request(`/projects/${projectId}/runs`),
  runDetail: (id: string) => request(`/runs/${id}`),
  cancelRun: (id: string) => request(`/runs/${id}/cancel`, { method: "POST" }),
  // 单独停掉一个子agent（主agent 与其它子agent 继续跑，互不影响）
  cancelTask: (runId: string, taskId: string) =>
    request(`/runs/${runId}/tasks/${taskId}/cancel`, { method: "POST" }),
  // 插话：并进正在执行的那一轮（不取消任务、不新开 run）。图和附件与聊天发送同一套格式。
  // clientMsgId 是幂等键：连点/重试同一条插话时后端只落一条消息。
  // 传 model 时顺带「运行中切换模型」：后端把这一轮后续的 LLM 换成新档位（下一轮生效）。
  //   model.profile 空串 = 回到「跟随角色默认」；不传 model = 本次不动模型。
  // 传 settings 时顺带「运行中改运行选项」：会话权限，下一轮生效。
  steerRun: (
    id: string,
    content: string,
    images: string[] = [],
    files: { path: string; name: string; size: number }[] = [],
    clientMsgId = "",
    model?: { profile: string; effort: string },
    settings?: { permission_scope?: string },
  ) => request(`/runs/${id}/steer`, {
    method: "POST",
    body: JSON.stringify({
      content, images, files, client_msg_id: clientMsgId,
      ...(model ? { model_profile_key: model.profile, reasoning_effort: model.effort } : {}),
      ...(settings || {}),
    }),
  }),

  // ---- events / artifacts ----
  events: (runId: string, since: number) =>
    request(`/runs/${runId}/events?since=${since}`),
  /** 计划机制：读取本轮待办清单（组长 update_plan 写入） */
  plan: (runId: string) => request(`/runs/${runId}/plan`),
  /** 提问机制：会话内的问题（待回答 + 已回答记录） */
  questions: (sessionId: string) => request(`/sessions/${sessionId}/questions`),
  /** 提问机制：作答（option_index 0-based，-1=跳过；也可只给 text） */
  answerQuestion: (id: string, body: { option_index?: number | null; text?: string | null }) =>
    request(`/questions/${id}/answer`, { method: "POST", body: JSON.stringify(body) }),
  runDiff: (runId: string) => request(`/runs/${runId}/diff`),
  /** 本次 run 改过哪些文件 + 前后差异（工具层写前快照，不依赖 git） */
  runChanges: (runId: string) => request(`/runs/${runId}/changes`),
  /** 看本次改动里的单个文件：mode=text 当前内容（预览走它），mode=orig 改动前快照 */
  runFileText: (runId: string, path: string, mode: "text" | "orig" = "text") =>
    request(`/runs/${runId}/file?mode=${mode}&path=${encodeURIComponent(path)}`),
  /** 同一文件的原样地址（图片 / HTML / PDF 预览用；HTML 里的相对路径会一起加载） */
  runFileRawUrl: (runId: string, path: string) =>
    `${BASE}/runs/${runId}/raw/${path.split("/").map(encodeURIComponent).join("/")}`,
  /** 在系统文件管理器里定位工作区中的该文件（Windows explorer /select） */
  runReveal: (runId: string, path: string) =>
    request(`/runs/${runId}/reveal`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  /** run 过程图稳定地址（工具落图另存到 DATA_DIR/run_shots，收尾清工作区也不丢）。
   *  前端统一走 fetchBlobUrl 取字节再转 blob: URL 喂给 <img>（<img> 带不了自定义头）。 */
  runShotUrl: (ref: string, thumb = 0) => {
    const path = ref.startsWith("run-shot/") ? ref : `run-shot/${ref}`;
    return `${BASE}/run-shots?ref=${encodeURIComponent(path)}${thumb ? `&thumb=${thumb}` : ""}`;
  },
  /** 取二进制并转 blob: URL（取图用；失败抛错由调用方降级）。 */
  fetchBlobUrl: async (url: string): Promise<string> => {
    const teamId = useAuth.getState().currentTeamId;
    const headers = new Headers();
    if (teamId) headers.set("X-Team-Id", teamId);
    const res = await fetch(url, { headers });
    if (!res.ok) throw new ApiError(`${res.status}`, res.status);
    return URL.createObjectURL(await res.blob());
  },
  /** 从中断处继续上次任务（不是把上一条消息重发一次）。
   *  model 传当前选择器里的档位：用户刚切过模型再点「继续」时以它为准（否则会回角色默认）。 */
  continueRun: (runId: string, note?: string, model?: { profile: string; effort: string }) =>
    request(`/runs/${runId}/continue`, {
      method: "POST",
      body: JSON.stringify({
        note: note || null,
        ...(model ? { model_profile_key: model.profile, reasoning_effort: model.effort } : {}),
      }),
    }),
  /** 审阅不通过 → 回滚（paths 为空表示全部回滚） */
  revertRun: (runId: string, paths?: string[]) =>
    request(`/runs/${runId}/revert`, {
      method: "POST",
      body: JSON.stringify({ paths: paths && paths.length ? paths : null }),
    }),
  addComment: (runId: string, body: { body: string; file: string; line: number }) =>
    request(`/runs/${runId}/comments`, { method: "POST", body: JSON.stringify(body) }),
  resolveComment: (id: string) => request(`/comments/${id}/resolve`, { method: "POST" }),
  /** 素材列表。不带 limit → 返回全部（轨迹块按 run_id 拉产物用）；
   *  素材库带 limit 分页：before/before_id=(上页最后一条 created_at,id) 复合游标。
   *  asset_kind/session_id 在服务端过滤（分页时必须服务端筛，否则只在已加载页里筛会像「素材变少」）。 */
  artifacts: (params: {
    run_id?: string; project_id?: string; asset_kind?: string; session_id?: string;
    placeholder?: boolean; limit?: number; before?: string; before_id?: string;
  }) => {
    const q = new URLSearchParams();
    if (params.run_id) q.set("run_id", params.run_id);
    if (params.project_id) q.set("project_id", params.project_id);
    if (params.asset_kind && params.asset_kind !== "all") q.set("asset_kind", params.asset_kind);
    if (params.session_id && params.session_id !== "all") q.set("session_id", params.session_id);
    if (params.placeholder) q.set("placeholder", "true");
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    if (params.before_id) q.set("before_id", params.before_id);
    return request(`/artifacts?${q.toString()}`);
  },
  artifactSessions: (projectId?: string) => {
    const q = new URLSearchParams();
    if (projectId) q.set("project_id", projectId);
    const qs = q.toString();
    return request(`/artifacts/session-sources${qs ? `?${qs}` : ""}`);
  },
  regenerate: (artifactId: string) =>
    request(`/artifacts/${artifactId}/regenerate`, { method: "POST" }),
  /** 删除单个素材（连同工作区文件与缩略图缓存） */
  deleteArtifact: (artifactId: string) =>
    request<{ ok: boolean; id: string }>(`/artifacts/${artifactId}`, { method: "DELETE" }),
  /** 按当前筛选清空素材库（删记录 + 文件 + 缩略图）；返回实际删除条数 */
  clearArtifacts: (body: {
    project_id: string; asset_kind?: string; session_id?: string; placeholder?: boolean;
  }) =>
    request<{ ok: boolean; deleted: number }>("/artifacts/clear", {
      method: "POST", body: JSON.stringify(body),
    }),
  artifactFile: (artifactId: string, v?: string | number) =>
    `${BASE}/artifacts/${artifactId}/file?rv=2${v ? `&v=${v}` : ""}`,
  /** 素材缩略图（长边 w）：列表/网格/消息附图都用它，点开大图才加载 artifactFile 原图。
   *  v 参数=内容版本（如重画后的 mtime），变了就破缓存。 */
  artifactThumb: (artifactId: string, w = 320, v?: string | number) =>
    `${BASE}/artifacts/${artifactId}/thumb?w=${w}&rv=2${v ? `&v=${v}` : ""}`,
  // ---- 项目脚本（工作区根目录 run.bat|start.bat / stop.bat）----
  projectScripts: (projectId: string) =>
    request<{ run_script: string | null; stop_script: string | null }>(`/projects/${projectId}/scripts`),
  runProjectScript: (projectId: string, action: "run" | "stop") =>
    request<{ started: boolean; script: string; pid: number }>(`/projects/${projectId}/scripts/${action}`, { method: "POST" }),
  // ---- AI 生图工作台 ----
  imageGenOptions: () => request("/image-gen/options"),  imageGenStart: () => request("/image-gen/comfyui/start", { method: "POST" }),
  imageGenStop: () => request("/image-gen/comfyui/stop", { method: "POST" }),
  // 用默认对话模型根据用户输入补全正面/负面提示词
  imageGenFillPrompts: (body: { user_input: string; current_positive?: string; current_negative?: string }) =>
    request<{ positive: string; negative: string; note: string; model: string; elapsed_ms: number }>(
      "/image-gen/fill-prompts", { method: "POST", body: JSON.stringify(body) }),
  imageGenUpload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request("/image-gen/reference", { method: "POST", body: fd });
  },
  // ---- 生图预设 / LoRA 管理 ----
  imageGenPresets: () => request("/image-gen/presets"),
  imageGenSavePreset: (body: { key?: string; label: string; params: Record<string, any>; image?: string }) =>
    request("/image-gen/presets", { method: "POST", body: JSON.stringify(body) }),
  imageGenPatchPreset: (id: string, body: { label?: string; params?: Record<string, any> }) =>
    request(`/image-gen/presets/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  imageGenDeletePreset: (id: string) =>
    request(`/image-gen/presets/${id}`, { method: "DELETE" }),
  imageGenPresetImage: (name: string) =>
    blobUrl(`/image-gen/presets/image/${encodeURIComponent(name)}`),
  // 项目绑定「强制预设」：agent 工具生图时强制套用
  imageGenProjectPreset: (projectId: string) =>
    request(`/image-gen/project-preset?project_id=${encodeURIComponent(projectId)}`),
  imageGenSetProjectPreset: (body: { project_id: string; preset_id: string }) =>
    request("/image-gen/project-preset", { method: "PUT", body: JSON.stringify(body) }),
  imageGenUploadLora: (file: File, subdir = "user") => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("subdir", subdir);
    return request("/image-gen/loras", { method: "POST", body: fd });
  },
  imageGen: (body: Record<string, any>) =>
    request("/image-gen", { method: "POST", body: JSON.stringify(body) }),
  imageGenJob: (jobId: string) =>
    request(`/image-gen/jobs/${encodeURIComponent(jobId)}`),
  imageGenBlob: async (name: string): Promise<string> => {
    const teamId = useAuth.getState().currentTeamId;
    const headers: Record<string, string> = {};
    if (teamId) headers["X-Team-Id"] = teamId;
    const res = await fetch(`${BASE}/image-gen/files/${encodeURIComponent(name)}`, { headers });
    if (!res.ok) throw new ApiError(`加载结果失败 ${res.status}`);
    return URL.createObjectURL(await res.blob());
  },
  // ---- 视频工作台（本地 ComfyUI · Wan2.2 TI2V 5B） ----
  videoGenOptions: () => request("/video-gen/options"),
  videoGenUpload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request("/video-gen/reference", { method: "POST", body: fd });
  },
  videoGen: (body: Record<string, any>) =>
    request("/video-gen", { method: "POST", body: JSON.stringify(body) }),
  videoGenJob: (jobId: string) =>
    request(`/video-gen/jobs/${encodeURIComponent(jobId)}`),
  videoGenBlob: async (name: string): Promise<string> => {
    const teamId = useAuth.getState().currentTeamId;
    const headers: Record<string, string> = {};
    if (teamId) headers["X-Team-Id"] = teamId;
    const res = await fetch(`${BASE}/video-gen/files/${encodeURIComponent(name)}`, { headers });
    if (!res.ok) throw new ApiError(`加载结果失败 ${res.status}`);
    return URL.createObjectURL(await res.blob());
  },
  // ---- 语音工作台（IndexTTS2 本地服务） ----
  voiceGenStatus: () => request("/voice-gen/status"),
  voiceGenStart: () => request("/voice-gen/start", { method: "POST" }),
  voiceGenStop: () => request("/voice-gen/stop", { method: "POST" }),
  voiceGenUpload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request("/voice-gen/upload", { method: "POST", body: fd });
  },
  voiceGen: (body: Record<string, any>) =>
    request("/voice-gen", { method: "POST", body: JSON.stringify(body) }),
  voiceGenJob: (jobId: string) =>
    request(`/voice-gen/jobs/${encodeURIComponent(jobId)}`),
  voiceGenBlob: (name: string) => blobUrl(`/voice-gen/files/${encodeURIComponent(name)}`),
  voiceGenDesign: (body: { desc: string; say?: string; n?: number; enhance?: boolean }) =>
    request("/voice-gen/design", { method: "POST", body: JSON.stringify(body) }),
  voiceGenDesignBlob: (name: string) =>
    blobUrl(`/voice-gen/design/files/${encodeURIComponent(name)}`),
  voiceGenSavePreset: (body: { name: string; file: string; overwrite?: boolean }) =>
    request("/voice-gen/presets", { method: "POST", body: JSON.stringify(body) }),
  /** 上传一段现成音频，直接存成 IndexTTS2 音色预设（供 agent generate_audio 按名字复用） */
  voiceGenUploadPreset: (file: File, body: {
    name: string; emo_weight?: number; emo_text?: string;
    emo_vector?: number[]; emo_audio?: File | null; overwrite?: boolean;
  }) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", body.name);
    if (body.emo_weight != null) fd.append("emo_weight", String(body.emo_weight));
    if (body.emo_text) fd.append("emo_text", body.emo_text);
    if (body.emo_vector && body.emo_vector.some((v) => v > 0)) fd.append("emo_vector", body.emo_vector.join(","));
    if (body.emo_audio) fd.append("emo_audio", body.emo_audio);
    if (body.overwrite) fd.append("overwrite", "true");
    return request("/voice-gen/presets/upload", { method: "POST", body: fd });
  },
  /** 取某个预设的参考音频（试听网上传的音频，确认存对了） */
  voiceGenPresetAudio: (name: string) =>
    blobUrl(`/voice-gen/presets/${encodeURIComponent(name)}/audio`),

  importAssets: (projectId: string, files: File[], kind: string) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    fd.append("kind", kind);
    return request(`/projects/${projectId}/assets/import`, { method: "POST", body: fd });
  },
  fetchAssets: (projectId: string, body: { source_id: string; query: string; limit?: number }) =>
    request(`/projects/${projectId}/assets/fetch`, { method: "POST", body: JSON.stringify(body) }),
  projectFileText: (projectId: string, path: string) =>
    request(`/projects/${projectId}/file?mode=text&path=${encodeURIComponent(path)}`),
  projectFileRaw: (projectId: string, path: string) =>
    `${BASE}/projects/${projectId}/file?mode=raw&path=${encodeURIComponent(path)}`,

  // ---- 消息通道（钉钉 / 企业微信 / 自定义 Webhook）----
  // 绑定已改为「直接填群机器人 Webhook」，不再有扫码 / code 接口。
  channels: () => request("/channels"),
  channelBind: (channel: string, body: any) =>
    request(`/channels/${channel}/bind`, { method: "POST", body: JSON.stringify(body) }),
  unlinkChannel: (id: string) =>
    request(`/channels/${id}/unlink`, { method: "POST" }),
  patchChannel: (id: string, body: any) =>
    request(`/channels/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  testChannel: (id: string) =>
    request(`/channels/${id}/test`, { method: "POST" }),
  channelMessages: (limit = 30) => request(`/channels/messages?limit=${limit}`),
  saveChannelAppConfig: (body: any) =>
    request("/channels/app-config", { method: "POST", body: JSON.stringify(body) }),

  // ---- 项目记忆 ----
  memories: (projectId: string) => request(`/projects/${projectId}/memory`),
  addMemory: (projectId: string, content: string) =>
    request(`/projects/${projectId}/memory`, { method: "POST", body: JSON.stringify({ content }) }),
  deleteMemory: (projectId: string, id: string) =>
    request(`/projects/${projectId}/memory/${id}`, { method: "DELETE" }),
  /** 立即让模型融合一次项目记忆（跨对话去重合并） */
  mergeMemories: (projectId: string) =>
    request(`/projects/${projectId}/memory/consolidate`, { method: "POST" }),
  // run 收尾判断「值得记」后沉淀的改动经验（工作区 .agent/notes/*.md）
  projectNotes: (projectId: string) => request(`/projects/${projectId}/notes`),
  deleteProjectNote: (projectId: string, category: string, key: string) =>
    request(`/projects/${projectId}/notes/${category}/${key}`, { method: "DELETE" }),
  /** 钉住（keep=true，永不淘汰）/ 标失效（stale=true，不再注入、优先淘汰）；null=不动该标记 */
  setProjectNoteFlag: (projectId: string, category: string, key: string,
                       body: { keep?: boolean | null; stale?: boolean | null }) =>
    request(`/projects/${projectId}/notes/${category}/${key}/flag`,
            { method: "POST", body: JSON.stringify(body) }),

  // ---- mcp servers ----
  mcpServers: (projectId: string) => request(`/projects/${projectId}/mcp`),
  createMcpServer: (projectId: string, body: any) =>
    request(`/projects/${projectId}/mcp`, { method: "POST", body: JSON.stringify(body) }),
  updateMcpServer: (projectId: string, id: string, body: any) =>
    request(`/projects/${projectId}/mcp/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteMcpServer: (projectId: string, id: string) =>
    request(`/projects/${projectId}/mcp/${id}`, { method: "DELETE" }),

  // ---- config ----
  agents: () => request("/agents"),
  updateAgent: (key: string, body: any) =>
    request(`/agents/${key}`, { method: "PUT", body: JSON.stringify(body) }),
  resetAgent: (key: string) => request(`/agents/${key}/reset`, { method: "POST" }),
  modelProfiles: () => request("/model-profiles"),
  /** 选「供应商 → 模型」时复用/创建对话档位，返回 { key, created } */
  autoModelProfile: (providerId: string, model: string) =>
    request("/model-profiles/auto", {
      method: "POST",
      body: JSON.stringify({ provider_id: providerId, model }),
    }),
  createModelProfile: (body: any) =>
    request("/model-profiles", { method: "POST", body: JSON.stringify(body) }),
  updateModelProfile: (id: string, body: any) =>
    request(`/model-profiles/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteModelProfile: (id: string) =>
    request(`/model-profiles/${id}`, { method: "DELETE" }),
  // ---- LLM 供应商（base_url / key / UA 只在这里配一次，档位选供应商 + 模型）----
  llmProviders: () => request("/llm-providers"),
  createLlmProvider: (body: any) =>
    request("/llm-providers", { method: "POST", body: JSON.stringify(body) }),
  updateLlmProvider: (id: string, body: any) =>
    request(`/llm-providers/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteLlmProvider: (id: string) =>
    request(`/llm-providers/${id}`, { method: "DELETE" }),
  /** 拉模型列表：给了 providerId 就用供应商存着的地址/密钥，并回填到供应商缓存 */
  llmModels: (arg: { providerId?: string; baseUrl?: string; apiKey?: string; protocol?: string }) =>
    request("/llm/models", {
      method: "POST",
      body: JSON.stringify({
        provider_id: arg.providerId || null,
        base_url: arg.baseUrl || "",
        api_key: arg.apiKey || "",
        protocol: arg.protocol || "",
      }),
    }),
  /** 一键测速：对模型列表逐个发短请求，返回每个模型的 tok/s */
  llmSpeedTest: (arg: { providerId?: string; baseUrl?: string; apiKey?: string; protocol?: string; models: string[] }) =>
    request("/llm/speed-test", {
      method: "POST",
      body: JSON.stringify({
        provider_id: arg.providerId || null,
        base_url: arg.baseUrl || "",
        api_key: arg.apiKey || "",
        protocol: arg.protocol || "",
        models: arg.models,
      }),
    }),
  driverSpecs: () => request("/providers/drivers"),
  providers: () => request("/providers"),
  createProvider: (body: any) =>
    request("/providers", { method: "POST", body: JSON.stringify(body) }),
  deleteProvider: (id: string) => request(`/providers/${id}`, { method: "DELETE" }),

  // ---- 用量 / 审计（个人版：只统计自己的工作区）----
  usage: (days = 30, agent?: string, model?: string) =>
    request(`/usage?days=${days}${agent ? `&agent=${encodeURIComponent(agent)}` : ""}${model ? `&model=${encodeURIComponent(model)}` : ""}`),
  audit: (limit = 50, q?: string) =>
    request(`/audit?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ""}`),

  // ---- 系统日志（顶栏「系统日志」弹窗）----
  /** 尾部轮询：只取 seq > since 的新行，前端自己拼接 */
  systemLogs: (params: { since?: number; limit?: number; level?: string; q?: string }) => {
    const q = new URLSearchParams();
    q.set("since", String(params.since ?? 0));
    if (params.limit) q.set("limit", String(params.limit));
    if (params.level) q.set("level", params.level);
    if (params.q) q.set("q", params.q);
    return request(`/system/logs?${q.toString()}`);
  },
  /** app.log 末尾若干行（进程刚重启、内存缓冲还空时翻旧账） */
  systemLogFileTail: (tail = 300) => request(`/system/logs/file?tail=${tail}`),
  systemInfo: () => request("/system/info"),
  /** LLM 就绪自检：这一轮到底会不会走真实大模型、用的哪个模型/地址、还差什么 */
  doctorLlm: () => request("/doctor/llm"),
  /** 本机 CPU / 内存 / GPU 瞬时状态（服务端 4s 缓存，前端 5s 一轮就够新） */
  systemMetrics: () => request("/system/metrics"),

  // ---- 技能系统（渐进式披露：清单只带名称+描述，正文按需加载） ----
  skills: (projectId?: string) =>
    request(`/skills${projectId ? `?project_id=${projectId}` : ""}`),
  skillContent: (name: string, projectId?: string) =>
    request(`/skills/${name}/content${projectId ? `?project_id=${projectId}` : ""}`),
  addSkillRoot: (path: string) =>
    request("/skills/roots", { method: "POST", body: JSON.stringify({ path }) }),
  removeSkillRoot: (path: string) =>
    request("/skills/roots", { method: "DELETE", body: JSON.stringify({ path }) }),
  saveGlobalSkill: (body: { name: string; description: string; content: string }) =>
    request("/skills/global", { method: "POST", body: JSON.stringify(body) }),
  deleteGlobalSkill: (name: string) =>
    request(`/skills/global/${name}`, { method: "DELETE" }),

  // ---- 外观偏好 / 壁纸（上传 / Wallpaper Engine） ----
  mePrefs: () => request("/me/prefs"),
  putPrefs: (prefs: Record<string, any>) =>
    request("/me/prefs", { method: "PUT", body: JSON.stringify({ prefs }) }),

  // ---- 助手行为：默认权限作用域 + 指导项 ----
  assistantPolicy: () => request("/assistant-policy"),
  saveAssistantPolicy: (body: { permission_scope?: "workspace" | "full"; guidance?: any[] }) =>
    request("/assistant-policy", { method: "PUT", body: JSON.stringify(body) }),
  uploadWallpaper: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ url: string; kind: "img" | "video"; size: number }>(
      "/me/wallpaper", { method: "POST", body: fd },
    );
  },
  weWallpapers: () => request("/wallpapers/engines"),
  setWeRoot: (path: string) =>
    request("/wallpapers/engines/root", { method: "PUT", body: JSON.stringify({ path }) }),
  /** WE 壁纸文件要鉴权头，浏览器标签带不上 → fetch→blob（见底部 blobUrl） */
  weFileBlob: (path: string) => blobUrl(`/wallpapers/file?path=${encodeURIComponent(path)}`),

  // ---- 数据存储位置（设置 / 存储空间）----
  storageInfo: () => request("/system/storage"),
  storageMigrate: (targetDir: string) =>
    request("/system/storage/migrate", {
      method: "POST",
      body: JSON.stringify({ target_dir: targetDir }),
    }),
  /** 迁移进度（轮询用）：phase = idle|scan|copy|db|done|error */
  storageProgress: () => request("/system/storage/migrate/progress"),
  /** 弹系统原生「选择文件夹」窗口（服务所在机器上）；501 = 弹不出来，前端回退内置浏览器 */
  storagePickDir: (title?: string) =>
    request<{ ok: boolean; path: string; cancelled: boolean }>("/system/storage/pick-dir", {
      method: "POST",
      body: JSON.stringify({ title: title || null }),
    }),

  // ---- 会话导出（zip：conversation.md + session.json）----
  /** 带鉴权头 fetch → blob → 触发浏览器另存；文件名优先取后端 Content-Disposition */
  exportSession: async (sessionId: string, title: string): Promise<void> => {    const teamId = useAuth.getState().currentTeamId;
    const headers: Record<string, string> = {};
    if (teamId) headers["X-Team-Id"] = teamId;
    const res = await fetch(`${BASE}/sessions/${sessionId}/export`, { headers });
    // 未知路由会被 SPA 兜底接住返回 200 HTML——下载下来就是个假 zip，必须拦
    const ct = res.headers.get("content-type") || "";
    if (!res.ok || !ct.includes("application/zip")) {
      throw new ApiError(!res.ok
        ? `导出失败 ${res.status}`
        : "导出接口不可用（后端重启后生效）");
    }
    const blob = await res.blob();
    let name = `会话-${(title || sessionId).slice(0, 30)}.zip`;
    const m = (res.headers.get("content-disposition") || "")
      .match(/filename\*=UTF-8''([^;]+)/);
    if (m) name = decodeURIComponent(m[1]);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
};

export { ApiError };

/**
 * 取二进制文件并转成 objectURL（图片/音频都走这里）。
 * 为什么不用 <img src> / <audio src> 直接指后端：这些接口要鉴权头，
 * 浏览器原生标签带不上 Authorization，只能先 fetch 再给 blob URL。
 */
async function blobUrl(path: string): Promise<string> {
  const teamId = useAuth.getState().currentTeamId;
  const headers: Record<string, string> = {};
  if (teamId) headers["X-Team-Id"] = teamId;
  const res = await fetch(BASE + path, { headers });
  if (!res.ok) throw new ApiError(`加载文件失败 ${res.status}`);
  return URL.createObjectURL(await res.blob());
}
