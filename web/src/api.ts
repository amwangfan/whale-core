/**
 * whale-core 管理 API 类型化客户端。
 *
 * - base 固定为同源 `/api/v1`（dev 由 vite proxy 转发，prod 由 API 服务托管）。
 * - token 仅存 localStorage，只通过 Authorization 头发送，绝不进 URL。
 * - 错误统一抛 ApiError（code/message 取自 `{error:{code,message}}`）。
 */

export const API_BASE = "/api/v1";

const TOKEN_KEY = "whale.console.token";

// ---------- 类型（与后端契约一一对应） ----------

export interface StatusResult {
  ok: boolean;
  version: string;
  profile: string;
  uptime_s: number;
  plugins: { loaded: number; enabled: number };
  memory: { episodes: number; preferences: number };
  drafts_pending: number;
}

export interface HealthResult {
  ok: boolean;
}

export interface PluginInfo {
  plugin_id: string;
  name: string;
  version: string;
  kind: string;
  status: string;
  enabled: boolean;
  /** 功能简介 / 输入事件 / 输出动作 / 提供能力（清单声明，缺失时为空数组）。 */
  description?: string;
  inputs?: string[];
  actions?: string[];
  provides?: string[];
  [key: string]: unknown;
}

export interface PluginDetail extends PluginInfo {
  config?: Record<string, unknown>;
}

export interface PluginHealth {
  plugin_id: string;
  status: "healthy" | "unhealthy" | "unknown" | "disabled" | "missing" | string;
  detail?: string;
}

export interface MemoryHit {
  key: string;
  kind: string;
  summary: string;
  score: number;
  reasons: string[];
  created_at: string;
}

export interface ChatResult {
  reply: string;
  status: string;
  receipts: unknown[];
  task_id?: string;
}

export interface Approval {
  draft_id: string;
  text: string;
  channel: string;
  status: string;
  decision?: "approve" | "reject" | "";
  execution_status?: string;
  kind?: "task" | "draft";
  task_id?: string;
  goal?: string;
}

/**
 * 任务轨迹记录（kv `task:{id}` 落库形态）：
 * - `goal` / `capabilities` 是「模型提出的计划」，不代表已执行；
 * - `receipts` 是「实际执行的回执」，逐条对应真实发生（或被拒/挂起）的动作。
 */
export interface TaskRecord {
  task_id: string;
  /** epoch 秒 */
  at: number;
  goal: string;
  status:
    | "answered"
    | "clarify"
    | "pending_approval"
    | "denied"
    | "failed"
    | string;
  capabilities: string[];
  receipts: string[];
  [key: string]: unknown;
}

export interface WhaleEvent {
  at: number | string;
  kind: string;
  detail: string;
}

// ---- Trace 账本（issue #39）：诊断视图 /traces + 用户视图 /activity ----

/** /traces 列表行（不含 spans）。 */
export interface TraceItem {
  trace_id: string;
  kind: string;
  conversation_id: string;
  principal_id: string;
  message_id: string;
  task_id: string;
  action_id: string;
  receipt_id: string;
  /** epoch 秒 */
  started_at: number;
  ended_at: number;
  /** open / replied / silent / failed / partial */
  status: string;
  summary: string;
}

export interface TraceSpan {
  span_id: string;
  phase: string;
  status: string;
  reason_code: string;
  detail: string;
  started_at: number;
  duration_ms: number;
  attempt: number;
}

/** /traces/{id} 完整记录（含 spans）。 */
export interface TraceDetail extends TraceItem {
  spans: TraceSpan[];
}

export interface TracesListResult {
  total: number;
  offset: number;
  limit: number;
  traces: TraceItem[];
}

/** /activity 用户可读条目：自然语言解释行。 */
export interface ActivityItem {
  trace_id: string;
  /** epoch 秒 */
  at: number;
  status: string;
  lines: string[];
}

export interface TracesQuery {
  kind?: string;
  conversation_id?: string;
  principal_id?: string;
  status?: string;
  /** epoch 秒 */
  since?: number;
  until?: number;
  limit?: number;
  offset?: number;
}

// ---- 任务时间线（issue #43）：GET /tasks/{id}/timeline ----

export interface TaskTimelineEvent {
  /** epoch 秒 */
  at: number;
  at_text: string;
  kind: string;
  text: string;
  detail: Record<string, unknown>;
}

export interface TaskTimeline {
  task_id: string;
  goal: string;
  phase: string;
  status: string;
  current: { label: string; explanation: string; next_step: string };
  timeline: TaskTimelineEvent[];
  receipts: string[];
  delivery: {
    state: string;
    attempts: number;
    last_error: string;
    channel: string;
  };
  retry: {
    allowed: boolean;
    kind: "resume" | "redeliver" | "inspect" | "none" | string;
    reason: string;
  };
  trace_ids: string[];
  capabilities: string[];
}

export interface BudgetLimits {
  model_calls: number;
  media_jobs: number;
}

export interface BudgetView {
  limits: BudgetLimits;
  used: BudgetLimits;
  day: string;
}

export interface RuntimeSettings {
  setup_completed?: boolean;
  profile: "cloud" | "local";
  model: { base_url: string; model: string; api_key_configured: boolean };
  intent_model_enabled: boolean;
  persona_taboos?: string[];
  models?: Record<string, { base_url?: string; model?: string; api_key_configured?: boolean }>;
  capabilities: string[];
  notes: string[];
  services?: Record<string, string | boolean>;
  budget?: BudgetView;
}
export interface DiagnosticReport {
  checks: { name: string; status: string; detail: string }[];
  summary: Record<string, unknown>;
}
export interface ProviderTest {
  ok: boolean;
  latency_ms: number | null;
  error_kind?: string;
}

export interface CapabilityResult {
  output: string;
  status: string;
  reply: string;
  receipts: unknown[];
  task_id?: string;
}
export interface VideoTask {
  task_id: string;
  status: string;
  prompt: string;
  url: string;
  error: string;
  created_at: number;
  updated_at: number;
}
export interface WorkflowSettings {
  paused: boolean;
  daily_limit: number;
  quiet_start: string;
  quiet_end: string;
  timezone: string;
  schedule_enabled?: boolean;
  interval_s?: number;
}
export interface SkillInfo {
  name: string;
  description: string;
  steps?: unknown[];
}
export interface LifeInterest {
  topic: string;
  weight: number;
}
export interface LifeDiaryEntry {
  day: string;
  note: string;
  sources: string[];
}
export interface LifeState {
  interests: LifeInterest[];
  diary: LifeDiaryEntry[];
}

// ---------- 错误 ----------

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

// ---------- token 管理 ----------

let cachedToken: string | null = null;

function storageAvailable(): boolean {
  try {
    return typeof localStorage !== "undefined";
  } catch {
    return false;
  }
}

export function getToken(): string {
  if (cachedToken === null) {
    try {
      cachedToken = storageAvailable()
        ? (localStorage.getItem(TOKEN_KEY) ?? "")
        : "";
    } catch {
      cachedToken = "";
    }
  }
  return cachedToken;
}

export function setToken(token: string): void {
  cachedToken = token.trim();
  if (!storageAvailable()) return;
  try {
    if (cachedToken) localStorage.setItem(TOKEN_KEY, cachedToken);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* Private browsers may deny storage; keep this tab's session in memory. */
  }
}

// 401 全局通知（App 订阅后提示「需要 token」）
type UnauthListener = () => void;
const unauthListeners = new Set<UnauthListener>();

export function onUnauthorized(cb: UnauthListener): () => void {
  unauthListeners.add(cb);
  return () => {
    unauthListeners.delete(cb);
  };
}

function notifyUnauthorized(): void {
  for (const cb of unauthListeners) {
    try {
      cb();
    } catch {
      /* 监听器异常不影响请求链路 */
    }
  }
}

// ---------- 请求核心 ----------

export function apiUrl(path: string): string {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

/** 发起请求并返回原始 Response；非 2xx 时按 `{error:{code,message}}` 解析并抛 ApiError。 */
async function rawRequest(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init?.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  let res: Response;
  try {
    res = await fetch(apiUrl(path), { ...init, headers, redirect: "error" });
  } catch (e) {
    throw new ApiError(
      "network",
      e instanceof Error ? e.message : "network error",
      0,
    );
  }
  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = res.statusText || `HTTP ${res.status}`;
    try {
      const body: unknown = await res.json();
      if (body && typeof body === "object" && "error" in body) {
        const err = (body as { error: { code?: unknown; message?: unknown } })
          .error;
        if (typeof err.code === "string") code = err.code;
        if (typeof err.message === "string") message = err.message;
      }
    } catch {
      /* 非 JSON 错误体：保留 http_<status> 兜底 */
    }
    if (res.status === 401) notifyUnauthorized();
    throw new ApiError(code, message, res.status);
  }
  return res;
}

/** JSON 端点：解包 `{result: ...}`。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await rawRequest(path, init);
  const body = (await res.json()) as { result: T };
  return body.result;
}

// ---------- 端点 ----------

export const api = {
  conversation: (id = "console") =>
    request<{
      conversation_id: string;
      messages: { role: "user" | "assistant"; content: string }[];
    }>(`/conversations/${encodeURIComponent(id)}`),
  deleteConversation: (id = "console") =>
    request<unknown>(`/conversations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  voiceOpen: (instructions: string) =>
    request<{ session_id: string; status: string }>("/voice/sessions", {
      method: "POST",
      body: JSON.stringify({ instructions }),
    }),
  voiceAppend: (id: string, audio_base64: string) =>
    request<unknown>(`/voice/sessions/${encodeURIComponent(id)}/append`, {
      method: "POST",
      body: JSON.stringify({ audio_base64 }),
    }),
  voiceCommit: (id: string) =>
    request<unknown>(`/voice/sessions/${encodeURIComponent(id)}/commit`, {
      method: "POST",
      body: "{}",
    }),
  voiceReceive: (id: string, signal?: AbortSignal) =>
    request<Record<string, unknown>>(
      `/voice/sessions/${encodeURIComponent(id)}/receive`,
      { method: "POST", body: JSON.stringify({ timeout: 5 }), signal },
    ),
  voiceCancel: (id: string) =>
    request<unknown>(`/voice/sessions/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: "{}",
    }),
  voiceClose: (id: string) =>
    request<unknown>(`/voice/sessions/${encodeURIComponent(id)}/close`, {
      method: "POST",
      body: "{}",
    }),
  /** whale-io PCM 低延迟通话：一次性 60 秒票据（PR#4 移植）。
   *  票据经 WebSocket subprotocol 出示，绝不拼进 URL。 */
  voiceSession: () =>
    request<{
      protocol: string;
      url: string;
      ticket: string;
      session_id: string;
      sample_rate: number;
    }>("/voice/session", { method: "POST", body: "{}" }),

  runCapability: (capability: string, params: Record<string, unknown>) =>
    request<CapabilityResult>(
      `/capabilities/${encodeURIComponent(capability)}`,
      { method: "POST", body: JSON.stringify({ params }) },
    ),
  videoTasks: async () =>
    (await request<{ tasks: VideoTask[] }>("/video/tasks")).tasks,
  videoAction: (id: string, action: "poll" | "cancel") =>
    request<VideoTask>(`/video/tasks/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
    }),
  artifact: async (path: string, signal?: AbortSignal) => {
    if (!/^\/api\/v1\/artifacts\/[a-f0-9]{32}$/.test(path))
      throw new ApiError("invalid_artifact", "产物地址无效", 0);
    return (await rawRequest(path.slice(API_BASE.length), { signal })).blob();
  },
  skills: async () =>
    (await request<{ skills: SkillInfo[] }>("/workflows/skills")).skills,
  runSkill: (name: string, input_text: string) =>
    request<unknown>(`/workflows/skills/${encodeURIComponent(name)}/run`, {
      method: "POST",
      body: JSON.stringify({ input_text }),
    }),
  explore: (topics: string[]) =>
    request<unknown>("/workflows/explore", {
      method: "POST",
      body: JSON.stringify({ topics }),
    }),
  life: () => request<LifeState>("/life"),
  addInterest: (topic: string) =>
    request<LifeInterest>("/life/interests", {
      method: "POST",
      body: JSON.stringify({ topic }),
    }),
  removeInterest: (topic: string) =>
    request<{ removed: boolean }>("/life/interests", {
      method: "POST",
      body: JSON.stringify({ topic, remove: true }),
    }),
  lifeFeedback: (topic: string, signal: "liked" | "disliked" | "ignored") =>
    request<{ saved: boolean }>("/life/feedback", {
      method: "POST",
      body: JSON.stringify({ topic, signal }),
    }),

  workflowSettings: () => request<WorkflowSettings>("/workflows/settings"),
  saveWorkflowSettings: (settings: WorkflowSettings) =>
    request<WorkflowSettings>("/workflows/settings", {
      method: "POST",
      body: JSON.stringify(settings),
    }),

  settings: () => request<RuntimeSettings>("/settings"),
  saveSettings: (settings: {
    setup_completed?: boolean;
    profile?: "cloud" | "local";
    model?: { base_url?: string; model?: string; api_key?: string | null };
  intent_model_enabled?: boolean;
  persona_taboos?: string[];
  models?: Record<string, { base_url?: string; model?: string; api_key?: string | null }>;
    services?: Record<string, string | null>;
    budget?: BudgetLimits;
  }) =>
    request<RuntimeSettings>("/settings", {
      method: "POST",
      body: JSON.stringify(settings),
    }),
  testProvider: () =>
    request<ProviderTest>("/providers/test", { method: "POST", body: "{}" }),
  diagnostics: () => request<DiagnosticReport>("/diagnostics"),
  testPlugin: (id: string) =>
    request<ProviderTest & { via?: string }>(
      `/plugins/${encodeURIComponent(id)}/test`,
      { method: "POST" },
    ),
  status: () => request<StatusResult>("/status"),

  health: () => request<HealthResult>("/health"),

  plugins: async () =>
    (await request<{ plugins: PluginInfo[] }>("/plugins")).plugins,

  plugin: (id: string) =>
    request<PluginDetail>(`/plugins/${encodeURIComponent(id)}`),

  setPluginEnabled: (id: string, enabled: boolean) =>
    request<{ plugin_id: string; status: string }>(
      `/plugins/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`,
      { method: "POST" },
    ),

  pluginHealth: (id: string) =>
    request<PluginHealth>(`/plugins/${encodeURIComponent(id)}/health`),

  /** 整体替换插件配置；422 时 ApiError.message 为校验错误（只含字段名/期望类型）。 */
  setPluginConfig: (id: string, config: Record<string, unknown>) =>
    request<{ plugin_id: string; saved: boolean }>(
      `/plugins/${encodeURIComponent(id)}/config`,
      { method: "POST", body: JSON.stringify({ config }) },
    ),

  tasks: async () => (await request<{ tasks: TaskRecord[] }>("/tasks")).tasks,

  task: (id: string) => request<TaskRecord>(`/tasks/${encodeURIComponent(id)}`),
  cancelTask: (id: string) =>
    request<TaskRecord>(`/tasks/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),
  decideTask: (id: string, decision: "approve" | "reject") =>
    request<TaskRecord>(`/tasks/${encodeURIComponent(id)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  resumeTask: (id: string) =>
    request<ChatResult>(`/tasks/${encodeURIComponent(id)}/resume`, {
      method: "POST",
    }),

  searchMemory: async (q: string) =>
    (
      await request<{ hits: MemoryHit[] }>(
        `/memory/search?q=${encodeURIComponent(q)}`,
      )
    ).hits,

  getMemory: (key: string) =>
    request<Record<string, unknown>>(`/memory/${encodeURIComponent(key)}`),

  deleteMemory: (key: string) =>
    request<{ deleted: boolean }>(`/memory/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }),

  /** 导出 NDJSON 原文（响应不是 JSON 包装，是 text/plain 流）。 */
  exportMemory: async (): Promise<string> => {
    const res = await rawRequest("/memory/export", { method: "POST" });
    return res.text();
  },

  chat: (text: string, conversationId = "console", signal?: AbortSignal) =>
    request<ChatResult>("/chat", {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId, text }),
      signal,
    }),

  approvals: async () =>
    (await request<{ approvals: Approval[] }>("/approvals")).approvals,

  decideApproval: (draftId: string, decision: "approve" | "reject") =>
    request<{ draft_id: string; decision: string }>(
      `/approvals/${encodeURIComponent(draftId)}/decision`,
      { method: "POST", body: JSON.stringify({ decision }) },
    ),

  // ---- Trace 账本（诊断视图走 console API token，放 api 组） ----

  tracesList: (params: TracesQuery = {}) => {
    const qs = new URLSearchParams();
    if (params.kind) qs.set("kind", params.kind);
    if (params.conversation_id)
      qs.set("conversation_id", params.conversation_id);
    if (params.principal_id) qs.set("principal_id", params.principal_id);
    if (params.status) qs.set("status", params.status);
    if (params.since) qs.set("since", String(params.since));
    if (params.until) qs.set("until", String(params.until));
    if (params.limit !== undefined) qs.set("limit", String(params.limit));
    if (params.offset !== undefined) qs.set("offset", String(params.offset));
    const suffix = qs.toString();
    return request<TracesListResult>(`/traces${suffix ? `?${suffix}` : ""}`);
  },

  tracesDetail: async (traceId: string) =>
    (
      await request<{ trace: TraceDetail }>(
        `/traces/${encodeURIComponent(traceId)}`,
      )
    ).trace,

  activityList: (limit = 30, offset = 0) =>
    request<{ total: number; items: ActivityItem[] }>(
      `/activity?limit=${limit}&offset=${offset}`,
    ),

  /** issue #43：任务执行时间线（可读阶段/回执/送达/重试建议/Trace 关联）。 */
  taskTimeline: (id: string) =>
    request<TaskTimeline>(`/tasks/${encodeURIComponent(id)}/timeline`),
};

// ---------- Whale Studio（S6：关系画布；所有授权判断都在服务端） ----------

export interface StudioConversation {
  key: string;
  channel?: string;
  kind?: string;
  conversation_id?: string;
  persona_id?: string | null;
  persona_version?: number;
  memory_spaces?: StudioMemoryBinding[];
  memory_version?: number;
  capabilities?: string[];
  capabilities_version?: number;
  ceiling?: string[];
  /** G4：会话节奏（只影响回复节奏，与能力/记忆授权无关）。 */
  rhythm?: ConversationRhythm;
  rhythm_version?: number;
  malformed?: boolean;
}

/** G4 会话节奏卡：活跃模式 / 单条回复分段上限（0=跟随默认）/ 免打扰。 */
export interface ConversationRhythm {
  activity: "quiet" | "balanced" | "lively";
  max_segments: number;
  dnd: boolean;
}

export interface StudioRhythmResult {
  key: string;
  rhythm: ConversationRhythm;
  version: number;
  configured?: boolean;
  changed?: boolean;
}

export interface StudioMemoryBinding {
  space_id: string;
  can_read?: boolean;
  can_write?: boolean;
  granted_by?: string;
  created_at?: string;
  missing?: boolean;
  restricted?: boolean;
  space_type?: string;
  name?: string;
  space_version?: number;
}

export interface StudioPersona {
  id: string;
  title: string;
  name: string;
  summary?: string;
  temperament?: string;
  relationship_style?: string;
  voice_hint?: string;
  category?: string;
  author?: string;
  license?: string;
  source_url?: string;
  builtin: boolean;
  has_external_prompt?: boolean;
}

export interface StudioMemorySpace {
  space_id: string;
  space_type: string;
  name: string;
  version: number;
  owner?: string;
  created_at?: string;
  record_count?: number;
  bindable: boolean;
  writable?: boolean;
  restricted?: boolean;
}

/** 人物卡编辑用完整视图（owner 管理面；含 external_prompt 等内部字段）。 */
export interface StudioPersonaDetail extends StudioPersona {
  description?: string;
  tone?: string;
  address_user_as?: string;
  style_rules?: string[];
  extra_taboos?: string[];
  timbre_hint?: string;
  speaking_rate?: number;
  artwork_ref?: string;
  external_prompt?: string;
  /** issue #37：示例对话（进模型上下文）。 */
  example_dialogue?: string;
  channels?: string[];
}


/** issue #36：有效人格快照——保存的设定实际进入模型请求的确切内容。 */
export interface PersonaFieldDisposition {
  field: string;
  destination: "model_context" | "tts_only" | "metadata_only";
  applied: string;
  used_value: boolean;
}

export interface PersonaEffectiveSnapshot {
  persona_id: string;
  card_title: string;
  source: "conversation_bound" | "global_active" | "global_legacy" | "builtin_default";
  model_context: string;
  voice: { timbre_hint?: string; speaking_rate?: number };
  content_sha: string;
  fallback_reason: string;
  dispositions: PersonaFieldDisposition[];
}

/** 新建/修改人物卡的可编辑字段（严格字段表；全可选，新建时 title 必填）。 */
export interface PersonaCardInput {
  card_id?: string;
  title?: string;
  name?: string;
  summary?: string;
  description?: string;
  temperament?: string;
  relationship_style?: string;
  voice_hint?: string;
  category?: string;
  tone?: string;
  address_user_as?: string;
  timbre_hint?: string;
  artwork_ref?: string;
  author?: string;
  source_url?: string;
  license?: string;
  external_prompt?: string;
  /** issue #37：示例对话（进模型上下文）。 */
  example_dialogue?: string;
  style_rules?: string[];
  extra_taboos?: string[];
  speaking_rate?: number;
}

/** issue #37：导入预览解析出的字段（SillyTavern v1/v2 JSON 或纯文本）。 */
export interface PersonaImportParsed {
  name?: string;
  summary?: string;
  temperament?: string;
  example_dialogue?: string;
  external_prompt?: string;
  relationship_style?: string;
}

export interface PersonaImportUnmapped {
  field: string;
  note: string;
  preview: string;
}

/** POST /studio/personas/import-preview 的结果（预览不写库）。 */
export interface PersonaImportPreview {
  parsed: PersonaImportParsed;
  suggested_card_id: string;
  mapping: Record<string, string>;
  unmapped: PersonaImportUnmapped[];
  missing: string[];
  conflicts: string[];
  warnings: string[];
  requires_confirmation: boolean;
}

/** issue #37：试玩一侧结果（reply 与 error 二选一）。 */
export interface PersonaPlaytestSide {
  persona_id: string;
  source: string;
  content_sha?: string;
  reply?: string;
  error?: string;
  fields_used?: string[];
}

export interface PersonaPlaytestResult {
  input: string;
  baseline: PersonaPlaytestSide;
  trial: PersonaPlaytestSide;
  note: string;
}

export interface StudioGraphNode {
  id: string;
  kind: "conversation" | "persona" | "memory_space" | "device";
  label: string;
  key?: string;
  channel?: string;
  conversation_kind?: string;
  capabilities?: string[];
  ceiling?: string[];
  builtin?: boolean;
  space_type?: string;
  version?: number;
  bindable?: boolean;
  restricted?: boolean;
  record_count?: number;
  writable?: boolean;
  /** kind=device：设备状态（pending/active/revoked）与事件排队水位。 */
  status?: string;
  queued_events?: number;
}

export interface StudioGraphEdge {
  id: string;
  kind: "persona" | "memory";
  from: string;
  to: string;
  version?: number;
  dangling?: boolean;
  missing?: boolean;
  can_read?: boolean;
  can_write?: boolean;
  granted_by?: string;
  created_at?: string;
}

export interface StudioGraph {
  nodes: StudioGraphNode[];
  edges: StudioGraphEdge[];
  explanations: Record<string, { code: string; message: string }>;
  generated_at: string;
}

export interface StudioForkPreviewItem {
  key: string;
  kind: string;
  tags: string[];
  content: Record<string, unknown>;
}

export interface StudioForkResult {
  fork: StudioMemorySpace & { auto_bound?: boolean };
}

/** S7+：Vault 管理管道的记录浏览项（摘要已脱敏）。 */
export interface StudioVaultRecord {
  key: string;
  kind: string;
  tags: string[];
  summary: string;
  created_at: string;
}

export interface StudioBindingResult {
  key: string;
  version: number;
  changed: boolean;
  persona_id?: string | null;
  space_id?: string;
  removed?: boolean;
  binding?: StudioMemoryBinding | null;
  capabilities?: string[];
  ceiling?: string[];
}

export interface TemporaryCode {
  code_id: string;
  allowed_scopes: string[];
  expires_at: number;
  created_by?: string;
  created_at?: number;
  consumed_at?: number | null;
  revoked_at?: number | null;
  failure_count?: number;
  proof_strength?: string;
  custom?: boolean;
  grant_ttl_s?: number;
  grant_max_uses?: number;
}

export interface TemporaryCodeCreated {
  code_id: string;
  /** 明文仅此一次出现在响应里，服务端只存校验值。 */
  code: string;
  proof_strength: string;
  scopes: string[];
  expires_at: number;
  note?: string;
}

export interface TemporaryGrant {
  grant_id: string;
  principal_id: string;
  channel: string;
  sender_id: string;
  conversation_id: string;
  scopes: string[];
  issued_at: number;
  expires_at: number;
  max_uses: number;
  used_count: number;
  remaining_uses: number;
  proof_method?: string;
  proof_strength?: string;
  source_code_id?: string;
  revoked?: boolean;
  expired?: boolean;
}

export interface StudioCodeCreateInput {
  scopes?: string[];
  custom_phrase?: string;
  ttl_s?: number;
  grant_ttl_s?: number;
  grant_max_uses?: number;
  action_fingerprint?: string;
}

export interface StudioForkInput {
  source_space: string;
  record_keys: string[];
  target_type?: string;
  target_name?: string;
  redact?: "builtin" | "none" | boolean;
  confirmed?: boolean;
}

/** 记忆管理面「主体总览」中的空间条目（GET /studio/memory/subjects）。 */
export interface MemorySpaceSubject {
  space_id: string;
  space_type: string;
  name: string;
  paused: boolean;
  scope_note: string;
  record_count: number;
}

/** 群友主体：principal → 其私有空间。 */
export interface MemoryMemberSubject {
  principal_id: string;
  space_id: string;
  record_count: number;
  opted_out: boolean;
  consent_active: boolean;
  paused: boolean;
  scope_note: string;
}

export interface MemorySubjects {
  owner: { spaces: MemorySpaceSubject[]; vault_present: boolean };
  members: MemoryMemberSubject[];
  groups: MemorySpaceSubject[];
  forks: MemorySpaceSubject[];
  legacy_unscoped_records: number;
}

/** GET /studio/memory/browse 的记录行；metadata_only 时无 summary。 */
export interface MemoryBrowseItem {
  key: string;
  kind: string;
  space_id: string;
  space_type: string;
  scope_note: string;
  created_at: string;
  superseded: boolean;
  confidence: number;
  source: string;
  source_group: string;
  expires_at?: string | null;
  last_confirmed?: string | null;
  summary?: string;
  source_message_ids?: string[];
  tags?: string[];
  restricted?: boolean;
}

export interface MemoryBrowseResult {
  space_ids: string[];
  metadata_only: boolean;
  total: number;
  offset: number;
  limit: number;
  items: MemoryBrowseItem[];
}

/** browse 查询参数：选择器三选一（space_id / principal_id / conversation_key）。 */
export interface MemoryBrowseParams {
  space_id?: string;
  principal_id?: string;
  conversation_key?: string;
  /** 逗号分隔：episode / preference / persona_continuity。 */
  kind?: string;
  source?: string;
  q?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
  include_superseded?: boolean;
}

// ---- 主动行为控制面板（issue #42）：GET/PUT /studio/initiative ----

/** 单个全局字段视图：当前生效值 + 来源 + 影响说明。 */
export interface InitiativeFieldView {
  /** daily_limit/passive_daily_limit 为整数；quiet_start/quiet_end 为 "HH:MM"。 */
  value: number | string;
  source: "console" | "default" | string;
  note: string;
}

export interface InitiativeGlobalView {
  fields: Record<string, InitiativeFieldView>;
  quiet_hours_rule: string;
  editable: string[];
  locked_by_deploy: string[];
}

export interface RhythmChannelDefault {
  activity: string;
  max_segments: number;
  dnd: boolean;
}

export interface RhythmConversationView {
  conversation_key: string;
  activity: string;
  max_segments: number;
  dnd: boolean;
  version: number;
}

export interface InitiativeRhythmView {
  channels: Record<string, RhythmChannelDefault>;
  conversations: RhythmConversationView[];
  activity_modes: string[];
}

export interface InitiativeDecision {
  trace_id: string;
  /** epoch 秒 */
  at: number;
  conversation_id: string;
  status: string;
  reason_code: string;
  reason_text: string;
}

export interface InitiativeView {
  global: InitiativeGlobalView;
  rhythm: InitiativeRhythmView;
  recent_decisions: InitiativeDecision[];
}

/** PUT /studio/initiative/global 的字段补丁；null = 删除该字段覆盖。 */
export interface InitiativeGlobalPatch {
  daily_limit?: number | null;
  passive_daily_limit?: number | null;
  quiet_start?: string | null;
  quiet_end?: string | null;
  reset?: boolean;
}

const studioConv = (key: string) =>
  `/studio/conversations/${encodeURIComponent(key)}`;

export const studioApi = {
  conversations: async () =>
    (await request<{ conversations: StudioConversation[] }>(
      "/studio/conversations",
    )).conversations,
  vaultRecords: async (spaceId: string) =>
    (await request<{
      space_id: string;
      count: number;
      truncated: boolean;
      items: StudioVaultRecord[];
    }>(`/studio/vault/records?space_id=${encodeURIComponent(spaceId)}`)).items,
  personas: async () =>
    (await request<{ personas: StudioPersona[] }>("/studio/personas")).personas,
  personaDetail: async (id: string) =>
    (
      await request<{ persona: StudioPersonaDetail }>(
        `/studio/personas/${encodeURIComponent(id)}`,
      )
    ).persona,
  personaEffective: async (id: string) =>
    (
      await request<{ effective: PersonaEffectiveSnapshot }>(
        `/studio/personas/${encodeURIComponent(id)}/effective`,
      )
    ).effective,
  createPersona: async (input: PersonaCardInput) =>
    (
      await request<{ persona: StudioPersonaDetail }>("/studio/personas", {
        method: "POST",
        body: JSON.stringify(input),
      })
    ).persona,
  updatePersona: async (id: string, input: PersonaCardInput) =>
    (
      await request<{ persona: StudioPersonaDetail }>(
        `/studio/personas/${encodeURIComponent(id)}`,
        { method: "PUT", body: JSON.stringify(input) },
      )
    ).persona,
  /** issue #37：导入预览——解析 SillyTavern/纯文本卡，不写库。 */
  personaImportPreview: (text: string, cardId?: string) =>
    request<PersonaImportPreview>("/studio/personas/import-preview", {
      method: "POST",
      body: JSON.stringify({
        text,
        ...(cardId ? { card_id: cardId } : {}),
      }),
    }),
  /** issue #37：即时试玩——persona_id 或 card（草稿字段）二选一。 */
  personaPlaytest: (input: {
    persona_id?: string;
    card?: PersonaCardInput;
    input?: string;
  }) =>
    request<PersonaPlaytestResult>("/studio/personas/playtest", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  memorySpaces: async () =>
    (await request<{ memory_spaces: StudioMemorySpace[] }>(
      "/studio/memory-spaces",
    )).memory_spaces,
  graph: () => request<StudioGraph>("/studio/graph"),

  putPersona: (key: string, personaId: string | null, expectedVersion?: number) =>
    request<StudioBindingResult>(`${studioConv(key)}/persona`, {
      method: "PUT",
      body: JSON.stringify({
        persona_id: personaId,
        ...(expectedVersion !== undefined
          ? { expected_version: expectedVersion }
          : {}),
      }),
    }),

  putMemory: (
    key: string,
    input: {
      space_id: string;
      can_read?: boolean;
      can_write?: boolean;
      remove?: boolean;
    },
    expectedVersion?: number,
  ) =>
    request<StudioBindingResult>(`${studioConv(key)}/memories`, {
      method: "PUT",
      body: JSON.stringify({
        ...input,
        ...(expectedVersion !== undefined
          ? { expected_version: expectedVersion }
          : {}),
      }),
    }),

  putCapabilities: (key: string, capabilities: string[], expectedVersion?: number) =>
    request<StudioBindingResult>(`${studioConv(key)}/capabilities`, {
      method: "PUT",
      body: JSON.stringify({
        capabilities,
        ...(expectedVersion !== undefined
          ? { expected_version: expectedVersion }
          : {}),
      }),
    }),

  getRhythm: (key: string) =>
    request<StudioRhythmResult>(`${studioConv(key)}/rhythm`),

  putRhythm: (
    key: string,
    rhythm: ConversationRhythm,
    expectedVersion?: number,
  ) =>
    request<StudioRhythmResult>(`${studioConv(key)}/rhythm`, {
      method: "PUT",
      body: JSON.stringify({
        ...rhythm,
        ...(expectedVersion !== undefined
          ? { expected_version: expectedVersion }
          : {}),
      }),
    }),

  forkPreview: (input: Pick<StudioForkInput, "source_space" | "record_keys" | "redact">) =>
    request<{
      source_space: string;
      redact: string;
      count: number;
      items: StudioForkPreviewItem[];
    }>("/studio/forks/preview", { method: "POST", body: JSON.stringify(input) }),

  fork: (input: StudioForkInput) =>
    request<StudioForkResult>("/studio/forks", {
      method: "POST",
      body: JSON.stringify({ ...input, confirmed: input.confirmed ?? true }),
    }),

  // ---- 记忆管理面（主体优先）：subjects 总览 + browse 浏览 + 治理操作 ----

  memorySubjects: () =>
    request<MemorySubjects>("/studio/memory/subjects"),

  memoryBrowse: (params: MemoryBrowseParams) => {
    const qs = new URLSearchParams();
    if (params.space_id) qs.set("space_id", params.space_id);
    else if (params.principal_id) qs.set("principal_id", params.principal_id);
    else if (params.conversation_key)
      qs.set("conversation_key", params.conversation_key);
    if (params.kind) qs.set("kind", params.kind);
    if (params.source) qs.set("source", params.source);
    if (params.q) qs.set("q", params.q);
    if (params.since) qs.set("since", params.since);
    if (params.until) qs.set("until", params.until);
    if (params.limit !== undefined) qs.set("limit", String(params.limit));
    if (params.offset !== undefined) qs.set("offset", String(params.offset));
    if (params.include_superseded) qs.set("include_superseded", "1");
    const suffix = qs.toString();
    return request<MemoryBrowseResult>(
      `/studio/memory/browse${suffix ? `?${suffix}` : ""}`,
    );
  },

  memoryCorrect: (key: string, newSummary: string) =>
    request<{ corrected: boolean; old_key: string; new_key: string }>(
      "/studio/memory/correct",
      {
        method: "POST",
        body: JSON.stringify({ key, new_summary: newSummary }),
      },
    ),

  memoryExpire: (key: string) =>
    request<{ expired: boolean; key: string }>("/studio/memory/expire", {
      method: "POST",
      body: JSON.stringify({ key }),
    }),

  memoryDelete: (key: string) =>
    request<{ deleted: boolean; key: string }>("/studio/memory/delete", {
      method: "POST",
      body: JSON.stringify({ key }),
    }),

  memoryPause: (spaceId: string, paused: boolean) =>
    request<{ space_id: string; paused: boolean }>("/studio/memory/pause", {
      method: "POST",
      body: JSON.stringify({ space_id: spaceId, paused }),
    }),

  // ---- 主动行为控制面板（issue #42） ----

  initiative: () => request<InitiativeView>("/studio/initiative"),

  putInitiativeGlobal: async (patch: InitiativeGlobalPatch) =>
    (
      await request<{ global: InitiativeGlobalView }>(
        "/studio/initiative/global",
        { method: "PUT", body: JSON.stringify(patch) },
      )
    ).global,

  putInitiativeChannel: async (
    channel: string,
    input: { activity: string; max_segments: number; dnd: boolean },
  ) =>
    (
      await request<{ rhythm: InitiativeRhythmView }>(
        "/studio/initiative/channel",
        {
          method: "PUT",
          body: JSON.stringify({ channel, ...input }),
        },
      )
    ).rhythm,

  createCode: (input: StudioCodeCreateInput) =>
    request<TemporaryCodeCreated>("/auth/temporary/codes", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  listCodes: async () =>
    (await request<{ codes: TemporaryCode[] }>("/auth/temporary/codes")).codes,
  revokeCode: (codeId: string) =>
    request<{ code_id: string; revoked: boolean }>(
      `/auth/temporary/codes/${encodeURIComponent(codeId)}/revoke`,
      { method: "POST", body: "{}" },
    ),
  listGrants: async () =>
    (await request<{ grants: TemporaryGrant[] }>("/auth/temporary/grants")).grants,
  revokeGrant: (grantId: string) =>
    request<{ grant_id: string; revoked: boolean }>(
      `/auth/temporary/grants/${encodeURIComponent(grantId)}/revoke`,
      { method: "POST", body: "{}" },
    ),
};

// ---------- SSE（EventSource 无法带 Authorization 头，用 fetch 流式读取实现） ----------

export interface EventStreamHandlers {
  onEvent: (event: WhaleEvent) => void;
  onOpen?: () => void;
  onError?: (error: Error) => void;
}

export interface EventStream {
  close(): void;
}

export function streamEvents(handlers: EventStreamHandlers): EventStream {
  const controller = new AbortController();
  void (async () => {
    try {
      const res = await rawRequest("/events", {
        headers: { Accept: "text/event-stream" },
        signal: controller.signal,
      });
      if (!res.body)
        throw new ApiError("no_stream", "SSE response has no body", res.status);
      handlers.onOpen?.();
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, idx).replace(/\r$/, "");
          buffer = buffer.slice(idx + 1);
          // 只处理 data: 行；心跳注释（": ping"）与空行忽略
          if (!line.startsWith("data:")) continue;
          const payload = line.slice("data:".length).trim();
          if (!payload) continue;
          try {
            handlers.onEvent(JSON.parse(payload) as WhaleEvent);
          } catch {
            /* 跳过无法解析的帧 */
          }
        }
      }
      handlers.onError?.(
        new ApiError("stream_closed", "event stream closed", 0),
      );
    } catch (e) {
      if (controller.signal.aborted) return; // 主动 close，不算错误
      handlers.onError?.(e instanceof Error ? e : new Error(String(e)));
    }
  })();
  return {
    close: () => {
      controller.abort();
    },
  };
}
