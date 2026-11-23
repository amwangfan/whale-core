// @ts-check
/**
 * 记忆 Memory: stats header · relationship graph (平面 2D / 星空 3D, optional 来源 nodes) / list /
 * candidate inbox · search with explanations · scope filter (server-side for the graph when one
 * scope is chosen) · 导入图谱 preview · detail side panel (/memory/:id).
 */
import { html, useMemo, useState, useEffect } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Badge, Button, AsyncButton, Chip, Empty, Segmented, Skeleton, ErrorBox, PageHead, Stat, cx, When, Drawer } from '../components/ui.js';
import { MemoryGraph, graphData } from '../components/memory-graph.js';
import { StarView } from '../components/star-view.js';
import { GraphImportButton, ImportPreview } from '../components/graph-import.js';
import { EvidenceModal } from '../components/evidence.js';
import { MemoryDetail, AddMemory } from '../components/memory-detail.js';
import { act, tryAct, useTopic } from '../lib/store.js';
import { useRpc, navigate, useDebounced, useDebouncedCallback, useLocal, useMedia } from '../lib/hooks.js';
import { KINDS, SCOPES, SCOPE_RULE, MEM_STATUS, scopeCat, scopeLabel, kindLabel, timeAgo, groupHref } from '../lib/format.js';

const SORTS = [
  { value: 'updated_at DESC', label: '最近更新' }, { value: 'created_at DESC', label: '最新创建' },
  { value: 'importance DESC', label: '最重要' }, { value: 'strength DESC', label: '最牢固' }, { value: 'access_count DESC', label: '最常想起' },
];
const STATUSES = [
  { value: 'active', label: '有效' }, { value: 'candidate', label: '候选' }, { value: 'superseded', label: '已被取代' },
  { value: 'archived', label: '已归档' }, { value: '', label: '全部' },
];

/** @param {{m: any, selected: boolean, onOpen: () => void, extra?: any}} p */
function MemoryRow({ m, selected, onOpen, extra }) {
  const cat = scopeCat(m.scope);
  return html`<li class=${cx('mem-row', selected && 'is-sel', `status-${m.status}`)} style=${{ '--kind': KINDS[m.kind]?.color }}>
    <button type="button" class="mem-row-main" onClick=${onOpen} aria-current=${selected ? 'true' : undefined}>
      <span class="mem-row-kind">${kindLabel(m.kind)}</span>
      <span class="mem-row-text">${m.content || html`<em class="muted">（已遗忘）</em>`}</span>
      <span class="mem-row-meta">
        <${Chip} color=${SCOPES[cat].color} title=${SCOPES[cat].hint}>${scopeLabel(m.scope)}<//>
        ${m.status !== 'active' ? html`<${Badge} tone=${MEM_STATUS[m.status]?.tone}>${MEM_STATUS[m.status]?.label}<//>` : null}
        ${(m.tags || []).slice(0, 3).map((t) => html`<span class="tag tag-sm" key=${t}>${t}</span>`)}
        <span class="imp" title=${`重要度 ${Math.round(m.importance * 100)}%`}><i style=${{ width: `${m.importance * 100}%` }}></i></span>
        <span class="muted small"><${When} ms=${m.updated_at} fmt=${timeAgo} /></span>
      </span>
    </button>
    ${extra}
  </li>`;
}

function StatsHeader({ stats }) {
  if (!stats) return html`<div class="stats-row"><${Skeleton} rows=${1} height=${48} /></div>`;
  const scope = stats.by_scope || {};
  return html`<div class="stats-row">
    <${Stat} label="有效记忆" value=${stats.active ?? 0} icon="memory" />
    <${Stat} label="候选知识" value=${stats.candidates ?? 0} tone=${stats.candidates ? 'warn' : ''} icon="inbox" />
    <${Stat} label="关系" value=${stats.links ?? 0} icon="graph" />
    <div class="stat stat-wide"><span class="stat-label">按范围</span>
      <span class="chips">${Object.keys(SCOPES).map((k) => html`<${Chip} key=${k} color=${SCOPES[k].color} title=${SCOPES[k].hint}>${SCOPES[k].label} ${scope[k] || 0}<//>`)}</span></div>
    <div class="stat stat-wide"><span class="stat-label">按类型</span>
      <span class="kind-strip" aria-hidden="true">${Object.entries(stats.by_kind || {}).map(([k, n]) => html`<span key=${k} title=${`${kindLabel(k)} ${n}`} style=${{ flex: n, background: KINDS[k]?.color }}></span>`)}</span></div>
  </div>`;
}

export function MemoryPage({ route }) {
  const selected = route.id || '';
  const [view, setView] = useLocal('memory.view', 'graph');
  const [dim, setDim] = useLocal('memory.dim', '2d');
  const [sources, setSources] = useLocal('memory.sources', false);
  const [imported, setImported] = useState(/** @type {any} */ (null));
  const [ev, setEv] = useState(/** @type {{memoryId: string, evidenceId: string}|null} */ (null));
  const v = route.query.view === 'inbox' ? 'inbox' : view;
  const [kinds, setKinds] = useState(/** @type {string[]} */ ([]));
  const [scopes, setScopes] = useState(/** @type {string[]} */ ([]));
  const [status, setStatus] = useState('active');
  const [order, setOrder] = useState('updated_at DESC');
  const [q, setQ] = useState(route.query.q || '');
  const [adding, setAdding] = useState(route.query.add === '1');
  const wide = useMedia('(min-width: 1100px)');
  const query = useDebounced(q.trim(), 350);
  useEffect(() => { if (route.query.q != null) setQ(route.query.q); }, [route.query.q]);

  const stats = useRpc('memory.stats');
  const serverScope = scopes.length === 1 ? scopes[0] : '';
  const graph = useRpc(v === 'graph' ? 'memory.graph' : null, { limit: 400, ...(sources ? { sources: true } : {}), ...(serverScope ? { scope: serverScope } : {}) },
    [v, sources, serverScope]);
  const gd = useMemo(() => graphData(graph.data), [graph.data]);
  const list = useRpc(v === 'list' && !query ? 'memory.list' : null,
    { status: status || null, kind: kinds.length === 1 ? kinds[0] : null, limit: 300, order: order === 'access_count DESC' ? 'updated_at DESC' : order }, [v, status, kinds.join(), order, query]);
  const inbox = useRpc(v === 'inbox' ? 'memory.list' : null, { status: 'candidate', limit: 200, order: 'created_at DESC' }, [v]);
  const search = useRpc(query ? 'memory.search' : null, { query, limit: 30 }, [query]);

  const reloadAll = useDebouncedCallback(() => { stats.reload(true); graph.reload(true); list.reload(true); inbox.reload(true); if (query) search.reload(true); }, 700);
  useTopic('memory.*', reloadAll);

  const matches = useMemo(() => (m) => (!kinds.length || kinds.includes(m.kind)) && (!scopes.length || scopes.includes(scopeCat(m.scope))), [kinds.join(), scopes.join()]);
  const toggle = (arr, set, k) => set(arr.includes(k) ? arr.filter((x) => x !== k) : [...arr, k]);
  const open = (id) => navigate(id ? `/memory/${id}${v === 'inbox' ? '?view=inbox' : ''}` : '/memory', { replace: !!selected });
  /** Source nodes: evidence opens the original message; a group space opens its 群聊 page. @param {any} n */
  const onExtra = (n) => {
    if (n.category === 'evidence') setEv({ memoryId: n.memory_id, evidenceId: n.evidence_id });
    else if (n.category === 'space' && String(n.id).startsWith('space:group:')) navigate(groupHref(String(n.id).slice('space:group:'.length)));
  };
  /** 3D view click: memory → detail, source node → as above. @param {any} n */
  const onPick = (n) => { if (!n) return; if (n.category) onExtra(n); else open(n.id); };
  const graphFilter = kinds.length || scopes.length > 1 ? matches : null;

  const filters = html`<div class="mem-filters">
    <div class="chips" role="group" aria-label="类型">${Object.entries(KINDS).map(([k, x]) => html`<${Chip} key=${k} color=${x.color} active=${kinds.includes(k)} title=${x.hint}
      onClick=${() => toggle(kinds, setKinds, k)}>${x.label}<//>`)}</div>
    <div class="chips" role="group" aria-label="范围">${Object.entries(SCOPES).map(([k, x]) => html`<${Chip} key=${k} color=${x.color} active=${scopes.includes(k)} title=${x.hint}
      onClick=${() => toggle(scopes, setScopes, k)}>${x.label}<//>`)}</div>
    ${v === 'list' && !query ? html`<div class="mem-filter-selects">
      <select class="input input-sm" value=${status} aria-label="状态" onChange=${(e) => setStatus(e.currentTarget.value)}>${STATUSES.map((s) => html`<option key=${s.value} value=${s.value}>${s.label}</option>`)}</select>
      <select class="input input-sm" value=${order} aria-label="排序" onChange=${(e) => setOrder(e.currentTarget.value)}>${SORTS.map((s) => html`<option key=${s.value} value=${s.value}>${s.label}</option>`)}</select>
    </div>` : null}
  </div>`;

  let body;
  if (query) {
    const hits = (search.data?.hits || []).filter(matches);
    body = html`<div class="mem-list-wrap">
      <p class="muted small pad-x">检索与对话时用的是同一个召回器：关键词 + 语义 + 时间衰减 + 重要度 + 强度。每条结果都写明了命中原因。</p>
      ${search.loading && !search.data ? html`<${Skeleton} rows=${5} height=${44} />` : hits.length ? html`<ol class="mem-list">${hits.map((m) => html`<${MemoryRow} key=${m.id} m=${m} selected=${m.id === selected} onOpen=${() => open(m.id)}
        extra=${html`<span class="mem-why"><span class="mem-hit-score">${Number(m.score).toFixed(2)}</span>${(m.why || []).map((w) => html`<span class="why" key=${w}>${w}</span>`)}</span>`} />`)}</ol>`
        : html`<${Empty} icon="search" title=${`没有想起与「${query}」相关的记忆`} compact />`}
    </div>`;
  } else if (v === 'graph') {
    const shown = graph.data?.nodes?.length || 0;
    body = imported ? html`<${ImportPreview} data=${imported} onClose=${() => setImported(null)} />`
      : graph.error ? html`<${ErrorBox} error=${graph.error} onRetry=${() => graph.reload()} />`
        : !graph.data ? html`<${Skeleton} rows=${8} height=${30} />`
          : html`${dim === '3d'
            ? html`<${StarView} nodes=${gd.nodes} edges=${gd.edges} selected=${selected} filter=${graphFilter} onPick=${onPick} />`
            : html`<${MemoryGraph} nodes=${gd.nodes} edges=${gd.edges} selected=${selected} filter=${graphFilter}
              onSelect=${(id) => open(id)} onExtra=${onExtra} kindFilter=${kinds} onToggleKind=${(k) => toggle(kinds, setKinds, k)} />`}
            ${graph.data.truncated ? html`<p class="muted small pad-x">图上显示了最重要的 ${shown} 条记忆（共 ${graph.data.total ?? shown} 条）。用筛选或搜索查看其余的。</p>` : null}`;
  } else if (v === 'inbox') {
    const items = (inbox.data?.items || []).filter(matches);
    body = html`<div class="mem-list-wrap">
      <div class="banner banner-info small"><${Icon} name="inbox" size=${16} />候选知识来自鲸鱼的自主探索：它读到觉得值得记住的东西。采纳后才会在对话中使用；30 天未处理会自动过期。</div>
      ${!inbox.data ? html`<${Skeleton} rows=${4} height=${44} />` : items.length ? html`<ol class="mem-list">${items.map((m) => html`<${MemoryRow} key=${m.id} m=${m} selected=${m.id === selected} onOpen=${() => open(m.id)}
        extra=${html`<span class="row-actions">
          <${AsyncButton} size="sm" variant="primary" icon="check" onClick=${async () => { await act('memory.update', { id: m.id, status: 'active' }, { ok: '已采纳' }); inbox.reload(true); stats.reload(true); }}>采纳<//>
          <${AsyncButton} size="sm" variant="ghost" icon="archive" onClick=${async () => { await act('memory.update', { id: m.id, status: 'archived' }, { ok: '已忽略' }); inbox.reload(true); stats.reload(true); }}>忽略<//>
        </span>`} />`)}</ol>` : html`<${Empty} icon="inbox" title="收件箱是空的" hint="开启 设置 → 自主 → 自主探索 后，鲸鱼会在空闲时按兴趣学习新知识。" compact />`}
    </div>`;
  } else {
    const items = (list.data?.items || []).filter(matches);
    // the server sorts by updated/created/importance/strength; "most recalled" is sorted here
    if (order === 'access_count DESC') items.sort((a, b) => (b.access_count || 0) - (a.access_count || 0));
    body = html`<div class="mem-list-wrap">
      ${list.error ? html`<${ErrorBox} error=${list.error} onRetry=${() => list.reload()} />` : null}
      ${!list.data ? html`<${Skeleton} rows=${6} height=${44} />` : items.length ? html`<ol class="mem-list">${items.map((m) => html`<${MemoryRow} key=${m.id} m=${m} selected=${m.id === selected} onOpen=${() => open(m.id)} />`)}</ol>`
        : html`<${Empty} icon="memory" title="没有符合条件的记忆" hint="换个筛选条件，或者手动添加一条。" compact><${Button} size="sm" variant="primary" icon="plus" onClick=${() => setAdding(true)}>添加记忆<//><//>`}
      ${list.data && list.data.total > items.length && !kinds.length && !scopes.length ? html`<p class="muted small pad-x">显示前 ${items.length} / ${list.data.total} 条</p>` : null}
    </div>`;
  }

  const detail = selected ? html`<${MemoryDetail} id=${selected} onClose=${() => open('')} onChanged=${reloadAll} />` : null;
  return html`<div class=${cx('page', 'memory', selected && wide && 'has-detail')}>
    <${PageHead} title="记忆" icon="memory" subtitle="鲸鱼记得的一切：从对话、手机和探索中提炼，带着证据与关系"
      actions=${html`<${AsyncButton} variant="ghost" icon="broom" onClick=${() => tryAct('memory.maintain', {}, { ok: '整理完成：衰减与归档已执行' })}>立即整理<//>
        <${Button} variant="primary" icon="plus" onClick=${() => setAdding(true)}>添加记忆<//>`} />
    <${StatsHeader} stats=${stats.data} />
    <p class="rule-hint"><${Icon} name="lock" size=${14} />${SCOPE_RULE}</p>
    <div class="mem-toolbar">
      <${Segmented} label="视图" value=${query ? '' : v} onChange=${(x) => { setQ(''); setView(x); setImported(null); if (route.query.view) navigate('/memory', { replace: true }); }}
        options=${[{ value: 'graph', label: html`<${Icon} name="graph" size=${15} /> 图谱` }, { value: 'list', label: html`<${Icon} name="rows" size=${15} /> 列表` },
          { value: 'inbox', label: html`<${Icon} name="inbox" size=${15} /> 候选${stats.data?.candidates ? html` <span class="seg-count">${stats.data.candidates}</span>` : ''}` }]} />
      <div class="search-box"><${Icon} name="search" size=${16} />
        <input type="search" value=${q} placeholder="像鲸鱼一样回忆：输入一句话…" aria-label="搜索记忆" onInput=${(e) => setQ(e.currentTarget.value)} />
        ${q ? html`<button type="button" class="icon-btn" aria-label="清除" onClick=${() => setQ('')}><${Icon} name="x" size=${14} /></button>` : null}</div>
    </div>
    ${filters}
    ${v === 'graph' && !query ? html`<div class="graph-bar" role="toolbar" aria-label="图谱选项">
      ${imported ? html`<span class="muted small"><${Icon} name="eye" size=${14} /> 正在预览导入的图谱</span>` : html`
        <${Segmented} size="sm" label="图谱样式" value=${dim} onChange=${setDim}
          options=${[{ value: '2d', label: html`<${Icon} name="graph" size=${14} /> 平面` }, { value: '3d', label: html`<${Icon} name="stars" size=${14} /> 星空` }]} />
        <label class="check-inline small" title="加入记忆的主体（人物）、依据（原始消息）和所在空间">
          <input type="checkbox" checked=${!!sources} onChange=${(e) => setSources(e.currentTarget.checked)} />来源</label>
        ${serverScope ? html`<span class="muted small">只看「${SCOPES[serverScope]?.label || serverScope}」范围</span>` : null}`}
      <span class="grow"></span>
      <${GraphImportButton} onLoaded=${(d) => setImported(d)} />
    </div>` : null}
    <div class="mem-body">
      <div class="mem-view">${body}</div>
      ${wide && selected ? html`<aside class="mem-side card" aria-label="记忆详情">
        <header class="card-head"><div class="card-titles"><h3 class="card-title">记忆详情</h3></div>
          <button type="button" class="icon-btn" aria-label="关闭详情" onClick=${() => open('')}><${Icon} name="x" /></button></header>
        <div class="card-body scroll">${detail}</div></aside>` : null}
    </div>
    ${!wide ? html`<${Drawer} open=${!!selected} onClose=${() => open('')} title="记忆详情">${detail}<//>` : null}
    <${AddMemory} open=${adding} onClose=${() => { setAdding(false); if (route.query.add) navigate('/memory', { replace: true }); }} onCreated=${(res) => { reloadAll(); if (res?.item?.id) open(res.item.id); }} />
    <${EvidenceModal} open=${!!ev} onClose=${() => setEv(null)} memoryId=${ev?.memoryId} evidenceId=${ev?.evidenceId} />
  </div>`;
}
