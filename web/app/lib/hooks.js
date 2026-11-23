// @ts-check
/** Small hooks shared by pages: data loading, timers, media queries, history routing. */
import { useCallback, useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { rpc } from './api.js';

/**
 * Load an RPC result; re-runs when `deps` change. `setData` allows optimistic/live patches.
 * @param {string|null} method  null → skip
 * @param {any} [params]
 * @param {any[]} [deps]
 */
export function useRpc(method, params = {}, deps = []) {
  const [state, setState] = useState(/** @type {{data: any, error: any, loading: boolean}} */ ({ data: undefined, error: null, loading: !!method }));
  const seq = useRef(0);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const reload = useCallback(async (silent = false) => {
    if (!method) return undefined;
    const mine = ++seq.current;
    if (!silent) setState((s) => ({ ...s, loading: true }));
    try {
      const data = await rpc(method, paramsRef.current);
      if (mine === seq.current) setState({ data, error: null, loading: false });
      return data;
    } catch (error) {
      if (mine === seq.current) setState((s) => ({ ...s, error, loading: false }));
      return undefined;
    }
  }, [method, ...deps]);
  useEffect(() => { reload(); }, [reload]);
  const setData = useCallback((fn) => setState((s) => ({ ...s, data: typeof fn === 'function' ? fn(s.data) : fn })), []);
  return { ...state, reload, setData };
}

/** Re-render every `ms` (for clocks, countdowns, relative times). @param {number} [ms] */
export function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** @param {string} query */
export function useMedia(query) {
  const get = () => !!globalThis.matchMedia?.(query).matches;
  const [match, setMatch] = useState(get);
  useEffect(() => {
    const mq = globalThis.matchMedia?.(query);
    if (!mq) return undefined;
    const fn = () => setMatch(mq.matches);
    mq.addEventListener?.('change', fn);
    return () => mq.removeEventListener?.('change', fn);
  }, [query]);
  return match;
}

/** @template T @param {T} value @param {number} [ms] @returns {T} */
export function useDebounced(value, ms = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** Debounced callback (e.g. live refresh after bursts of events). @param {Function} fn @param {number} [ms] */
export function useDebouncedCallback(fn, ms = 400) {
  const ref = useRef(fn);
  ref.current = fn;
  const timer = useRef(/** @type {any} */ (0));
  useEffect(() => () => clearTimeout(timer.current), []);
  return useCallback((...args) => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => ref.current(...args), ms);
  }, [ms]);
}

/** State persisted in localStorage. @template T @param {string} key @param {T} initial @returns {[T, (v: T) => void]} */
export function useLocal(key, initial) {
  const [v, setV] = useState(() => {
    try {
      const raw = globalThis.localStorage?.getItem('whale.ui.' + key);
      return raw == null ? initial : JSON.parse(raw);
    } catch { return initial; }
  });
  const set = useCallback((next) => {
    setV(next);
    try { globalThis.localStorage?.setItem('whale.ui.' + key, JSON.stringify(next)); } catch { /* ignore */ }
  }, [key]);
  return [v, set];
}

/** Close on Escape / outside click. @param {boolean} open @param {() => void} onClose */
export function useEscape(open, onClose) {
  const ref = useRef(onClose);
  ref.current = onClose;
  useEffect(() => {
    if (!open) return undefined;
    const fn = (e) => { if (e.key === 'Escape') ref.current(); };
    globalThis.addEventListener?.('keydown', fn);
    return () => globalThis.removeEventListener?.('keydown', fn);
  }, [open]);
}

// -- router (history API) -------------------------------------------------------------------------------
// Hash mode (`#/memory`) is used where the history API cannot rewrite the path: file:// pages (the
// self-contained demo) or when `globalThis.WHALE_HASH_ROUTES` is set before boot.
const routeFns = new Set();
function hashMode() {
  return !!globalThis.WHALE_HASH_ROUTES || globalThis.location?.protocol === 'file:';
}
function currentPath() {
  const loc = globalThis.location;
  if (!loc) return '/';
  if (hashMode()) {
    const h = String(loc.hash || '').replace(/^#/, '');
    return h.startsWith('/') ? h : '/';
  }
  return loc.pathname + loc.search;
}
let current = currentPath();

/** @param {string} to @param {{replace?: boolean}} [opts] */
export function navigate(to, opts = {}) {
  if (to === current) return;
  const url = hashMode() ? '#' + to : to;
  if (opts.replace) globalThis.history?.replaceState(null, '', url);
  else globalThis.history?.pushState(null, '', url);
  current = to;
  routeFns.forEach((fn) => fn(to));
  globalThis.scrollTo?.(0, 0);
}
const onPop = () => {
  const next = currentPath();
  if (next === current) return;
  current = next;
  routeFns.forEach((fn) => fn(current));
};
globalThis.addEventListener?.('popstate', onPop);
globalThis.addEventListener?.('hashchange', onPop);

/** @param {string} path */
export function parseRoute(path) {
  const [pathname, search = ''] = path.split('?');
  const parts = pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const query = Object.fromEntries(new URLSearchParams(search));
  return { path: pathname || '/', page: parts[0] || 'bridge', id: parts[1] || '', parts, query };
}

export function useRoute() {
  const [path, setPath] = useState(current);
  useEffect(() => {
    const fn = (p) => setPath(p);
    routeFns.add(fn);
    return () => { routeFns.delete(fn); };
  }, []);
  return parseRoute(path);
}

/** onClick handler for <a href> that routes in-app (keeps middle-click / ctrl-click native). @param {MouseEvent} e */
export function linkClick(e) {
  const a = /** @type {HTMLAnchorElement|null} */ (/** @type {any} */ (e.currentTarget));
  const href = a?.getAttribute?.('href');
  if (!href || !href.startsWith('/') || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigate(href);
}
