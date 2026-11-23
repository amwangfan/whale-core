// @ts-check
/**
 * ForceGraph: a small force-directed layout + canvas renderer for the memory graph.
 * No dependencies. Nodes are memory items ({id, kind, importance, strength_now, scope, status, content});
 * edges are memory links ({src, dst, relation, weight}).
 *
 * Forces: pairwise repulsion, edge springs, soft pull towards a per-kind anchor
 * (so kinds form neighbourhoods), centre gravity and collision. Interaction:
 * wheel/pinch zoom, background drag pans, node drag pins while dragging,
 * hover → onHover, click → onClick.
 */

/** @typedef {{id: string, kind: string, importance?: number, strength_now?: number, scope?: string, status?: string, content?: string}} MemNode */
/** @typedef {{src: string, dst: string, relation: string, weight?: number}} MemEdge */
/** @typedef {MemNode & {x: number, y: number, vx: number, vy: number, r: number, fx?: number|null, fy?: number|null, data: MemNode}} SimNode */

const KIND_ORDER = ['fact', 'preference', 'person', 'episode', 'knowledge', 'skill', 'diary', 'style'];
const SCOPE_VAR = { owner: '--sc-owner', shared: '--sc-shared', group: '--sc-group', person: '--sc-person' };

/** @param {string} scope */
function scopeCat(scope) {
  if (!scope || scope === 'owner') return 'owner';
  if (scope === 'shared') return 'shared';
  return scope.startsWith('group:') ? 'group' : 'person';
}

export class ForceGraph {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{onHover?: (n: MemNode|null, x: number, y: number) => void, onClick?: (n: MemNode|null) => void, reduceMotion?: boolean,
   *   colorOf?: (n: any) => string, groups?: string[]}} [opts]  colorOf/groups: colour and neighbourhood anchors for non-memory graphs (import preview)
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'));
    this.opts = opts;
    /** @type {SimNode[]} */ this.nodes = [];
    /** @type {{s: SimNode, t: SimNode, relation: string, weight: number}[]} */ this.edges = [];
    /** @type {Map<string, SimNode>} */ this.byId = new Map();
    /** @type {Map<string, Set<string>>} */ this.adj = new Map();
    this.view = { k: 1, x: 0, y: 0 };
    this.alpha = 1;
    this.w = 0; this.h = 0; this.dpr = 1;
    /** @type {SimNode|null} */ this.hover = null;
    /** @type {string|null} */ this.selected = null;
    /** @type {((n: MemNode) => boolean)|null} */ this.filter = null;
    this.colors = {};
    this.raf = 0;
    this.dirty = true;
    /** @type {Map<number, {x: number, y: number}>} */ this.pointers = new Map();
    /** @type {any} */ this.drag = null;
    this.destroyed = false;
    this.refreshColors();
    this.bind();
    this.resize();
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  refreshColors() {
    const cs = getComputedStyle(this.canvas);
    const v = (name, fb) => (cs.getPropertyValue(name) || '').trim() || fb;
    this.colors = {
      kind: Object.fromEntries(KIND_ORDER.map((k) => [k, v(`--k-${k}`, '#38bdf8')])),
      scope: Object.fromEntries(Object.entries(SCOPE_VAR).map(([k, name]) => [k, v(name, '#fff')])),
      text: v('--text', '#e3f1fb'), text3: v('--text-3', '#6f8aa5'), edge: v('--border-strong', 'rgba(125,211,252,.24)'),
      accent: v('--accent', '#2ee6d6'), bg: v('--panel-solid', '#0c1e38'),
      person: v('--k-person', '#fb923c'), evidence: v('--text-2', '#a9c2d8'), space: v('--sc-group', '#a78bfa'),
    };
    this.dirty = true;
  }

  /** @param {MemNode[]} nodes @param {MemEdge[]} edges */
  setData(nodes, edges) {
    const old = this.byId;
    this.byId = new Map();
    const anchors = this.anchors();
    this.nodes = nodes.map((n, i) => {
      const prev = old.get(n.id);
      const a = anchors[n.kind] || anchors[/** @type {any} */ (n).category] || { x: 0, y: 0 };
      const ang = (i * 2.399) % (Math.PI * 2);
      const cat = /** @type {any} */ (n).category;
      const r = cat === 'person' ? 9 : cat === 'space' ? 13 : cat === 'evidence' ? 3.5 : 5 + Math.max(0, Math.min(1, n.importance ?? 0.5)) * 11;
      /** @type {SimNode} */
      const sn = { ...n, data: n, r,
        x: prev ? prev.x : a.x + Math.cos(ang) * (20 + (i % 7) * 9), y: prev ? prev.y : a.y + Math.sin(ang) * (20 + (i % 5) * 9),
        vx: 0, vy: 0, fx: null, fy: null };
      this.byId.set(n.id, sn);
      return sn;
    });
    this.adj = new Map();
    this.edges = [];
    for (const e of edges) {
      const s = this.byId.get(e.src); const t = this.byId.get(e.dst);
      if (!s || !t || s === t) continue;
      this.edges.push({ s, t, relation: e.relation, weight: e.weight ?? 0.5, extra: !!(/** @type {any} */ (e).extra) });
      if (!this.adj.has(s.id)) this.adj.set(s.id, new Set());
      if (!this.adj.has(t.id)) this.adj.set(t.id, new Set());
      /** @type {Set<string>} */ (this.adj.get(s.id)).add(t.id);
      /** @type {Set<string>} */ (this.adj.get(t.id)).add(s.id);
    }
    this.alpha = old.size ? 0.5 : 1;
    // reduced motion: lay out synchronously (fewer passes for big imported graphs)
    if (this.opts.reduceMotion) { const passes = this.nodes.length > 500 ? 70 : 260; for (let i = 0; i < passes; i++) this.tick(); this.alpha = 0; }
    this.dirty = true;
  }

  anchors() {
    const R = 150;
    const order = this.opts.groups || KIND_ORDER;
    /** @type {Record<string, {x: number, y: number}>} */
    const out = {};
    const RR = order.length > 8 ? R * 1.6 : R;
    order.forEach((k, i) => {
      const a = (i / order.length) * Math.PI * 2 - Math.PI / 2;
      out[k] = { x: Math.cos(a) * RR, y: Math.sin(a) * RR };
    });
    return out;
  }

  /** @param {((n: MemNode) => boolean)|null} fn */
  setFilter(fn) { this.filter = fn; this.dirty = true; }
  /** @param {string|null} id */
  select(id) { this.selected = id; this.dirty = true; }

  /** Centre the view on a node (animated by the next frames). @param {string} id */
  focus(id) {
    const n = this.byId.get(id);
    if (!n) return;
    this.view.k = Math.max(this.view.k, 1.3);
    this.view.x = -n.x * this.view.k;
    this.view.y = -n.y * this.view.k;
    this.select(id);
  }

  fit() {
    if (!this.nodes.length) return;
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const n of this.nodes) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r); }
    const k = Math.min(2, Math.max(0.25, Math.min(this.w / (x1 - x0 + 80), this.h / (y1 - y0 + 80))));
    this.view = { k, x: -((x0 + x1) / 2) * k, y: -((y0 + y1) / 2) * k };
    this.dirty = true;
  }

  /** @param {number} f */
  zoomBy(f) { this.view.k = Math.max(0.2, Math.min(4, this.view.k * f)); this.view.x *= f; this.view.y *= f; this.dirty = true; }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(2, globalThis.devicePixelRatio || 1);
    this.w = Math.max(1, rect.width); this.h = Math.max(1, rect.height);
    this.canvas.width = Math.round(this.w * this.dpr);
    this.canvas.height = Math.round(this.h * this.dpr);
    this.dirty = true;
  }

  tick() {
    const nodes = this.nodes;
    const a = this.alpha;
    const anchors = this.anchors();
    const n = nodes.length;
    for (let i = 0; i < n; i++) {
      const p = nodes[i];
      for (let j = i + 1; j < n; j++) {
        const q = nodes[j];
        let dx = q.x - p.x; let dy = q.y - p.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.5; }
        if (d2 > 160000) continue;
        const d = Math.sqrt(d2);
        const f = (-260 * a) / Math.max(d2, 36);
        const fx = (dx / d) * f; const fy = (dy / d) * f;
        p.vx += fx; p.vy += fy; q.vx -= fx; q.vy -= fy;
        const min = p.r + q.r + 3;
        if (d < min) {
          const push = ((min - d) / d) * 0.5;
          p.x -= dx * push; p.y -= dy * push; q.x += dx * push; q.y += dy * push;
        }
      }
    }
    for (const e of this.edges) {
      const dx = e.t.x - e.s.x; const dy = e.t.y - e.s.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const L = (e.extra ? 22 : 46) + e.s.r + e.t.r;
      const f = ((d - L) / d) * 0.05 * a * (0.5 + e.weight);
      e.s.vx += dx * f; e.s.vy += dy * f; e.t.vx -= dx * f; e.t.vy -= dy * f;
    }
    for (const p of nodes) {
      const an = anchors[p.kind] || { x: 0, y: 0 };
      p.vx += (an.x - p.x) * 0.012 * a - p.x * 0.004 * a;
      p.vy += (an.y - p.y) * 0.012 * a - p.y * 0.004 * a;
      if (p.fx != null && p.fy != null) { p.x = p.fx; p.y = p.fy; p.vx = 0; p.vy = 0; continue; }
      p.vx *= 0.58; p.vy *= 0.58;
      p.x += p.vx; p.y += p.vy;
    }
    this.alpha = Math.max(0, a * 0.988 - 0.0004);
  }

  loop() {
    if (this.destroyed) return;
    if (this.alpha > 0.003 || this.drag?.node) { this.tick(); this.dirty = true; }
    if (this.dirty) { this.draw(); this.dirty = false; }
    this.raf = requestAnimationFrame(this.loop);
  }

  /** @param {SimNode} n */
  visible(n) { return !this.filter || this.filter(n.data); }

  draw() {
    const { ctx, view, dpr } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.translate(this.w / 2 + view.x, this.h / 2 + view.y);
    ctx.scale(view.k, view.k);
    const focusId = this.hover?.id || this.selected;
    const near = focusId ? this.adj.get(focusId) || new Set() : null;
    const lit = (/** @type {SimNode} */ n) => !near || n.id === focusId || near.has(n.id);

    for (const e of this.edges) {
      const show = this.visible(e.s) && this.visible(e.t);
      const on = near ? (e.s.id === focusId || e.t.id === focusId) : true;
      ctx.globalAlpha = !show ? 0.04 : on ? (near ? 0.9 : e.extra ? 0.28 : 0.45) : 0.08;
      ctx.strokeStyle = on && near ? this.colors.accent : this.colors.edge;
      ctx.lineWidth = (e.extra ? 0.7 : 0.8 + e.weight * 1.4) / Math.sqrt(view.k);
      const sup = e.relation === 'supersedes';
      ctx.setLineDash(sup ? [5, 4] : e.extra ? [2, 3] : []);
      ctx.beginPath(); ctx.moveTo(e.s.x, e.s.y); ctx.lineTo(e.t.x, e.t.y); ctx.stroke();
      if (sup || e.relation === 'derived_from') this.arrow(e.s, e.t);
    }
    ctx.setLineDash([]);

    for (const n of this.nodes) {
      const vis = this.visible(n);
      const strength = Math.max(0.2, Math.min(1, n.strength_now ?? 0.6));
      ctx.globalAlpha = !vis ? 0.07 : lit(n) ? 0.3 + strength * 0.7 : 0.12;
      const cat = /** @type {any} */ (n).category;
      const color = this.colorOf(n);
      if (cat === 'person' || cat === 'evidence' || cat === 'space') this.shape(n, cat, color);
      else {
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        if (n.status === 'superseded') { ctx.fillStyle = this.colors.bg; ctx.fill(); ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke(); }
        else { ctx.fillStyle = color; ctx.fill(); }
        if (cat !== 'entity') {
          // scope ring
          ctx.strokeStyle = this.colors.scope[scopeCat(n.scope || 'owner')];
          ctx.lineWidth = 1.6;
          if (n.status === 'candidate') ctx.setLineDash([2.5, 2.5]);
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 3, 0, Math.PI * 2); ctx.stroke();
          ctx.setLineDash([]);
        }
      }
      if (n.id === focusId) {
        ctx.globalAlpha = 0.9; ctx.strokeStyle = this.colors.text; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 7, 0, Math.PI * 2); ctx.stroke();
      }
    }
    // labels
    ctx.font = `${12 / Math.max(0.8, view.k)}px ${getComputedStyle(this.canvas).fontFamily}`;
    if (near) {
      // relation names of the focused node's source edges (主体 / 依据 / 包含), at the midpoint
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.globalAlpha = 0.85; ctx.fillStyle = this.colors.text3;
      for (const e of this.edges) if (e.extra && (e.s.id === focusId || e.t.id === focusId)) ctx.fillText(e.relation, (e.s.x + e.t.x) / 2, (e.s.y + e.t.y) / 2 - 6 / view.k);
    }
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (const n of this.nodes) {
      const cat = /** @type {any} */ (n).category;
      const important = (n.importance ?? 0) >= 0.8 || cat === 'space' || cat === 'person';
      const showLabel = n.id === focusId || (near && near.has(n.id)) || (view.k > 1.35 && this.visible(n)) || (important && view.k > 0.8 && !near);
      if (!showLabel) continue;
      ctx.globalAlpha = n.id === focusId ? 1 : 0.82;
      ctx.fillStyle = this.colors.text;
      const t = String(n.content || /** @type {any} */ (n).label || '');
      ctx.fillText(t.length > 16 ? t.slice(0, 15) + '…' : t, n.x, n.y + n.r + 6);
    }
    ctx.globalAlpha = 1;
  }

  /** @param {any} n */
  colorOf(n) {
    if (this.opts.colorOf) return this.opts.colorOf(n);
    if (n.category && this.colors[n.category]) return this.colors[n.category];
    return this.colors.kind[n.kind] || this.colors.accent;
  }

  /** Source nodes: person = ring + glyph, evidence = small square, space = hexagon. @param {SimNode} n @param {string} cat @param {string} color */
  shape(n, cat, color) {
    const { ctx } = this;
    const r = n.r;
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = color;
    if (cat === 'evidence') {
      ctx.fillStyle = color;
      ctx.fillRect(n.x - r, n.y - r, r * 2, r * 2);
      return;
    }
    if (cat === 'space') {
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 3) * i + Math.PI / 6;
        const x = n.x + Math.cos(a) * r; const y = n.y + Math.sin(a) * r;
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      }
      ctx.closePath();
      ctx.fillStyle = this.colors.bg; ctx.fill(); ctx.lineWidth = 2.2; ctx.stroke();
      ctx.beginPath(); ctx.arc(n.x, n.y, r * 0.35, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
      return;
    }
    // person: filled disc with a head-and-shoulders glyph
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
    ctx.fillStyle = this.colors.bg;
    ctx.beginPath(); ctx.arc(n.x, n.y - r * 0.28, r * 0.3, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(n.x, n.y + r * 0.62, r * 0.52, Math.PI, 0); ctx.fill();
  }

  /** @param {SimNode} s @param {SimNode} t */
  arrow(s, t) {
    const { ctx } = this;
    const ang = Math.atan2(t.y - s.y, t.x - s.x);
    const x = t.x - Math.cos(ang) * (t.r + 5); const y = t.y - Math.sin(ang) * (t.r + 5);
    const L = 7 / Math.sqrt(this.view.k);
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x - Math.cos(ang - 0.45) * L, y - Math.sin(ang - 0.45) * L);
    ctx.lineTo(x - Math.cos(ang + 0.45) * L, y - Math.sin(ang + 0.45) * L);
    ctx.closePath();
    ctx.fillStyle = ctx.strokeStyle; ctx.fill();
    ctx.setLineDash([5, 4]);
  }

  /** screen (css px, canvas-relative) → world @param {number} sx @param {number} sy */
  toWorld(sx, sy) {
    return { x: (sx - this.w / 2 - this.view.x) / this.view.k, y: (sy - this.h / 2 - this.view.y) / this.view.k };
  }

  /** @param {number} sx @param {number} sy */
  hit(sx, sy) {
    const p = this.toWorld(sx, sy);
    let best = null; let bd = Infinity;
    for (const n of this.nodes) {
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d < n.r + 5 / this.view.k && d < bd) { best = n; bd = d; }
    }
    return best;
  }

  bind() {
    const c = this.canvas;
    const local = (/** @type {PointerEvent|WheelEvent} */ e) => { const r = c.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    this.onWheel = (/** @type {WheelEvent} */ e) => {
      e.preventDefault();
      const p = local(e);
      const f = Math.exp(-e.deltaY * 0.0015);
      const k = Math.max(0.2, Math.min(4, this.view.k * f));
      const real = k / this.view.k;
      const cx = p.x - this.w / 2; const cy = p.y - this.h / 2;
      this.view.x = cx - (cx - this.view.x) * real;
      this.view.y = cy - (cy - this.view.y) * real;
      this.view.k = k;
      this.dirty = true;
    };
    this.onDown = (/** @type {PointerEvent} */ e) => {
      const p = local(e);
      this.pointers.set(e.pointerId, p);
      c.setPointerCapture?.(e.pointerId);
      if (this.pointers.size === 2) { this.drag = { pinch: this.pinchState() }; return; }
      const node = this.hit(p.x, p.y);
      this.drag = { node, start: p, view: { ...this.view }, moved: false };
      if (node) { const w = this.toWorld(p.x, p.y); node.fx = w.x; node.fy = w.y; this.alpha = Math.max(this.alpha, 0.25); }
    };
    this.onMove = (/** @type {PointerEvent} */ e) => {
      const p = local(e);
      if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, p);
      const d = this.drag;
      if (d?.pinch && this.pointers.size === 2) {
        const now = this.pinchState();
        const f = now.dist / (d.pinch.dist || 1);
        this.view.k = Math.max(0.2, Math.min(4, d.pinch.k * f));
        this.dirty = true;
        return;
      }
      if (d && !d.pinch) {
        if (Math.hypot(p.x - d.start.x, p.y - d.start.y) > 3) d.moved = true;
        if (d.node) { const w = this.toWorld(p.x, p.y); d.node.fx = w.x; d.node.fy = w.y; this.alpha = Math.max(this.alpha, 0.12); }
        else { this.view.x = d.view.x + (p.x - d.start.x); this.view.y = d.view.y + (p.y - d.start.y); }
        this.dirty = true;
        return;
      }
      const n = this.hit(p.x, p.y);
      if (n !== this.hover) { this.hover = n; this.dirty = true; c.style.cursor = n ? 'pointer' : 'grab'; }
      this.opts.onHover?.(n ? n.data : null, p.x, p.y);
    };
    this.onUp = (/** @type {PointerEvent} */ e) => {
      this.pointers.delete(e.pointerId);
      const d = this.drag;
      this.drag = null;
      if (!d || d.pinch) return;
      if (d.node) { d.node.fx = null; d.node.fy = null; }
      if (!d.moved) this.opts.onClick?.(d.node ? d.node.data : null);
    };
    this.onLeave = () => { if (this.hover) { this.hover = null; this.dirty = true; } this.opts.onHover?.(null, 0, 0); };
    c.addEventListener('wheel', this.onWheel, { passive: false });
    c.addEventListener('pointerdown', this.onDown);
    c.addEventListener('pointermove', this.onMove);
    c.addEventListener('pointerup', this.onUp);
    c.addEventListener('pointercancel', this.onUp);
    c.addEventListener('pointerleave', this.onLeave);
    this.ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => this.resize()) : null;
    this.ro?.observe(c);
  }

  pinchState() {
    const [a, b] = [...this.pointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y), k: this.view.k };
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
    this.ro?.disconnect();
  }
}
