// @ts-check
/**
 * StarField: the memory graph as a slowly rotating 3D starfield on a 2D canvas. No dependencies.
 *
 * Math (exported for tests): yaw/pitch rotation, perspective projection, Fibonacci sphere, and a
 * small 3D force layout (repulsion + edge springs + a soft spherical shell, nodes of the same kind
 * start next to each other so kinds form constellations).
 * Rendering: depth-sorted, size and alpha by depth, glow per node, faint edges, background dust.
 * Interaction: drag rotates, wheel/pinch zooms, click selects, hover reports. Auto-rotation stops
 * while dragging, when the tab is hidden, and entirely under reduced motion.
 */

/** @typedef {{x: number, y: number, z: number}} Vec3 */
/** @typedef {{w: number, h: number, dist: number, focal: number, zoom: number, near?: number}} Camera */

/** Rotate around Y (yaw) then X (pitch). @param {Vec3} p @param {number} yaw @param {number} pitch @returns {Vec3} */
export function rotate(p, yaw, pitch) {
  const cy = Math.cos(yaw); const sy = Math.sin(yaw);
  const x1 = p.x * cy + p.z * sy;
  const z1 = -p.x * sy + p.z * cy;
  const cp = Math.cos(pitch); const sp = Math.sin(pitch);
  return { x: x1, y: p.y * cp - z1 * sp, z: p.y * sp + z1 * cp };
}

/**
 * Perspective projection; +z points away from the viewer. Returns null behind the near plane.
 * @param {Vec3} p @param {Camera} cam @returns {{x: number, y: number, s: number, depth: number}|null}
 */
export function project(p, cam) {
  const depth = cam.dist + p.z;
  if (depth <= (cam.near ?? 1)) return null;
  const s = (cam.focal * cam.zoom) / depth;
  return { x: cam.w / 2 + p.x * s, y: cam.h / 2 + p.y * s, s, depth };
}

/** Evenly spread points on a sphere. @param {number} n @param {number} r @returns {Vec3[]} */
export function fibonacciSphere(n, r = 1) {
  const out = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = n === 1 ? 0 : 1 - (i / (n - 1)) * 2;
    const rad = Math.sqrt(Math.max(0, 1 - y * y));
    const th = golden * i;
    out.push({ x: Math.cos(th) * rad * r, y: y * r, z: Math.sin(th) * rad * r });
  }
  return out;
}

/** Depth → alpha (front 1, back `min`). @param {number} depth @param {number} near @param {number} far @param {number} [min] */
export function depthAlpha(depth, near, far, min = 0.22) {
  if (far <= near) return 1;
  const t = Math.max(0, Math.min(1, (depth - near) / (far - near)));
  return 1 - t * (1 - min);
}

/**
 * 3D force layout. Deterministic (no randomness).
 * @param {{id: string, kind?: string, importance?: number}[]} nodes @param {{src: string, dst: string}[]} edges
 * @param {{radius?: number, iterations?: number}} [opts] @returns {Vec3[]}
 */
export function layout3d(nodes, edges, opts = {}) {
  const n = nodes.length;
  const R = opts.radius ?? Math.max(120, Math.cbrt(n) * 60);
  const order = nodes.map((nd, i) => i).sort((a, b) => String(nodes[a].kind || '').localeCompare(String(nodes[b].kind || '')) || a - b);
  const seeds = fibonacciSphere(n, R);
  /** @type {Vec3[]} */
  const pos = new Array(n);
  order.forEach((idx, k) => { pos[idx] = { ...seeds[k] }; });
  const index = new Map(nodes.map((nd, i) => [nd.id, i]));
  const links = edges.map((e) => [index.get(e.src), index.get(e.dst)]).filter(([a, b]) => a != null && b != null && a !== b);
  const iters = opts.iterations ?? (n > 600 ? 40 : n > 250 ? 70 : 120);
  const vel = pos.map(() => ({ x: 0, y: 0, z: 0 }));
  for (let it = 0; it < iters; it++) {
    const a = 1 - it / iters;
    for (let i = 0; i < n; i++) {
      const p = pos[i];
      for (let j = i + 1; j < n; j++) {
        const q = pos[j];
        let dx = q.x - p.x; let dy = q.y - p.y; let dz = q.z - p.z;
        let d2 = dx * dx + dy * dy + dz * dz;
        if (d2 < 0.01) { dx = 0.1 * ((i % 3) - 1) || 0.1; dy = 0.1; dz = 0.05; d2 = 0.0225; }
        if (d2 > R * R * 1.2) continue;
        const f = (900 * a) / Math.max(d2, 25);
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f; const fy = (dy / d) * f; const fz = (dz / d) * f;
        vel[i].x -= fx; vel[i].y -= fy; vel[i].z -= fz;
        vel[j].x += fx; vel[j].y += fy; vel[j].z += fz;
      }
    }
    for (const [s, t] of links) {
      const p = pos[/** @type {number} */ (s)]; const q = pos[/** @type {number} */ (t)];
      const dx = q.x - p.x; const dy = q.y - p.y; const dz = q.z - p.z;
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      const f = ((d - R * 0.35) / d) * 0.04 * a;
      vel[/** @type {number} */ (s)].x += dx * f; vel[/** @type {number} */ (s)].y += dy * f; vel[/** @type {number} */ (s)].z += dz * f;
      vel[/** @type {number} */ (t)].x -= dx * f; vel[/** @type {number} */ (t)].y -= dy * f; vel[/** @type {number} */ (t)].z -= dz * f;
    }
    for (let i = 0; i < n; i++) {
      const p = pos[i]; const v = vel[i];
      const shell = R * (0.55 + 0.45 * (1 - Math.max(0, Math.min(1, nodes[i].importance ?? 0.5))));
      const d = Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z) || 1;
      const k = ((shell - d) / d) * 0.06;
      v.x += p.x * k; v.y += p.y * k; v.z += p.z * k;
      v.x *= 0.6; v.y *= 0.6; v.z *= 0.6;
      p.x += v.x; p.y += v.y; p.z += v.z;
    }
  }
  return pos;
}

/** Small deterministic PRNG for the background dust. @param {number} seed */
function mulberry(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

const KIND_ORDER = ['fact', 'preference', 'person', 'episode', 'knowledge', 'skill', 'diary', 'style'];

export class StarField {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{onHover?: (n: any, x: number, y: number) => void, onClick?: (n: any) => void, reduceMotion?: boolean, colorOf?: (n: any) => string}} [opts]
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'));
    this.opts = opts;
    /** @type {any[]} */ this.nodes = [];
    /** @type {{s: any, t: any, relation: string, extra: boolean}[]} */ this.edges = [];
    this.yaw = 0.6; this.pitch = -0.25; this.zoom = 1;
    this.R = 200;
    this.w = 1; this.h = 1; this.dpr = 1;
    /** @type {any} */ this.hover = null;
    /** @type {string|null} */ this.selected = null;
    /** @type {((n: any) => boolean)|null} */ this.filter = null;
    this.paused = !!globalThis.document?.hidden;
    this.spin = !opts.reduceMotion;
    this.dirty = true;
    this.raf = 0;
    this.last = 0;
    this.stats = { fps: 0, nodes: 0, edges: 0, drawn: 0 };
    /** @type {any} */ this.drag = null;
    /** @type {Map<number, {x: number, y: number}>} */ this.pointers = new Map();
    const rnd = mulberry(7);
    this.dust = Array.from({ length: 240 }, () => ({ x: (rnd() * 2 - 1), y: (rnd() * 2 - 1), z: (rnd() * 2 - 1), b: 0.25 + rnd() * 0.75 }));
    this.destroyed = false;
    this.refreshColors();
    this.bind();
    this.resize();
    this.frame = this.frame.bind(this);
    this.raf = requestAnimationFrame(this.frame);
  }

  refreshColors() {
    const cs = getComputedStyle(this.canvas);
    const v = (name, fb) => (cs.getPropertyValue(name) || '').trim() || fb;
    this.colors = {
      kind: Object.fromEntries(KIND_ORDER.map((k) => [k, v(`--k-${k}`, '#38bdf8')])),
      text: v('--text', '#e3f1fb'), edge: v('--accent-2', '#38bdf8'), accent: v('--accent', '#2ee6d6'),
      person: v('--k-person', '#fb923c'), evidence: v('--text-3', '#6f8aa5'), space: v('--sc-group', '#a78bfa'),
    };
    this.font = cs.fontFamily || 'sans-serif';
    this.dirty = true;
  }

  /** @param {any} n */
  colorOf(n) {
    if (this.opts.colorOf) return this.opts.colorOf(n);
    if (n.category && n.category !== 'memory') return this.colors[n.category] || this.colors.accent;
    return this.colors.kind[n.kind] || this.colors.accent;
  }

  /** @param {any[]} nodes @param {{src: string, dst: string, relation?: string, extra?: boolean}[]} edges */
  setData(nodes, edges) {
    const pos = layout3d(nodes, edges);
    this.R = Math.max(120, Math.cbrt(nodes.length || 1) * 60);
    /** @type {Map<string, any>} */
    const byId = new Map();
    this.nodes = nodes.map((n, i) => {
      const cat = n.category || 'memory';
      const r = cat === 'evidence' ? 2 : cat === 'space' ? 7 : cat === 'person' ? 5 : 2.6 + Math.max(0, Math.min(1, n.importance ?? 0.5)) * 4.4;
      const sn = { data: n, id: n.id, cat, r, p: pos[i], q: /** @type {any} */ (null) };
      byId.set(n.id, sn);
      return sn;
    });
    this.byId = byId;
    this.edges = [];
    for (const e of edges) {
      const s = byId.get(e.src); const t = byId.get(e.dst);
      if (s && t && s !== t) this.edges.push({ s, t, relation: e.relation || '', extra: !!e.extra });
    }
    this.stats.nodes = this.nodes.length; this.stats.edges = this.edges.length;
    this.dirty = true;
  }

  /** @param {((n: any) => boolean)|null} fn */
  setFilter(fn) { this.filter = fn; this.dirty = true; }
  /** @param {string|null} id */
  select(id) { this.selected = id; this.dirty = true; }
  /** @param {number} f */
  zoomBy(f) { this.zoom = Math.max(0.35, Math.min(4, this.zoom * f)); this.dirty = true; }
  reset() { this.yaw = 0.6; this.pitch = -0.25; this.zoom = 1; this.dirty = true; }
  /** @param {boolean} on */
  setSpin(on) { this.spin = on && !this.opts.reduceMotion; this.dirty = true; }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(2, globalThis.devicePixelRatio || 1);
    this.w = Math.max(1, rect.width); this.h = Math.max(1, rect.height);
    this.canvas.width = Math.round(this.w * this.dpr);
    this.canvas.height = Math.round(this.h * this.dpr);
    this.dirty = true;
  }

  camera() {
    const focal = Math.min(this.w, this.h) * 0.95;
    return { w: this.w, h: this.h, dist: this.R * 2.6, focal, zoom: this.zoom, near: 10 };
  }

  /** @param {number} t */
  frame(t) {
    if (this.destroyed) return;
    const dt = this.last ? Math.min(100, t - this.last) : 16;
    this.last = t;
    if (!this.paused) {
      if (this.spin && !this.drag) { this.yaw += dt * 0.00007; this.dirty = true; }
      if (this.dirty) {
        this.draw();
        this.dirty = false;
        this.stats.fps = this.stats.fps ? this.stats.fps * 0.9 + (1000 / Math.max(1, dt)) * 0.1 : 60;
      }
    }
    this.raf = requestAnimationFrame(this.frame);
  }

  /** Project every node with the current rotation. */
  projectAll() {
    const cam = this.camera();
    for (const n of this.nodes) {
      const r = rotate(n.p, this.yaw, this.pitch);
      n.q = project(r, cam);
    }
    return cam;
  }

  draw() {
    const { ctx, dpr } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    const cam = this.projectAll();
    const near = cam.dist - this.R * 1.1; const far = cam.dist + this.R * 1.1;
    // background dust: rotates slower, gives the depth cue
    ctx.fillStyle = this.colors.text;
    for (const d of this.dust) {
      const r = rotate({ x: d.x * this.R * 3, y: d.y * this.R * 3, z: d.z * this.R * 3 }, this.yaw * 0.35, this.pitch * 0.35);
      const q = project(r, { ...cam, dist: cam.dist + this.R * 2.2, zoom: 1 });
      if (!q) continue;
      ctx.globalAlpha = 0.12 * d.b;
      ctx.fillRect(q.x, q.y, 1.2, 1.2);
    }
    const focusId = this.hover?.id || this.selected;
    const vis = (n) => !this.filter || n.cat !== 'memory' || this.filter(n.data);
    // edges
    for (const e of this.edges) {
      if (!e.s.q || !e.t.q) continue;
      const on = focusId && (e.s.id === focusId || e.t.id === focusId);
      const a = depthAlpha((e.s.q.depth + e.t.q.depth) / 2, near, far, 0.1);
      ctx.globalAlpha = (vis(e.s) && vis(e.t) ? (on ? 0.85 : e.extra ? 0.16 : 0.28) : 0.04) * a;
      ctx.strokeStyle = on ? this.colors.accent : this.colors.edge;
      ctx.lineWidth = on ? 1.4 : 0.7;
      ctx.beginPath(); ctx.moveTo(e.s.q.x, e.s.q.y); ctx.lineTo(e.t.q.x, e.t.q.y); ctx.stroke();
    }
    // nodes, far → near
    const order = this.nodes.filter((n) => n.q).sort((a, b) => b.q.depth - a.q.depth);
    this.stats.drawn = order.length;
    for (const n of order) {
      const q = n.q;
      const size = Math.max(0.8, n.r * q.s * 0.55);
      const a = depthAlpha(q.depth, near, far) * (vis(n) ? 1 : 0.12);
      const c = this.colorOf(n.data);
      ctx.globalAlpha = a * 0.22;
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.arc(q.x, q.y, size * 2.6, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = a;
      ctx.beginPath();
      if (n.cat === 'evidence') ctx.rect(q.x - size, q.y - size, size * 2, size * 2);
      else ctx.arc(q.x, q.y, size, 0, Math.PI * 2);
      ctx.fill();
      if (n.cat === 'space' || n.cat === 'person') {
        ctx.strokeStyle = c; ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(q.x, q.y, size + 3, 0, Math.PI * 2); ctx.stroke();
      }
      if (n.id === focusId) {
        ctx.globalAlpha = 1; ctx.strokeStyle = this.colors.text; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(q.x, q.y, size + 5, 0, Math.PI * 2); ctx.stroke();
      }
    }
    // labels: focus and its neighbours, plus the nearest important stars
    ctx.font = `12px ${this.font}`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    ctx.fillStyle = this.colors.text;
    const nearSet = new Set();
    if (focusId) this.edges.forEach((e) => { if (e.s.id === focusId) nearSet.add(e.t.id); if (e.t.id === focusId) nearSet.add(e.s.id); });
    for (const n of order) {
      const important = (n.data.importance ?? 0) >= 0.85 && n.q.depth < cam.dist - this.R * 0.3;
      if (!(n.id === focusId || nearSet.has(n.id) || (!focusId && important))) continue;
      ctx.globalAlpha = n.id === focusId ? 1 : 0.75;
      const t = String(n.data.content || n.data.label || '');
      ctx.fillText(t.length > 16 ? t.slice(0, 15) + '…' : t, n.q.x, n.q.y + n.r * n.q.s * 0.55 + 6);
    }
    ctx.globalAlpha = 1;
  }

  /** Front-most node under a canvas point. @param {number} x @param {number} y */
  hit(x, y) {
    let best = null; let bd = Infinity;
    for (const n of this.nodes) {
      if (!n.q) continue;
      const size = Math.max(0.8, n.r * n.q.s * 0.55) + 4;
      const d = Math.hypot(n.q.x - x, n.q.y - y);
      if (d <= size && n.q.depth < bd) { best = n; bd = n.q.depth; }
    }
    return best;
  }

  bind() {
    const c = this.canvas;
    const local = (/** @type {any} */ e) => { const r = c.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    this.onWheel = (/** @type {WheelEvent} */ e) => { e.preventDefault(); this.zoomBy(Math.exp(-e.deltaY * 0.0015)); };
    this.onDown = (/** @type {PointerEvent} */ e) => {
      const p = local(e);
      this.pointers.set(e.pointerId, p);
      c.setPointerCapture?.(e.pointerId);
      if (this.pointers.size === 2) { const [a, b] = [...this.pointers.values()]; this.drag = { pinch: Math.hypot(a.x - b.x, a.y - b.y), zoom: this.zoom }; return; }
      this.drag = { start: p, yaw: this.yaw, pitch: this.pitch, moved: false };
    };
    this.onMove = (/** @type {PointerEvent} */ e) => {
      const p = local(e);
      if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, p);
      const d = this.drag;
      if (d?.pinch && this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.zoom = Math.max(0.35, Math.min(4, d.zoom * (Math.hypot(a.x - b.x, a.y - b.y) / (d.pinch || 1))));
        this.dirty = true; return;
      }
      if (d && !d.pinch) {
        const dx = p.x - d.start.x; const dy = p.y - d.start.y;
        if (Math.hypot(dx, dy) > 3) d.moved = true;
        this.yaw = d.yaw + dx * 0.006;
        this.pitch = Math.max(-1.35, Math.min(1.35, d.pitch + dy * 0.006));
        this.dirty = true; return;
      }
      const n = this.hit(p.x, p.y);
      if (n !== this.hover) { this.hover = n; this.dirty = true; c.style.cursor = n ? 'pointer' : 'grab'; }
      this.opts.onHover?.(n ? n.data : null, p.x, p.y);
    };
    this.onUp = (/** @type {PointerEvent} */ e) => {
      this.pointers.delete(e.pointerId);
      const d = this.drag;
      this.drag = null;
      if (!d || d.pinch || d.moved) return;
      const n = this.hit(d.start.x, d.start.y);
      this.opts.onClick?.(n ? n.data : null);
    };
    this.onLeave = () => { if (this.hover) { this.hover = null; this.dirty = true; } this.opts.onHover?.(null, 0, 0); };
    this.onVis = () => { this.paused = !!globalThis.document?.hidden; this.last = 0; this.dirty = true; };
    c.addEventListener('wheel', this.onWheel, { passive: false });
    c.addEventListener('pointerdown', this.onDown);
    c.addEventListener('pointermove', this.onMove);
    c.addEventListener('pointerup', this.onUp);
    c.addEventListener('pointercancel', this.onUp);
    c.addEventListener('pointerleave', this.onLeave);
    globalThis.document?.addEventListener?.('visibilitychange', this.onVis);
    this.ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => this.resize()) : null;
    this.ro?.observe(c);
  }

  destroy() {
    this.destroyed = true;
    cancelAnimationFrame(this.raf);
    const c = this.canvas;
    c.removeEventListener('wheel', this.onWheel);
    c.removeEventListener('pointerdown', this.onDown);
    c.removeEventListener('pointermove', this.onMove);
    c.removeEventListener('pointerup', this.onUp);
    c.removeEventListener('pointercancel', this.onUp);
    c.removeEventListener('pointerleave', this.onLeave);
    globalThis.document?.removeEventListener?.('visibilitychange', this.onVis);
    this.ro?.disconnect();
  }
}
