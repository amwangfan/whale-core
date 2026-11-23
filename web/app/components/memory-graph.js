// @ts-check
/** Memory relationship graph: canvas + legend + tooltip around lib/graph.js (memory nodes and optional source nodes). */
import { html, useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { cx } from './ui.js';
import { NodeTip, SOURCE_CATS } from './star-view.js';
import { ForceGraph } from '../lib/graph.js';
import { useStore } from '../lib/store.js';
import { KINDS, SCOPES } from '../lib/format.js';

/**
 * Merge `memory.graph` extras into one node/edge list (source edges flagged `extra`).
 * @param {any} data `memory.graph` result
 */
export function graphData(data) {
  if (!data) return { nodes: [], edges: [] };
  const extraNodes = (data.extra_nodes || []).map((n) => ({ ...n, content: n.label || n.id, importance: n.category === 'space' ? 0.9 : 0.4, strength_now: 0.9 }));
  const extraEdges = (data.extra_edges || []).map((e) => ({ ...e, weight: 0.25, extra: true }));
  return { nodes: [...(data.nodes || []), ...extraNodes], edges: [...(data.edges || []), ...extraEdges] };
}

/**
 * @param {{nodes: any[], edges: any[], selected?: string, filter?: ((n: any) => boolean)|null, onSelect: (id: string|null) => void,
 *   onExtra?: (n: any) => void, kindFilter: string[], onToggleKind: (k: string) => void}} p
 */
export function MemoryGraph({ nodes, edges, selected, filter, onSelect, onExtra, kindFilter, onToggleKind }) {
  const canvas = useRef(/** @type {HTMLCanvasElement|null} */ (null));
  const graph = useRef(/** @type {ForceGraph|null} */ (null));
  const [tip, setTip] = useState(/** @type {{n: any, x: number, y: number}|null} */ (null));
  const theme = useStore((s) => s.theme);
  const reduce = useStore((s) => s.reduceMotion);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const onExtraRef = useRef(onExtra);
  onExtraRef.current = onExtra;

  useEffect(() => {
    if (!canvas.current || typeof requestAnimationFrame === 'undefined') return undefined;
    const g = new ForceGraph(canvas.current, {
      reduceMotion: reduce,
      onHover: (n, x, y) => setTip(n ? { n, x, y } : null),
      onClick: (n) => { const c = /** @type {any} */ (n)?.category; if (c) onExtraRef.current?.(n); else onSelectRef.current(n ? n.id : null); },
    });
    graph.current = g;
    return () => { g.destroy(); graph.current = null; };
  }, []);
  useEffect(() => {
    const g = graph.current;
    if (!g) return;
    const first = !g.nodes.length;
    g.setData(nodes, edges);
    if (first) setTimeout(() => g.fit(), reduce ? 0 : 900);
  }, [nodes, edges]);
  useEffect(() => { graph.current?.setFilter(filter ? (n) => !!(/** @type {any} */ (n).category) || filter(n) : null); }, [filter]);
  useEffect(() => { graph.current?.select(selected || null); }, [selected]);
  useEffect(() => { const t = setTimeout(() => graph.current?.refreshColors(), 30); return () => clearTimeout(t); }, [theme]);

  const counts = {};
  const cats = {};
  let memories = 0;
  nodes.forEach((n) => {
    if (n.category) cats[n.category] = (cats[n.category] || 0) + 1;
    else { counts[n.kind] = (counts[n.kind] || 0) + 1; memories++; }
  });
  return html`<div class="graph-wrap">
    <canvas ref=${canvas} class="graph-canvas" role="img"
      aria-label=${`记忆关系图：${memories} 条记忆，${edges.length} 条关系。可用列表视图以键盘浏览。`}></canvas>
    ${!nodes.length ? html`<div class="graph-empty"><${Icon} name="graph" size=${28} /><p>还没有可以画出来的记忆</p></div>` : null}
    <div class="graph-tools" role="toolbar" aria-label="图谱缩放">
      <button type="button" class="icon-btn" aria-label="放大" onClick=${() => graph.current?.zoomBy(1.25)}><${Icon} name="zoomIn" /></button>
      <button type="button" class="icon-btn" aria-label="缩小" onClick=${() => graph.current?.zoomBy(0.8)}><${Icon} name="zoomOut" /></button>
      <button type="button" class="icon-btn" aria-label="适应窗口" onClick=${() => graph.current?.fit()}><${Icon} name="fit" /></button>
    </div>
    <div class="graph-legend">
      <div class="legend-row" role="group" aria-label="按类型筛选">
        ${Object.entries(KINDS).filter(([k]) => counts[k]).map(([k, v]) => html`<button type="button" key=${k}
          class=${cx('legend-item', kindFilter.length && !kindFilter.includes(k) && 'is-off')} aria-pressed=${!kindFilter.length || kindFilter.includes(k) ? 'true' : 'false'}
          onClick=${() => onToggleKind(k)} title=${v.hint}><i style=${{ background: v.color }}></i>${v.label}<small>${counts[k]}</small></button>`)}
      </div>
      ${Object.keys(cats).length ? html`<div class="legend-row legend-meta" aria-label="来源节点">
        ${Object.entries(SOURCE_CATS).filter(([k]) => cats[k]).map(([k, v]) => html`<span key=${k} class="legend-item" title=${v.hint}><i class=${'shape shape-' + k}></i>${v.label}<small>${cats[k]}</small></span>`)}
        <span class="legend-item"><i class="line dotted"></i>主体 / 依据 / 包含（悬停显示）</span>
      </div>` : null}
      <div class="legend-row legend-meta">
        ${Object.entries(SCOPES).map(([k, v]) => html`<span key=${k} class="legend-item" title=${v.hint}><i class="ring" style=${{ borderColor: v.color }}></i>${v.label}</span>`)}
        <span class="legend-item"><i class="line"></i>相关</span>
        <span class="legend-item"><i class="line dashed"></i>取代 →</span>
        <span class="legend-item muted">大小 = 重要度 · 透明度 = 记忆强度</span>
      </div>
    </div>
    ${tip ? html`<div class="graph-tip" style=${{ left: `${Math.min(tip.x + 14, 9999)}px`, top: `${tip.y + 14}px` }} role="tooltip"><${NodeTip} n=${tip.n} /></div>` : null}
  </div>`;
}
