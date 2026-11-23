// @ts-check
/**
 * 导入图谱: preview an MCP knowledge-graph file (JSON / JSONL) as its own graph. Read-only: nothing is
 * sent to the server, and the preview is dropped when the owner leaves it.
 */
import { html, useEffect, useMemo, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Button, Chip, cx } from './ui.js';
import { ForceGraph } from '../lib/graph.js';
import { parseMcpGraph, toPreview, typePalette, MAX_IMPORT_BYTES, MAX_PREVIEW_NODES } from '../lib/mcp-graph.js';
import { toast, useStore } from '../lib/store.js';
import { clip } from '../lib/format.js';

/** File picker that parses a knowledge-graph file. @param {{onLoaded: (d: {graph: any, fileName: string}) => void}} p */
export function GraphImportButton({ onLoaded }) {
  const input = useRef(/** @type {HTMLInputElement|null} */ (null));
  const take = async (f) => {
    if (!f) return;
    if (f.size > MAX_IMPORT_BYTES) { toast({ kind: 'error', title: '文件太大', body: `${(f.size / 1048576).toFixed(1)} MiB，预览上限是 8 MiB` }); return; }
    try {
      const graph = parseMcpGraph(await f.text());
      onLoaded({ graph, fileName: f.name });
    } catch (e) { toast({ kind: 'error', title: '无法读取这个图谱文件', body: String(/** @type {any} */ (e)?.message || e) }); }
    if (input.current) input.current.value = '';
  };
  return html`<span class="import-btn">
    <input ref=${input} type="file" class="sr-only" accept=".json,.jsonl,.ndjson,application/json" tabindex="-1" aria-hidden="true"
      onChange=${(e) => take(e.currentTarget.files?.[0])} />
    <${Button} variant="ghost" icon="upload" title="预览一个 MCP 知识图谱文件（JSON / JSONL，≤ 8 MiB，不会保存）" onClick=${() => input.current?.click()}>导入图谱<//>
  </span>`;
}

/** @param {{nodes: any[], edges: any[], colorOf: (n: any) => string, groups: string[], filter: ((n: any) => boolean)|null, selected: string, onSelect: (id: string) => void}} p */
function PreviewCanvas({ nodes, edges, colorOf, groups, filter, selected, onSelect }) {
  const canvas = useRef(/** @type {HTMLCanvasElement|null} */ (null));
  const graph = useRef(/** @type {ForceGraph|null} */ (null));
  const reduce = useStore((s) => s.reduceMotion);
  const [tip, setTip] = useState(/** @type {any} */ (null));
  const sel = useRef(onSelect);
  sel.current = onSelect;
  useEffect(() => {
    if (!canvas.current || typeof requestAnimationFrame === 'undefined') return undefined;
    const g = new ForceGraph(canvas.current, { reduceMotion: reduce, colorOf, groups, onHover: (n, x, y) => setTip(n ? { n, x, y } : null), onClick: (n) => sel.current(n ? n.id : '') });
    g.setData(nodes, edges);
    setTimeout(() => g.fit(), reduce ? 0 : 1200);
    graph.current = g;
    return () => { g.destroy(); graph.current = null; };
  }, [nodes]);
  useEffect(() => { graph.current?.setFilter(filter); }, [filter]);
  useEffect(() => { if (selected) graph.current?.focus(selected); else graph.current?.select(null); }, [selected]);
  return html`<div class="graph-wrap import-graph">
    <canvas ref=${canvas} class="graph-canvas" role="img" aria-label=${`导入的知识图谱：${nodes.length} 个实体、${edges.length} 条关系`}></canvas>
    <div class="graph-tools" role="toolbar" aria-label="图谱缩放">
      <button type="button" class="icon-btn" aria-label="放大" onClick=${() => graph.current?.zoomBy(1.25)}><${Icon} name="zoomIn" /></button>
      <button type="button" class="icon-btn" aria-label="缩小" onClick=${() => graph.current?.zoomBy(0.8)}><${Icon} name="zoomOut" /></button>
      <button type="button" class="icon-btn" aria-label="适应窗口" onClick=${() => graph.current?.fit()}><${Icon} name="fit" /></button>
    </div>
    ${tip ? html`<div class="graph-tip" style=${{ left: `${tip.x + 14}px`, top: `${tip.y + 14}px` }} role="tooltip">
      <span class="graph-tip-kind" style=${{ color: colorOf(tip.n) }}>${tip.n.kind}</span><p>${tip.n.content}</p>
      <span class="muted small">${tip.n.observations?.length || 0} 条观察 · ${tip.n.degree} 条关系</span></div>` : null}
  </div>`;
}

/**
 * @param {{data: {graph: any, fileName: string}, onClose: () => void}} p
 */
export function ImportPreview({ data, onClose }) {
  const { graph, fileName } = data;
  const preview = useMemo(() => toPreview(graph, MAX_PREVIEW_NODES), [graph]);
  const types = useMemo(() => Object.entries(graph.types).sort((a, b) => b[1] - a[1]), [graph]);
  const palette = useMemo(() => typePalette(types.map(([t]) => t)), [types]);
  const colorOf = useMemo(() => (n) => palette[n.kind] || 'hsl(190 70% 60%)', [palette]);
  const [hidden, setHidden] = useState(/** @type {string[]} */ ([]));
  const [q, setQ] = useState('');
  const [sel, setSel] = useState('');
  const query = q.trim().toLowerCase();
  const filter = useMemo(() => (hidden.length || query ? (n) => !hidden.includes(n.kind)
    && (!query || n.id.toLowerCase().includes(query) || (n.observations || []).some((o) => o.toLowerCase().includes(query))) : null), [hidden.join('|'), query]);
  const byName = useMemo(() => new Map(graph.entities.map((e) => [e.name, e])), [graph]);
  const ent = sel ? byName.get(sel) : null;
  const out = ent ? graph.relations.filter((r) => r.from === sel) : [];
  const inc = ent ? graph.relations.filter((r) => r.to === sel) : [];
  const hits = query ? graph.entities.filter((e) => e.name.toLowerCase().includes(query) || e.observations.some((o) => o.toLowerCase().includes(query))).slice(0, 12) : [];
  const toggle = (t) => setHidden(hidden.includes(t) ? hidden.filter((x) => x !== t) : [...hidden, t]);

  return html`<div class="import-preview">
    <div class="banner banner-info small import-banner"><${Icon} name="eye" size=${16} />
      <div><strong>预览：${fileName}</strong><p>${graph.entities.length} 个实体 · ${graph.relations.length} 条关系${graph.skipped ? ` · ${graph.skipped} 行无法识别已跳过` : ''}${graph.implicit ? ` · ${graph.implicit} 个实体只出现在关系里` : ''}。只在本页预览，不会保存到鲸鱼的记忆里；离开即清除。</p>
        ${preview.truncated ? html`<p class="tone-text-warn">实体太多：只画出了关系最多的 ${preview.shown} 个（共 ${preview.total} 个）。搜索仍覆盖全部实体。</p>` : null}</div>
      <${Button} size="sm" variant="ghost" icon="x" onClick=${onClose}>关闭预览<//>
    </div>
    <div class="import-tools">
      <div class="chips" role="group" aria-label="按类型筛选">${types.map(([t, n]) => html`<${Chip} key=${t} color=${palette[t]} active=${!hidden.includes(t)} onClick=${() => toggle(t)}>${t}<span class="chip-count">${n}</span><//>`)}</div>
      <div class="search-box"><${Icon} name="search" size=${16} />
        <input type="search" value=${q} placeholder="搜索实体或观察…" aria-label="搜索导入的实体" onInput=${(e) => setQ(e.currentTarget.value)} /></div>
    </div>
    ${hits.length ? html`<ul class="import-hits" aria-label="搜索结果">${hits.map((e) => html`<li key=${e.name}><button type="button" class=${cx('linkish', e.name === sel && 'is-on')} onClick=${() => setSel(e.name)}>
      <i class="kind-dot" style=${{ background: palette[e.entityType] }}></i>${e.name}</button><span class="muted small">${e.entityType}</span></li>`)}</ul>` : null}
    <div class=${cx('import-body', ent && 'has-detail')}>
      <${PreviewCanvas} nodes=${preview.nodes} edges=${preview.edges} colorOf=${colorOf} groups=${types.slice(0, 12).map(([t]) => t)} filter=${filter} selected=${sel} onSelect=${setSel} />
      ${ent ? html`<aside class="card import-detail" aria-label="实体详情">
        <header class="card-head"><div class="card-titles"><h3 class="card-title">${ent.name}</h3><p class="card-sub"><${Chip} color=${palette[ent.entityType]}>${ent.entityType}<//></p></div>
          <button type="button" class="icon-btn" aria-label="关闭详情" onClick=${() => setSel('')}><${Icon} name="x" /></button></header>
        <div class="card-body scroll">
          <h4>观察 <span class="muted">${ent.observations.length}</span></h4>
          ${ent.observations.length ? html`<ul class="obs">${ent.observations.map((o, i) => html`<li key=${i}>${o}</li>`)}</ul>` : html`<p class="muted small">没有观察记录。</p>`}
          <h4>关系 <span class="muted">${out.length + inc.length}</span></h4>
          <ul class="relations">${[...out.map((r) => ({ r, other: r.to, dir: '→' })), ...inc.map((r) => ({ r, other: r.from, dir: '←' }))].slice(0, 60).map(({ r, other, dir }) => html`<li key=${r.from + r.relationType + r.to}>
            <span class="rel-kind">${clip(r.relationType, 14)} ${dir}</span><button type="button" class="linkish" onClick=${() => setSel(other)}>${other}</button><span></span></li>`)}</ul>
        </div></aside>` : null}
    </div>
  </div>`;
}
