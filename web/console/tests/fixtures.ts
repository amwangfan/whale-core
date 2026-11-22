import type { Route } from '@playwright/test';

// e2e 测试基建：拦截 /api/v1/** 并返回夹具数据。
// 注意：这不是产品内的 mock——产品只接真实服务；这里模拟的是服务端。
export const TEST_TOKEN = 'e2e-test-token';

export type FixtureState = {
  authed: boolean;
  pluginsEnabled: Record<string, boolean>;
  approvals: unknown[];
  sessionsIssued: number;
  modelsRegistry: {
    entries: { name: string; provider: string; model: string; effort: string; base_url?: string }[];
    roles: Record<string, string>;
  };
  settings: {
    profile: string;
    model: { base_url: string; model: string; api_key_configured: boolean };
    intent_model_enabled: boolean;
    models: Record<string, { base_url?: string; model?: string; api_key_configured?: boolean }>;
    capabilities: string[];
    notes: string[];
    persona_taboos?: string[];
    services?: Record<string, string | boolean>;
    budget?: {
      limits: { model_calls: number; media_jobs: number };
      used: { model_calls: number; media_jobs: number };
      day: string;
    };
  };
  settingsPatches: unknown[];
  personas: Record<string, unknown>[];
  personaWrites: { method: string; id: string; body: unknown }[];
  providers: Record<string, unknown>[];
  providerWrites: { method: string; path: string; body: unknown }[];
};

const nowS = () => Math.floor(Date.now() / 1000);
// 任务状态跨请求可变（批准决策后回读）
const taskStatus: Record<string, string> = { 'task-77': 'pending_approval' };

const deviceDetail = {
  device_id: 'ae6224a4c1d9d34c9784c6e322c66918',
  label: 'magic8-silent（真机对账机）',
  status: 'active',
  role: 'root-phone',
  scopes: ['device.shell', 'device.observe', 'device.status'],
  online: true,
  created_at: Date.now() - 40 * 86400000,
  updated_at: Date.now() - 3600000,
  session: { session_id: 'sess-1', status: 'active' },
  activity: {
    uplink: { events: 42, last_at: Date.now() - 300000 },
    downlink: {
      pending: 1,
      last_receipt: {
        idempotency_key: 'agent:01JFXK7G9A0B1C2D3E4F5G6H7',
        action_id: 'act-1',
        capability: 'device.shell',
        state: 'succeeded',
        route: 'ssh',
        created_at: Date.now() - 600000,
        updated_at: Date.now() - 599000,
        summary: 'ok',
      },
    },
  },
};

export function makeState(): FixtureState {
  return {
    authed: false,
    pluginsEnabled: { web_fetch: true, dashscope_media: false },
    approvals: [
      {
        kind: 'draft',
        draft_id: 'draft-qq-01',
        text: '看到你最近在忙控制台重构，需要我整理一份变更摘要吗？',
        channel: 'qq',
        status: 'draft',
        execution_status: 'not_dispatched',
      },
      {
        kind: 'task',
        draft_id: 'task-77',
        task_id: 'task-77',
        text: '整理下载文件夹：清理 30 天未打开的安装包',
        status: 'pending_approval',
        execution_status: 'not_dispatched',
      },
    ],
    sessionsIssued: 0,
    modelsRegistry: {
      entries: [
        { name: 'v4-pro-0813', provider: 'agentscope', model: 'deepseek-v4-pro-0813', effort: '' },
        { name: 'qwen4-max', provider: 'agentscope', model: 'qwen4-max', effort: '' },
        { name: 'kimi-k3', provider: 'agentscope', model: 'kimi-k3', effort: 'high' },
      ],
      roles: { reply_fast: 'v4-pro-0813', vision: 'qwen4-max', intent: 'kimi-k3' },
    },
    settings: {
      profile: 'cloud',
      model: {
        base_url: 'https://api.deepseek.com',
        model: 'deepseek-chat',
        api_key_configured: true,
      },
      intent_model_enabled: true,
      models: {
        intent: { model: 'kimi-k3', base_url: '', api_key_configured: false },
      },
      capabilities: ['web_fetch', 'web.search'],
      notes: [],
      persona_taboos: ['不谈论政治'],
      services: { WHALE_SHELL_ENABLED: '0', WHALE_SHELL_TIER: 'managed', TAVILY_API_KEY: true },
      budget: {
        limits: { model_calls: 200, media_jobs: 50 },
        used: { model_calls: 37, media_jobs: 2 },
        day: '2026-11-22',
      },
    },
    settingsPatches: [],
    personas: [
      {
        id: 'whale-default',
        title: '鲸鱼·默认',
        name: '鲸鱼',
        summary: '温和可靠的默认人格',
        temperament: '沉稳',
        relationship_style: '伙伴',
        category: '默认',
        builtin: true,
      },
      {
        id: 'sea-scribe',
        title: '海底记录员',
        name: '记录者',
        summary: '专注整理与回顾',
        temperament: '细致',
        category: '效率',
        builtin: false,
      },
    ],
    personaWrites: [],
    providers: [
      {
        id: 'prov-1',
        name: 'DeepSeek',
        base_url: 'https://api.deepseek.com',
        enabled: true,
        api_key_configured: true,
      },
    ],
    providerWrites: [],
  };
}

const json = (route: Route, result: unknown, status = 200) =>
  route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ result }),
  });
const err = (route: Route, status: number, code: string, message: string) =>
  route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ error: { code, message } }),
  });

type RegistryEntryShape = { name: string; provider: string; model: string; effort?: string; base_url?: string };

export function apiHandler(state: FixtureState) {
  return async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/v1/, '') || '/';
    const method = req.method();

    // —— 会话端点（自鉴权）——
    if (path === '/console/session') {
      if (method === 'POST') {
        const token = String(req.postDataJSON?.()?.token ?? '');
        if (token !== TEST_TOKEN) return err(route, 401, 'unauthorized', '访问令牌无效');
        state.authed = true;
        state.sessionsIssued++;
        return json(route, { ok: true, expires_at: nowS() + 43200 });
      }
      if (method === 'GET') {
        return state.authed
          ? json(route, { ok: true, expires_at: nowS() + 43200 })
          : err(route, 401, 'no_session', '无有效控制台会话');
      }
      if (method === 'DELETE') {
        state.authed = false;
        return json(route, { ok: true, revoked: true });
      }
    }

    // —— 以下全部要求已认证 ——
    if (!state.authed) return err(route, 401, 'unauthorized', 'missing or invalid bearer token');

    if (path === '/status' && method === 'GET') {
      return json(route, {
        ok: true,
        version: '1.4.2',
        profile: 'production',
        uptime_s: 8123,
        plugins: { loaded: 2, enabled: 1 },
        memory: { episodes: 128, preferences: 36 },
        drafts_pending: state.approvals.length,
      });
    }
    if (path === '/settings' && method === 'GET') {
      return json(route, JSON.parse(JSON.stringify(state.settings)));
    }
    if (path === '/settings' && method === 'POST') {
      const patch = req.postDataJSON() as Record<string, unknown>;
      state.settingsPatches.push(patch);
      // 补丁合并语义（与后端一致）：只动补丁里出现的键
      if (patch.profile) state.settings.profile = patch.profile as string;
      if (patch.model) {
        const m = patch.model as { base_url?: string; model?: string; api_key?: string | null };
        if (m.base_url !== undefined) state.settings.model.base_url = m.base_url;
        if (m.model !== undefined) state.settings.model.model = m.model;
        if (m.api_key) state.settings.model.api_key_configured = true;
        if (m.api_key === null) state.settings.model.api_key_configured = false;
      }
      if (patch.intent_model_enabled !== undefined)
        state.settings.intent_model_enabled = Boolean(patch.intent_model_enabled);
      if (patch.models) {
        for (const [role, cfg] of Object.entries(
          patch.models as Record<string, { base_url?: string; model?: string; api_key?: string | null }>,
        )) {
          const cur = state.settings.models[role] ?? {};
          if (cfg.base_url !== undefined) cur.base_url = cfg.base_url;
          if (cfg.model !== undefined) cur.model = cfg.model;
          if (cfg.api_key) cur.api_key_configured = true;
          if (cfg.api_key === null) cur.api_key_configured = false;
          state.settings.models[role] = cur;
        }
      }
      return json(route, JSON.parse(JSON.stringify(state.settings)));
    }
    if (path === '/providers/test' && method === 'POST') {
      return json(route, { ok: true, latency_ms: 812 });
    }
    if (path === '/models/registry' && method === 'GET') {
      return json(route, JSON.parse(JSON.stringify(state.modelsRegistry)));
    }
    if (path === '/models/registry' && method === 'POST') {
      const body = req.postDataJSON() as RegistryEntryShape;
      const entries = state.modelsRegistry.entries;
      const idx = entries.findIndex((e) => e.name === body.name);
      const clean = {
        name: body.name,
        provider: body.provider || 'agentscope',
        model: body.model,
        effort: body.effort || '',
        ...(body.base_url ? { base_url: body.base_url } : {}),
      };
      if (idx >= 0) entries[idx] = clean;
      else entries.push(clean);
      state.providerWrites.push({ method: 'POST', path: '/models/registry', body });
      return json(route, clean);
    }
    const roleBind = path.match(/^\/models\/roles\/([^/]+)$/);
    if (roleBind && method === 'POST') {
      const body = req.postDataJSON() as { name?: string | null };
      state.modelsRegistry.roles[roleBind[1]] = body.name || '';
      if (!body.name) delete state.modelsRegistry.roles[roleBind[1]];
      state.providerWrites.push({ method: 'POST', path, body });
      return json(route, { role: roleBind[1], name: body.name ?? null });
    }
    const roleTest = path.match(/^\/models\/roles\/([^/]+)\/test$/);
    if (roleTest && method === 'POST') {
      const bound = state.modelsRegistry.roles[roleTest[1]];
      return json(route, bound
        ? { ok: true, latency_ms: 402, role: roleTest[1], entry: bound }
        : { ok: false, latency_ms: 0, error_kind: 'unbound', role: roleTest[1] });
    }
    if (path === '/studio/personas' && method === 'GET') {
      return json(route, { personas: state.personas });
    }
    if (path === '/studio/personas' && method === 'POST') {
      const body = req.postDataJSON() as Record<string, unknown>;
      const created = {
        id: `persona-${state.personas.length + 1}`,
        builtin: false,
        ...body,
      };
      state.personas.push(created);
      state.personaWrites.push({ method: 'POST', id: created.id, body });
      return json(route, { persona: created });
    }
    const personaPut = path.match(/^\/studio\/personas\/([^/]+)$/);
    if (personaPut && method === 'PUT') {
      const body = req.postDataJSON() as Record<string, unknown>;
      const idx = state.personas.findIndex((p) => (p as { id: string }).id === personaPut[1]);
      if (idx >= 0)
        state.personas[idx] = { ...(state.personas[idx] as object), ...body } as never;
      state.personaWrites.push({ method: 'PUT', id: personaPut[1], body });
      return json(route, { persona: state.personas[idx] });
    }
    const personaEffective = path.match(/^\/studio\/personas\/([^/]+)\/effective$/);
    if (personaEffective && method === 'GET') {
      return json(route, {
        effective: {
          persona_id: personaEffective[1],
          card_title: '鲸鱼·默认',
          source: 'global_active',
          model_context: '你是鲸鱼，温和可靠。',
          voice: { timbre_hint: '温暖', speaking_rate: 1.0 },
          content_sha: 'sha256abcdef0123456789',
          fallback_reason: '',
          dispositions: [
            { field: 'external_prompt', destination: 'model_context', applied: '已注入', used_value: true },
            { field: 'timbre_hint', destination: 'tts_only', applied: '温暖', used_value: true },
          ],
        },
      });
    }
    if (path === '/studio/personas/import-preview' && method === 'POST') {
      return json(route, {
        parsed: { name: '旅人', summary: '来自远方的旅人', temperament: '好奇' },
        suggested_card_id: 'traveler',
        mapping: { name: '角色名', summary: '简短介绍' },
        unmapped: [],
        missing: [],
        conflicts: [],
        warnings: [],
        requires_confirmation: false,
      });
    }
    if (path === '/studio/personas/playtest' && method === 'POST') {
      return json(route, {
        input: '你好',
        baseline: { persona_id: 'whale-default', source: 'global_active', reply: '你好，我是鲸鱼。' },
        trial: { persona_id: 'whale-default', source: 'draft', reply: '你好呀，旅人。' },
        note: '草稿与基线的对照。',
      });
    }
    if (path === '/providers' && method === 'GET') {
      return json(route, { providers: state.providers });
    }
    if (path === '/providers' && method === 'POST') {
      const body = req.postDataJSON() as Record<string, unknown>;
      const created = {
        id: `prov-${state.providers.length + 1}`,
        name: body.name,
        base_url: body.base_url,
        enabled: true,
        api_key_configured: Boolean(body.api_key),
      };
      state.providers.push(created as never);
      state.providerWrites.push({ method: 'POST', path: '/providers', body });
      return json(route, created);
    }
    const providerPatch = path.match(/^\/providers\/([^/]+)$/);
    if (providerPatch && method === 'POST') {
      const body = req.postDataJSON() as Record<string, unknown>;
      state.providerWrites.push({ method: 'POST', path, body });
      const p = state.providers.find(
        (x) => (x as { id: string }).id === providerPatch[1],
      ) as Record<string, unknown> | undefined;
      if (p) {
        if (body.name) p.name = body.name;
        if (body.base_url) p.base_url = body.base_url;
        if (body.api_key) p.api_key_configured = true;
        if (body.api_key === null) p.api_key_configured = false;
        if (body.enabled !== undefined) p.enabled = body.enabled;
      }
      return json(route, p);
    }
    const providerDelete = path.match(/^\/providers\/([^/]+)\/delete$/);
    if (providerDelete && method === 'POST') {
      state.providers = state.providers.filter(
        (x) => (x as { id: string }).id !== providerDelete[1],
      );
      return json(route, { id: providerDelete[1], deleted: true });
    }
    const providerTest = path.match(/^\/providers\/([^/]+)\/test$/);
    if (providerTest && method === 'POST') {
      return json(route, { ok: true, latency_ms: 188, models: 3 });
    }
    const providerModels = path.match(/^\/providers\/([^/]+)\/models$/);
    if (providerModels && method === 'GET') {
      return json(route, {
        provider_id: providerModels[1],
        models: ['deepseek-chat', 'deepseek-v4-pro-0813', 'kimi-k3'],
        cached: false,
      });
    }
    if (path === '/studio/memory/subjects' && method === 'GET') {
      return json(route, {
        owner: {
          spaces: [
            {
              space_id: 'space-owner',
              space_type: 'owner',
              name: '主人空间',
              paused: false,
              scope_note: '主人私有',
              record_count: 12,
            },
          ],
          vault_present: true,
        },
        members: [
          {
            principal_id: 'qq:10001',
            space_id: 'space-member-1',
            record_count: 5,
            opted_out: false,
            consent_active: true,
            paused: false,
            scope_note: '',
          },
        ],
        groups: [],
        forks: [],
        legacy_unscoped_records: 2,
      });
    }
    if (path === '/studio/memory/browse' && method === 'GET') {
      return json(route, {
        space_ids: ['space-owner'],
        metadata_only: false,
        total: 2,
        offset: 0,
        limit: 10,
        items: [
          {
            key: 'mem:1',
            kind: 'preference',
            space_id: 'space-owner',
            space_type: 'owner',
            scope_note: '',
            created_at: '2026-11-20T08:00:00Z',
            superseded: false,
            confidence: 0.92,
            source: 'chat',
            source_group: 'console',
            summary: '偏好简洁、安静、有留白的表达方式',
            tags: ['偏好'],
          },
          {
            key: 'mem:2',
            kind: 'episode',
            space_id: 'space-owner',
            space_type: 'owner',
            scope_note: '',
            created_at: '2026-11-21T10:00:00Z',
            superseded: true,
            confidence: 0.7,
            source: 'chat',
            source_group: 'console',
            summary: '上周完成了控制台重构第一阶段',
            tags: [],
          },
        ],
      });
    }
    if (path === '/studio/memory/correct' && method === 'POST') {
      return json(route, { corrected: true, old_key: 'mem:1', new_key: 'mem:1#2' });
    }
    if (path === '/studio/memory/expire' && method === 'POST') {
      return json(route, { expired: true, key: 'mem:1' });
    }
    if (path === '/studio/memory/delete' && method === 'POST') {
      return json(route, { deleted: true, key: 'mem:1' });
    }
    if (path === '/studio/memory/pause' && method === 'POST') {
      return json(route, { space_id: 'space-owner', paused: true });
    }
    if (path === '/plugins' && method === 'GET') {
      return json(route, {
        plugins: [
          {
            plugin_id: 'web_fetch',
            name: '网页阅读',
            version: '1.2.0',
            enabled: state.pluginsEnabled.web_fetch,
            kind: 'read',
            description: '提取网页正文。',
            inputs: ['url'],
            provides: ['web.fetch'],
          },
          {
            plugin_id: 'dashscope_media',
            name: '媒体生成',
            version: '0.9.1',
            enabled: state.pluginsEnabled.dashscope_media,
            kind: 'image',
            description: '生成图像与视频。',
          },
        ],
      });
    }
    const pluginGet = path.match(/^\/plugins\/([^/]+)$/);
    if (pluginGet && method === 'GET') {
      return json(route, {
        plugin_id: pluginGet[1],
        name: pluginGet[1] === 'web_fetch' ? '网页阅读' : '媒体生成',
        version: '1.2.0',
        kind: 'read',
        status: 'loaded',
        enabled: state.pluginsEnabled[pluginGet[1]] ?? false,
        description: '提取网页正文。',
        inputs: ['url'],
        provides: ['web.fetch'],
        config: { timeout_s: '30', api_key: '***' },
      });
    }
    const pluginHealth = path.match(/^\/plugins\/([^/]+)\/health$/);
    if (pluginHealth && method === 'GET') {
      return json(route, { plugin_id: pluginHealth[1], status: 'healthy', detail: '就绪' });
    }
    const pluginTest = path.match(/^\/plugins\/([^/]+)\/test$/);
    if (pluginTest && method === 'POST') {
      return json(route, { ok: true, latency_ms: 233, via: 'web_fetch' });
    }
    const pluginConfig = path.match(/^\/plugins\/([^/]+)\/config$/);
    if (pluginConfig && method === 'POST') {
      return json(route, { plugin_id: pluginConfig[1], saved: true });
    }
    const pluginToggle = path.match(/^\/plugins\/([^/]+)\/(enable|disable)$/);
    if (pluginToggle && method === 'POST') {
      state.pluginsEnabled[pluginToggle[1]] = pluginToggle[2] === 'enable';
      return json(route, { plugin_id: pluginToggle[1], status: pluginToggle[2] });
    }
    if (path === '/tasks' && method === 'GET') {
      return json(route, {
        tasks: [
          {
            task_id: 'task-77',
            at: nowS() - 7200,
            goal: '整理下载文件夹：清理 30 天未打开的安装包',
            status: taskStatus['task-77'],
            receipts: ['等待主人审批'],
          },
          {
            task_id: 'task-76',
            at: nowS() - 90000,
            goal: '整理上周阅读笔记',
            status: 'completed',
            receipts: ['已完成'],
          },
        ],
      });
    }
    const taskDecision = path.match(/^\/tasks\/([^/]+)\/decision$/);
    if (taskDecision && method === 'POST') {
      state.approvals = state.approvals.filter(
        (a) => (a as { draft_id: string }).draft_id !== taskDecision[1],
      );
      taskStatus['task-77'] = 'approved';
      return json(route, { task_id: taskDecision[1], status: 'approved' });
    }
    if (path === '/memory/search' && method === 'GET') {
      const q = url.searchParams.get('q') || '';
      const hits = [
        {
          key: 'mem-1',
          kind: 'preference',
          summary: '偏好简洁、安静、有留白的表达方式',
          created_at: '2026-11-10T08:00:00Z',
        },
        {
          key: 'mem-2',
          kind: 'note',
          summary: 'Whale Core 工作区结构：对话捕捉、记忆沉淀、任务推进',
          created_at: '2026-11-12T09:30:00Z',
        },
      ].filter((m) => !q || m.summary.includes(q));
      return json(route, { hits });
    }
    if (path === '/memory/export' && method === 'POST') {
      return route.fulfill({
        status: 200,
        contentType: 'application/x-ndjson',
        body: '{"key":"mem-1"}\n{"key":"mem-2"}\n',
      });
    }
    const memDelete = path.match(/^\/memory\/([^/]+)$/);
    if (memDelete && method === 'DELETE') return json(route, { deleted: true });
    if (path === '/activity' && method === 'GET') {
      return json(route, {
        total: 2,
        items: [
          {
            trace_id: 'tr-01',
            at: nowS() - 300,
            status: 'replied',
            lines: ['群聊里来了一条消息。', '已回复。'],
          },
          {
            trace_id: 'tr-02',
            at: nowS() - 1800,
            status: 'replied',
            lines: ['私聊里来了一条消息。', '已回复。'],
          },
        ],
      });
    }
    if (path === '/conversations/console' && method === 'GET') {
      return json(route, {
        messages: [{ role: 'assistant', content: '欢迎回来，控制台已连接真实服务。' }],
      });
    }
    const convoDelete = path.match(/^\/conversations\/([^/]+)$/);
    if (convoDelete && method === 'DELETE') return json(route, { deleted: true });
    if (path === '/chat' && method === 'POST') {
      return json(route, { reply: '这是来自测试服务的真实回复。', status: 'replied' });
    }
    if (path === '/workflows/skills' && method === 'GET') {
      return json(route, {
        skills: [{ name: 'search-and-read', description: '检索并阅读整理为笔记。' }],
      });
    }
    const skillRun = path.match(/^\/workflows\/skills\/([^/]+)\/run$/);
    if (skillRun && method === 'POST') return json(route, '工作流执行完成：已生成回顾笔记。');
    const taskTimeline = path.match(/^\/tasks\/([^/]+)\/timeline$/);
    if (taskTimeline && method === 'GET') {
      return json(route, {
        task_id: taskTimeline[1],
        goal: '整理下载文件夹',
        phase: '等待审批',
        status: 'pending_approval',
        current: { label: '等待审批', explanation: '任务在策略层挂起。', next_step: '批准后执行' },
        timeline: [
          { at: nowS() - 7200, at_text: '10:00', kind: 'planned', text: '任务已规划', detail: {} },
          { at: nowS() - 7100, at_text: '10:01', kind: 'policy', text: '进入审批队列', detail: {} },
        ],
        receipts: ['等待主人审批'],
        delivery: { state: 'not_dispatched', attempts: 0, last_error: '', channel: 'console' },
        retry: { allowed: true, kind: 'resume', reason: '被拒绝或失败后可恢复' },
        trace_ids: ['tr-01'],
        capabilities: ['file.organize'],
      });
    }
    const taskResume = path.match(/^\/tasks\/([^/]+)\/resume$/);
    if (taskResume && method === 'POST') {
      return json(route, { reply: '任务已恢复', status: 'running' });
    }
    if (path === '/workflows/explore' && method === 'POST') {
      return json(route, '已创建探索任务：座头鲸迁徙');
    }
    if (path === '/workflows/settings' && method === 'GET') {
      return json(route, {
        paused: false,
        daily_limit: 12,
        quiet_start: '23:00',
        quiet_end: '07:30',
        timezone: 'Asia/Shanghai',
        schedule_enabled: true,
        interval_s: 3600,
      });
    }
    if (path === '/workflows/settings' && method === 'POST') {
      return json(route, req.postDataJSON());
    }
    if (path === '/life' && method === 'GET') {
      return json(route, {
        interests: [{ topic: '座头鲸迁徙', weight: 0.8 }],
        diary: [{ day: '2026-11-21', note: '读了海洋声学文章', sources: ['web'] }],
      });
    }
    if (path === '/life/interests' && method === 'POST') return json(route, { ok: true });
    if (path === '/life/feedback' && method === 'POST') return json(route, { saved: true });
    if (path === '/diagnostics' && method === 'GET') {
      return json(route, {
        checks: [
          { name: 'api', status: 'ok', detail: 'Authenticated API is running' },
          { name: 'shell sandbox', status: 'ok', detail: 'bwrap+prlimit available' },
        ],
        summary: { ok: 2, warn: 0, fail: 0 },
        memory_worker: {
          state: 'idle',
          queue_depth: 3,
          completed: 41,
          failed: 0,
          retried: 1,
          in_flight: false,
          model_busy: false,
        },
      });
    }
    if (path === '/devices' && method === 'GET') {
      return json(route, {
        devices: [deviceDetail],
        capabilities: ['device.shell', 'device.observe', 'device.status', 'device.tap'],
        ws_path: '/api/v2/devices/ws',
        ws_port: 8192,
        retention: { state: 'ok', scanned: 21, receipt_scanned: 21, deleted: 0, marked_unknown: 0 },
      });
    }
    if (path === `/devices/${deviceDetail.device_id}`) {
      if (method === 'GET') return json(route, deviceDetail);
      if (method === 'DELETE') return json(route, { device_id: deviceDetail.device_id, status: 'revoked', kicked: true });
    }
    const deviceCmd = path.match(/^\/devices\/([0-9a-f]{32})\/(rename|role|pause|resume|scopes|purge|receipts)$/);
    if (deviceCmd) {
      const [, , cmd] = deviceCmd;
      if (cmd === 'receipts' && method === 'GET') {
        return json(route, {
          device_id: deviceDetail.device_id,
          receipts: [
            {
              idempotency_key: 'agent:01JFXK7G9A0B1C2D3E4F5G6H7',
              action_id: 'act-1',
              capability: 'device.shell',
              state: 'succeeded',
              route: 'ssh',
              created_at: Date.now() - 600000,
              updated_at: Date.now() - 599000,
              summary: 'ok',
            },
          ],
        });
      }
      if (method === 'POST') {
        const body = req.postDataJSON?.() ?? {};
        if (cmd === 'rename') deviceDetail.label = String(body.label || deviceDetail.label);
        if (cmd === 'role') deviceDetail.role = String(body.role || '');
        if (cmd === 'pause') deviceDetail.status = 'paused';
        if (cmd === 'resume') deviceDetail.status = 'active';
        if (cmd === 'scopes') deviceDetail.scopes = (body.scopes as string[]) ?? deviceDetail.scopes;
        if (cmd === 'purge') return json(route, { device_id: deviceDetail.device_id, purged: true });
        return json(route, { ...deviceDetail });
      }
    }
    if (path === '/approvals' && method === 'GET') {
      return json(route, { approvals: state.approvals });
    }
    const approvalDecision = path.match(/^\/approvals\/([^/]+)\/decision$/);
    if (approvalDecision && method === 'POST') {
      state.approvals = state.approvals.filter(
        (a) => (a as { draft_id: string }).draft_id !== approvalDecision[1],
      );
      return json(route, {
        draft_id: approvalDecision[1],
        decision: 'approve',
        execution_status: 'not_dispatched',
      });
    }
    if (path === '/traces' && method === 'GET') {
      const all = [
        {
          trace_id: 'tr-01',
          kind: 'qq_inbound',
          conversation_id: 'group:演示群',
          principal_id: 'owner',
          message_id: 'm-1',
          task_id: '',
          action_id: '',
          receipt_id: '',
          started_at: nowS() - 300,
          ended_at: nowS() - 297,
          status: 'replied',
          summary: '群聊消息 → 已回复',
        },
      ];
      const kind = url.searchParams.get('kind') || '';
      const status = url.searchParams.get('status') || '';
      const traces = all.filter(
        (t) => (!kind || t.kind === kind) && (!status || t.status === status),
      );
      return json(route, { total: traces.length, offset: 0, limit: 30, traces });
    }
    const traceGet = path.match(/^\/traces\/([^/]+)$/);
    if (traceGet && method === 'GET') {
      return json(route, {
        trace: {
          trace_id: traceGet[1],
          kind: 'qq_inbound',
          conversation_id: 'group:演示群',
          principal_id: 'owner',
          message_id: 'm-1',
          task_id: '',
          action_id: '',
          receipt_id: '',
          started_at: nowS() - 300,
          ended_at: nowS() - 297,
          status: 'replied',
          summary: '群聊消息 → 已回复',
          spans: [
            {
              span_id: 's1',
              phase: 'received',
              status: 'ok',
              reason_code: 'message_received',
              detail: '收到群消息',
              started_at: nowS() - 300,
              duration_ms: 12,
              attempt: 1,
            },
            {
              span_id: 's2',
              phase: 'send',
              status: 'ok',
              reason_code: 'dispatched',
              detail: '回复已投递',
              started_at: nowS() - 298,
              duration_ms: 240,
              attempt: 1,
            },
          ],
        },
      });
    }
    if (path === '/events' && method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `data: {"at": ${nowS() - 60}, "seq": 1, "kind": "device:transports", "detail": "boot:registered=['adb']"}\n\n`,
      });
    }
    return err(route, 404, 'not_found', `fixture 未覆盖: ${method} ${path}`);
  };
}
