/* Demo smoke: render every page of the screenshot demo (tests/demo-data.js + demo-runtime.js) with
   react-dom/server, useRpc answered synchronously by the demo's own handlers. Same failure rules as
   smoke.js: exceptions, visible undefined/NaN, [object Object]. Also drives a few demo actions. */
(function () {
  const { renderToString } = require('react-dom/server');
  const DEMO = globalThis.__WHALE_DEMO;
  const results = [];
  const warnings = new Map();
  const origError = console.error;
  console.error = (...a) => { const k = String(a[0]).slice(0, 110); warnings.set(k, (warnings.get(k) || 0) + 1); };
  let wide = false;
  globalThis.matchMedia = (q) => ({ matches: wide && /min-width/.test(q), addEventListener() {}, removeEventListener() {} });
  const hooks = __req('app/lib/hooks.js');
  const sync = (method, params) => {
    const fn = DEMO.H[method] || DEMO.D.G.R[method];
    if (!fn) throw new Error('demo has no handler for ' + method);
    const r = fn(params || {});
    if (r && typeof r.then === 'function') return undefined;
    return JSON.parse(JSON.stringify(r));
  };
  const used = new Set();
  __overrides['app/lib/hooks.js'] = Object.assign({}, hooks, {
    useRpc(method, params) {
      if (!method) return { data: undefined, error: null, loading: false, reload: async () => undefined, setData() {} };
      used.add(method);
      return { data: sync(method, params), error: null, loading: false, reload: async () => undefined, setData() {} };
    },
  });
  const { html } = __req('vendor');
  const { store } = __req('app/lib/store.js');
  const D = DEMO.D;
  store.set({
    token: 'demo-token', conn: 'online', status: sync('system.status'), agent: sync('system.status').agent, approvals: sync('approvals.list').approvals,
    activity: sync('events.query').events.map((e) => ({ id: e.id, topic: e.topic, ts: e.ts, data: e.data })), setup: sync('setup.status'),
    runs: { run_task_trip: D.runs.run_task_trip, run_cat: D.runs.run_cat },
  });
  const check = (name, fn) => {
    try {
      const out = fn();
      if (typeof out !== 'string' || out.length < 50) throw new Error('small output');
      if (/\[object Object\]/.test(out)) throw new Error('rendered [object Object]');
      const text = out.replace(/<[^>]+>/g, ' ');
      const m = text.match(/.{0,40}\b(undefined|NaN)\b.{0,40}/);
      if (m) throw new Error('visible undefined/NaN: …' + m[0] + '…');
      results.push({ name, ok: true });
    } catch (e) { results.push({ name, ok: false, error: e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : String(e) }); }
  };
  const main = __req('app/main.js');
  const routes = Object.values(DEMO.PAGES).concat(['/memory/m_cat', '/memory/mem_gp_2345678', '/memory?view=inbox', '/groups', '/groups/qq%3A745812309', '/groups/qq%3A745812309?tab=keywords',
    '/groups/qq%3A745812309?tab=transcript&sender=3456789', '/groups/qq%3A600112233', '/devices/' + D.DAILY_PHONE, '/tasks/task_report', '/tasks', '/chat/conv_jp', '/settings/service', '/chat']);
  for (const pass of ['narrow', 'wide']) {
    wide = pass === 'wide';
    for (const r of routes) {
      hooks.navigate(r);
      check(`${pass} ${r}`, () => renderToString(html`<${main.App} />`));
    }
    hooks.navigate('/memory');
    for (const [view, dim, src] of [['list', '2d', false], ['graph', '3d', true], ['graph', '2d', true]]) {
      globalThis.__demoLS.set('whale.ui.memory.view', JSON.stringify(view));
      globalThis.__demoLS.set('whale.ui.memory.dim', JSON.stringify(dim));
      globalThis.__demoLS.set('whale.ui.memory.sources', String(src));
      check(`${pass} /memory [${view} ${dim}${src ? ' 来源' : ''}]`, () => renderToString(html`<${main.App} />`));
    }
    globalThis.__demoLS.delete('whale.ui.memory.view'); globalThis.__demoLS.delete('whale.ui.memory.dim'); globalThis.__demoLS.delete('whale.ui.memory.sources');
  }
  const pending = [];
  const t = (name, p) => pending.push(Promise.resolve().then(p).then(() => results.push({ name, ok: true }), (e) => results.push({ name, ok: false, error: String(e && e.stack || e) })));
  t('demo rpc over fake fetch', async () => {
    const r = await (await fetch('/api/rpc/memory.stats', { method: 'POST', body: '{}' })).json();
    if (!r.ok || !(r.result.active > 30)) throw new Error('stats: ' + JSON.stringify(r).slice(0, 120));
    const g = await (await fetch('/api/rpc/memory.graph', { method: 'POST', body: JSON.stringify({ limit: 400, sources: true }) })).json();
    if (!g.result.extra_nodes.length || g.result.nodes.length < 40) throw new Error('graph sources');
    const bad = await (await fetch('/api/rpc/memory.get', { method: 'POST', body: JSON.stringify({ id: 'nope' }) })).json();
    if (bad.ok || bad.error.code !== 'not_found') throw new Error('error envelope');
  });
  t('demo websocket: hello → welcome, rpc, chat.send streams a run', () => new Promise((resolve, reject) => {
    const ws = new WebSocket('ws://demo/api/link');
    const seen = [];
    ws.onopen = () => ws.send(JSON.stringify({ v: '1.0', id: 'h1', type: 'hello', payload: { token: 'demo-token' } }));
    ws.onmessage = (m) => {
      const env = JSON.parse(m.data);
      seen.push(env.type === 'event' ? env.payload.topic : env.type);
      if (env.type === 'welcome') ws.send(JSON.stringify({ v: '1.0', id: 'r1', type: 'rpc.request', payload: { method: 'chat.send', params: { text: '年糕', conversation_id: 'conv_cat' } } }));
      if (env.type === 'event' && env.payload.topic === 'run.completed') {
        ws.close();
        const msgs = DEMO.H['conversations.messages']({ id: 'conv_cat' }).messages;
        if (!seen.includes('rpc.response') || !seen.includes('run.delta') || msgs[msgs.length - 1].role !== 'assistant') reject(new Error('stream: ' + seen.join(',')));
        else resolve();
      }
    };
    setTimeout(() => reject(new Error('timeout: ' + seen.join(','))), 8000);
  }));
  globalThis.__DEMO_SMOKE_DONE = Promise.all(pending).then(() => {
    console.error = origError;
    const failed = results.filter((r) => !r.ok);
    console.log(`DEMO SMOKE: ${results.length - failed.length}/${results.length} passed, rpc methods rendered: ${used.size}`);
    failed.forEach((r) => console.log('FAIL', r.name, '\n   ', r.error));
    console.log('React warnings (unique):', warnings.size);
    for (const [k, n] of warnings) console.log(`  ${n}× ${k}`);
  });
})();
