// @ts-check
/**
 * 依据 (evidence): the real messages a memory was built from, fetched one by one with
 * `memory.evidence {id, evidence_id}`. Used by group portraits/claims, memory detail and the graph.
 */
import { html, useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Button, Modal, Skeleton, cx } from './ui.js';
import { useRpc, useEscape } from '../lib/hooks.js';
import { EVIDENCE_REASONS, fmtTime } from '../lib/format.js';

/** Evidence ids recorded on a memory's source (`evidence_ids[]` or a single `evidence_id`). @param {any} source */
export function evidenceIdsOf(source) {
  const s = source || {};
  const ids = Array.isArray(s.evidence_ids) ? s.evidence_ids : [];
  return [...new Set([...ids, ...(s.evidence_id ? [s.evidence_id] : [])].filter(Boolean).map(String))];
}

/** @param {{memoryId: string, evidenceId: string}} p */
export function EvidenceItem({ memoryId, evidenceId }) {
  const res = useRpc('memory.evidence', { id: memoryId, evidence_id: evidenceId }, [memoryId, evidenceId]);
  if (res.loading && !res.data) return html`<li class="ev-item is-loading"><${Skeleton} rows=${2} height=${12} /></li>`;
  if (res.error) return html`<li class="ev-item is-missing"><${Icon} name="alert" size=${14} /><span>读取失败：${res.error.message || '未知错误'}</span>
    <${Button} size="sm" variant="plain" icon="refresh" onClick=${() => res.reload()} aria-label="重试" /></li>`;
  const ev = res.data || {};
  if (!ev.available) {
    return html`<li class="ev-item is-missing"><${Icon} name="info" size=${14} /><span>${EVIDENCE_REASONS[ev.reason] || ev.message || '原文暂不可查看'}</span></li>`;
  }
  return html`<li class="ev-item">
    <div class="ev-head"><strong>${ev.sender_name || ev.sender_id || '群友'}</strong><span class="muted small">${fmtTime(ev.sent_at)}</span></div>
    <p class="ev-text">${ev.text || html`<em class="muted">（无文字内容）</em>`}</p>
    ${ev.note ? html`<p class="muted small ev-note">${ev.note}</p>` : null}
  </li>`;
}

/** @param {{memoryId: string, ids: string[], max?: number}} p */
export function EvidenceList({ memoryId, ids, max = 3 }) {
  const [limit, setLimit] = useState(max);
  const list = (ids || []).slice(0, limit);
  if (!memoryId || !list.length) return html`<p class="muted small">没有记录原始消息。</p>`;
  return html`<div class="ev-list-wrap">
    <ul class="ev-list">${list.map((e) => html`<${EvidenceItem} key=${e} memoryId=${memoryId} evidenceId=${e} />`)}</ul>
    ${ids.length > limit ? html`<button type="button" class="linkish small" onClick=${() => setLimit(limit + max)}>再看 ${Math.min(max, ids.length - limit)} 条（共 ${ids.length} 条）</button>` : null}
  </div>`;
}

/**
 * Button that opens a small popover with up to three evidence messages.
 * @param {{memoryId: string, ids: string[], label?: string, initialOpen?: boolean, title?: string}} p
 */
export function EvidenceButton({ memoryId, ids, label = '依据', initialOpen = false, title }) {
  const [open, setOpen] = useState(initialOpen);
  const box = useRef(/** @type {HTMLElement|null} */ (null));
  useEscape(open, () => setOpen(false));
  useEffect(() => {
    if (!open) return undefined;
    const fn = (e) => { if (box.current && !box.current.contains(/** @type {any} */ (e.target))) setOpen(false); };
    globalThis.document?.addEventListener?.('pointerdown', fn);
    return () => globalThis.document?.removeEventListener?.('pointerdown', fn);
  }, [open]);
  const n = (ids || []).length;
  return html`<span class=${cx('ev-pop-wrap', open && 'is-open')} ref=${box}>
    <button type="button" class="ev-btn" aria-expanded=${open ? 'true' : 'false'} disabled=${!n || !memoryId}
      title=${n ? `查看 ${Math.min(3, n)} 条原始消息` : '没有记录原始消息'} onClick=${() => setOpen(!open)}>
      <${Icon} name="quote" size=${13} />${label}${n ? html`<small>${n}</small>` : null}</button>
    ${open ? html`<div class="ev-pop" role="dialog" aria-label=${title || '依据：原始消息'}>
      <header class="ev-pop-head"><span><${Icon} name="quote" size=${14} />${title || '依据：原始消息'}</span>
        <button type="button" class="icon-btn" aria-label="关闭" onClick=${() => setOpen(false)}><${Icon} name="x" size=${14} /></button></header>
      <${EvidenceList} memoryId=${memoryId} ids=${ids} max=${3} />
      <p class="muted small ev-foot"><${Icon} name="lock" size=${12} />原文只保存在本机存档里，不会直接进入提示词。</p>
    </div>` : null}
  </span>`;
}

/** Modal for one evidence node clicked in the graph. @param {{open: boolean, onClose: () => void, memoryId?: string, evidenceId?: string}} p */
export function EvidenceModal({ open, onClose, memoryId, evidenceId }) {
  return html`<${Modal} open=${open} onClose=${onClose} title="依据：原始消息" footer=${html`<${Button} variant="ghost" onClick=${onClose}>关闭<//>`}>
    ${open && memoryId && evidenceId ? html`<ul class="ev-list"><${EvidenceItem} memoryId=${memoryId} evidenceId=${evidenceId} /></ul>` : html`<p class="muted small">这个节点没有关联的记忆。</p>`}
    <p class="muted small"><${Icon} name="lock" size=${12} /> 原文只保存在本机存档里；鲸鱼只会用从中整理出的记忆。</p>
  <//>`;
}
