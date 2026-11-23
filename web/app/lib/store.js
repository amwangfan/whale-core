// @ts-check
/**
 * Tiny global store: one immutable state object, selector hooks, and the live
 * wiring that folds Link events into it (agent state, approvals, runs, toasts,
 * activity). Pages keep their own local data and subscribe to topics with
 * `useTopic`; only cross-page state lives here.
 */
import { useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { Link, rpc, errorText, getToken, onUnauthorized, clearToken } from './api.js';

/**
 * @typedef {{run: any, steps: any[], text: string, plan: any[], progress: string, children: any[], loadedAt?: number}} RunView
 * @typedef {{id: string, kind: 'info'|'ok'|'warn'|'error'|'notify', title: string, body?: string, action?: {label: string, href?: string, run?: Function}, timeout?: number}} Toast
 * @typedef {{id: string, topic: string, ts: number, data: any}} Activity
 */

const initial = {
  token: getToken(),
  /** @type {import('./api.js').LinkState} */ conn: 'idle',
  connInfo: /** @type {any} */ (null),
  status: /** @type {any} */ (null),
  agent: { phase: 'idle', active: [] },
  approvals: /** @type {any[]} */ ([]),
  /** @type {Record<string, RunView>} */ runs: {},
  /** @type {Activity[]} */ activity: [],
  /** @type {Toast[]} */ toasts: [],
  theme: 'auto',
  reduceMotion: false,
  paletteOpen: false,
  trayOpen: false,
  navOpen: false,
  authReason: '',
  /** setup.status result; null until loaded */ setup: /** @type {any} */ (null),
  /** the owner chose 「稍后再说」 in the first-run wizard */ setupLater: readLater(),
};

const micro = globalThis.queueMicrotask ? (fn) => globalThis.queueMicrotask(fn) : (fn) => Promise.resolve().then(fn);

/** @template S */
function createStore(/** @type {S} */ state) {
  /** @type {Set<() => void>} */
  const listeners = new Set();
  let scheduled = false;
  const flush = () => { scheduled = false; listeners.forEach((fn) => fn()); };
  return {
    get: () => state,
    /** @param {Partial<S> | ((s: S) => Partial<S>)} patch */
    set(patch) {
      const p = typeof patch === 'function' ? patch(state) : patch;
      if (!p) return;
      state = { ...state, ...p };
      if (!scheduled) { scheduled = true; micro(flush); }
    },
    /** @param {() => void} fn */
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  };
}

export const store = createStore(initial);
export const link = new Link({ token: () => store.get().token, name: 'Web 控制台', subscribe: ['*'] });

/** Shallow equality for arrays/objects returned by selectors. */
export function shallow(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const ka = Object.keys(a); const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => Object.is(a[k], b[k]));
}

/**
 * @template T
 * @param {(s: typeof initial) => T} selector
 * @param {(a: T, b: T) => boolean} [eq]
 * @returns {T}
 */
export function useStore(selector, eq = Object.is) {
  const [, force] = useState(0);
  const sel = useRef(selector);
  sel.current = selector;
  const value = selector(store.get());
  const last = useRef(value);
  last.current = value;
  useEffect(() => store.subscribe(() => {
    const next = sel.current(store.get());
    if (!eq(next, last.current)) { last.current = next; force((x) => x + 1); }
  }), []);
  return value;
}

// -- toasts ----------------------------------------------------------------------------------------
let toastSeq = 0;
/** @param {Omit<Toast, 'id'> & {id?: string}} t */
export function toast(t) {
  const id = t.id || `t${++toastSeq}`;
  store.set((s) => ({ toasts: [...s.toasts.filter((x) => x.id !== id), { kind: 'info', timeout: 4500, ...t, id }].slice(-5) }));
  const ms = t.timeout ?? (t.kind === 'error' ? 7000 : t.kind === 'notify' ? 9000 : 4500);
  if (ms > 0) setTimeout(() => dismissToast(id), ms);
  return id;
}
/** @param {string} id */
export function dismissToast(id) { store.set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })); }

/**
 * Run an RPC as a user action: toasts the error (and optional success), returns result or undefined.
 * @param {string} method @param {any} [params] @param {{ok?: string, quiet?: boolean, timeout?: number}} [opts]
 */
export async function act(method, params = {}, opts = {}) {
  try {
    const res = await rpc(method, params, { timeout: opts.timeout });
    if (opts.ok) toast({ kind: 'ok', title: opts.ok });
    return res;
  } catch (e) {
    if (!opts.quiet) toast({ kind: 'error', title: '操作失败', body: errorText(e) });
    throw e;
  }
}
/** Like act() but never throws (returns undefined on error). */
export async function tryAct(method, params = {}, opts = {}) {
  try { return await act(method, params, opts); } catch { return undefined; }
}

/** chat.send over the socket when online (channel "console"), else HTTP. @param {any} params */
export async function sendChat(params) {
  if (link.online) {
    try { return await link.call('chat.send', params, 30000); } catch (e) {
      if (/** @type {any} */ (e).code !== 'unavailable') throw e;
    }
  }
  return rpc('chat.send', params);
}

// -- runs ------------------------------------------------------------------------------------------
const MAX_RUNS = 80;
/** @param {Record<string, RunView>} runs @param {string} id */
function runOf(runs, id) {
  return runs[id] || { run: { id, status: 'running' }, steps: [], text: '', plan: [], progress: '', children: [] };
}
/** @param {Record<string, RunView>} runs */
function prune(runs) {
  const ids = Object.keys(runs);
  if (ids.length <= MAX_RUNS) return runs;
  const done = ids.filter((id) => !['running', 'waiting'].includes(runs[id].run?.status))
    .sort((a, b) => (runs[a].run?.created_at || 0) - (runs[b].run?.created_at || 0));
  const out = { ...runs };
  done.slice(0, ids.length - MAX_RUNS).forEach((id) => delete out[id]);
  return out;
}
/** @param {any[]} steps @param {any} step */
export function upsertStep(steps, step) {
  const i = steps.findIndex((s) => s.id === step.id);
  const next = i >= 0 ? steps.map((s, j) => (j === i ? { ...s, ...step } : s)) : [...steps, step];
  return next.sort((a, b) => (a.seq || 0) - (b.seq || 0));
}
/** @param {string} id @param {(r: RunView) => Partial<RunView>} fn */
function patchRun(id, fn) {
  store.set((s) => {
    const cur = runOf(s.runs, id);
    return { runs: prune({ ...s.runs, [id]: { ...cur, ...fn(cur) } }) };
  });
}

/** Fetch a run with its steps and children and merge into the store. @param {string} id */
export async function loadRun(id) {
  const res = await rpc('runs.get', { id });
  patchRun(id, (cur) => {
    let steps = res.steps || [];
    cur.steps.forEach((s) => { steps = upsertStep(steps, s); });
    return { run: { ...res.run, ...(cur.run?.status && cur.run.status !== 'running' ? cur.run : {}) }, steps,
      children: res.children || [], text: cur.text || '', loadedAt: Date.now() };
  });
  return store.get().runs[id];
}

/** Find a recent run, optionally matching a predicate. @param {(r: any) => boolean} pred */
export function findRun(pred) {
  const all = Object.values(store.get().runs).map((v) => v.run).filter(Boolean);
  return all.sort((a, b) => (b.created_at || 0) - (a.created_at || 0)).find(pred);
}

// -- live wiring -------------------------------------------------------------------------------------
const ACTIVITY_SKIP = /^(run\.delta|run\.progress|run\.status|agent\.state|link\.|clients\.changed|device\.status|device\.trace|capability\.)/;
const MAX_ACTIVITY = 120;

/** @param {import('./api.js').LinkEvent} ev */
function reduce(ev) {
  const { topic, data } = ev;
  if (topic === 'agent.state') store.set({ agent: { phase: data.phase || 'idle', active: data.active || [] } });
  else if (topic === 'approval.requested') {
    store.set((s) => ({ approvals: [data, ...s.approvals.filter((a) => a.id !== data.id)] }));
    toast({ kind: 'warn', title: '需要你的批准', body: data.title || data.capability, action: { label: '查看', run: () => store.set({ trayOpen: true }) } });
  } else if (topic === 'approval.resolved') store.set((s) => ({ approvals: s.approvals.filter((a) => a.id !== data.id) }));
  else if (topic === 'notify') toast({ kind: 'notify', title: data.title || '鲸鱼', body: data.body || '' });
  else if (topic === 'run.started' && data.run) patchRun(data.run.id, (cur) => ({ run: { ...cur.run, ...data.run } }));
  else if (/^run\.(completed|failed|cancelled)$/.test(topic) && data.run) patchRun(data.run.id, (cur) => ({ run: { ...cur.run, ...data.run } }));
  else if (topic === 'run.status') patchRun(data.run_id, (cur) => ({ run: { ...cur.run, status: data.status } }));
  else if (topic === 'run.step' && data.step) patchRun(data.step.run_id, (cur) => ({ steps: upsertStep(cur.steps, data.step) }));
  else if (topic === 'run.delta') patchRun(data.run_id, (cur) => ({ text: cur.text + (data.delta || ''), run: { ...cur.run, conversation_id: cur.run.conversation_id || data.conversation_id } }));
  else if (topic === 'run.plan') patchRun(data.run_id, () => ({ plan: data.plan || [] }));
  else if (topic === 'run.progress') patchRun(data.run_id, () => ({ progress: data.message || '' }));
  else if (topic === 'config.changed' || topic === 'persona.activated') refreshStatusSoon(200);

  if (!ACTIVITY_SKIP.test(topic) && !(topic === 'run.step' && data.step?.status === 'running')) {
    store.set((s) => ({ activity: [{ id: ev.event_id || `${topic}:${ev.ts}`, topic, ts: ev.ts, data }, ...s.activity].slice(0, MAX_ACTIVITY) }));
  }
  if (/^(memory|task|device|capability|plugin|approval|schedule|clients|persona)\./.test(topic) || topic === 'clients.changed') refreshStatusSoon();
}

let statusTimer = 0;
/** Debounced `system.status` refresh. */
export function refreshStatusSoon(ms = 1500) {
  clearTimeout(statusTimer);
  statusTimer = /** @type {any} */ (setTimeout(refreshStatus, ms));
}
export async function refreshStatus() {
  try {
    const status = await rpc('system.status');
    store.set({ status, agent: { phase: status.agent?.phase || 'idle', active: status.agent?.active || [] } });
  } catch { /* connection indicator covers it */ }
}

function readLater() {
  try { return globalThis.localStorage?.getItem('whale.setup.later') === '1'; } catch { return false; }
}
/** Close the first-run wizard without completing it (it stays reachable from 设置 and the palette). */
export function snoozeSetup() {
  try { globalThis.localStorage?.setItem('whale.setup.later', '1'); } catch { /* ignore */ }
  store.set({ setupLater: true });
}
/** Load `setup.status`; an older backend without it never blocks the console. */
export async function refreshSetup() {
  try {
    const setup = await rpc('setup.status');
    store.set({ setup });
    return setup;
  } catch (e) {
    const code = /** @type {any} */ (e)?.code;
    if (code === 'not_found' || code === 'invalid_params') store.set({ setup: { completed: true, unsupported: true, steps: {} } });
    return null;
  }
}

async function snapshot() {
  await refreshStatus();
  refreshSetup();
  try {
    const [ap, ev] = await Promise.all([rpc('approvals.list', { status: 'pending' }), rpc('events.query', { pattern: '*', limit: 80 })]);
    const activity = (ev.events || []).filter((e) => !ACTIVITY_SKIP.test(e.topic) && !(e.topic === 'run.step' && e.data?.step?.status === 'running'))
      .map((e) => ({ id: e.id, topic: e.topic, ts: e.ts, data: e.data }));
    store.set((s) => {
      const seen = new Set(s.activity.map((a) => a.id));
      return { approvals: ap.approvals || [], activity: [...s.activity, ...activity.filter((a) => !seen.has(a.id))].sort((a, b) => b.ts - a.ts).slice(0, MAX_ACTIVITY) };
    });
  } catch { /* partial snapshot is fine */ }
}

let started = false;
export function startLive() {
  if (started) { link.connect(); return; }
  started = true;
  link.onState((conn, info) => {
    store.set({ conn, connInfo: info || null });
    if (conn === 'online') snapshot();
    if (conn === 'unauthorized') logout('令牌被拒绝，请重新输入');
  });
  link.on('*', reduce);
  onUnauthorized(() => logout('令牌无效或已轮换，请重新输入'));
  globalThis.document?.addEventListener?.('visibilitychange', () => {
    if (document.visibilityState === 'visible' && store.get().token && !link.online && link.state !== 'connecting') link.retryNow();
  });
  globalThis.addEventListener?.('online', () => { if (store.get().token) link.retryNow(); });
  link.connect();
}

/** @param {string} [reason] */
export function logout(reason) {
  clearToken();
  link.close();
  started = false;
  store.set({ token: '', status: null, setup: null, approvals: [], activity: [], runs: {}, conn: 'idle', authReason: reason || '' });
}

/**
 * Subscribe to live topics for the lifetime of a component; the handler may change between renders.
 * @param {string|string[]} patterns @param {(ev: import('./api.js').LinkEvent) => void} fn
 */
export function useTopic(patterns, fn) {
  const ref = useRef(fn);
  ref.current = fn;
  const key = Array.isArray(patterns) ? patterns.join('|') : patterns;
  useEffect(() => link.on(key.split('|'), (ev) => ref.current(ev)), [key]);
}

/** Theme: 'auto' follows the OS; persisted in localStorage. @param {'auto'|'light'|'dark'} theme */
export function applyTheme(theme) {
  const root = globalThis.document?.documentElement;
  try { globalThis.localStorage?.setItem('whale.theme', theme); } catch { /* ignore */ }
  store.set({ theme });
  if (!root) return;
  root.dataset.theme = theme;
  const mq = globalThis.matchMedia?.('(prefers-color-scheme: light)');
  root.classList.toggle('prefers-light', theme === 'auto' && !!mq?.matches);
}
export function initTheme() {
  let theme = 'auto';
  try { theme = globalThis.localStorage?.getItem('whale.theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(/** @type {any} */ (theme));
  globalThis.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', () => applyTheme(store.get().theme));
  const rm = globalThis.matchMedia?.('(prefers-reduced-motion: reduce)');
  store.set({ reduceMotion: !!rm?.matches });
  rm?.addEventListener?.('change', () => store.set({ reduceMotion: rm.matches }));
}
