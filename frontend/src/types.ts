// 与后端 /api 响应对齐的共享类型（设计 §10）。

export interface User {
  id: string;
  email: string;
  display_name: string;
  avatar?: string | null;
  is_active: boolean;
  role: string | null;
}

export interface Team {
  id: string;
  name: string;
  slug: string;
  role: string; // 个人版固定 owner；后端保留字段以便将来扩展
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  repo_path: string;
  project_type: string;
  python_bin: string | null;
  commands_json: Record<string, string>;
  archived_at: string | null;
}

export interface ProjectSummary {
  id: string;
  name: string;
  slug: string;
  repo_path: string;
  run_count: number;
  archived_at: string | null;
}

export interface RunSummary {
  id: string;
  status: string;
  run_type: string;
  priority: number;
  has_placeholder: boolean;
  created_at: string;
  finished_at: string | null;
  final_summary: string;
}

/** 会话权限：仅工作区 / 全部权限（设置里有默认值，每个会话可单独改） */
export type PermissionScope = "workspace" | "full";

export interface Session {
  id: string;
  title: string;
  status?: string; // active|archived|deleted
  running?: boolean; // 该会话是否有正在执行/排队的 run（列表转圈动画）
  /** 会话挂载的技能：null=未指定（跟随项目 .agent/skills 默认），[]=明确不挂，数组=指定清单 */
  skills?: string[] | null;
  /** 会话权限作用域：workspace=仅工作区（默认）/ full=全部权限（可访问工作区外） */
  permission_scope?: PermissionScope;
  created_at: string;
  /** 最近活跃（任一消息落库 / 发起 run 时更新）；侧栏按它排序与显示相对时间 */
  last_active?: string;
}

/** 会话级用量（/sessions/{id}/usage） */
export interface ContextOccupancy {
  agent_key: string;
  agent_label: string;
  model: string | null;
  profile_key: string | null;
  profile_label: string | null;
  rounds: number;
  /** 该角色最近若干轮（≤500）的输入/输出/缓存合计：缓存弹层按角色拆开看 */
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_tokens?: number;
  cache_reported_rounds?: number;
  context_window: number;
  window_configured: boolean; // false=档位没填，按默认 256k 估算
  budget_tokens: number; // 超过这条线 compress_messages 就开始裁剪
  last_prompt_tokens: number;
  peak_prompt_tokens: number;
  used_pct: number;
  peak_pct: number;
  remaining_tokens: number;
  will_compress: boolean;
  near_threshold: boolean;
  skewed_usage_calls: number; // 端点把累计量塞进单次 usage 的轮数（比例参考价值下降）
}

export interface SessionUsage {
  session_id: string;
  runs: number;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  /** 未命中缓存的输入 = prompt - cached；弹层里「总量 = 未缓存输入 + 缓存读取 + 输出」可自校验 */
  uncached_tokens?: number;
  total_tokens: number;
  latency_ms: number;
  tokens_per_sec: number | null; // 瞬时：最近一轮往返的输出速度
  /** 会话累计平均速度（全历史、含重试）：只作对照，换模型后会滞后 */
  tokens_per_sec_avg?: number | null;
  /** 首 token 耗时（TTFT）：最近一轮实测毫秒；0/null = 该轮没测到（非流式端点） */
  ttft_ms?: number | null;
  /** 会话内已测到的 TTFT 中位数（毫秒）：偶发排队不会把它拉爆，代表「常态等多久」 */
  ttft_median_ms?: number | null;
  /** 测到过 TTFT 的往返条数（0 = 这个端点的响应拿不到首字时间） */
  ttft_calls?: number;
  /** 最近一轮往返的明细（tooltip 用）：哪个角色/模型、输出多少、耗时多久 */
  last_round?: {
    agent_key: string;
    agent_label: string;
    model: string | null;
    completion_tokens: number;
    latency_ms: number;
    ttft_ms?: number;
    /** 最近一轮的输入/缓存明细（缓存弹层里「这一轮」那几行） */
    prompt_tokens?: number;
    cached_tokens?: number;
    cache_reported?: boolean;
    created_at: string;
  } | null;
  cache_hit_rate: number | null; // 0-100，null=无缓存信息
  /** 端点是否上报过缓存字段：false 时候弹层写「未上报」，不把「没数据」说成 0% */
  cache_reported?: boolean;
  context: ContextOccupancy | null; // 占用比例最高的角色
  context_by_agent: ContextOccupancy[];
  compress_at_pct: number; // 到这条百分比就开始压缩历史
  /** 会话累计动作（分桶口径与执行轨迹那行摘要一致） */
  activity?: {
    commands: number;
    reads: number;
    writes: number;
    other: number;
    total: number;
    run_seconds: number; // 各任务墙钟时间合计；并行任务会各算一份
  };
}

/** 本机资源瞬时状态（GET /system/metrics） */
export interface SystemMetrics {
  cpu_pct?: number;
  cores?: number;
  mem_total_mb?: number;
  mem_used_mb?: number;
  mem_pct?: number;
  swap_total_mb?: number;
  swap_used_mb?: number;
  swap_pct?: number;
  proc_mem_mb?: number;
  proc_cpu_pct?: number;
  gpus?: { name: string; util_pct: number; mem_used_mb: number; mem_total_mb: number; temp_c: number }[];
  sampled_at?: number;
  now?: string;
}

/** 一次 LLM 往返的分类占比（后端 stats_json） */
export interface PromptStats {
  categories?: Record<string, { tokens: number; chars: number }>;
  est_prompt_tokens?: number;
  messages?: number;
  tools?: number;
  images?: number;
  tool_calls?: Record<string, number>;
  completion_tokens?: number;
  cached_tokens?: number;
  cache_reported?: number;
  latency_ms?: number;
}

/** 运行面板「上下文请求」列表的一行（/runs/{id}/transcripts） */
export interface TranscriptRound {
  id: string;
  call_id: string | null;
  agent_key: string | null;
  model: string | null;
  round_no: number;
  purpose: string; // agent | summarize
  compressed: boolean;
  dropped_msgs: number;
  req_bytes: number;
  resp_bytes: number;
  truncated: boolean;
  created_at: string;
  stats: PromptStats;
  prompt_tokens: number; // 端点上报的真实值，缺失时回落到估算值
  completion_tokens: number;
  cached_tokens: number;
  latency_ms: number;
  est_vs_real: number | null; // 真实/估算 偏离倍数，明显偏离说明启发式低估了
}

export interface OaiMessage {
  role: string; // system | user | assistant | tool
  content: any; // string 或 content parts 数组
  tool_calls?: { id?: string; function?: { name?: string; arguments?: string } }[];
  tool_call_id?: string;
  name?: string;
}

/** 单轮往返全文（/llm-transcripts/{id}） */
export interface TranscriptDetail {
  id: string;
  call_id: string | null;
  agent_key: string | null;
  model: string | null;
  round_no: number;
  purpose: string;
  compressed: boolean;
  dropped_msgs: number;
  truncated: boolean;
  created_at: string;
  stats: PromptStats;
  req_bytes: number;
  resp_bytes: number;
  request: {
    messages: OaiMessage[];
    // 每条消息的估算 token（与 messages 同下标、同口径 _msg_tokens）；工具定义原文未存时为空
    msg_tokens?: number[];
    tools?: any[];
    tools_omitted?: number;
    truncated_note?: string;
    parse_error?: boolean;
    raw?: string;
  };
  response: {
    text: string;
    reasoning: string;
    tool_calls: { id?: string; name?: string; arguments?: string }[];
    usage?: any;
    parse_error?: boolean;
    raw?: string;
  };
}

/** 上下文统计（/sessions/{id}/context-stats） */
export interface ContextRound {
  transcript_id: string;
  agent_key: string;
  round_no: number;
  created_at: string;
  est_tokens: number;
  prompt_tokens: number;
  est_only: boolean; // 端点没上报真实值，这条只有估算
  compressed: boolean;
  dropped_msgs: number;
  req_kb: number;
}

export interface ContextAgentLatest {
  transcript_id: string;
  agent_key: string;
  agent_label: string;
  model: string | null;
  round_no: number;
  created_at: string;
  categories: Record<string, { tokens: number; chars: number }>;
  est_prompt_tokens: number;
  prompt_tokens: number;
  messages: number;
  tools: number;
  images: number;
  tool_calls: Record<string, number>;
  compressed: boolean;
  dropped_msgs: number;
  context_window: number;
  window_configured: boolean;
  budget_tokens: number;
  used_pct: number;
  will_compress: boolean;
}

export interface ContextStats {
  session_id: string;
  rounds: number;
  history_capped: boolean;
  categories_cumulative: Record<string, { tokens: number; rounds: number }>;
  categories_current: Record<string, { tokens: number; chars: number }>;
  tool_calls: Record<string, number>;
  trend: ContextRound[];
  latest: ContextAgentLatest | null;
  by_agent: ContextAgentLatest[];
  compress_rounds: number;
  summarize_rounds: number; // 压缩历史时额外发起的摘要调用次数（不计入 llm_calls 账单）
  summarize_est_tokens: number;
  compress_at_pct: number;
}

export interface ProjectMemory {  id: string;
  content: string;
  kind: string; // auto|manual
  title?: string;
  tags?: string;
  session_id?: string | null;
  updated_at?: string;
  source_run_id: string | null;
  created_at: string;
}

/** 项目改动经验（工作区 .agent/notes/*.md 里的一条）：run 收尾判断「值得记」才写 */
export interface ProjectNote {
  category: string;      // decision|pitfall|convention|playbook|codemap
  category_cn: string;
  category_desc: string;
  title: string;
  detail: string;
  when: string;          // YYYY-MM-DD
  files: string[];
  run_id: string;
  key: string;           // 删除/标记用
  keep: boolean;         // 钉住：永不淘汰（满了也不丢）
  stale: boolean;        // 已失效：不再注入上下文、优先淘汰（留痕不删除）
}

export interface Message {
  id: string;
  author_type: string; // user|agent|system
  agent_key: string | null;
  author_id: string | null;
  run_id?: string | null;
  content: string;
  meta: { images?: string[]; [k: string]: any };
  created_at: string;
}

export interface Task {
  id: string;
  kind: string;
  agent_key: string;
  title: string;
  status: string;
  attempt: number;
  result_json: any;
}

export interface WorkOrder {
  goal: string;
  checklist: any[];
  art_direction: string | null;
  asset_plan: any[];
}

export interface Artifact {
  id: string;
  asset_kind: string;
  slug: string;
  rel_path: string;
  is_placeholder: boolean;
  provider: string | null;
  size: number | null;
  /** 产出该素材的 run / 会话（素材库按「来源会话」筛选用；导入素材为空） */
  run_id?: string | null;
  session_id?: string | null;
  session_title?: string | null;
  /** 产出时间（ISO）：素材库分页游标用 */
  created_at?: string | null;
}

export interface RunDetail {
  id: string;
  status: string;
  run_type: string;
  project_id?: string;
  session_id?: string;
  has_placeholder: boolean;
  final_summary: string | null;
  change_report: ChangeReport | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  usage?: RunUsage;
  tasks: Task[];
  work_order: WorkOrder | null;
  artifacts: Artifact[];
  plan?: PlanItem[];
}

export interface PlanItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | string; // 组长维护的勾选状态
  note?: string;
}

/** 组长「拿不准就问」的问题（run_questions） */
export interface RunQuestion {
  id: string;
  run_id?: string | null;
  session_id: string;
  agent_key?: string;
  question: string;
  options: { label: string; detail?: string }[];
  allow_text: boolean;
  status: "pending" | "answered" | "skipped" | string;
  answer_index?: number | null;
  answer_text?: string | null;
  created_at: string;
  answered_at?: string | null;
}

export interface RunUsage {
  calls: number;
  prompt: number;
  completion: number;
  cached?: number;
  total?: number;
  cache_hit_rate?: number | null;
  latency_ms: number;
  tokens_per_sec: number | null;
}

export interface ChangeReport {
  run_type: string;
  files: { file: string; added: number; deleted: number; status?: string }[];
  summary: { added: number; deleted: number; count: number };
  test_report: any;
  has_placeholder: boolean;
  placeholder_count: number;
  placeholders: { slug: string; rel_path: string }[];
  model_map: Record<string, string>;
}

// WebSocket 事件（设计 §10.7）
export interface WSEvent {
  seq: number;
  type: string;
  run_id: string | null;
  task_id: string | null;
  agent: string | null;
  payload: any;
}

export const ROLE_LABEL: Record<string, string> = {
  main: "助手",
  sub: "子agent",
  // 旧数据里的角色 key，统一显示成「助手」
  planner: "助手",
  artist: "助手",
  voice: "助手",
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  claimed: "已领取",
  running: "运行中",
  blocked: "等待回答",  // 组长提问后本轮暂停，等用户在聊天里作答
  orphaned: "孤儿",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

export const STATUS_COLOR: Record<string, string> = {
  queued: "bg-amber-100 text-amber-700",
  claimed: "bg-sky-100 text-sky-700",
  running: "bg-blue-100 text-blue-700",
  blocked: "bg-orange-100 text-orange-700",
  orphaned: "bg-red-100 text-red-700",
  succeeded: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  cancelled: "bg-gray-200 text-gray-600",
};
