/* Boot the real demo (real Preact + htm from vendor, demo runtime, fake fetch/WebSocket) on MiniDOM
   and walk every page. Built by tests/demo.py → tests/out/demo-dom.js. Fails on page crashes
   (error boundary text), visible undefined/NaN, [object Object], console errors, or no live link. */
(async function () {
  const RUN = globalThis.__DEMO_RUN;
  const errors = [];
  const origError = console.error;
  console.error = (...a) => { errors.push(a.map((x) => (x && x.stack) || String(x)).join(' ').slice(0, 300)); };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const req = globalThis.__whaleReq;
  const hooks = req('app/lib/hooks.js');
  const { store } = req('app/lib/store.js');
  const PAGES = globalThis.__WHALE_DEMO.PAGES;
  const D = globalThis.__WHALE_DEMO.D;
  const app = document.getElementById('app');
  const results = [];
  const find = (n, pred) => { if (pred(n)) return n; for (const c of n.childNodes || []) { const r = find(c, pred); if (r) return r; } return null; };
  const count = (n, pred) => (pred(n) ? 1 : 0) + (n.childNodes || []).reduce((s, c) => s + count(c, pred), 0);
  await sleep(2500);
  results.push({ name: 'live link online after hello/welcome', ok: store.get().conn === 'online', info: store.get().conn });
  results.push({ name: 'snapshot loaded (status, activity, setup)', ok: !!store.get().status && store.get().activity.length > 5 && store.get().setup?.completed === true, info: `${store.get().activity.length} activity` });
  // [name, path, text that proves the page's data arrived]
  const EXPECT = { bridge: '实时活动', chat: '28 克', memory: '有效记忆', groups: '明确说过的喜好', tasks: '子 Agent', capabilities: '和风天气', devices: '鲸鱼的手机', persona: '深海学者',
    settings: '允许局域网里的手机连接', setup: '给它起个名字' };
  const routes = Object.entries(PAGES).map(([n, p]) => [n, p, EXPECT[n]]).concat([['memory 3d+sources', '/memory', '星空'], ['memory detail', '/memory/mem_gp_2345678', '依据'],
    ['group keywords', '/groups/qq%3A745812309?tab=keywords', '火锅'], ['group timeline', '/groups/qq%3A745812309', '展开整段'], ['daily phone', '/devices/' + D.DAILY_PHONE, '隐私'],
    ['settings service', '/settings/service', 'http://192.168.1.23:8080']]);
  for (const [name, path, expect] of routes) {
    if (name === 'memory 3d+sources') { localStorage.setItem('whale.ui.memory.dim', '"3d"'); localStorage.setItem('whale.ui.memory.sources', 'true'); hooks.navigate('/'); await sleep(200); }
    hooks.navigate(path);
    await sleep(1400);
    const text = app.textContent.replace(/\s+/g, ' ');
    const problems = [];
    if (expect && !text.includes(expect)) problems.push(`missing 「${expect}」 (${text.length} chars): …${text.slice(-100)}`);
    if (text.length < 200) problems.push('little text');
    if (text.includes('这个页面出错了')) problems.push('error boundary shown');
    const bad = text.match(/.{0,30}\b(undefined|NaN)\b.{0,30}/);
    if (bad) problems.push('visible ' + bad[0]);
    if (text.includes('[object Object]')) problems.push('[object Object]');
    const canvases = count(app, (n) => n.localName === 'canvas');
    results.push({ name: `${name} ${path}`, ok: !problems.length, info: problems.join('; ') || `${text.length} chars${canvases ? `, ${canvases} canvas` : ''}` });
  }
  // an interaction through real Preact event wiring: open the 群聊 page and press 更新
  hooks.navigate('/groups/qq%3A745812309');
  await sleep(1200);
  const btn = find(app, (n) => n.localName === 'button' && /^\s*更新\s*$/.test(n.textContent));
  if (btn) { btn.click(); await sleep(2600); }
  const toasted = store.get().toasts.some((t) => /整理完成/.test(t.title));
  results.push({ name: 'click 更新 → groups.refresh → toast', ok: !!btn && toasted, info: btn ? store.get().toasts.map((t) => t.title).join(' | ') : 'button not found' });
  // chat: send a message, watch the run stream in
  hooks.navigate('/chat/conv_cat');
  await sleep(1000);
  const { sendChat } = req('app/lib/store.js');
  await sendChat({ text: '年糕今天吃什么', conversation_id: 'conv_cat', wait: false });
  await sleep(4500);
  const last = (D.messages.conv_cat || []).slice(-1)[0];
  results.push({ name: 'chat.send over WS streams a run into the page', ok: last?.role === 'assistant' && app.textContent.includes('演示模式'), info: last?.content?.slice(0, 40) });
  console.error = origError;
  if (RUN !== globalThis.__DEMO_RUN) return; // a newer run took over this realm
  const failed = results.filter((r) => !r.ok);
  const errs = errors.filter((e) => !/act\(|not wrapped|ResizeObserver/.test(e));
  console.log(`DEMO DOM: ${results.length - failed.length}/${results.length} passed · console errors: ${errs.length}`);
  results.forEach((r) => console.log(r.ok ? '  ok  ' : '  FAIL', r.name, '—', r.info));
  errs.slice(0, 8).forEach((e) => console.log('  console.error:', e));
  globalThis.__DEMO_DOM_DONE = RUN;
})();
