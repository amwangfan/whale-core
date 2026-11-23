// @ts-check
/** 星空 view: the memory graph as a slowly rotating 3D starfield (lib/star3d.js) with an optional 性能 overlay. */
import { html, useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { cx } from './ui.js';
import { StarField } from '../lib/star3d.js';
import { useStore } from '../lib/store.js';
import { KINDS, kindLabel, scopeLabel, clip } from '../lib/format.js';

export const SOURCE_CATS = {
  person: { label: '人物', hint: '记忆的主体（群友、联系人）' },
  evidence: { label: '依据', hint: '一条原始消息，点击查看原文' },
  space: { label: '空间', hint: '记忆所在的作用域（如某个群）' },
};

/** Tooltip body shared by the 2D and 3D views. @param {{n: any}} p */
export function NodeTip({ n }) {
  if (n.category && SOURCE_CATS[n.category]) {
    return html`<span class="graph-tip-kind">${SOURCE_CATS[n.category].label}</span><p>${clip(n.label || n.id, 120)}</p>
      <span class="muted small">${SOURCE_CATS[n.category].hint}</span>`;
  }
  return html`<span class="graph-tip-kind" style=${{ color: KINDS[n.kind]?.color }}>${kindLabel(n.kind)} · ${scopeLabel(n.scope || 'owner')}</span>
    <p>${clip(n.content, 140)}</p><span class="muted small">重要度 ${Math.round((n.importance ?? 0) * 100)}% · 强度 ${Math.round((n.strength_now ?? 0) * 100)}%</span>`;
}

/**
 * @param {{nodes: any[], edges: any[], selected?: string, filter?: ((n: any) => boolean)|null, onPick: (n: any|null) => void, initialPerf?: boolean}} p
 */
export function StarView({ nodes, edges, selected, filter, onPick, initialPerf = false }) {
  const canvas = useRef(/** @type {HTMLCanvasElement|null} */ (null));
  const field = useRef(/** @type {StarField|null} */ (null));
  const reduce = useStore((s) => s.reduceMotion);
  const theme = useStore((s) => s.theme);
  const [tip, setTip] = useState(/** @type {any} */ (null));
  const [perf, setPerf] = useState(initialPerf);
  const [spin, setSpin] = useState(!reduce);
  const [stats, setStats] = useState({ fps: 0, nodes: nodes.length, edges: edges.length, drawn: 0 });
  const pick = useRef(onPick);
  pick.current = onPick;

  useEffect(() => {
    if (!canvas.current || typeof requestAnimationFrame === 'undefined') return undefined;
    const f = new StarField(canvas.current, { reduceMotion: reduce, onHover: (n, x, y) => setTip(n ? { n, x, y } : null), onClick: (n) => pick.current(n) });
    field.current = f;
    return () => { f.destroy(); field.current = null; };
  }, []);
  useEffect(() => { field.current?.setData(nodes, edges); }, [nodes, edges]);
  useEffect(() => { field.current?.setFilter(filter || null); }, [filter]);
  useEffect(() => { field.current?.select(selected || null); }, [selected]);
  useEffect(() => { field.current?.setSpin(spin); }, [spin]);
  useEffect(() => { const t = setTimeout(() => field.current?.refreshColors(), 30); return () => clearTimeout(t); }, [theme]);
  useEffect(() => {
    if (!perf) return undefined;
    const t = setInterval(() => { const s = field.current?.stats; if (s) setStats({ ...s }); }, 500);
    return () => clearInterval(t);
  }, [perf]);

  return html`<div class="graph-wrap star-wrap">
    <canvas ref=${canvas} class="graph-canvas star-canvas" role="img"
      aria-label=${`记忆星空：${nodes.length} 颗星、${edges.length} 条连线。拖动旋转，滚轮缩放。可用列表视图以键盘浏览。`}></canvas>
    ${!nodes.length ? html`<div class="graph-empty"><${Icon} name="stars" size=${28} /><p>星空里还没有星星</p></div>` : null}
    <div class="graph-tools" role="toolbar" aria-label="星空控制">
      <button type="button" class="icon-btn" aria-label="放大" onClick=${() => field.current?.zoomBy(1.25)}><${Icon} name="zoomIn" /></button>
      <button type="button" class="icon-btn" aria-label="缩小" onClick=${() => field.current?.zoomBy(0.8)}><${Icon} name="zoomOut" /></button>
      <button type="button" class="icon-btn" aria-label="回到初始视角" onClick=${() => field.current?.reset()}><${Icon} name="fit" /></button>
      <button type="button" class="icon-btn" aria-pressed=${spin ? 'true' : 'false'} disabled=${reduce} aria-label=${spin ? '停止旋转' : '缓慢旋转'}
        title=${reduce ? '系统设置了减少动态效果' : spin ? '停止旋转' : '缓慢旋转'} onClick=${() => setSpin(!spin)}><${Icon} name=${spin ? 'pause' : 'play'} /></button>
    </div>
    <label class="star-perf-toggle small"><input type="checkbox" checked=${perf} onChange=${(e) => setPerf(e.currentTarget.checked)} />性能</label>
    ${perf ? html`<div class="star-perf mono" aria-live="off">${Math.round(stats.fps)} fps · ${stats.nodes} 星 · ${stats.edges} 线 · 绘制 ${stats.drawn}</div>` : null}
    <p class="star-hint muted small">拖动旋转 · 滚轮缩放 · 点击一颗星查看</p>
    ${tip ? html`<div class=${cx('graph-tip')} style=${{ left: `${tip.x + 14}px`, top: `${tip.y + 14}px` }} role="tooltip"><${NodeTip} n=${tip.n} /></div>` : null}
  </div>`;
}
