// @ts-check
/** 群空间 detail views: 话题时间线 · 关键词 · 群友（画像/喜好/依据） · 成员发言. */
import { html, useEffect, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Badge, Button, AsyncButton, Chip, Empty, Modal, Skeleton, Switch, ErrorBox, cx } from './ui.js';
import { EvidenceButton } from './evidence.js';
import { act, toast } from '../lib/store.js';
import { rpc, errorText } from '../lib/api.js';
import { useRpc } from '../lib/hooks.js';
import { TRAIT_KINDS, KEYWORD_TYPES, fmtSpan, fmtTime, fmtDate, daySpan, clip } from '../lib/format.js';

/** Text with every occurrence of `word` marked. @param {{text: string, word?: string}} p */
function Marked({ text, word }) {
  const t = String(text || '');
  if (!word || !t.includes(word)) return t;
  const parts = t.split(word);
  return parts.map((s, i) => html`${s}${i < parts.length - 1 ? html`<mark>${word}</mark>` : ''}`);
}

/** @param {{m: any, groupId: string, word?: string, focus?: boolean}} p */
export function MessageRow({ m, groupId, word, focus }) {
  const [ctx, setCtx] = useState(/** @type {any[]|null} */ (null));
  const [busy, setBusy] = useState(false);
  const load = async () => {
    if (ctx) { setCtx(null); return; }
    setBusy(true);
    try { setCtx((await rpc('groups.context', { id: groupId, message_id: m.message_id })).items || []); } catch (e) { toast({ kind: 'error', title: '读取上下文失败', body: errorText(e) }); } finally { setBusy(false); }
  };
  return html`<li class=${cx('msg', focus && 'is-focus')}>
    <div class="msg-head"><strong>${m.display_name || m.sender_name || m.sender_id}</strong>
      ${m.display_name && m.sender_name && m.display_name !== m.sender_name ? html`<span class="muted small">${m.sender_name}</span>` : null}
      <time class="muted small">${fmtTime(m.sent_at)}</time>
      ${focus ? null : html`<button type="button" class="linkish small msg-ctx" aria-expanded=${ctx ? 'true' : 'false'} onClick=${load}>${busy ? '读取中…' : ctx ? '收起上下文' : '看上下文'}</button>`}</div>
    <p class="msg-text"><${Marked} text=${m.text} word=${word} /></p>
    ${ctx ? html`<ol class="msg-context" aria-label="上下文">${ctx.map((c) => html`<${MessageRow} key=${c.message_id} m=${c} groupId=${groupId} focus=${c.message_id === m.message_id} />`)}</ol>` : null}
  </li>`;
}

/**
 * Paged message list over `groups.messages` (50 per page).
 * @param {{groupId: string, query: {sender?: string, segment?: string, word?: string}, title?: any, onClose?: () => void, empty?: string}} p
 */
export function MessageList({ groupId, query, title, onClose, empty = '没有找到消息' }) {
  const key = JSON.stringify(query);
  const res = useRpc('groups.messages', { id: groupId, ...query, offset: 0 }, [groupId, key]);
  const [extra, setExtra] = useState(/** @type {any[]} */ ([]));
  const [more, setMore] = useState(/** @type {boolean|null} */ (null));
  useEffect(() => { setExtra([]); setMore(null); }, [groupId, key]);
  const items = [...(res.data?.items || []), ...extra];
  const hasMore = more ?? !!res.data?.more;
  const loadMore = async () => {
    const r = await act('groups.messages', { id: groupId, ...query, offset: items.length });
    setExtra((x) => [...x, ...(r.items || [])]);
    setMore(!!r.more);
  };
  return html`<div class="msg-panel">
    ${title || onClose ? html`<header class="msg-panel-head"><span>${title}</span>${onClose ? html`<button type="button" class="icon-btn" aria-label="收起" onClick=${onClose}><${Icon} name="x" size=${14} /></button>` : null}</header>` : null}
    ${res.error ? html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />` : !res.data ? html`<${Skeleton} rows=${3} />`
      : items.length ? html`<ol class="msg-list">${items.map((m) => html`<${MessageRow} key=${m.message_id} m=${m} groupId=${groupId} word=${query.word} />`)}</ol>`
        : html`<p class="muted small pad-sm">${empty}</p>`}
    ${hasMore ? html`<div class="msg-more"><${AsyncButton} size="sm" variant="ghost" icon="down" onClick=${loadMore}>再加载 50 条<//><span class="muted small">已显示 ${items.length} 条</span></div>` : null}
  </div>`;
}

/** @param {{label: string}} p */
function NotYet({ label }) {
  return html`<${Empty} icon="sparkles" title=${`还没有${label}`} hint="点右上角「更新」，鲸鱼会把存档的消息整理成话题、关键词和群友画像。" compact />`;
}

/** 话题时间线. @param {{group: any, digest: any}} p */
export function Timeline({ group, digest }) {
  const [open, setOpen] = useState(/** @type {{seg: string, word?: string}|null} */ (null));
  const segs = digest?.segments || [];
  if (!segs.length) return html`<${NotYet} label="话题" />`;
  return html`<ol class="timeline">${segs.map((s) => {
    const on = open?.seg === s.id;
    return html`<li key=${s.id} class=${cx('tl-item', on && 'is-open')}>
      <span class="tl-dot" aria-hidden="true"></span>
      <div class="tl-card">
        <div class="tl-head"><strong>${fmtSpan(s.start, s.end)}</strong>
          <span class="muted small">${s.count} 条消息 · ${s.members} 人参与</span>
          <${Button} size="sm" variant=${on && !open?.word ? 'soft' : 'ghost'} icon=${on && !open?.word ? 'x' : 'rows'}
            onClick=${() => setOpen(on && !open?.word ? null : { seg: s.id })}>${on && !open?.word ? '收起' : '展开整段'}<//></div>
        <div class="chips chips-dense">${(s.keywords || []).map((k) => html`<${Chip} key=${k} active=${on && open?.word === k}
          title=${`${(s.keyword_sources?.[k] || []).length} 条消息提到`} onClick=${() => setOpen(on && open?.word === k ? null : { seg: s.id, word: k })}>${k}<//>`)}</div>
        ${on ? html`<${MessageList} groupId=${group.id} query=${open.word ? { segment: s.id, word: open.word } : { segment: s.id }}
          title=${open.word ? html`提到「<strong>${open.word}</strong>」的消息` : '这一段的全部消息'} onClose=${() => setOpen(null)} />` : null}
      </div>
    </li>`;
  })}</ol>`;
}

/** 关键词. @param {{group: any, digest: any, initialWord?: string}} p */
export function Keywords({ group, digest, initialWord = '' }) {
  const [q, setQ] = useState('');
  const [word, setWord] = useState(initialWord);
  const all = digest?.keywords || [];
  if (!all.length) return html`<${NotYet} label="关键词" />`;
  const max = Math.max(1, ...all.map((k) => k.segments));
  const min = Math.min(...all.map((k) => k.segments));
  const shown = q.trim() ? all.filter((k) => k.word.includes(q.trim())) : all;
  const types = [...new Set(all.map((k) => k.type))];
  return html`<div class="kw">
    <div class="kw-tools">
      <div class="search-box"><${Icon} name="search" size=${16} /><input type="search" value=${q} placeholder="筛选关键词…" aria-label="筛选关键词" onInput=${(e) => setQ(e.currentTarget.value)} /></div>
      <div class="kw-legend">${types.map((t) => html`<span key=${t} class="legend-item"><i style=${{ background: KEYWORD_TYPES[t] || 'var(--accent)' }}></i>${t}</span>`)}</div>
    </div>
    <p class="muted small">字越大，出现在越多的话题段里。点一个词，看它出现在哪些消息里。</p>
    ${shown.length ? html`<div class="kw-cloud" role="list">${shown.map((k) => {
      const t = max === min ? 0.5 : (k.segments - min) / (max - min);
      return html`<button type="button" role="listitem" key=${k.word} class=${cx('kw-word', word === k.word && 'is-on')}
        style=${{ fontSize: `${(12.5 + t * 15).toFixed(1)}px`, '--kw': KEYWORD_TYPES[k.type] || 'var(--accent)' }}
        title=${`${k.type} · ${k.segments} 个话题段`} aria-pressed=${word === k.word ? 'true' : 'false'} onClick=${() => setWord(word === k.word ? '' : k.word)}>${k.word}</button>`;
    })}</div>` : html`<p class="muted small">没有包含「${q.trim()}」的关键词。</p>`}
    ${word ? html`<${MessageList} groupId=${group.id} query=${{ word }} title=${html`提到「<strong>${word}</strong>」的消息`} onClose=${() => setWord('')} />` : null}
  </div>`;
}

/** @param {{label: string, value: any}} p */
const Fig = ({ label, value }) => html`<div class="fig"><span class="fig-v">${value ?? 0}</span><span class="fig-l">${label}</span></div>`;

/** 画像 + 明确说过的喜好. @param {{group: any, m: any, onTranscript: (id: string) => void}} p */
export function Portrait({ group, m, onTranscript }) {
  const pt = m.portrait;
  const claims = m.claims || [];
  return html`<div class="portrait card">
    <header class="portrait-head">
      <span class="portrait-avatar" aria-hidden="true">${(m.name || '?').slice(0, 1)}</span>
      <div><h3>${m.name}</h3><p class="muted small">${[...(m.aliases || [])].join('、') || '没有别名'} · QQ ${(m.accounts || []).join(' / ')}</p></div>
      <div class="portrait-actions"><${Button} size="sm" variant="ghost" icon="rows" onClick=${() => onTranscript(m.id)}>TA 的全部发言<//>
        ${pt?.memory_id ? html`<${Button} size="sm" variant="ghost" icon="memory" href=${`/memory/${pt.memory_id}`}>在记忆中查看<//>` : null}</div>
    </header>
    ${m.opted_out ? html`<p class="banner banner-warn small"><${Icon} name="lock" size=${16} />TA 已退出记录：不再存档 TA 的消息，画像和喜好已被遗忘。</p>`
      : !pt ? html`<p class="muted small">还没有画像——发言还太少，或者还没「更新」过。</p>` : html`
      <p class="portrait-summary">${pt.summary}</p>
      <div class="figs"><${Fig} label="发言数" value=${pt.text_count} /><${Fig} label="活跃天数" value=${pt.active_days} /><${Fig} label="中位字数" value=${pt.median_chars} /><${Fig} label="提问数" value=${pt.questions} /></div>
      ${Object.entries(TRAIT_KINDS).map(([kind, meta]) => {
        const traits = (pt.traits || []).filter((t) => t.kind === kind);
        if (!traits.length) return null;
        return html`<section key=${kind} class="traits"><h4><${Icon} name=${meta.icon} size=${14} />${meta.label} <span class="muted small">${meta.hint}</span></h4>
          <ul>${traits.map((t) => html`<li key=${t.label}><span class="trait-label">${t.label}</span><span class="muted small">${t.count} 次 · ${t.days} 天</span>
            <${EvidenceButton} memoryId=${pt.memory_id} ids=${t.evidence_ids || []} title=${`「${clip(t.label, 16)}」的依据`} /></li>`)}</ul></section>`;
      })}`}
    ${m.opted_out ? null : html`<section class="claims"><h4><${Icon} name="quote" size=${14} />明确说过的喜好 <span class="muted small">只记 TA 用第一人称亲口说的</span></h4>
      ${claims.length ? html`<ul>${claims.map((c) => html`<li key=${c.memory_id} class=${cx(!c.current && 'is-expired')}>
        <div class="claim-main"><q>${c.quote}</q><span class="muted small">${c.content}</span></div>
        <div class="claim-meta"><time class="muted small">${fmtDate(c.sent_at)}</time>
          ${c.current ? html`<${Badge} tone="ok">当前<//>` : html`<${Badge} tone="muted" title="之后说过相反的话，或已过时">已过期<//>`}
          <${EvidenceButton} memoryId=${c.memory_id} ids=${c.evidence_id ? [c.evidence_id] : []} title="原话" /></div></li>`)}</ul>`
        : html`<p class="muted small">还没有听 TA 明确说过「我喜欢……」「我不吃……」这样的话。</p>`}</section>`}
  </div>`;
}

/** @param {{group: any, m: any, selected: boolean, onSelect: () => void, onOptOut: (m: any, v: boolean) => void}} p */
function MemberCard({ m, selected, onSelect, onOptOut }) {
  return html`<li class=${cx('member', selected && 'is-sel', m.opted_out && 'is-out')}>
    <button type="button" class="member-main" aria-pressed=${selected ? 'true' : 'false'} onClick=${onSelect}>
      <span class="portrait-avatar sm" aria-hidden="true">${(m.name || '?').slice(0, 1)}</span>
      <span class="member-text"><strong>${m.name}</strong>
        <span class="muted small">${m.count} 条 · ${m.first ? `${fmtDate(m.first)} 起，${daySpan(m.first, m.last)} 天` : '还没有发言'}</span>
        ${m.aliases?.length ? html`<span class="muted small">也叫 ${m.aliases.join('、')}</span>` : null}</span>
      ${m.portrait ? html`<${Badge} tone="violet" icon="user">有画像<//>` : null}
    </button>
    ${m.keywords?.length ? html`<div class="chips chips-dense">${m.keywords.slice(0, 5).map((k) => html`<${Chip} key=${k}>${k}<//>`)}</div>` : null}
    <div class="member-opt"><span class="small">${m.opted_out ? '已退出记录' : '记录中'}</span>
      <${Switch} size="sm" checked=${!m.opted_out} label=${`记录 ${m.name}`} onChange=${(v) => onOptOut(m, !v)} /></div>
  </li>`;
}

/** 群友. @param {{group: any, digest: any, member?: string, onMember: (id: string) => void, onTranscript: (id: string) => void, onChanged: () => void}} p */
export function Members({ group, digest, member, onMember, onTranscript, onChanged }) {
  const [confirm, setConfirm] = useState(/** @type {any} */ (null));
  const list = digest?.members || [];
  const sel = list.find((m) => m.id === member) || null;
  const setOut = async (m, out) => {
    await act('groups.set_opt_out', { id: group.id, account: m.id, opted_out: out }, { ok: out ? `${m.name} 已退出记录` : `重新开始记录 ${m.name}` });
    setConfirm(null); onChanged();
  };
  if (!list.length) return html`<${Empty} icon="group" title="还没有群友数据" hint="名单里的群友发言并「更新」后，会在这里出现。" compact />`;
  return html`<div class=${cx('members-layout', sel && 'has-sel')}>
    <ul class="members">${list.map((m) => html`<${MemberCard} key=${m.id} m=${m} selected=${m.id === member}
      onSelect=${() => onMember(m.id === member ? '' : m.id)} onOptOut=${(x, out) => (out ? setConfirm(x) : setOut(x, false))} />`)}</ul>
    ${sel ? html`<${Portrait} group=${group} m=${sel} onTranscript=${onTranscript} />` : html`<p class="muted small members-hint"><${Icon} name="info" size=${14} />点一位群友，看鲸鱼整理的说话方式和喜好，以及每一条的依据。</p>`}
    <${Modal} open=${!!confirm} onClose=${() => setConfirm(null)} title=${`让 ${confirm?.name || ''} 退出记录？`} footer=${html`
      <${Button} variant="ghost" onClick=${() => setConfirm(null)}>取消<//><${AsyncButton} variant="danger" icon="lock" onClick=${() => setOut(confirm, true)}>退出记录<//>`}>
      <p>退出后不再记录此人、已有画像和喜好会被遗忘。TA 的旧消息仍留在存档里作为上下文，但不会再被整理。</p>
      <p class="muted small">适合不希望被 AI 记住的群友。随时可以重新打开。</p>
    <//>
  </div>`;
}

/** 成员发言. @param {{group: any, digest: any, sender?: string, onSender: (id: string) => void}} p */
export function Transcript({ group, digest, sender, onSender }) {
  const list = digest?.members || [];
  const cur = sender || list[0]?.id || '';
  const m = list.find((x) => x.id === cur);
  if (!list.length) return html`<${Empty} icon="rows" title="还没有群友数据" compact />`;
  return html`<div class="transcript">
    <label class="inline-form"><span class="small">看谁的发言</span>
      <select class="input input-sm" value=${cur} onChange=${(e) => onSender(e.currentTarget.value)}>
        ${list.map((x) => html`<option key=${x.id} value=${x.id}>${x.name}（${x.count} 条）</option>`)}</select></label>
    ${m?.opted_out ? html`<p class="banner banner-warn small"><${Icon} name="lock" size=${16} />${m.name} 已退出记录，之后的发言不会存档。</p>` : null}
    <${MessageList} groupId=${group.id} query=${{ sender: cur }} empty=${`${m?.name || 'TA'} 还没有存档的发言`} />
  </div>`;
}

/** Overview numbers for the detail header. @param {{group: any, digest: any}} p */
export function GroupFigures({ group, digest }) {
  const withPortrait = (digest?.members || []).filter((m) => m.portrait).length;
  const claims = (digest?.members || []).reduce((n, m) => n + (m.claims || []).filter((c) => c.current).length, 0);
  return html`<div class="figs figs-row">
    <${Fig} label="存档消息" value=${group.messages} /><${Fig} label="待整理" value=${group.pending} />
    <${Fig} label="话题段" value=${(digest?.segments || []).length} /><${Fig} label="群友画像" value=${withPortrait} /><${Fig} label="明确的喜好" value=${claims} />
  </div>`;
}
