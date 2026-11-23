/* Screenshot demo runtime (tests/demo.py inlines this after the app modules and demo-data.js).
   Installs a fake `fetch` for /api/rpc/* and a fake WebSocket for /api/link, answers from buildDemo(),
   simulates live runs for chat.send, pre-seeds the token and opens the page from #/route or ?page=. */
(function () {
  const now = Date.now();
  const D = buildDemo(now);
  const G = D.G;
  const M = 60000;
  const sockets = new Set();
  const emit = (topic, data) => sockets.forEach((ws) => ws._event(topic, data));
  const later = (ms, fn) => setTimeout(fn, ms);
  const err = (code, message) => { const e = new Error(message); e.code = code; throw e; };
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const memById = (id) => D.memories.find((m) => m.id === id);
  const visibleMem = (m) => m.status !== 'forgotten';
  let netState = { ...G.networkOn };

  function memoryList(p) {
    let items = D.memories.filter(visibleMem).filter((m) => (!p.status || m.status === p.status) && (!p.kind || m.kind === p.kind));
    const key = String(p.order || 'updated_at DESC').split(' ')[0];
    items = items.slice().sort((a, b) => (b[key] || 0) - (a[key] || 0));
    return { items: items.slice(0, p.limit || 100), total: items.length };
  }
  function memoryGraph(p) {
    const cat = p.scope;
    let nodes = D.memories.filter(visibleMem).filter((m) => m.status !== 'archived');
    if (cat) nodes = nodes.filter((m) => (['owner', 'shared', 'group', 'person'].includes(cat) ? D.scopeCat(m.scope) === cat : m.scope === cat));
    if (p.kind) nodes = nodes.filter((m) => m.kind === p.kind);
    const total = nodes.length;
    nodes = nodes.slice(0, p.limit || 400);
    const ids = new Set(nodes.map((n) => n.id));
    const res = { nodes, edges: D.links.filter((l) => ids.has(l.src) && ids.has(l.dst)), total, truncated: total > nodes.length, extra_nodes: [], extra_edges: [] };
    return p.sources ? { ...res, ...G.graphSources(nodes) } : res;
  }
  function search(q) {
    const words = String(q).split(/\s+/).filter(Boolean);
    const hits = D.memories.filter((m) => m.status === 'active').map((m) => {
      const kw = words.filter((w) => m.content.includes(w)).length;
      const chars = [...String(q)].filter((c) => /[\u4e00-\u9fa5]/.test(c) && m.content.includes(c)).length;
      const score = kw * 0.4 + Math.min(0.4, chars * 0.06) + m.importance * 0.15 + (m.strength_now || 0) * 0.05;
      const why = [kw ? '关键词' : '', chars > 1 ? '语义' : '', m.importance >= 0.8 ? '重要' : '', now - m.updated_at < 3 * 86400000 ? '近期' : ''].filter(Boolean);
      return { ...m, score, why };
    }).filter((m) => m.score > 0.25 && m.why.length).sort((a, b) => b.score - a.score).slice(0, 12);
    return { hits };
  }
  function taskGet(id) {
    const t = D.tasks.find((x) => x.id === id) || D.tasks[0];
    const rv = D.runs[t.last_run_id];
    if (rv) return { task: t, runs: [rv.run], latest: { run: rv.run, steps: rv.steps, children: rv.children } };
    const run = { id: t.last_run_id, conversation_id: null, parent_id: null, task_id: t.id, mode: 'goal', status: t.status === 'done' ? 'completed' : 'completed', input: t.goal, output: t.result, error: '',
      channel: 'task', model: 'deepseek-chat', created_at: t.created_at, ended_at: t.updated_at, stats: { latency_ms: 42000, memories: 2, tools: 2 } };
    const steps = [{ id: run.id + '_s1', run_id: run.id, seq: 1, kind: 'recall', title: '回忆到 2 条记忆', status: 'done', input: {}, output: { memories: [] }, started_at: t.created_at, ended_at: t.created_at + 200 },
      { id: run.id + '_s2', run_id: run.id, seq: 2, kind: 'reply', title: '完成', status: 'done', input: {}, output: { text: t.result }, started_at: t.updated_at - 100, ended_at: t.updated_at }];
    return { task: t, runs: [run], latest: { run, steps, children: [] } };
  }
  function deviceGet(id) {
    const d = D.devices.find((x) => x.id === id) || D.devices[0];
    const actions = d.role === 'agent' ? [{ action_id: 'act_1', device_id: d.id, capability: 'device.tap', idempotency_key: 'run:tap1', arguments: { x: 540, y: 1680 }, origin: 'run', run_id: 'run_task_trip', status: 'succeeded', outcome_source: 'device', error_code: null, summary: '点击了「展览排期」', created_at: now - 6 * 3600000, updated_at: now - 6 * 3600000 + 900, expires_at: now }] : [];
    return { ...d, events: D.devEvents[d.id] || [], actions, event_count: d.role === 'agent' ? 318 : 1207 };
  }

  /** simulate a streamed chat run */
  function chatSend(p) {
    let conv = D.conversations.find((c) => c.id === p.conversation_id);
    if (!conv) {
      conv = { id: D.id('conv'), title: String(p.text).slice(0, 16), channel: 'console', peer: 'owner', persona_id: null, created_at: Date.now(), updated_at: Date.now(), turns: 0, meta: {} };
      D.conversations.unshift(conv); D.messages[conv.id] = [];
    }
    const rid = D.id('run');
    const t0 = Date.now();
    const run = { id: rid, conversation_id: conv.id, parent_id: null, task_id: null, mode: 'chat', status: 'running', input: p.text, output: '', error: '', channel: 'console', model: 'deepseek-chat', created_at: t0, ended_at: null, stats: {} };
    D.messages[conv.id].push(D.msg(conv.id, 'user', p.text, t0, { run_id: rid }));
    const hits = search(p.text).hits.slice(0, 3);
    const reply = hits.length
      ? `嗯哼，我记得这些：\n\n${hits.map((h) => `- ${h.content}`).join('\n')}\n\n（这是演示模式：回复是预设的，但记忆、轨迹和事件流都和真实实例一样。）`
      : '收到～ 这是演示模式，我不会真的调用模型，但你可以看看右侧的思维轨迹是怎么一步步出现的。';
    const steps = [
      { id: rid + '_s1', run_id: rid, seq: 1, kind: 'recall', title: `回忆到 ${hits.length} 条记忆`, status: 'done', input: { query: p.text }, output: { memories: hits.map((h) => ({ id: h.id, kind: h.kind, content: h.content, score: h.score, why: h.why })) }, started_at: t0 + 100, ended_at: t0 + 260 },
      { id: rid + '_s2', run_id: rid, seq: 2, kind: 'think', title: '思考', status: 'done', input: { turn: 1, model: 'deepseek-chat' }, output: { latency_ms: 700, decision: 'reply', text: '', reasoning: '' }, started_at: t0 + 300, ended_at: t0 + 1000 },
    ];
    D.runs[rid] = { run, steps, text: '', plan: [], progress: '', children: [] };
    later(120, () => { emit('run.started', { run }); emit('agent.state', { phase: 'thinking', active: [{ run_id: rid, mode: 'chat', status: 'thinking', conversation_id: conv.id, title: p.text, channel: 'console' }] }); });
    later(300, () => emit('run.step', { step: steps[0] }));
    later(1000, () => emit('run.step', { step: steps[1] }));
    const chunks = reply.match(/[\s\S]{1,6}/g) || [];
    chunks.forEach((c, i) => later(1100 + i * 35, () => emit('run.delta', { run_id: rid, conversation_id: conv.id, delta: c })));
    later(1200 + chunks.length * 35, () => {
      const done = { ...run, status: 'completed', output: reply, ended_at: Date.now(), stats: { latency_ms: Date.now() - t0, memories: hits.length, tools: 0 } };
      D.runs[rid].run = done;
      D.messages[conv.id].push(D.msg(conv.id, 'assistant', reply, Date.now(), { run_id: rid, meta: { memories: hits.map((h) => h.id) } }));
      conv.updated_at = Date.now(); conv.turns += 1;
      emit('run.step', { step: { id: rid + '_s3', run_id: rid, seq: 3, kind: 'reply', title: '回复', status: 'done', input: {}, output: { text: reply }, started_at: Date.now(), ended_at: Date.now() } });
      emit('run.completed', { run: done });
      emit('agent.state', { phase: 'acting', active: D.status().agent.active });
    });
    return { accepted: true, run_id: rid, conversation_id: conv.id };
  }

  const H = {
    'system.status': () => D.status(),
    'events.query': () => ({ events: D.activity }),
    'approvals.list': () => ({ approvals: D.approvals }),
    'approvals.resolve': (p) => {
      const i = D.approvals.findIndex((a) => a.id === p.id);
      const a = D.approvals.splice(i, 1)[0];
      later(200, () => emit('approval.resolved', { ...a, status: p.approve ? 'approved' : 'denied' }));
      return { ...a, status: p.approve ? 'approved' : 'denied' };
    },
    'conversations.list': () => ({ conversations: D.conversations.slice().sort((a, b) => b.updated_at - a.updated_at) }),
    'conversations.messages': (p) => ({ conversation: D.conversations.find((c) => c.id === p.id) || D.conversations[0], messages: D.messages[p.id] || [] }),
    'conversations.update': (p) => Object.assign(D.conversations.find((c) => c.id === p.id) || {}, p),
    'conversations.delete': (p) => { const i = D.conversations.findIndex((c) => c.id === p.id); if (i >= 0) D.conversations.splice(i, 1); return { deleted: p.id }; },
    'runs.get': (p) => { const r = D.runs[p.id] || D.runs.run_cat; return { run: r.run, steps: r.steps, children: r.children }; },
    'chat.send': chatSend,
    'chat.cancel': (p) => ({ cancelled: p.run_id }),
    'memory.list': memoryList,
    'memory.get': (p) => {
      const item = memById(p.id) || err('not_found', '记忆不存在');
      const ls = D.links.filter((l) => l.src === p.id || l.dst === p.id);
      return { item, links: ls, related: ls.map((l) => memById(l.src === p.id ? l.dst : l.src)).filter(Boolean) };
    },
    'memory.search': (p) => search(p.query || ''),
    'memory.graph': memoryGraph,
    'memory.stats': () => D.stats(),
    'memory.update': (p) => { const m = memById(p.id); Object.assign(m, p, { updated_at: Date.now() }); later(100, () => emit('memory.updated', { item: m })); return { item: m }; },
    'memory.remember': (p) => {
      const m = { id: D.id('m'), kind: p.kind || 'fact', content: p.content, subject: 'user', status: 'active', importance: p.importance ?? 0.6, confidence: 1, strength: 1, source: { manual: true },
        tags: p.tags || [], scope: 'owner', created_at: Date.now(), updated_at: Date.now(), last_access_at: null, access_count: 0, strength_now: 1 };
      D.memories.unshift(m); later(100, () => emit('memory.created', { item: m }));
      return { action: 'created', item: m };
    },
    'memory.forget': (p) => { const m = memById(p.id); if (m) { m.status = 'forgotten'; m.content = ''; } later(100, () => emit('memory.forgotten', { id: p.id })); return { forgotten: p.id }; },
    'memory.maintain': () => { later(100, () => emit('memory.maintained', { archived: 1, expired_candidates: 0 })); return { archived: 1, expired_candidates: 0 }; },
    'capabilities.list': () => ({ capabilities: D.caps, rules: D.config.policy.rules }),
    'policy.set': (p) => { if (p.verdict) D.config.policy.rules[p.pattern] = p.verdict; else delete D.config.policy.rules[p.pattern]; D.caps.forEach((c) => { if (c.name === p.pattern) c.policy = p.verdict || 'allow'; }); return { rules: D.config.policy.rules }; },
    'plugins.list': () => ({ plugins: D.plugins }),
    'plugins.set_enabled': (p) => { const x = D.plugins.find((y) => y.id === p.id); x.enabled = p.enabled; x.status = p.enabled ? (x.status === 'disabled' ? 'active' : x.status) : 'disabled'; return x; },
    'plugins.configure': (p) => { const x = D.plugins.find((y) => y.id === p.id); Object.assign(x.config, p.config || {}); return x; },
    'tasks.list': () => ({ tasks: D.tasks }),
    'tasks.get': (p) => taskGet(p.id),
    'tasks.create': (p) => { const t = { ...D.tasks[2], id: D.id('task'), title: p.title || String(p.goal).slice(0, 20), goal: p.goal, status: 'queued', result: '', created_at: Date.now(), updated_at: Date.now(), last_run_id: 'run_none' }; D.tasks.unshift(t); return t; },
    'tasks.start': (p) => D.tasks.find((t) => t.id === p.id), 'tasks.cancel': (p) => Object.assign(D.tasks.find((t) => t.id === p.id), { status: 'cancelled' }),
    'tasks.delete': (p) => { const i = D.tasks.findIndex((t) => t.id === p.id); D.tasks.splice(i, 1); return { deleted: p.id }; },
    'schedule.list': () => ({ jobs: D.jobs }),
    'schedule.set_enabled': (p) => Object.assign(D.jobs.find((j) => j.id === p.id), { enabled: p.enabled }),
    'schedule.run_now': (p) => D.jobs.find((j) => j.id === p.id), 'schedule.delete': (p) => ({ deleted: p.id }),
    'personas.list': () => ({ personas: D.personas, active: D.config.agent.persona_id }),
    'personas.activate': (p) => { D.config.agent.persona_id = p.id; later(100, () => emit('persona.activated', { id: p.id })); return { active: p.id }; },
    'personas.save': (p) => p, 'personas.delete': (p) => ({ deleted: p.id }), 'personas.import': (p) => ({ ...D.personas[1], id: D.id('persona'), name: p.card?.data?.name || '导入的角色' }),
    'config.get': () => D.config,
    'config.update': (p) => { const merge = (a, b) => { Object.entries(b || {}).forEach(([k, v]) => { if (v && typeof v === 'object' && !Array.isArray(v) && a[k]) merge(a[k], v); else a[k] = v; }); }; merge(D.config, p.patch); return D.config; },
    'models.status': () => D.modelsStatus(),
    'models.list_remote': () => ({ models: ['deepseek-chat', 'deepseek-reasoner'] }),
    'models.test': () => ({ ok: true, model: 'deepseek-chat', reply: '在的～', latency_ms: 812 }),
    'models.upsert_provider': (p) => p, 'models.remove_provider': (p) => ({ removed: p.id }),
    'autonomy.status': () => ({ config: D.config.autonomy, proactive_ready: true, proactive_reason: '', topics: D.config.autonomy.interests,
      recent: D.activity.filter((e) => e.topic.startsWith('autonomy.')).map((e) => ({ id: e.id, topic: e.topic, ts: e.ts, data: e.data })) }),
    'autonomy.explore': () => ({ started: true }),
    'link.clients': () => ({ clients: D.clients }),
    'devices.list': () => ({ devices: D.devices, capabilities: D.devices[0].capabilities, ws_path: '/api/devices/ws' }),
    'devices.get': (p) => deviceGet(p.id),
    'devices.privacy': (p) => ({ role: (D.devices.find((d) => d.id === p.id) || D.devices[1]).role, settings: D.devices[1].privacy,
      builtin: { system_packages: ['com.android.systemui', 'com.android.settings', 'com.miui.securitycenter'], ime_patterns: ['inputmethod', 'keyboard', 'sogou', 'baidu.input'], screen_flags: ['locked', 'keyguard', 'secure', 'password'], redaction: ['一次性验证码', '银行卡号', '身份证号'] },
      filtered: { system_private_app: 41, system_private_screen: 17, excluded_app: 9, excluded_domain: 2, phone_marked_sensitive: 1 } }),
    'devices.search_events': () => ({ events: [{ device: '日常机', time: '11-22 21:10', app: '沪江', summary: '做了 N2 听力练习第 3 套' }] }),
    'devices.digest_now': () => ({ devices: 2, segments: 1, episodes: 1, facts: 0 }),
    'devices.action': () => ({ status: 'succeeded', summary: '已执行' }),
    'devices.set_role': (p) => Object.assign(D.devices.find((d) => d.id === p.id), { role: p.role, role_explicit: true }),
    'devices.set_scopes': (p) => Object.assign(D.devices.find((d) => d.id === p.id), { scopes: p.scopes }),
    'devices.set_privacy': (p) => Object.assign(D.devices[1].privacy, p.settings || {}), 'devices.rename': (p) => Object.assign(D.devices.find((d) => d.id === p.id), { name: p.name }),
    'devices.revoke': (p) => ({ revoked: p.id }), 'devices.notify': () => ({ sent: true }),
    'devices.pair_start': () => ({ code: '482913', expires_at: Date.now() + 600000, ttl_s: 600 }),
    'setup.status': () => ({ completed: true, steps: { model: true, persona: true, phone: true, qq: true }, models_offline: false, network: netState }),
    'setup.quick_model': (p) => new Promise((res) => later(1400, () => res({ ok: true, test: { ok: true, model: (G.presets.find((x) => x.id === p.preset) || G.presets[0]).chat_model, reply: '你好，我是鲸鱼。以后请多关照～', latency_ms: 1186 }, status: D.modelsStatus() }))),
    'setup.complete': (p) => { D.config.agent.name = p.name || '鲸鱼'; return { completed: true }; },
    'system.network': () => netState,
    'system.set_lan': (p) => { netState = { ...netState, lan_enabled: !!p.enabled, restart_required: true }; return netState; },
    'system.restart': () => {
      later(400, () => sockets.forEach((ws) => ws.close(1012)));
      later(500, () => { netState = { ...netState, restart_required: false, host: netState.lan_enabled ? '0.0.0.0' : '127.0.0.1' }; });
      return { restarting: true };
    },
    'groups.refresh': (p) => new Promise((res) => later(1600, () => {
      const g = p.id === G.group.id ? G.group : G.group2;
      const r = { processed: g.pending, pending: 0, counts: { profiles_added: 0, profiles_updated: 2, preferences_added: 1, preferences_updated: 0, preferences_expired: 0, scanned: g.pending } };
      g.pending = 0; g.refreshed_at = Date.now();
      emit('group.refreshed', { id: g.id, name: g.name, counts: r.counts });
      res(r);
    })),
    'groups.set_paused': (p) => { const g = [G.group, G.group2].find((x) => x.id === p.id); g.paused = !!p.paused; later(50, () => emit('group.updated', { id: g.id, name: g.name })); return g; },
    'groups.update': (p) => { const g = [G.group, G.group2].find((x) => x.id === p.id); ['name', 'description', 'auto_refresh_min'].forEach((k) => { if (p[k] !== undefined) g[k] = p[k]; }); later(50, () => emit('group.updated', { id: g.id, name: g.name })); return g; },
  };

  async function call(method, params) {
    const fn = H[method] || G.R[method];
    if (!fn) return { ok: true, note: 'demo: no-op' };
    return clone(await fn(params || {}));
  }

  // -- fake fetch -------------------------------------------------------------------------------------------
  const realFetch = globalThis.fetch ? globalThis.fetch.bind(globalThis) : null;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const m = u.match(/\/api\/rpc\/([^?#]+)/);
    if (!m) { if (realFetch) return realFetch(url, opts); throw new TypeError('offline demo'); }
    const method = decodeURIComponent(m[1]);
    let params = {};
    try { params = JSON.parse(opts.body || '{}'); } catch { /* ignore */ }
    await new Promise((r) => setTimeout(r, 40 + Math.random() * 120));
    let body;
    try { body = { ok: true, result: await call(method, params) }; } catch (e) { body = { ok: false, error: { code: e.code || 'internal', message: e.message, retryable: false } }; }
    return { ok: body.ok, status: body.ok ? 200 : 400, json: async () => body };
  };

  // -- fake WebSocket -----------------------------------------------------------------------------------------
  class DemoSocket {
    constructor(url) {
      this.url = url; this.readyState = 0; this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
      sockets.add(this);
      setTimeout(() => { this.readyState = 1; this.onopen && this.onopen({}); }, 60);
    }
    _send(env) { if (this.readyState === 1) setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify({ v: '1.0', id: 's' + Math.random().toString(36).slice(2), ts: Date.now(), corr: null, ...env }) }), 0); }
    _event(topic, data) { this._send({ type: 'event', payload: { topic, data, event_id: 'e' + Math.random().toString(36).slice(2) } }); }
    send(text) {
      const env = JSON.parse(text);
      if (env.type === 'hello') this._send({ type: 'welcome', corr: env.id, payload: { client_id: 'cli_console', protocol: '1.0', server: 'whale-demo' } });
      else if (env.type === 'ping') this._send({ type: 'pong', corr: env.id, payload: {} });
      else if (env.type === 'rpc.request') {
        call(env.payload.method, env.payload.params)
          .then((result) => this._send({ type: 'rpc.response', corr: env.id, payload: { ok: true, result } }))
          .catch((e) => this._send({ type: 'rpc.response', corr: env.id, payload: { ok: false, error: { code: e.code || 'internal', message: e.message } } }));
      }
    }
    close(code = 1000) {
      if (this.readyState === 3) return;
      this.readyState = 3; sockets.delete(this);
      setTimeout(() => this.onclose && this.onclose({ code }), 0);
    }
  }
  globalThis.WebSocket = DemoSocket;

  // ambient life: the trip task keeps working, the group keeps chatting
  later(6000, () => { G.group.pending += 2; emit('group.archived', { id: G.group.id, name: G.group.name, count: 2 }); });
  later(9000, () => emit('run.progress', { run_id: 'run_task_trip', message: '正在比较两条路线的步行距离' }));

  // -- route & token --------------------------------------------------------------------------------------------
  const PAGES = { bridge: '/', chat: '/chat/conv_cat?trace=run_cat', memory: '/memory', groups: '/groups/' + encodeURIComponent(G.group.id) + '?tab=members&member=2345678',
    tasks: '/tasks/task_trip', capabilities: '/capabilities', devices: '/devices/' + D.AGENT_PHONE, persona: '/persona', settings: '/settings', setup: '/setup' };
  globalThis.WHALE_HASH_ROUTES = true;
  try { localStorage.setItem('whale.token', 'demo-token'); localStorage.setItem('whale.setup.later', '0'); } catch { /* ignore */ }
  globalThis.__WHALE_DEMO = { H, call, D, PAGES };
  const loc = globalThis.location;
  if (!loc || !globalThis.history) return;
  const q = new URLSearchParams(loc.search);
  const page = q.get('page');
  const theme = q.get('theme');
  if (theme === 'light' || theme === 'dark') { try { localStorage.setItem('whale.theme', theme); } catch { /* ignore */ } }
  if (q.get('view')) { try { localStorage.setItem('whale.ui.memory.view', JSON.stringify(q.get('view'))); } catch { /* ignore */ } }
  else if (page === 'memory') { try { localStorage.setItem('whale.ui.memory.view', '"graph"'); } catch { /* ignore */ } }
  if (q.get('dim')) { try { localStorage.setItem('whale.ui.memory.dim', JSON.stringify(q.get('dim'))); } catch { /* ignore */ } }
  if (q.get('sources')) { try { localStorage.setItem('whale.ui.memory.sources', q.get('sources') === '1' ? 'true' : 'false'); } catch { /* ignore */ } }
  if (page && PAGES[page] && !loc.hash) history.replaceState(null, '', loc.pathname + loc.search + '#' + PAGES[page]);
  else if (!loc.hash) history.replaceState(null, '', loc.pathname + loc.search + '#/');
})();
