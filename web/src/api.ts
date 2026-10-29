/**
 * whale-core 管理 API 类型化客户端。
 *
 * - base 固定为同源 `/api/v1`（dev 由 vite proxy 转发，prod 由 API 服务托管）。
 * - token 仅存 localStorage，只通过 Authorization 头发送，绝不进 URL。
 * - 错误统一抛 ApiError（code/message 取自 `{error:{code,message}}`）。
 */

export const API_BASE = '/api/v1';

const TOKEN_KEY = 'whale.console.token';

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
  [key: string]: unknown;
}

export interface PluginDetail extends PluginInfo {
  config?: Record<string, unknown>;
}

export interface PluginHealth {
  plugin_id: string;
  status: 'healthy' | 'unhealthy' | 'unknown' | 'disabled' | 'missing' | string;
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
}

export interface WhaleEvent {
  at: number | string;
  kind: string;
  detail: string;
}

// ---------- 错误 ----------

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = 'ApiError';
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
    return typeof localStorage !== 'undefined';
  } catch {
    return false;
  }
}

export function getToken(): string {
  if (cachedToken === null) {
    cachedToken = storageAvailable() ? localStorage.getItem(TOKEN_KEY) ?? '' : '';
  }
  return cachedToken;
}

export function setToken(token: string): void {
  cachedToken = token.trim();
  if (!storageAvailable()) return;
  if (cachedToken) localStorage.setItem(TOKEN_KEY, cachedToken);
  else localStorage.removeItem(TOKEN_KEY);
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
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

/** 发起请求并返回原始 Response；非 2xx 时按 `{error:{code,message}}` 解析并抛 ApiError。 */
async function rawRequest(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init?.body != null && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  let res: Response;
  try {
    res = await fetch(apiUrl(path), { ...init, headers });
  } catch (e) {
    throw new ApiError('network', e instanceof Error ? e.message : 'network error', 0);
  }
  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = res.statusText || `HTTP ${res.status}`;
    try {
      const body: unknown = await res.json();
      if (body && typeof body === 'object' && 'error' in body) {
        const err = (body as { error: { code?: unknown; message?: unknown } }).error;
        if (typeof err.code === 'string') code = err.code;
        if (typeof err.message === 'string') message = err.message;
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
  status: () => request<StatusResult>('/status'),

  health: () => request<HealthResult>('/health'),

  plugins: async () => (await request<{ plugins: PluginInfo[] }>('/plugins')).plugins,

  plugin: (id: string) => request<PluginDetail>(`/plugins/${encodeURIComponent(id)}`),

  setPluginEnabled: (id: string, enabled: boolean) =>
    request<{ plugin_id: string; status: string }>(
      `/plugins/${encodeURIComponent(id)}/${enabled ? 'enable' : 'disable'}`,
      { method: 'POST' },
    ),

  pluginHealth: (id: string) =>
    request<PluginHealth>(`/plugins/${encodeURIComponent(id)}/health`),

  searchMemory: async (q: string) =>
    (await request<{ hits: MemoryHit[] }>(`/memory/search?q=${encodeURIComponent(q)}`)).hits,

  getMemory: (key: string) => request<Record<string, unknown>>(`/memory/${encodeURIComponent(key)}`),

  deleteMemory: (key: string) =>
    request<{ deleted: boolean }>(`/memory/${encodeURIComponent(key)}`, { method: 'DELETE' }),

  /** 导出 NDJSON 原文（响应不是 JSON 包装，是 text/plain 流）。 */
  exportMemory: async (): Promise<string> => {
    const res = await rawRequest('/memory/export', { method: 'POST' });
    return res.text();
  },

  chat: (text: string, conversationId = 'console') =>
    request<ChatResult>('/chat', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: conversationId, text }),
    }),

  approvals: async () => (await request<{ approvals: Approval[] }>('/approvals')).approvals,

  decideApproval: (draftId: string, decision: 'approve' | 'reject') =>
    request<{ draft_id: string; decision: string }>(
      `/approvals/${encodeURIComponent(draftId)}/decision`,
      { method: 'POST', body: JSON.stringify({ decision }) },
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
      const res = await rawRequest('/events', {
        headers: { Accept: 'text/event-stream' },
        signal: controller.signal,
      });
      if (!res.body) throw new ApiError('no_stream', 'SSE response has no body', res.status);
      handlers.onOpen?.();
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, idx).replace(/\r$/, '');
          buffer = buffer.slice(idx + 1);
          // 只处理 data: 行；心跳注释（": ping"）与空行忽略
          if (!line.startsWith('data:')) continue;
          const payload = line.slice('data:'.length).trim();
          if (!payload) continue;
          try {
            handlers.onEvent(JSON.parse(payload) as WhaleEvent);
          } catch {
            /* 跳过无法解析的帧 */
          }
        }
      }
      handlers.onError?.(new ApiError('stream_closed', 'event stream closed', 0));
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
