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
