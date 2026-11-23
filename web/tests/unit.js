/* Unit tests for logic that the render smoke test cannot reach: Link (fake WebSocket), store
   reducers, markdown, schedule helpers, the force graph (on a real node-canvas) and PNG cards. */
(function () {
  // the sandbox realm persists between runs: drop browser globals a previous demo-dom run left behind
  ['document', 'location', 'history', 'window', 'WHALE_HASH_ROUTES', '__WHALE_DEMO', '__whaleReq'].forEach((k) => { try { delete globalThis[k]; } catch { /* ignore */ } });
  const results = [];
  const pending = [];
  const fail = (name, e) => results.push({ name, ok: false, error: e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : String(e) });
  const t = (name, fn) => {
    try {
      const r = fn();
      if (r && typeof r.then === 'function') pending.push(r.then(() => results.push({ name, ok: true }), (e) => fail(name, e)));
      else results.push({ name, ok: true });
    } catch (e) { fail(name, e); }
  };
  const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg || 'not equal'}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`); };
  const ok = (v, msg) => { if (!v) throw new Error(msg || 'assertion failed'); };

  // -- fake browser ---------------------------------------------------------------------------
  const ls = new Map();
  globalThis.localStorage = { getItem: (k) => (ls.has(k) ? ls.get(k) : null), setItem: (k, v) => ls.set(k, String(v)), removeItem: (k) => ls.delete(k) };
  globalThis.location = { protocol: 'http:', host: 'localhost:8080', href: 'http://localhost:8080/?token=abc&x=1#h', pathname: '/', search: '?token=abc&x=1', hash: '#h' };
  let replaced = '';
  globalThis.history = { replaceState: (_, __, url) => { replaced = url; }, pushState() {} };
  const sockets = [];
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    close(code = 1000) { this.readyState = 3; if (this.onclose) this.onclose({ code }); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    recv(env) { this.onmessage && this.onmessage({ data: JSON.stringify(env) }); }
  }
  globalThis.WebSocket = FakeWS;

  const api = __req('app/lib/api.js');
  const md = __req('app/lib/markdown.js');
  const fmt = __req('app/lib/format.js');

  t('consumeUrlToken stores and strips ?token=', () => {
    ok(api.consumeUrlToken(), 'should consume');
    eq(api.getToken(), 'abc');
    eq(replaced, '/?x=1#h');
  });

  t('Link: hello frame, welcome → online, events by glob, rpc over WS', () => {
    const link = new api.Link({ token: () => 'tok' });
    const states = [];
    link.onState((s) => states.push(s));
    const got = [];
    link.on('run.*', (ev) => got.push(ev.topic));
    link.on(['memory.created', 'notify'], (ev) => got.push('m:' + ev.topic));
    link.connect();
    const ws = sockets[sockets.length - 1];
    eq(ws.url, 'ws://localhost:8080/api/link');
    ws.open();
    const hello = ws.sent[0];
    eq([hello.v, hello.type, hello.payload.token, hello.payload.role, hello.payload.name, hello.payload.subscribe], ['1.0', 'hello', 'tok', 'console', 'Web 控制台', ['*']]);
    ws.recv({ type: 'welcome', corr: hello.id, payload: { client_id: 'cli_1', protocol: '1.0' } });
    eq(link.state, 'online');
    ws.recv({ type: 'event', payload: { topic: 'run.step', data: {}, event_id: 'e1' } });
    ws.recv({ type: 'event', payload: { topic: 'runx.step', data: {} } });
    ws.recv({ type: 'event', payload: { topic: 'memory.created', data: {} } });
    eq(got, ['run.step', 'm:memory.created']);
    let result = null; let error = null;
    link.call('chat.send', { text: 'hi' }).then((r) => { result = r; });
    link.call('x.y').catch((e) => { error = e; });
    const r1 = ws.sent[1]; const r2 = ws.sent[2];
    eq([r1.type, r1.payload.method, r1.payload.params.text], ['rpc.request', 'chat.send', 'hi']);
    ws.recv({ type: 'rpc.response', corr: r1.id, payload: { ok: true, result: { accepted: true } } });
    ws.recv({ type: 'rpc.response', corr: r2.id, payload: { ok: false, error: { code: 'not_found', message: 'no' } } });
    return Promise.resolve().then(() => {}).then(() => {
      ok(result && result.accepted, 'rpc result');
      ok(error && error.code === 'not_found', 'rpc error');
      link.close();
    });
  });

  t('Link: server ping answered, server rpc.request refused politely', () => {
    const link = new api.Link({ token: () => 'tok' });
    link.connect();
    const ws = sockets[sockets.length - 1];
    ws.open();
    ws.recv({ type: 'welcome', payload: {} });
    ws.recv({ type: 'ping', id: 'p1', payload: {} });
    ws.recv({ type: 'rpc.request', id: 'q1', payload: { method: 'capability.invoke' } });
    const pong = ws.sent.find((e) => e.type === 'pong');
    const resp = ws.sent.find((e) => e.type === 'rpc.response');
    eq([pong.corr, resp.corr, resp.payload.ok], ['p1', 'q1', false]);
    link.close();
  });

  t('Link: drop → reconnecting with backoff; 4401 → unauthorized, no retry', () => {
    const link = new api.Link({ token: () => 'tok' });
    link.connect();
    let ws = sockets[sockets.length - 1];
    ws.open(); ws.recv({ type: 'welcome', payload: {} });
    ws.close(1006);
    eq(link.state, 'reconnecting');
    ok(link.nextRetryAt > Date.now(), 'retry scheduled');
    ok(link.attempt === 1, 'attempt counted');
    link.retryNow();
    ws = sockets[sockets.length - 1];
    ws.open();
    ws.close(4401);
    eq(link.state, 'unauthorized');
    ok(link.stopped, 'stopped after 4401');
    link.close();
  });

  t('Link: no token → unauthorized without opening a socket', () => {
    const n = sockets.length;
    const link = new api.Link({ token: () => '' });
    link.connect();
    eq([link.state, sockets.length], ['unauthorized', n]);
  });

  t('store: reducers fold live events', () => {
    const { store, link } = __req('app/lib/store.js');
    store.set({ token: 'tok' });
    link.connect();
    const ws = sockets[sockets.length - 1];
    ws.open();
    // subscribe the store reducer like startLive() does (without snapshot RPCs)
    const s = __req('app/lib/store.js');
    const fire = (topic, data) => ws.recv({ type: 'event', payload: { topic, data, event_id: topic + Math.random() } });
    // startLive registers link.on('*', reduce); emulate by calling it once with a stubbed snapshot
    globalThis.fetch = () => Promise.reject(new Error('offline in tests'));
    s.startLive();
    ws.recv({ type: 'welcome', payload: {} });
    fire('agent.state', { phase: 'acting', active: [{ run_id: 'r1' }] });
    fire('approval.requested', { id: 'a1', run_id: 'r1', capability: 'qq.send', title: '发 QQ' });
    fire('run.started', { run: { id: 'r1', status: 'running', mode: 'chat', input: 'hi', conversation_id: 'c1', created_at: 1 } });
    fire('run.step', { step: { id: 's1', run_id: 'r1', seq: 1, kind: 'recall', status: 'running' } });
    fire('run.step', { step: { id: 's1', run_id: 'r1', seq: 1, kind: 'recall', status: 'done', title: '回忆到 1 条' } });
    fire('run.step', { step: { id: 's2', run_id: 'r1', seq: 2, kind: 'think', status: 'running' } });
    fire('run.delta', { run_id: 'r1', conversation_id: 'c1', delta: '你' });
    fire('run.delta', { run_id: 'r1', conversation_id: 'c1', delta: '好' });
    fire('run.plan', { run_id: 'r1', plan: [{ step: 'a', status: 'done' }] });
    fire('approval.resolved', { id: 'a1', status: 'approved' });
    fire('run.completed', { run: { id: 'r1', status: 'completed', output: '你好' } });
    fire('notify', { title: '鲸鱼', body: '早' });
    const st = store.get();
    eq(st.agent.phase, 'acting');
    eq(st.approvals.length, 0, 'approval removed after resolve');
    const rv = st.runs.r1;
    eq([rv.run.status, rv.run.input, rv.text, rv.steps.length, rv.steps[0].status, rv.plan.length], ['completed', 'hi', '你好', 2, 'done', 1]);
    ok(st.toasts.some((x) => x.kind === 'notify'), 'notify toast');
    ok(st.activity.some((a) => a.topic === 'run.completed') && !st.activity.some((a) => a.topic === 'run.delta'), 'activity filtering');
    eq(s.findRun((r) => r.input === 'hi').id, 'r1');
    link.close();
  });

  t('markdown: blocks and inline', () => {
    const b = md.parseBlocks('# T\n\n段落一\n第二行\n\n- a\n- b\n1. x\n2. y\n> q\n\n```py\ncode\n```\n---\n```\nopen');
    eq(b.map((x) => x.type), ['h', 'p', 'ul', 'ol', 'quote', 'code', 'hr', 'code']);
    eq([b[5].lang, b[5].text, b[5].open, b[7].open], ['py', 'code', false, true]);
    eq(md.safeUrl('javascript:alert(1)'), '');
    eq(md.safeUrl('https://a.b'), 'https://a.b');
    const parts = md.inline('**粗 [l](https://x.y)** 和 `c` https://q.r/s。');
    ok(parts.length >= 4, 'inline parts');
  });

  t('format: times, scopes, spec helpers', () => {
    const now = Date.now();
    eq(fmt.timeAgo(now - 5000, now), '刚刚');
    eq(fmt.timeAgo(now - 3 * 60000, now), '3 分钟前');
    eq(fmt.timeUntil(now + 90 * 60000, now), '1 小时 30 分后');
    eq([fmt.scopeCat('owner'), fmt.scopeCat('shared'), fmt.scopeCat('group:1'), fmt.scopeCat('alice')], ['owner', 'shared', 'group', 'person']);
    eq([fmt.scopeLabel('group:123'), fmt.scopeLabel('alice'), fmt.scopeLabel('shared')], ['群聊 123', '私聊 alice', '共享']);
    eq(fmt.fmtDuration(812), '812 ms');
    eq(fmt.providerGroup('plugin:qweather'), 'plugin');
    const tasks = __req('app/pages/tasks.js');
    eq(tasks.buildSchedule({ mode: 'daily', time: '07:05' }), { cron: '5 7 * * *' });
    eq(tasks.buildSchedule({ mode: 'every', every: '45' }), { every_min: 45 });
    eq(tasks.buildSchedule({ mode: 'none' }), null);
    const future = new Date(Date.now() + 86400000);
    const local = `${future.getFullYear()}-${String(future.getMonth() + 1).padStart(2, '0')}-${String(future.getDate()).padStart(2, '0')}T08:00`;
    const at = tasks.buildSchedule({ mode: 'once', at: local }).at;
    eq(new Date(at).getHours(), 8);
    let threw = false; try { tasks.buildSchedule({ mode: 'once', at: '2001-01-01T00:00' }); } catch { threw = true; }
    ok(threw, 'past time rejected');
    eq([tasks.describeSpec({ cron: '5 7 * * *' }), tasks.describeSpec({ every_min: 30 }), tasks.describeSpec({ cron: '0 8 * * 1-5' })], ['每天 07:05', '每 30 分钟', '工作日 08:00']);
  });

  t('graph: force layout converges, hit-test and fit work (node-canvas)', () => {
    const { createCanvas } = require('canvas');
    const real = createCanvas(800, 600);
    const canvas = {
      getContext: (k) => real.getContext(k), width: 800, height: 600, style: {},
      getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
      addEventListener() {}, removeEventListener() {}, setPointerCapture() {},
    };
    globalThis.getComputedStyle = () => ({ getPropertyValue: (n) => (n === '--k-fact' ? '#38bdf8' : ''), fontFamily: 'sans-serif' });
    let frames = 0;
    globalThis.requestAnimationFrame = () => ++frames;
    globalThis.cancelAnimationFrame = () => {};
    const { ForceGraph } = __req('app/lib/graph.js');
    const g = new ForceGraph(canvas, {});
    g.setData(MOCK.memories, MOCK.R['memory.graph']().edges);
    for (let i = 0; i < 400; i++) g.tick();
    ok(g.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)), 'finite positions');
    const d = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    for (let i = 0; i < g.nodes.length; i++) for (let j = i + 1; j < g.nodes.length; j++) ok(d(g.nodes[i], g.nodes[j]) > 4, 'nodes do not overlap');
    g.draw();
    g.fit();
    const n = g.nodes[0];
    const sx = n.x * g.view.k + g.w / 2 + g.view.x; const sy = n.y * g.view.k + g.h / 2 + g.view.y;
    eq(g.hit(sx, sy)?.id, n.id, 'hit test');
    g.setFilter((m) => m.kind === 'fact'); g.select('mem_1'); g.draw();
    g.zoomBy(1.5); g.destroy();
  });

  t('persona: Character Card from PNG tEXt chunk', () => {
    const { cardFromPng } = __req('app/pages/persona.js');
    const json = JSON.stringify({ spec: 'chara_card_v2', data: { name: '小鲸' } });
    const b64 = Buffer.from(json, 'utf8').toString('base64');
    const text = Buffer.concat([Buffer.from('chara\0', 'latin1'), Buffer.from(b64, 'latin1')]);
    const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); return Buffer.concat([len, Buffer.from(type, 'latin1'), data, Buffer.alloc(4)]); };
    const png = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', Buffer.alloc(13)), chunk('tEXt', text), chunk('IEND', Buffer.alloc(0))]);
    globalThis.atob = globalThis.atob || ((s) => Buffer.from(s, 'base64').toString('latin1'));
    const ab = png.buffer.slice(png.byteOffset, png.byteOffset + png.byteLength);
    eq(cardFromPng(ab).data.name, '小鲸');
  });

  // -- 2026-11-24 ------------------------------------------------------------------------------------
  t('mcp-graph: object, array, flat list and JSONL forms', () => {
    const { parseMcpGraph } = __req('app/lib/mcp-graph.js');
    const obj = parseMcpGraph(JSON.stringify({ entities: [{ name: 'Alice', entityType: 'person', observations: ['喜欢跑团'] }, { name: 'Whale', entityType: 'project', observations: [] }],
      relations: [{ from: 'Alice', to: 'Whale', relationType: 'uses' }] }));
    eq([obj.entities.length, obj.relations.length, obj.types], [2, 1, { person: 1, project: 1 }]);
    const arr = parseMcpGraph(JSON.stringify([{ entities: [{ name: 'A', entityType: 't', observations: ['x'] }] }, { entities: [{ name: 'A', entityType: 't', observations: ['x', 'y'] }], relations: [{ from: 'A', to: 'B', relationType: 'r' }] }]));
    eq(arr.entities.find((e) => e.name === 'A').observations, ['x', 'y'], 'observations merged without duplicates');
    eq([arr.implicit, arr.entities.find((e) => e.name === 'B').entityType], [1, '（未声明）'], 'implicit entity from relation');
    const flat = parseMcpGraph(JSON.stringify([{ name: 'P', entityType: 'person', observations: 'single' }, { from: 'P', to: 'Q', relationType: 'knows' }]));
    eq([flat.entities.length, flat.relations.length, flat.entities[0].observations], [2, 1, ['single']]);
    const jsonl = parseMcpGraph(['{"type":"entity","name":"Alice","entityType":"person","observations":["a"]}', '', 'not json', '{"type":"relation","from":"Alice","to":"Bob","relationType":"knows"}',
      '{"type":"relation","from":"Alice","to":"Bob","relationType":"knows"}', '{"type":"weird"}'].join('\r\n'));
    eq([jsonl.entities.length, jsonl.relations.length, jsonl.skipped], [2, 1, 2], 'jsonl: dedupe relations, count skipped lines');
    const bom = parseMcpGraph('\uFEFF{"entities":[{"name":"X","entityType":"t"}]}');
    eq(bom.entities[0].observations, []);
    let threw = 0;
    for (const bad of ['', '   ', '{"relations":[]}', 'hello\nworld', '[1,2,3]']) { try { parseMcpGraph(bad); } catch { threw++; } }
    eq(threw, 5, 'empty / no entities / garbage rejected');
  });

  t('mcp-graph: preview keeps the best-connected 1000 and says it truncated', () => {
    const { parseMcpGraph, toPreview, typePalette, MAX_PREVIEW_NODES } = __req('app/lib/mcp-graph.js');
    const entities = Array.from({ length: 1500 }, (_, i) => ({ name: 'n' + i, entityType: 'k' + (i % 3), observations: [] }));
    const relations = Array.from({ length: 40 }, (_, i) => ({ from: 'n1499', to: 'n' + i, relationType: 'r' }));
    const g = parseMcpGraph(JSON.stringify({ entities, relations }));
    const p = toPreview(g, MAX_PREVIEW_NODES);
    eq([p.nodes.length, p.truncated, p.total, p.nodes[0].id], [1000, true, 1500, 'n1499'], 'hub first');
    ok(p.edges.every((e) => p.nodes.some((n) => n.id === e.src) && p.nodes.some((n) => n.id === e.dst)), 'edges only between drawn nodes');
    ok(p.nodes.every((n) => n.importance > 0 && n.importance <= 1), 'importance in (0,1]');
    const small = toPreview(parseMcpGraph('{"entities":[{"name":"a","entityType":"x"}]}'));
    eq([small.truncated, small.shown], [false, 1]);
    const pal = typePalette(['b', 'a', '（未声明）']);
    eq(pal, typePalette(['a', '（未声明）', 'b']), 'palette independent of input order');
    ok(/^hsl\(/.test(pal.a) && pal.a !== pal.b, 'distinct colours');
  });

  t('star3d: rotation, projection, sphere and depth math', () => {
    const s3 = __req('app/lib/star3d.js');
    const near = (a, b, e = 1e-9) => Math.abs(a - b) < e;
    const p = { x: 1, y: 2, z: 3 };
    const r0 = s3.rotate(p, 0, 0);
    ok(near(r0.x, 1) && near(r0.y, 2) && near(r0.z, 3), 'identity');
    const ry = s3.rotate({ x: 1, y: 0, z: 0 }, Math.PI / 2, 0);
    ok(near(ry.x, 0) && near(ry.z, -1), 'yaw 90° maps +x to -z: ' + JSON.stringify(ry));
    const rp = s3.rotate({ x: 0, y: 1, z: 0 }, 0, Math.PI / 2);
    ok(near(rp.y, 0) && near(rp.z, 1), 'pitch 90° maps +y to +z: ' + JSON.stringify(rp));
    const rr = s3.rotate(p, 0.7, -0.4);
    ok(near(Math.hypot(rr.x, rr.y, rr.z), Math.hypot(1, 2, 3)), 'rotation preserves length');
    const back = s3.rotate(s3.rotate(p, 0.9, 0), -0.9, 0);
    ok(near(back.x, 1) && near(back.z, 3), 'yaw inverse');
    const cam = { w: 800, h: 600, dist: 500, focal: 400, zoom: 1, near: 10 };
    const c = s3.project({ x: 0, y: 0, z: 0 }, cam);
    eq([c.x, c.y, c.s, c.depth], [400, 300, 0.8, 500], 'origin projects to centre');
    const front = s3.project({ x: 100, y: 0, z: -100 }, cam); const rear = s3.project({ x: 100, y: 0, z: 100 }, cam);
    ok(front.s > rear.s && front.x - 400 > rear.x - 400, 'nearer is larger and farther from centre');
    eq(s3.project({ x: 0, y: 0, z: -495 }, cam), null, 'behind the near plane → null');
    const zoomed = s3.project({ x: 100, y: 50, z: 0 }, { ...cam, zoom: 2 });
    ok(near(zoomed.x - 400, 2 * (s3.project({ x: 100, y: 50, z: 0 }, cam).x - 400)), 'zoom scales linearly');
    const pts = s3.fibonacciSphere(200, 5);
    eq(pts.length, 200);
    ok(pts.every((q) => near(Math.hypot(q.x, q.y, q.z), 5, 1e-6)), 'points on the sphere');
    const cy = pts.reduce((a, q) => a + q.y, 0) / pts.length;
    ok(Math.abs(cy) < 0.05, 'balanced');
    eq(s3.fibonacciSphere(1, 3), [{ x: 3, y: 0, z: 0 }]);
    ok(s3.depthAlpha(100, 100, 900) === 1 && near(s3.depthAlpha(900, 100, 900, 0.2), 0.2) && s3.depthAlpha(500, 100, 900) < 1, 'depth alpha');
    const nodes = MOCK.memories.map((m) => ({ id: m.id, kind: m.kind, importance: m.importance }));
    const edges = MOCK.R['memory.graph']({}).edges;
    const L1 = s3.layout3d(nodes, edges); const L2 = s3.layout3d(nodes, edges);
    ok(L1.every((q) => [q.x, q.y, q.z].every(Number.isFinite)), 'finite layout');
    eq(L1, L2, 'deterministic');
    for (let i = 0; i < L1.length; i++) for (let j = i + 1; j < L1.length; j++) ok(Math.hypot(L1[i].x - L1[j].x, L1[i].y - L1[j].y, L1[i].z - L1[j].z) > 2, 'stars do not coincide');
  });

  t('star3d: StarField renders on canvas, hit-tests, filters, pauses when hidden', () => {
    const { createCanvas } = require('canvas');
    const real = createCanvas(800, 600);
    const listeners = {};
    const canvas = { getContext: (k) => real.getContext(k), width: 800, height: 600, style: {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
      addEventListener(n, f) { listeners[n] = f; }, removeEventListener(n) { delete listeners[n]; }, setPointerCapture() {} };
    globalThis.getComputedStyle = () => ({ getPropertyValue: () => '', fontFamily: 'sans-serif' });
    const frames = [];
    globalThis.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
    globalThis.cancelAnimationFrame = () => {};
    const docListeners = {};
    globalThis.document = { hidden: false, addEventListener: (n, f) => { docListeners[n] = f; }, removeEventListener() {} };
    const { StarField } = __req('app/lib/star3d.js');
    let clicked = null;
    const f = new StarField(canvas, { onClick: (n) => { clicked = n; } });
    const g = MOCK.G;
    const src = g.graphSources(MOCK.memories);
    f.setData([...MOCK.memories, ...src.extra_nodes], [...MOCK.R['memory.graph']({}).edges, ...src.extra_edges.map((e) => ({ ...e, extra: true }))]);
    eq(f.stats.nodes, MOCK.memories.length + src.extra_nodes.length);
    const yaw0 = f.yaw;
    frames.shift()(16); frames.shift()(32);
    ok(f.yaw > yaw0, 'auto-rotates');
    ok(f.stats.drawn > 0 && f.stats.fps > 0, 'drew and measured fps');
    const n = f.nodes.find((x) => x.q);
    eq(f.hit(n.q.x, n.q.y)?.id !== undefined, true, 'hit something at a projected star');
    listeners.pointerdown({ clientX: n.q.x, clientY: n.q.y, pointerId: 1 });
    listeners.pointerup({ clientX: n.q.x, clientY: n.q.y, pointerId: 1 });
    ok(clicked && clicked.id, 'click selects a node');
    listeners.pointerdown({ clientX: 100, clientY: 100, pointerId: 2 });
    listeners.pointermove({ clientX: 200, clientY: 140, pointerId: 2 });
    listeners.pointerup({ clientX: 200, clientY: 140, pointerId: 2 });
    ok(Math.abs(f.yaw - (yaw0 + 0.6)) < 0.2, 'drag rotates');
    f.zoomBy(10); eq(f.zoom, 4, 'zoom clamped');
    f.setFilter((m) => m.kind === 'fact'); f.select('mem_1'); f.draw();
    globalThis.document.hidden = true; docListeners.visibilitychange();
    const y1 = f.yaw; frames.shift()(48); frames.shift()(64);
    eq(f.yaw, y1, 'paused while hidden');
    const rm = new StarField(canvas, { reduceMotion: true });
    rm.setData(MOCK.memories, []);
    eq(rm.spin, false, 'no auto-rotation under reduced motion');
    f.destroy(); rm.destroy();
    delete globalThis.document;
  });

  t('graph: source nodes (person / evidence / space) draw and hit-test', () => {
    const { createCanvas } = require('canvas');
    const real = createCanvas(800, 600);
    const canvas = { getContext: (k) => real.getContext(k), width: 800, height: 600, style: {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
      addEventListener() {}, removeEventListener() {}, setPointerCapture() {} };
    globalThis.requestAnimationFrame = () => 1;
    const { ForceGraph } = __req('app/lib/graph.js');
    const { graphData } = __req('app/components/memory-graph.js');
    const gd = graphData(MOCK.R['memory.graph']({ sources: true }));
    ok(gd.nodes.some((n) => n.category === 'person') && gd.nodes.some((n) => n.category === 'evidence') && gd.nodes.some((n) => n.category === 'space'), 'all three categories present');
    const g = new ForceGraph(canvas, { reduceMotion: true });
    g.setData(gd.nodes, gd.edges);
    ok(g.edges.some((e) => e.extra && e.relation === '依据'), 'extra edges flagged');
    const person = g.nodes.find((n) => n.category === 'person');
    g.hover = person; g.draw();
    g.fit();
    const sx = person.x * g.view.k + g.w / 2 + g.view.x; const sy = person.y * g.view.k + g.h / 2 + g.view.y;
    ok(g.hit(sx, sy), 'hit test near a person node');
    g.destroy();
  });

  t('groups: roster validation', () => {
    const { validateRoster, newRow } = __req('app/components/group-form.js');
    const r = (name, accounts, aliases = []) => newRow({ name, accounts, aliases });
    ok(validateRoster([r('小王', ['1234567'])], []).ok, 'one valid member');
    eq(validateRoster([], [r('骰娘', ['9988776'])]).message, '至少添加一位群友');
    eq(validateRoster([newRow()], []).message, '至少添加一位群友', 'empty rows ignored');
    const dup = validateRoster([r('小王', ['1234567']), r('小王 ', ['7654321'])], []);
    ok(!dup.ok && Object.values(dup.rows)[0].includes('重复'), 'duplicate names (trimmed)');
    const dupBot = validateRoster([r('Bot', ['1234567'])], [r('bot', ['7654321'])]);
    ok(!dupBot.ok, 'names unique across members and bots (case-insensitive)');
    const acc = validateRoster([r('A', ['1234567']), r('B', ['1234567'])], []);
    ok(!acc.ok && Object.values(acc.rows)[0].includes('已经属于'), 'accounts unique');
    ok(!validateRoster([r('A', ['12ab'])], []).ok, 'QQ numbers only');
    ok(!validateRoster([r('A', [])], []).ok, 'needs an account');
    ok(!validateRoster([r('', ['1234567'])], []).ok, 'needs a name');
  });

  t('router: hash mode for file:// pages', () => {
    const hooks = __req('app/lib/hooks.js');
    eq(hooks.parseRoute('/groups/qq%3A745812309?tab=members&member=1').id, 'qq:745812309');
    eq(hooks.parseRoute('/setup').page, 'setup');
  });

  globalThis.__UNIT_DONE = Promise.all(pending).then(() => {
    const failed = results.filter((r) => !r.ok);
    console.log(`UNIT: ${results.length - failed.length}/${results.length} passed`);
    failed.forEach((r) => console.log('FAIL', r.name, '\n   ', r.error));
  });
})();
