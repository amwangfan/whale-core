/* Browser-ish globals for running the demo runtime under the smoke harness (tests/demo-smoke.js). */
(function () {
  const ls = new Map();
  globalThis.localStorage = { getItem: (k) => (ls.has(k) ? ls.get(k) : null), setItem: (k, v) => ls.set(k, String(v)), removeItem: (k) => ls.delete(k) };
  globalThis.__demoLS = ls;
})();
