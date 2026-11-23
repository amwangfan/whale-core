// @ts-check
/**
 * Parser for MCP "memory" knowledge-graph files (the reference `@modelcontextprotocol/server-memory`
 * format and its common exports), for the read-only import preview on the 记忆 page.
 *
 * Accepted inputs:
 *   - `{entities: [{name, entityType, observations[]}], relations: [{from, to, relationType}]}`
 *   - an array of such objects, or a flat array of entities / relations
 *   - JSONL: one object per line with `type: "entity" | "relation"` (lines may also be whole graphs)
 * Nothing is sent to the server. Pure functions, no DOM.
 */

export const MAX_IMPORT_BYTES = 8 * 1024 * 1024;
export const MAX_PREVIEW_NODES = 1000;
const UNDECLARED = '（未声明）';

/** @typedef {{name: string, entityType: string, observations: string[]}} Entity */
/** @typedef {{from: string, to: string, relationType: string}} Relation */
/** @typedef {{entities: Entity[], relations: Relation[], types: Record<string, number>, skipped: number, implicit: number}} McpGraph */

/** @param {any} v */
const str = (v) => (v == null ? '' : String(v)).trim();

/**
 * @param {string} text
 * @returns {McpGraph}
 */
export function parseMcpGraph(text) {
  const src = String(text ?? '').replace(/^\uFEFF/, '').trim();
  if (!src) throw new Error('文件是空的');
  if (src.length > MAX_IMPORT_BYTES) throw new Error('文件超过 8 MiB，无法预览');
  /** @type {any[]} */
  let items = [];
  let skipped = 0;
  let whole;
  if (src[0] === '{' || src[0] === '[') {
    try { whole = JSON.parse(src); } catch { whole = undefined; }
  }
  if (whole !== undefined) items = Array.isArray(whole) ? whole : [whole];
  else {
    for (const line of src.split(/\r?\n/)) {
      const t = line.trim();
      if (!t || t.startsWith('//')) continue;
      try { items.push(JSON.parse(t)); } catch { skipped++; }
    }
    if (!items.length) throw new Error('无法解析：既不是 JSON，也不是每行一个对象的 JSONL');
  }

  /** @type {Map<string, Entity>} */
  const entities = new Map();
  /** @type {Map<string, Relation>} */
  const relations = new Map();
  const addEntity = (/** @type {any} */ e) => {
    const name = str(e.name);
    if (!name) { skipped++; return; }
    const type = str(e.entityType ?? e.entity_type ?? e.type_name) || UNDECLARED;
    const obs = (Array.isArray(e.observations) ? e.observations : e.observations != null ? [e.observations] : []).map(str).filter(Boolean);
    const cur = entities.get(name);
    if (cur) {
      if (cur.entityType === UNDECLARED && type !== UNDECLARED) cur.entityType = type;
      obs.forEach((o) => { if (!cur.observations.includes(o)) cur.observations.push(o); });
    } else entities.set(name, { name, entityType: type, observations: [...new Set(obs)] });
  };
  const addRelation = (/** @type {any} */ r) => {
    const from = str(r.from ?? r.source); const to = str(r.to ?? r.target);
    const rel = str(r.relationType ?? r.relation_type ?? r.relation) || '相关';
    if (!from || !to) { skipped++; return; }
    const key = `${from}\u0000${rel}\u0000${to}`;
    if (!relations.has(key)) relations.set(key, { from, to, relationType: rel });
  };
  /** @param {any} it @param {number} depth */
  const visit = (it, depth) => {
    if (!it || typeof it !== 'object') { skipped++; return; }
    if (Array.isArray(it)) { if (depth < 3) it.forEach((x) => visit(x, depth + 1)); else skipped++; return; }
    if (Array.isArray(it.entities) || Array.isArray(it.relations)) {
      (it.entities || []).forEach(addEntity);
      (it.relations || []).forEach(addRelation);
      return;
    }
    if (it.type === 'entity' || (it.type == null && it.name != null && it.from == null)) { addEntity(it); return; }
    if (it.type === 'relation' || (it.type == null && it.from != null && it.to != null)) { addRelation(it); return; }
    skipped++;
  };
  items.forEach((it) => visit(it, 0));

  // relations may mention entities that were never declared: keep them as implicit nodes
  let implicit = 0;
  relations.forEach((r) => {
    [r.from, r.to].forEach((n) => {
      if (!entities.has(n)) { entities.set(n, { name: n, entityType: UNDECLARED, observations: [] }); implicit++; }
    });
  });
  if (!entities.size) throw new Error('文件里没有找到实体（entities）');
  /** @type {Record<string, number>} */
  const types = {};
  entities.forEach((e) => { types[e.entityType] = (types[e.entityType] || 0) + 1; });
  return { entities: [...entities.values()], relations: [...relations.values()], types, skipped, implicit };
}

/**
 * Pick what to draw: the best-connected entities first, at most `max`.
 * @param {McpGraph} g @param {number} [max]
 */
export function toPreview(g, max = MAX_PREVIEW_NODES) {
  /** @type {Map<string, number>} */
  const degree = new Map();
  g.relations.forEach((r) => {
    degree.set(r.from, (degree.get(r.from) || 0) + 1);
    degree.set(r.to, (degree.get(r.to) || 0) + 1);
  });
  const ranked = g.entities.map((e, i) => ({ e, i, d: degree.get(e.name) || 0 }))
    .sort((a, b) => b.d - a.d || b.e.observations.length - a.e.observations.length || a.i - b.i);
  const kept = ranked.slice(0, max).map((x) => x.e);
  const names = new Set(kept.map((e) => e.name));
  const nodes = kept.map((e) => ({
    id: e.name, kind: e.entityType, content: e.name, category: 'entity',
    importance: Math.min(1, 0.25 + Math.log2(1 + (degree.get(e.name) || 0)) / 5 + Math.min(0.2, e.observations.length / 40)),
    strength_now: 0.9, scope: 'owner', status: 'active', observations: e.observations, degree: degree.get(e.name) || 0,
  }));
  const edges = g.relations.filter((r) => names.has(r.from) && names.has(r.to))
    .map((r) => ({ src: r.from, dst: r.to, relation: r.relationType, weight: 0.5 }));
  return { nodes, edges, truncated: g.entities.length > kept.length, total: g.entities.length, shown: kept.length };
}

/** Stable colour per entity type (golden-angle hues, theme-neutral saturation). @param {string[]} types */
export function typePalette(types) {
  /** @type {Record<string, string>} */
  const out = {};
  [...types].sort().forEach((t, i) => { out[t] = t === UNDECLARED ? 'hsl(210 12% 60%)' : `hsl(${Math.round((i * 137.508 + 190) % 360)} 70% 62%)`; });
  return out;
}
