/* Smoke test: render every page and shared component to a string with mock data shaped like the
   backend. Any exception fails the run. React warnings are collected and summarised. */
(function () {
  // the sandbox realm persists between runs: drop browser globals a previous demo-dom run left behind
  // (a `document` with #app would make main.js boot itself here)
  ['document', 'location', 'history', 'window', 'WHALE_HASH_ROUTES', '__WHALE_DEMO', '__whaleReq'].forEach((k) => { try { delete globalThis[k]; } catch { /* ignore */ } });
  const React = require('react');
  const { renderToString } = require('react-dom/server');
  const results = [];
  const warnings = new Map();
  const origError = console.error;
  const origWarn = console.warn;
  console.error = (...a) => { const k = String(a[0]).slice(0, 110); warnings.set(k, (warnings.get(k) || 0) + 1); };
  console.warn = console.error;

  // -- minimal browser globals ------------------------------------------------------------------
  const ls = new Map();
  globalThis.localStorage = { getItem: (k) => (ls.has(k) ? ls.get(k) : null), setItem: (k, v) => ls.set(k, String(v)), removeItem: (k) => ls.delete(k) };
  let wide = false;
  globalThis.matchMedia = (q) => ({ matches: wide && /min-width/.test(q), addEventListener() {}, removeEventListener() {} });

  // -- RPC mock: useRpc returns mock data synchronously ------------------------------------------
  const realHooks = __req('app/lib/hooks.js');
  const calls = [];
  function useRpc(method, params) {
    if (!method) return { data: undefined, error: null, loading: false, reload: async () => undefined, setData() {} };
    calls.push(method);
    const fn = MOCK.R[method];
    if (!fn) throw new Error('no mock for ' + method);
    return { data: fn(params || {}), error: null, loading: false, reload: async () => undefined, setData() {} };
  }
  __overrides['app/lib/hooks.js'] = Object.assign({}, realHooks, { useRpc });
  const api = __req('app/lib/api.js');
  const { html } = __req('vendor');
  const { store } = __req('app/lib/store.js');

  function check(name, fn) {
    const t0 = Date.now();
    try {
      const out = fn();
      if (typeof out !== 'string' || out.length < 20) throw new Error('suspiciously small output: ' + String(out).slice(0, 80));
      if (/\[object Object\]/.test(out)) throw new Error('rendered [object Object]');
      if (/undefined|NaN/.test(out.replace(/<[^>]+>/g, ' ').replace(/data-[a-z-]+="[^"]*"/g, ''))) {
        const text = out.replace(/<[^>]+>/g, ' ');
        const m = text.match(/.{0,40}(undefined|NaN).{0,40}/);
        throw new Error('visible "undefined"/NaN in text: …' + (m ? m[0] : '') + '…');
      }
      results.push({ name, ok: true, bytes: out.length, ms: Date.now() - t0 });
    } catch (e) {
      results.push({ name, ok: false, error: (e && e.stack ? e.stack.split('\n').slice(0, 4).join(' | ') : String(e)) });
    }
  }
  const render = (el) => renderToString(el);

  // -- store snapshot like after snapshot() ----------------------------------------------------------
  const now = Date.now();
  store.set({
    token: 'tok', conn: 'online', status: MOCK.status, agent: MOCK.status.agent,
    approvals: MOCK.R['approvals.list']().approvals,
    activity: [
      { id: 'a1', topic: 'memory.created', ts: now - 1000, data: { item: MOCK.memories[0] } },
      { id: 'a2', topic: 'run.completed', ts: now - 2000, data: { run: MOCK.run('run_01') } },
      { id: 'a3', topic: 'device.event_filtered', ts: now - 86400000 * 2, data: { device_id: 'x', reason: 'excluded_app' } },
      { id: 'a4', topic: 'approval.requested', ts: now - 3000, data: { title: '发送 QQ 消息', capability: 'qq.send' } },
      { id: 'a5', topic: 'device.digest', ts: now - 4000, data: { devices: 1, segments: 2, episodes: 2, facts: 1 } },
      { id: 'a6', topic: 'autonomy.proactive', ts: now - 5000, data: { text: 'hi', sent: false, why: '静默时段' } },
      { id: 'a7', topic: 'weird.topic', ts: now - 6000, data: {} },
    ],
    runs: {
      run_01: { run: MOCK.run('run_01'), steps: MOCK.steps, text: '', plan: [], progress: '', children: [], loadedAt: now },
      run_live: { run: MOCK.run('run_live', { status: 'running', input: '帮我查天气', stats: {} }), steps: MOCK.steps.slice(0, 2).map((s) => ({ ...s, run_id: 'run_live' })), text: '正在查**天气**', plan: [], progress: '', children: [] },
    },
    toasts: [{ id: 't1', kind: 'notify', title: '鲸鱼', body: '早上好', action: { label: '查看', href: '/chat' } }, { id: 't2', kind: 'error', title: '操作失败', body: 'x' }],
  });

  const { parseRoute, navigate } = realHooks;
  const pages = [
    ['app/pages/bridge.js', 'BridgePage', ['/', '/?focus=memory', '/?focus=caps', '/?focus=tasks', '/?focus=devices']],
    ['app/pages/chat.js', 'ChatPage', ['/chat', '/chat/conv_1', '/chat/conv_1?trace=run_01', '/chat/new?draft=你好', '/chat/new']],
    ['app/pages/memory.js', 'MemoryPage', ['/memory', '/memory/mem_1', '/memory?q=猫', '/memory?view=inbox', '/memory/mem_4?view=inbox', '/memory/mem_3']],
    ['app/pages/groups.js', 'GroupsPage', ['/groups', '/groups/qq%3A745812309', '/groups/qq%3A745812309?tab=keywords', '/groups/qq%3A745812309?tab=members',
      '/groups/qq%3A745812309?tab=members&member=2345678', '/groups/qq%3A745812309?tab=members&member=5678901', '/groups/qq%3A745812309?tab=transcript&sender=1234567', '/groups/qq%3A600112233', '/groups?new=1']],
    ['app/pages/tasks.js', 'TasksPage', ['/tasks', '/tasks/task_1', '/tasks?new=1']],
    ['app/pages/capabilities.js', 'CapabilitiesPage', ['/capabilities']],
    ['app/pages/devices.js', 'DevicesPage', ['/devices', '/devices/ba4bb14c75917cfc0e69fb7a1caf8ba3', '/devices/cc11dd22ee33ff44aa55bb66cc77dd88', '/devices?pair=1']],
    ['app/pages/persona.js', 'PersonaPage', ['/persona']],
    ['app/pages/settings.js', 'SettingsPage', ['/settings', '/settings/models', '/settings/autonomy']],
  ];
  for (const pass of ['narrow', 'wide']) {
    wide = pass === 'wide';
    for (const [id, exp, routes] of pages) {
      let mod;
      try { mod = __req(id); } catch (e) { results.push({ name: id + ' (load)', ok: false, error: String(e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : e) }); continue; }
      for (const r of routes) {
        navigate(r);
        check(`${pass} ${exp} ${r}`, () => render(html`<${mod[exp]} route=${parseRoute(r)} />`));
        if (id.endsWith('memory.js') && r === '/memory') {
          ls.set('whale.ui.memory.view', JSON.stringify('list'));
          check(`${pass} ${exp} ${r} [list view]`, () => render(html`<${mod[exp]} route=${parseRoute(r)} />`));
          ls.set('whale.ui.memory.view', JSON.stringify('graph'));
          ls.set('whale.ui.memory.sources', 'true');
          check(`${pass} ${exp} ${r} [graph + 来源]`, () => render(html`<${mod[exp]} route=${parseRoute(r)} />`));
          ls.set('whale.ui.memory.dim', JSON.stringify('3d'));
          check(`${pass} ${exp} ${r} [星空 3D + 来源]`, () => render(html`<${mod[exp]} route=${parseRoute(r)} />`));
          ls.delete('whale.ui.memory.sources'); ls.delete('whale.ui.memory.dim');
        }
        if (id.endsWith('memory.js') && r === '/memory/mem_3') {
          check(`${pass} ${exp} /memory/mem_gp_1234567 [依据 section]`, () => render(html`<${mod[exp]} route=${parseRoute('/memory/mem_gp_1234567')} />`));
        }
      }
    }
  }

  // -- 2026-11-24 features: setup wizard, groups, evidence, graph import, 3D, network -----------------
  wide = true;
  try {
    const setupMod = __req('app/pages/setup.js');
    const main = __req('app/main.js');
    const bridge = __req('app/pages/bridge.js');
    const groupsMod = __req('app/pages/groups.js');
    const gform = __req('app/components/group-form.js');
    const gviews = __req('app/components/group-views.js');
    const ev = __req('app/components/evidence.js');
    const imp = __req('app/components/graph-import.js');
    const mcp = __req('app/lib/mcp-graph.js');
    const star = __req('app/components/star-view.js');
    const net = __req('app/components/network.js');
    const settings = __req('app/pages/settings.js');
    const G = MOCK.G;
    const W = setupMod.SetupWizard;
    for (const s of setupMod.STEPS) check(`wizard step ${s.id}`, () => render(html`<${W} initial=${{ step: s.id }} />`));
    check('wizard model: preset chosen (cloud, key entered)', () => render(html`<${W} initial=${{ step: 'model', preset: 'moonshot', key: 'sk-test' }} />`));
    check('wizard model: local preset (Ollama)', () => render(html`<${W} initial=${{ step: 'model', preset: 'ollama' }} />`));
    check('wizard model: testing', () => render(html`<${W} initial=${{ step: 'model', preset: 'deepseek', key: 'sk-x', testing: true }} />`));
    check('wizard model: success', () => {
      const out = render(html`<${W} initial=${{ step: 'model', preset: 'deepseek', key: 'sk-x', test: { ok: true, model: 'deepseek-chat', reply: '你好！我是鲸鱼。', latency_ms: 842 } }} />`);
      if (!out.includes('连接成功') || !out.includes('842 ms')) throw new Error('success state missing'); return out;
    });
    check('wizard model: error', () => {
      const out = render(html`<${W} initial=${{ step: 'model', preset: 'deepseek', key: 'sk-bad', testError: '401 Unauthorized: invalid api key' }} />`);
      if (!out.includes('还没连上') || !out.includes('invalid api key') || !out.includes('重试')) throw new Error('error state missing'); return out;
    });
    check('wizard persona: import later chosen', () => render(html`<${W} initial=${{ step: 'persona', persona: 'persona_x', landing: '/persona' }} />`));
    const netOff = MOCK.R['system.network'];
    MOCK.R['system.network'] = () => G.networkOn;
    check('wizard phone: LAN on, urls + pairing code', () => {
      const out = render(html`<${W} initial=${{ step: 'phone', pair: { code: '482913', expires_at: Date.now() + 500000, ttl_s: 600 } }} />`);
      if (!out.includes('http://192.168.1.23:8080') || !out.includes('4')) throw new Error('urls/code missing'); return out;
    });
    check('settings 服务 card: LAN on', () => render(html`<${settings.SettingsPage} route=${parseRoute('/settings/service')} />`));
    check('devices pair: phone address', () => render(html`<${__req('app/pages/devices.js').DevicesPage} route=${parseRoute('/devices?pair=1')} />`));
    MOCK.R['system.network'] = () => ({ ...G.network, lan_enabled: true, restart_required: true });
    check('network panel: restart required', () => { const out = render(html`<${net.NetworkPanel} confirmRestart />`); if (!out.includes('立即重启服务')) throw new Error('restart button missing'); return out; });
    check('restart states', () => ['restarting', 'slow', 'back'].map((p) => render(html`<${net.RestartState} phase=${p} />`)).join(''));
    MOCK.R['system.network'] = netOff;
    check('settings 服务 card: LAN off', () => { const out = render(html`<${settings.SettingsPage} route=${parseRoute('/settings/service')} />`); if (!out.includes('允许局域网里的手机连接') || !out.includes('重新运行引导')) throw new Error('service card'); return out; });

    store.set({ setup: { ...G.setupIncomplete, network: G.network }, setupLater: false });
    navigate('/');
    check('App gate: incomplete setup → wizard', () => { const out = render(html`<${main.App} />`); if (!out.includes('首次设置')) throw new Error('wizard not shown'); return out; });
    check('Bridge: 完成设置 card', () => { const out = render(html`<${bridge.BridgePage} />`); if (!out.includes('继续设置')) throw new Error('setup card missing'); return out; });
    store.set({ setupLater: true });
    check('App gate: 稍后再说 → shell', () => { const out = render(html`<${main.App} />`); if (out.includes('wiz-dots')) throw new Error('wizard still shown'); return out; });
    navigate('/setup');
    check('App /setup route → wizard', () => { const out = render(html`<${main.App} />`); if (!out.includes('wiz-dots')) throw new Error('wizard not shown at /setup'); return out; });
    navigate('/');
    store.set({ setup: { completed: true, steps: { model: true, persona: true, phone: true, qq: false } }, setupLater: false });

    const listFn = MOCK.R['groups.list'];
    MOCK.R['groups.list'] = () => ({ groups: [] });
    check('groups: empty state', () => { const out = render(html`<${groupsMod.GroupsPage} route=${parseRoute('/groups')} />`); if (!out.includes('还没有群空间')) throw new Error('empty state'); return out; });
    MOCK.R['groups.list'] = listFn;
    const gg = MOCK.R['groups.get']({ id: G.group.id });
    check('group form: create', () => render(html`<${gform.GroupForm} open=${true} onClose=${() => {}} />`));
    check('group form: edit with roster', () => render(html`<${gform.GroupForm} open=${true} onClose=${() => {}} group=${gg.group} />`));
    check('group import modal', () => render(html`<${gform.ImportChat} open=${true} onClose=${() => {}} group=${G.group} />`));
    check('group delete modal', () => render(html`<${gform.DeleteGroup} open=${true} onClose=${() => {}} group=${G.group} />`));
    const mem = gg.digest.members.find((m) => m.portrait);
    check('member portrait (traits + claims + evidence buttons)', () => { const out = render(html`<${gviews.Portrait} group=${G.group} m=${mem} onTranscript=${() => {}} />`); if (!out.includes('口头禅') || !out.includes('依据')) throw new Error('portrait'); return out; });
    check('keywords: with selected word', () => render(html`<${gviews.Keywords} group=${G.group} digest=${gg.digest} initialWord="火锅" />`));
    check('evidence popover open (available)', () => { const out = render(html`<${ev.EvidenceButton} memoryId=${mem.portrait.memory_id} ids=${mem.portrait.traits[0].evidence_ids} initialOpen=${true} />`); if (!out.includes('ev-pop') || !out.includes('ev-text')) throw new Error('popover'); return out; });
    check('evidence: unavailable reasons', () => { const out = render(html`<${ev.EvidenceList} memoryId="mem_1" ids=${['conv:1', 'qq:group:745812309:m999']} />`); if (!out.includes('不支持查看原文') || !out.includes('不在存档里')) throw new Error('reasons'); return out; });
    check('evidence modal', () => render(html`<${ev.EvidenceModal} open=${true} onClose=${() => {}} memoryId=${mem.portrait.memory_id} evidenceId=${mem.portrait.traits[0].evidence_ids[0]} />`));
    const sample = mcp.parseMcpGraph(['{"type":"entity","name":"Alice","entityType":"person","observations":["喜欢跑团","住在杭州"]}',
      '{"type":"entity","name":"Whale","entityType":"project","observations":["私人 AI"]}', '{"type":"relation","from":"Alice","to":"Whale","relationType":"uses"}',
      '{"type":"relation","from":"Alice","to":"Bob","relationType":"knows"}'].join('\n'));
    check('graph import preview', () => { const out = render(html`<${imp.ImportPreview} data=${{ graph: sample, fileName: 'memory.jsonl' }} onClose=${() => {}} />`); if (!out.includes('memory.jsonl') || !out.includes('不会保存')) throw new Error('preview'); return out; });
    const big = mcp.parseMcpGraph(JSON.stringify({ entities: Array.from({ length: 1200 }, (_, i) => ({ name: 'e' + i, entityType: 't' + (i % 5), observations: [] })), relations: [] }));
    check('graph import preview: truncated note', () => { const out = render(html`<${imp.ImportPreview} data=${{ graph: big, fileName: 'big.json' }} onClose=${() => {}} />`); if (!out.includes('只画出了')) throw new Error('truncation note'); return out; });
    check('star view (3D) with perf overlay', () => render(html`<${star.StarView} nodes=${MOCK.memories} edges=${[]} onPick=${() => {}} initialPerf=${true} />`));
  } catch (e) { results.push({ name: 'new features (load)', ok: false, error: String(e && e.stack ? e.stack.split('\n').slice(0, 4).join(' | ') : e) }); }

  // -- shell, login, overlays ---------------------------------------------------------------------------
  try {
    const main = __req('app/main.js');
    const live = __req('app/components/live.js');
    const shell = __req('app/components/shell.js');
    const trace = __req('app/components/trace.js');
    navigate('/');
    check('App (signed in, shell)', () => render(html`<${main.App} />`));
    store.set({ trayOpen: true, paletteOpen: true });
    check('Approval tray open', () => render(html`<${live.ApprovalTray} />`));
    check('Palette open', () => render(html`<${shell.Palette} />`));
    check('Toasts', () => render(html`<${live.Toasts} />`));
    check('StepList', () => render(html`<${trace.StepList} steps=${MOCK.steps} run=${MOCK.run('run_01')} progress="子 Agent：调研" />`));
    check('Orb phases', () => ['idle', 'thinking', 'acting', 'waiting', 'bogus'].map((p) => render(html`<${live.Orb} phase=${p} avatar=${p === 'waiting' ? 'data:image/png;base64,AA' : undefined} label />`)).join(''));
    store.set({ conn: 'reconnecting', connInfo: { attempt: 2, retryAt: Date.now() + 3000 } });
    check('Conn indicator reconnecting', () => render(html`<${shell.ConnIndicator} />`));
    store.set({ token: '' });
    check('App (signed out → login)', () => render(html`<${main.App} />`));
  } catch (e) { results.push({ name: 'shell (load)', ok: false, error: String(e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : e) }); }

  // -- libraries ---------------------------------------------------------------------------------------------
  const md = __req('app/lib/markdown.js');
  const fmt = __req('app/lib/format.js');
  check('markdown safety', () => {
    const out = render(html`<div>${md.renderMarkdown('[x](javascript:alert(1)) <img src=x onerror=alert(1)> **b** `c` ```\nunterminated')}</div>`);
    if (/href="javascript/i.test(out) || /<img/i.test(out)) throw new Error('unsafe markup: ' + out);
    return out;
  });
  check('humanize all topics', () => ['run.started', 'run.completed', 'run.failed', 'run.cancelled', 'run.step', 'run.plan', 'memory.created', 'memory.updated', 'memory.reinforced',
    'memory.superseded', 'memory.forgotten', 'memory.extracted', 'memory.maintained', 'approval.requested', 'approval.resolved', 'task.created', 'task.updated', 'task.deleted',
    'schedule.fired', 'schedule.changed', 'notify', 'device.connected', 'device.disconnected', 'device.paired', 'device.revoked', 'device.updated', 'device.event',
    'device.event_filtered', 'device.action', 'device.notification', 'device.pair_requested', 'device.digest', 'autonomy.exploring', 'autonomy.explored',
    'autonomy.proactive', 'plugin.changed', 'config.changed', 'persona.activated', 'persona.saved', 'system.started', 'group.updated', 'group.refreshed', 'group.archived', 'setup.completed', 'x.y']
    .map((t) => { const h = fmt.humanize({ topic: t, data: {} }); if (!h.text || !h.icon) throw new Error('bad humanize ' + t); return h.text; }).join('\n'));
  check('glob match', () => {
    const ok = api.topicMatches('run.step', 'run.*') && !api.topicMatches('runx.step', 'run.*') && api.topicMatches('device.digest', ['memory.*', 'device.*']) && api.topicMatches('a.b', '*');
    if (!ok) throw new Error('glob mismatch');
    return 'glob ok glob ok glob ok glob ok';
  });

  console.error = origError;
  console.warn = origWarn;
  const failed = results.filter((r) => !r.ok);
  console.log(`SMOKE: ${results.length - failed.length}/${results.length} passed, ${__stats.elements} elements created, rpc mocks used: ${[...new Set(calls)].length}`);
  for (const r of failed) console.log('FAIL', r.name, '\n   ', r.error);
  console.log('React warnings (unique):', warnings.size);
  for (const [k, n] of warnings) console.log(`  ${n}× ${k}`);
})();
