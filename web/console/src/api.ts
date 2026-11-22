import { DAY, dayKey, type Workspace, type Kind, type MemoryItem } from './data';

// 连接即同源：部署态由 whale-api 同源伺服，认证走控制台会话 cookie
//（HttpOnly，JS 不可读；POST /console/session 用管理凭据一次性交换）。
export type Connection = { origin: string; token: string };
export const sameOrigin = (): Connection => ({ origin: window.location.origin, token: '' });

// 会话失效回调（运行中 401 → 回首开配置流）
let unauthorizedHandler: (() => void) | null = null;
export function onSessionExpired(cb: (() => void) | null) {
  unauthorizedHandler = cb;
}

export async function probeSession(): Promise<{ expires_at: number }> {
  // 首开探测：有效会话 → 200；无会话 → 401；非 whale-api 宿主 → 网络错误
  const res = await fetch(`${window.location.origin}/api/v1/console/session`, {
    credentials: 'same-origin',
    redirect: 'error',
  });
  if (res.status === 401) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.error?.message || '无有效会话'), { code: 'no_session' });
  }
  if (!res.ok) throw new Error(`服务返回错误 (${res.status})`);
  const body = await res.json();
  return body.result as { expires_at: number };
}
export async function createSession(token: string): Promise<{ expires_at: number }> {
  const res = await fetch(`${window.location.origin}/api/v1/console/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
    credentials: 'same-origin',
    redirect: 'error',
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401) throw new Error('访问令牌无效，请检查后重试。');
    if (res.status === 429) throw new Error(body.error?.message || '尝试过于频繁，请稍后再试。');
    throw new Error(body.error?.message || `服务返回错误 (${res.status})`);
  }
  return body.result as { expires_at: number };
}
export async function deleteSession(): Promise<void> {
  await fetch(`${window.location.origin}/api/v1/console/session`, {
    method: 'DELETE',
    headers: { 'X-Whale-Console': '1' },
    credentials: 'same-origin',
    redirect: 'error',
  }).catch(() => undefined);
}
export async function request<T>(
  c: Connection,
  path: string,
  method = 'GET',
  data?: unknown,
  raw = false,
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    path === '/chat' || path.endsWith('/run') ? 120000 : 15000,
  );
  try {
    const res = await fetch(`${c.origin}/api/v1${path}`, {
      method,
      headers: {
        ...(c.token ? { Authorization: `Bearer ${c.token}` } : {}),
        // 会话模式（无 token）的写操作须带 CSRF 自定义头（服务端纵深校验）
        ...(c.token || method === 'GET' ? {} : { 'X-Whale-Console': '1' }),
        ...(data === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: controller.signal,
      redirect: 'error',
      credentials: 'same-origin',
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      if (res.status === 401) {
        unauthorizedHandler?.();
        throw new Error('会话已过期，请重新认证。');
      }
      throw new Error(body.error?.message || `服务返回错误 (${res.status})`);
    }
    if (raw) return (await res.text()) as T;
    const body = await res.json().catch(() => {
      throw new Error('此地址未返回 Whale Core API，请核对服务地址。');
    });
    if (!Object.prototype.hasOwnProperty.call(body, 'result')) throw new Error('服务响应格式不正确。');
    return body.result as T;
  } catch (e) {
    if (e instanceof TypeError) throw new Error('无法连接服务。请检查服务状态与网络。');
    if (e instanceof DOMException && e.name === 'AbortError')
      throw new Error('请求超时，请检查服务后重试。');
    throw e;
  } finally {
    clearTimeout(timeout);
  }
}
export type LiveStatus = {
  ok: boolean;
  version: string;
  profile: string;
  uptime_s: number;
  plugins: { loaded: number; enabled: number };
  memory: { episodes: number; preferences: number };
  drafts_pending: number;
};
type ApiPlugin = {
  plugin_id: string;
  name: string;
  version: string;
  enabled: boolean;
  kind: string;
  description?: string;
  inputs?: string[];
  actions?: string[];
  provides?: string[];
};
type ApiTask = { task_id: string; at: number; goal: string; status: string; receipts: string[] };
type ApiMemory = { key: string; kind: string; summary: string; created_at: string };
// ---------------------------------------------------------------- 设备管理（#60 真实契约）
export type DeviceReceipt = {
  idempotency_key: string;
  action_id: string;
  capability: string;
  state: string;
  route: string;
  created_at: number | null;
  updated_at: number | null;
  outcome_source?: string;
  summary?: string;
  error_code?: string;
  task_id?: string;
  run_id?: string;
};
export type DeviceActivity = {
  uplink: { events: number; last_at: number | null };
  downlink: { pending: number; last_receipt: DeviceReceipt | null };
};
export type DeviceView = {
  device_id: string;
  label: string;
  status: string;
  role?: string;
  scopes: string[];
  online?: boolean;
  created_at?: number;
  updated_at?: number;
  session?: { session_id: string; status: string } | null;
  capabilities_effective?: string[];
  transports?: Record<string, unknown>;
  activity?: DeviceActivity;
};
export type DevicesReport = {
  devices: DeviceView[];
  capabilities: string[];
  ws_path: string;
  ws_port: number | null;
  retention: {
    state: string;
    scanned?: number;
    receipt_scanned?: number;
    deleted?: number;
    marked_unknown?: number;
  };
};
export const fetchDevices = (c: Connection) => request<DevicesReport>(c, '/devices');
export const fetchDevice = (c: Connection, id: string) =>
  request<DeviceView>(c, `/devices/${encodeURIComponent(id)}`);
export const fetchDeviceReceipts = (c: Connection, id: string, limit = 20) =>
  request<{ device_id: string; receipts: DeviceReceipt[] }>(
    c,
    `/devices/${encodeURIComponent(id)}/receipts?limit=${limit}`,
  );
export function deviceCommand(
  c: Connection,
  id: string,
  command: 'rename' | 'role' | 'pause' | 'resume' | 'scopes' | 'purge',
  body?: unknown,
) {
  return request<Record<string, unknown>>(
    c,
    `/devices/${encodeURIComponent(id)}/${command}`,
    'POST',
    body,
  );
}
export const revokeDevice = (c: Connection, id: string) =>
  request<{ device_id: string; status: string; kicked: boolean }>(
    c,
    `/devices/${encodeURIComponent(id)}`,
    'DELETE',
  );

// ---------------------------------------------------------------- 审批队列（真实契约）
export type ApprovalItem = {
  kind: 'task' | 'draft';
  draft_id: string;
  task_id?: string;
  text: string;
  channel?: string;
  status: string;
  decision?: string;
  execution_status?: string;
};
export const fetchApprovals = async (c: Connection) =>
  (await request<{ approvals: ApprovalItem[] }>(c, '/approvals')).approvals;
export function decideApproval(c: Connection, item: ApprovalItem, decision: 'approve' | 'reject') {
  // 任务与草稿的决策端点不同（服务端口径）：task 走 /tasks/{id}/decision，
  // 主动外发草稿走 /approvals/{draft_id}/decision。
  return item.kind === 'task'
    ? request<Record<string, unknown>>(
        c,
        `/tasks/${encodeURIComponent(item.task_id || item.draft_id)}/decision`,
        'POST',
        { decision },
      )
    : request<{ draft_id: string; decision: string; execution_status: string }>(
        c,
        `/approvals/${encodeURIComponent(item.draft_id)}/decision`,
        'POST',
        { decision },
      );
}

// ---------------------------------------------------------------- 事件追踪（traces + SSE）
export type TraceItem = {
  trace_id: string;
  kind: string;
  conversation_id: string;
  principal_id: string;
  message_id: string;
  task_id: string;
  action_id: string;
  receipt_id: string;
  started_at: number;
  ended_at: number;
  status: string;
  summary: string;
};
export type TraceSpan = {
  span_id: string;
  phase: string;
  status: string;
  reason_code: string;
  detail: string;
  started_at: number;
  duration_ms: number;
  attempt: number;
};
export type TraceDetail = TraceItem & { spans: TraceSpan[] };
export const fetchTraces = (c: Connection, limit = 30, filters?: { kind?: string; status?: string }) => {
  const qs = new URLSearchParams();
  qs.set('limit', String(limit));
  if (filters?.kind) qs.set('kind', filters.kind);
  if (filters?.status) qs.set('status', filters.status);
  return request<{ total: number; traces: TraceItem[] }>(c, `/traces?${qs}`);
};
export const fetchTrace = async (c: Connection, id: string) =>
  (await request<{ trace: TraceDetail }>(c, `/traces/${encodeURIComponent(id)}`)).trace;
export type WhaleEvent = { at: number; seq: number; kind: string; detail: string };
export function streamEvents(
  c: Connection,
  handlers: { onEvent: (e: WhaleEvent) => void; onOpen?: () => void; onError?: () => void },
): { close: () => void } {
  // EventSource 不能带 Authorization 头，用 fetch + ReadableStream 解析 SSE。
  const controller = new AbortController();
  void (async () => {
    try {
      const res = await fetch(`${c.origin}/api/v1/events`, {
        headers: {
          ...(c.token ? { Authorization: `Bearer ${c.token}` } : {}),
          Accept: 'text/event-stream',
        },
        signal: controller.signal,
        redirect: 'error',
        credentials: 'same-origin',
      });
      if (!res.ok || !res.body) throw new Error(`events stream failed (${res.status})`);
      handlers.onOpen?.();
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const payload = trimmed.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;
          try {
            handlers.onEvent(JSON.parse(payload) as WhaleEvent);
          } catch {
            /* 跳过格式异常的帧 */
          }
        }
      }
      if (!controller.signal.aborted) handlers.onError?.();
    } catch {
      if (!controller.signal.aborted) handlers.onError?.();
    }
  })();
  return { close: () => controller.abort() };
}
export async function searchMemories(c: Connection, q: string): Promise<MemoryItem[]> {
  const result = await request<{ hits: ApiMemory[] }>(c, `/memory/search?q=${encodeURIComponent(q)}`);
  return result.hits.map((m) => ({
    id: m.key,
    title: m.summary.slice(0, 30),
    body: m.summary,
    category: m.kind,
    at: new Date(m.created_at).getTime() || Date.now(),
  }));
}

// ---------------------------------------------------------------- 模型配置（真实后端面）
export type RoleModelConfig = {
  base_url?: string;
  model?: string;
  api_key_configured?: boolean;
};
export type ModelSettings = {
  setup_completed?: boolean;
  profile: 'cloud' | 'local';
  model: { base_url: string; model: string; api_key_configured: boolean };
  intent_model_enabled: boolean;
  models?: Record<string, RoleModelConfig>;
  capabilities?: string[];
  notes?: string[];
  persona_taboos?: string[];
  services?: Record<string, string | boolean>;
  budget?: {
    limits: { model_calls: number; media_jobs: number };
    used: { model_calls: number; media_jobs: number };
    day: string;
  };
};
export const fetchSettings = (c: Connection) => request<ModelSettings>(c, '/settings');
export const saveSettings = (
  c: Connection,
  patch: {
    profile?: 'cloud' | 'local';
    model?: { base_url?: string; model?: string; api_key?: string | null };
    models?: Record<string, { base_url?: string; model?: string; api_key?: string | null }>;
    intent_model_enabled?: boolean;
    persona_taboos?: string[];
    services?: Record<string, string | null>;
    budget?: { model_calls: number; media_jobs: number };
  },
) => request<ModelSettings>(c, '/settings', 'POST', patch);
export const testProvider = (c: Connection) =>
  request<{ ok: boolean; latency_ms: number | null; error_kind?: string }>(
    c,
    '/providers/test',
    'POST',
    {},
  );
export type DiagnosticsReport = {
  checks: { name: string; status: string; detail: string }[];
  summary?: Record<string, unknown>;
  memory_worker?: {
    state: string;
    error?: string;
    claimed?: number;
    completed?: number;
    failed?: number;
    retried?: number;
    queue_depth?: number;
    in_flight?: boolean;
    model_busy?: boolean;
  };
};
export const fetchDiagnostics = (c: Connection) => request<DiagnosticsReport>(c, '/diagnostics');

// ---------------------------------------------------------------- 记忆管理（/studio/memory/* 既有面）
export type MemorySpaceSubject = {
  space_id: string;
  space_type: string;
  name: string;
  paused: boolean;
  scope_note: string;
  record_count: number;
};
export type MemoryMemberSubject = {
  principal_id: string;
  space_id: string;
  record_count: number;
  opted_out: boolean;
  consent_active: boolean;
  paused: boolean;
  scope_note: string;
};
export type MemorySubjects = {
  owner: { spaces: MemorySpaceSubject[]; vault_present: boolean };
  members: MemoryMemberSubject[];
  groups: MemorySpaceSubject[];
  forks: MemorySpaceSubject[];
  legacy_unscoped_records: number;
};
export type MemoryBrowseItem = {
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
};
export type MemoryBrowseResult = {
  space_ids: string[];
  metadata_only: boolean;
  total: number;
  offset: number;
  limit: number;
  items: MemoryBrowseItem[];
};
export const memorySubjects = (c: Connection) =>
  request<MemorySubjects>(c, '/studio/memory/subjects');
export function memoryBrowse(
  c: Connection,
  params: {
    space_id?: string;
    principal_id?: string;
    kind?: string;
    q?: string;
    limit?: number;
    offset?: number;
    include_superseded?: boolean;
  },
) {
  const qs = new URLSearchParams();
  if (params.space_id) qs.set('space_id', params.space_id);
  else if (params.principal_id) qs.set('principal_id', params.principal_id);
  if (params.kind) qs.set('kind', params.kind);
  if (params.q) qs.set('q', params.q);
  if (params.limit !== undefined) qs.set('limit', String(params.limit));
  if (params.offset !== undefined) qs.set('offset', String(params.offset));
  if (params.include_superseded) qs.set('include_superseded', '1');
  const suffix = qs.toString();
  return request<MemoryBrowseResult>(c, `/studio/memory/browse${suffix ? `?${suffix}` : ''}`);
}
export const memoryCorrect = (c: Connection, key: string, newSummary: string) =>
  request<{ corrected: boolean; old_key: string; new_key: string }>(
    c,
    '/studio/memory/correct',
    'POST',
    { key, new_summary: newSummary },
  );
export const memoryExpire = (c: Connection, key: string) =>
  request<{ expired: boolean; key: string }>(c, '/studio/memory/expire', 'POST', { key });
export const memoryDelete = (c: Connection, key: string) =>
  request<{ deleted: boolean; key: string }>(c, '/studio/memory/delete', 'POST', { key });
export const memoryPause = (c: Connection, spaceId: string, paused: boolean) =>
  request<{ space_id: string; paused: boolean }>(c, '/studio/memory/pause', 'POST', {
    space_id: spaceId,
    paused,
  });

// ---------------------------------------------------------------- 插件（健康/测试/配置）
export type PluginFull = {
  plugin_id: string;
  name: string;
  version: string;
  kind: string;
  status: string;
  enabled: boolean;
  description?: string;
  inputs?: string[];
  actions?: string[];
  provides?: string[];
  config?: Record<string, unknown>;
};
export const fetchPlugin = (c: Connection, id: string) =>
  request<PluginFull>(c, `/plugins/${encodeURIComponent(id)}`);
export const pluginHealth = (c: Connection, id: string) =>
  request<{ plugin_id: string; status: string; detail?: string }>(
    c,
    `/plugins/${encodeURIComponent(id)}/health`,
  );
export const pluginTest = (c: Connection, id: string) =>
  request<{ ok: boolean; latency_ms: number | null; error_kind?: string; via?: string }>(
    c,
    `/plugins/${encodeURIComponent(id)}/test`,
    'POST',
    {},
  );
export const setPluginConfig = (c: Connection, id: string, config: Record<string, unknown>) =>
  request<{ plugin_id: string; saved: boolean }>(
    c,
    `/plugins/${encodeURIComponent(id)}/config`,
    'POST',
    { config },
  );

// ---------------------------------------------------------------- 任务时间线 / 恢复（#43 既有面）
export type TaskTimeline = {
  task_id: string;
  goal: string;
  phase: string;
  status: string;
  current: { label: string; explanation: string; next_step: string };
  timeline: { at: number; at_text: string; kind: string; text: string; detail: Record<string, unknown> }[];
  receipts: string[];
  delivery: { state: string; attempts: number; last_error: string; channel: string };
  retry: { allowed: boolean; kind: string; reason: string };
  trace_ids: string[];
  capabilities: string[];
};
export const fetchTaskTimeline = (c: Connection, id: string) =>
  request<TaskTimeline>(c, `/tasks/${encodeURIComponent(id)}/timeline`);
export const resumeTask = (c: Connection, id: string) =>
  request<{ reply: string; status: string }>(c, `/tasks/${encodeURIComponent(id)}/resume`, 'POST');
export const explore = (c: Connection, topics: string[]) =>
  request<unknown>(c, '/workflows/explore', 'POST', { topics });

// ---------------------------------------------------------------- 主动行为设置 + 兴趣台账（既有面）
export type WorkflowSettings = {
  paused: boolean;
  daily_limit: number;
  quiet_start: string;
  quiet_end: string;
  timezone: string;
  schedule_enabled?: boolean;
  interval_s?: number;
};
export const fetchWorkflowSettings = (c: Connection) =>
  request<WorkflowSettings>(c, '/workflows/settings');
export const saveWorkflowSettings = (c: Connection, settings: WorkflowSettings) =>
  request<WorkflowSettings>(c, '/workflows/settings', 'POST', settings);
export type LifeState = {
  interests: { topic: string; weight: number }[];
  diary: { day: string; note: string; sources: string[] }[];
};
export const fetchLife = (c: Connection) => request<LifeState>(c, '/life');
export const addInterest = (c: Connection, topic: string) =>
  request<unknown>(c, '/life/interests', 'POST', { topic });
export const removeInterest = (c: Connection, topic: string) =>
  request<{ removed: boolean }>(c, '/life/interests', 'POST', { topic, remove: true });
export const lifeFeedback = (c: Connection, topic: string, signal: 'liked' | 'disliked' | 'ignored') =>
  request<{ saved: boolean }>(c, '/life/feedback', 'POST', { topic, signal });

// ---------------------------------------------------------------- 供应商管理（2026-11-22 新增面）
export type Provider = {
  id: string;
  name: string;
  base_url: string;
  enabled: boolean;
  api_key_configured: boolean;
  created_at?: number;
  updated_at?: number;
};
export const fetchProviders = async (c: Connection) =>
  (await request<{ providers: Provider[] }>(c, '/providers')).providers;
export const createProvider = (c: Connection, input: { name: string; base_url: string; api_key?: string }) =>
  request<Provider>(c, '/providers', 'POST', input);
export const updateProvider = (
  c: Connection,
  id: string,
  patch: { name?: string; base_url?: string; api_key?: string | null; enabled?: boolean },
) => request<Provider>(c, `/providers/${encodeURIComponent(id)}`, 'POST', patch);
export const deleteProvider = (c: Connection, id: string) =>
  request<{ id: string; deleted: boolean }>(c, `/providers/${encodeURIComponent(id)}/delete`, 'POST', {});
export const testProviderConn = (c: Connection, id: string) =>
  request<{ ok: boolean; latency_ms: number | null; error_kind?: string; models?: number }>(
    c,
    `/providers/${encodeURIComponent(id)}/test`,
    'POST',
    {},
  );
export const fetchProviderModels = (c: Connection, id: string) =>
  request<{ provider_id: string; models: string[]; cached: boolean }>(
    c,
    `/providers/${encodeURIComponent(id)}/models`,
  );

// ---------------------------------------------------------------- 模型注册表与角色（裁决①②③）
export type RegistryEntry = {
  name: string;
  provider: string;
  model: string;
  effort: string;
  base_url?: string;
};
export type ModelsRegistry = {
  entries: RegistryEntry[];
  roles: Record<string, string>;
};
export const fetchModelsRegistry = (c: Connection) =>
  request<ModelsRegistry>(c, '/models/registry');
export const upsertRegistryEntry = (
  c: Connection,
  entry: { name: string; provider: string; model: string; effort?: string; base_url?: string },
) => request<RegistryEntry>(c, '/models/registry', 'POST', entry);
export const bindRole = (c: Connection, role: string, name: string | null) =>
  request<{ role: string; name: string | null }>(
    c,
    `/models/roles/${encodeURIComponent(role)}`,
    'POST',
    { name },
  );
export const testRole = (c: Connection, role: string) =>
  request<{ ok: boolean; latency_ms: number; error_kind?: string; role: string; entry?: string }>(
    c,
    `/models/roles/${encodeURIComponent(role)}/test`,
    'POST',
    {},
  );

// ---------------------------------------------------------------- 角色与人格（/studio/personas 既有面）
export type Persona = {
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
};
export type PersonaCardInput = {
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
  example_dialogue?: string;
  style_rules?: string[];
  extra_taboos?: string[];
  speaking_rate?: number;
};
export type PersonaDetail = Persona & PersonaCardInput;
export type PersonaEffective = {
  persona_id: string;
  card_title: string;
  source: string;
  model_context: string;
  voice: { timbre_hint?: string; speaking_rate?: number };
  content_sha: string;
  fallback_reason: string;
  dispositions: { field: string; destination: string; applied: string; used_value: boolean }[];
};
export type PersonaImportPreview = {
  parsed: Record<string, string>;
  suggested_card_id: string;
  mapping: Record<string, string>;
  unmapped: { field: string; note: string; preview: string }[];
  missing: string[];
  conflicts: string[];
  warnings: string[];
  requires_confirmation: boolean;
};
export type PersonaPlaytest = {
  input: string;
  baseline: { persona_id: string; source: string; reply?: string; error?: string; fields_used?: string[] };
  trial: { persona_id: string; source: string; reply?: string; error?: string; fields_used?: string[] };
  note: string;
};
export const fetchPersonas = async (c: Connection) =>
  (await request<{ personas: Persona[] }>(c, '/studio/personas')).personas;
export const fetchPersona = async (c: Connection, id: string) =>
  (await request<{ persona: PersonaDetail }>(c, `/studio/personas/${encodeURIComponent(id)}`))
    .persona;
export const createPersona = async (c: Connection, input: PersonaCardInput) =>
  (
    await request<{ persona: PersonaDetail }>(c, '/studio/personas', 'POST', input)
  ).persona;
export const updatePersona = async (c: Connection, id: string, input: PersonaCardInput) =>
  (
    await request<{ persona: PersonaDetail }>(
      c,
      `/studio/personas/${encodeURIComponent(id)}`,
      'PUT',
      input,
    )
  ).persona;
export const personaEffective = async (c: Connection, id: string) =>
  (
    await request<{ effective: PersonaEffective }>(
      c,
      `/studio/personas/${encodeURIComponent(id)}/effective`,
    )
  ).effective;
export const personaImportPreview = (c: Connection, text: string, cardId?: string) =>
  request<PersonaImportPreview>(c, '/studio/personas/import-preview', 'POST', {
    text,
    ...(cardId ? { card_id: cardId } : {}),
  });
export const personaPlaytest = (
  c: Connection,
  input: { persona_id?: string; card?: PersonaCardInput; input?: string },
) => request<PersonaPlaytest>(c, '/studio/personas/playtest', 'POST', input);
export async function fetchWorkspace(
  c: Connection,
): Promise<{ workspace: Workspace; status: LiveStatus }> {
  const status = await request<LiveStatus>(c, '/status');
  if (!status.ok || !status.memory || !status.plugins)
    throw new Error('服务状态异常，暂时无法打开工作区。');
  const [pluginResult, taskResult, memoryResult, activity, conversation] = await Promise.all([
    request<{ plugins: ApiPlugin[] }>(c, '/plugins'),
    request<{ tasks: ApiTask[] }>(c, '/tasks'),
    request<{ hits: ApiMemory[] }>(c, '/memory/search?q='),
    request<{ items: { trace_id: string; at: number; status: string; lines: string[] }[] }>(
      c,
      '/activity?limit=100',
    ),
    request<{ messages: { role: 'user' | 'assistant'; content: string }[] }>(
      c,
      '/conversations/console',
    ),
  ]);
  const days = Array.from({ length: 30 }, (_, i) => ({
    date: dayKey(Date.now() - (29 - i) * DAY),
    messages: 0,
    memories: 0,
  }));
  for (const e of activity.items) {
    const d = days.find((d) => d.date === dayKey(e.at * 1000));
    if (d) d.messages++;
  }
  for (const m of memoryResult.hits) {
    const d = days.find((d) => d.date === m.created_at.slice(0, 10));
    if (d) d.memories++;
  }
  return {
    status,
    workspace: {
      name: '已连接的工作区',
      version: status.version,
      days,
      plugins: pluginResult.plugins.map((p) => ({
        id: p.plugin_id,
        name: p.name,
        english: p.plugin_id,
        version: p.version,
        enabled: p.enabled,
        kind: p.kind,
        description: p.description || '已注册的 Whale Core 能力插件。',
        inputs: p.inputs,
        actions: p.actions,
        provides: p.provides,
      })),
      tasks: taskResult.tasks.map((t) => ({
        id: t.task_id,
        title: t.goal,
        at: t.at * 1000,
        status: t.status,
        description: t.receipts.join('\n') || '暂无执行回执。',
      })),
      memories: memoryResult.hits.map((m) => ({
        id: m.key,
        title: m.summary.slice(0, 30),
        body: m.summary,
        category: m.kind,
        at: new Date(m.created_at).getTime() || Date.now(),
      })),
      events: activity.items.map((e) => ({
        id: e.trace_id,
        at: e.at * 1000,
        kind: 'system' as Kind,
        title: e.lines[0] || e.status,
        description: e.lines.slice(1).join('\n') || e.status,
      })),
      threads: [
        {
          id: 'console',
          title: '控制台对话',
          messages: conversation.messages.map((m, i) => ({
            id: `remote-${i}`,
            role: m.role,
            text: m.content,
          })),
        },
      ],
    },
  };
}
