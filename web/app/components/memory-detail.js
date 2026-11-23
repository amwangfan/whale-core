// @ts-check
/** Memory detail (edit, evidence, relations, lifecycle actions) and the add-memory form. */
import { html, useEffect, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Badge, Button, AsyncButton, ConfirmButton, Field, TagEditor, Meter, Skeleton, ErrorBox, Chip, Modal, When } from './ui.js';
import { act, toast } from '../lib/store.js';
import { EvidenceList, evidenceIdsOf } from './evidence.js';
import { useRpc, linkClick, navigate } from '../lib/hooks.js';
import { KINDS, MEM_STATUS, SCOPES, scopeCat, scopeLabel, kindLabel, clip, timeAgo, fmtTime } from '../lib/format.js';

const RELATIONS = { related: '相关', supersedes: '取代', derived_from: '派生自', about: '关于', mentions: '提及' };
const EDITABLE_KINDS = Object.keys(KINDS);

/** @param {{value: number, onChange: (v: number) => void, label: string}} p */
export function ImportanceSlider({ value, onChange, label }) {
  return html`<span class="slider">
    <input type="range" min="0" max="1" step="0.05" value=${value} aria-label=${label} onInput=${(e) => onChange(Number(e.currentTarget.value))} />
    <output>${Math.round(value * 100)}%</output>
  </span>`;
}

/** @param {{source: any}} p */
function Evidence({ source }) {
  const s = source || {};
  const rows = [];
  if (s.conversation_id) rows.push(html`<li key="c"><${Icon} name="chat" size=${14} />来自对话 <a href=${`/chat/${s.conversation_id}`} onClick=${linkClick}>打开会话</a>${s.channel ? html` · ${s.channel}` : null}</li>`);
  if (s.run_id && !s.conversation_id) rows.push(html`<li key="r"><${Icon} name="branch" size=${14} />来自运行 <code>${clip(s.run_id, 18)}</code></li>`);
  if (s.task_id) rows.push(html`<li key="t"><${Icon} name="flag" size=${14} />来自任务 <a href=${`/tasks/${s.task_id}`} onClick=${linkClick}>查看任务</a></li>`);
  if (s.device_id || s.from === '手机') rows.push(html`<li key="d"><${Icon} name="phone" size=${14} />来自${s.role === 'agent' ? '鲸鱼自己的手机' : '主人的手机'}
    ${s.device ? html` <a href=${`/devices/${s.device_id}`} onClick=${linkClick}>${s.device}</a>` : null}${s.span ? html` · ${s.span}` : null}${s.event_ids?.length ? html` · ${s.event_ids.length} 条手机经历` : null}</li>`);
  if (s.autonomy || s.topic) rows.push(html`<li key="a"><${Icon} name="compass" size=${14} />自主探索${s.topic ? `「${clip(s.topic, 30)}」` : ''}</li>`);
  if (s.url) rows.push(html`<li key="u"><${Icon} name="external" size=${14} /><a href=${/^https?:\/\//.test(s.url) ? s.url : undefined} target="_blank" rel="noopener noreferrer">${clip(s.url, 60)}</a></li>`);
  if (s.manual) rows.push(html`<li key="m"><${Icon} name="edit" size=${14} />你手动添加</li>`);
  if (s.group_id || String(s.evidence_id || s.evidence_ids?.[0] || '').startsWith('qq:group:')) {
    const gid = s.group_id || String(s.evidence_id || s.evidence_ids[0]).split(':')[2];
    rows.push(html`<li key="g"><${Icon} name="group" size=${14} />来自群聊 <a href=${`/groups/${encodeURIComponent('qq:' + gid)}`} onClick=${linkClick}>QQ 群 ${gid}</a>${s.subject_name ? html` · 关于 ${s.subject_name}` : null}</li>`);
  }
  if (!rows.length && !s.quote) return html`<p class="muted small">没有记录来源。</p>`;
  return html`<div class="evidence">
    ${s.quote ? html`<blockquote class="evidence-quote">“${s.quote}”<small>原话 —— 没有原话的推断不会被记住</small></blockquote>` : null}
    ${rows.length ? html`<ul class="evidence-list">${rows}</ul>` : null}
  </div>`;
}

/** @param {{id: string, onClose: () => void, onChanged?: () => void}} p */
export function MemoryDetail({ id, onClose, onChanged }) {
  const res = useRpc('memory.get', { id }, [id]);
  const [form, setForm] = useState(/** @type {any} */ (null));
  useEffect(() => {
    const it = res.data?.item;
    if (it) setForm({ content: it.content, kind: it.kind, importance: it.importance, tags: it.tags || [] });
  }, [res.data]);
  if (res.error) return html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />`;
  const it = res.data?.item;
  if (!it || !form) return html`<${Skeleton} rows=${6} />`;
  const dirty = form.content !== it.content || form.kind !== it.kind || form.importance !== it.importance || JSON.stringify(form.tags) !== JSON.stringify(it.tags || []);
  const forgotten = it.status === 'forgotten';
  const save = async () => {
    const patch = { id };
    if (form.content !== it.content) patch.content = form.content;
    if (form.kind !== it.kind) patch.kind = form.kind;
    if (form.importance !== it.importance) patch.importance = form.importance;
    if (JSON.stringify(form.tags) !== JSON.stringify(it.tags || [])) patch.tags = form.tags;
    await act('memory.update', patch, { ok: '记忆已更新' });
    await res.reload(true); onChanged?.();
  };
  const setStatus = async (status, ok) => { await act('memory.update', { id, status }, { ok }); await res.reload(true); onChanged?.(); };
  const cat = scopeCat(it.scope);
  const links = res.data.links || [];
  const related = Object.fromEntries((res.data.related || []).map((r) => [r.id, r]));

  return html`<div class="mem-detail" style=${{ '--kind': KINDS[it.kind]?.color }}>
    <div class="mem-detail-badges">
      <${Badge} tone="accent" class="kind-badge">${kindLabel(it.kind)}<//>
      <${Badge} tone=${MEM_STATUS[it.status]?.tone}>${MEM_STATUS[it.status]?.label || it.status}<//>
      <${Chip} color=${SCOPES[cat].color} title=${SCOPES[cat].hint}>${scopeLabel(it.scope)}<//>
      ${it.subject && it.subject !== 'user' ? html`<${Chip}>关于 ${it.subject === 'self' ? '鲸鱼自己' : it.subject}<//>` : null}
    </div>
    ${it.status === 'candidate' ? html`<div class="banner banner-info small"><${Icon} name="inbox" size=${16} />这是一条候选知识：鲸鱼探索时发现的，采纳后才会在对话中使用。
      <${AsyncButton} size="sm" variant="primary" icon="check" onClick=${() => setStatus('active', '已采纳为有效记忆')}>采纳<//></div>` : null}
    ${forgotten ? html`<p class="banner banner-warn small">这条记忆已被遗忘：内容已清除，只保留编号防止被重新写入。</p>` : html`
    <${Field} label="内容"><textarea class="input" rows="4" value=${form.content} onInput=${(e) => setForm({ ...form, content: e.currentTarget.value })}></textarea><//>
    <div class="form-grid">
      <${Field} label="类型"><select class="input" value=${form.kind} onChange=${(e) => setForm({ ...form, kind: e.currentTarget.value })}>
        ${EDITABLE_KINDS.map((k) => html`<option key=${k} value=${k}>${KINDS[k].label}</option>`)}</select><//>
      <${Field} label="重要度" hint="越重要越容易被想起，也越不会被遗忘"><${ImportanceSlider} label="重要度" value=${form.importance} onChange=${(v) => setForm({ ...form, importance: v })} /><//>
    </div>
    <${Field} label="标签"><${TagEditor} value=${form.tags} label="标签" onChange=${(v) => setForm({ ...form, tags: v })} /><//>
    ${dirty ? html`<div class="form-actions"><${AsyncButton} variant="primary" icon="check" onClick=${save}>保存修改<//><${Button} variant="ghost" onClick=${() => setForm({ content: it.content, kind: it.kind, importance: it.importance, tags: it.tags || [] })}>撤销<//></div>` : null}`}

    <h4>记忆强度</h4>
    <div class="strength">
      <${Meter} value=${it.strength_now ?? 0} tone=${(it.strength_now ?? 0) < 0.2 ? 'warn' : 'accent'} label="当前强度" />
      <span class="muted small">当前 ${Math.round((it.strength_now ?? 0) * 100)}% · 被想起 ${it.access_count} 次 · 最近 <${When} ms=${it.last_access_at} fmt=${timeAgo} /></span>
      <p class="muted small">强度会随时间指数衰减，每次被回忆都会增强；低强度、不重要的经历会被自动归档。</p>
    </div>

    <h4>来源与证据</h4>
    <${Evidence} source=${it.source} />
    ${evidenceIdsOf(it.source).length && !forgotten ? html`<h4>依据 <span class="muted">${evidenceIdsOf(it.source).length}</span></h4>
      <p class="muted small">这条记忆是从下面这些真实消息里整理出来的（原文只保存在本机存档）：</p>
      <${EvidenceList} memoryId=${it.id} ids=${evidenceIdsOf(it.source)} max=${3} />` : null}

    <h4>关系 ${links.length ? html`<span class="muted">${links.length}</span>` : null}</h4>
    ${links.length ? html`<ul class="relations">${links.map((l) => {
      const other = l.src === id ? l.dst : l.src;
      const r = related[other];
      const dir = l.src === id ? '→' : '←';
      return html`<li key=${l.src + l.dst + l.relation}>
        <span class="rel-kind">${l.relation === 'supersedes' ? (l.src === id ? '取代了' : '被它取代') : RELATIONS[l.relation] || l.relation} ${dir}</span>
        <button type="button" class="linkish" onClick=${() => navigate(`/memory/${other}`)}>
          ${r ? html`<i class="kind-dot" style=${{ background: KINDS[r.kind]?.color }}></i>${clip(r.content || '（已遗忘）', 70)}` : clip(other, 20)}</button>
        <span class="muted small">${Math.round((l.weight ?? 0) * 100)}%</span></li>`;
    })}</ul>` : html`<p class="muted small">还没有与其他记忆建立关系。</p>`}

    <dl class="kv kv-meta">
      <div><dt>编号</dt><dd><code>${it.id}</code></dd></div>
      <div><dt>置信度</dt><dd>${Math.round((it.confidence ?? 0) * 100)}%</dd></div>
      <div><dt>创建</dt><dd>${fmtTime(it.created_at)}</dd></div>
      <div><dt>更新</dt><dd>${fmtTime(it.updated_at)}</dd></div>
    </dl>
    ${forgotten ? null : html`<div class="danger-zone">
      ${it.status === 'active' ? html`<${AsyncButton} size="sm" variant="ghost" icon="archive" onClick=${() => setStatus('archived', '已归档')}>归档<//>` : null}
      ${it.status === 'archived' || it.status === 'superseded' ? html`<${AsyncButton} size="sm" variant="ghost" icon="refresh" onClick=${() => setStatus('active', '已恢复为有效')}>恢复为有效<//>` : null}
      <${ConfirmButton} icon="trash" confirmText="确认遗忘？不可恢复" onConfirm=${async () => { await act('memory.forget', { id }, { ok: '已遗忘' }); onChanged?.(); onClose(); }}>遗忘<//>
    </div>`}
  </div>`;
}

/** @param {{open: boolean, onClose: () => void, onCreated?: (res: any) => void, kind?: string}} p */
export function AddMemory({ open, onClose, onCreated, kind = 'fact' }) {
  const [form, setForm] = useState({ content: '', kind, importance: 0.6, tags: /** @type {string[]} */ ([]) });
  useEffect(() => { if (open) setForm({ content: '', kind, importance: 0.6, tags: [] }); }, [open, kind]);
  const submit = async () => {
    if (!form.content.trim()) return;
    const res = await act('memory.remember', { ...form, content: form.content.trim() });
    const msg = { created: '已记住', reinforced: '已有类似记忆，已强化它', superseded: '已记住，并取代了旧的说法' }[res?.action] || '已记住';
    toast({ kind: 'ok', title: msg, body: res?.item?.content });
    onCreated?.(res);
    onClose();
  };
  return html`<${Modal} open=${open} onClose=${onClose} title="添加一条记忆" footer=${html`
    <${Button} variant="ghost" onClick=${onClose}>取消<//>
    <${AsyncButton} variant="primary" icon="check" disabled=${!form.content.trim()} onClick=${submit}>记住<//>`}>
    <p class="muted small">手动添加的记忆会与已有记忆比较：重复的会强化旧记忆，矛盾的会取代旧记忆。</p>
    <${Field} label="内容"><textarea class="input" rows="3" value=${form.content} placeholder="例如：我对花生过敏" onInput=${(e) => setForm({ ...form, content: e.currentTarget.value })}></textarea><//>
    <div class="form-grid">
      <${Field} label="类型"><select class="input" value=${form.kind} onChange=${(e) => setForm({ ...form, kind: e.currentTarget.value })}>
        ${EDITABLE_KINDS.map((k) => html`<option key=${k} value=${k}>${KINDS[k].label} · ${KINDS[k].hint}</option>`)}</select><//>
      <${Field} label="重要度"><${ImportanceSlider} label="重要度" value=${form.importance} onChange=${(v) => setForm({ ...form, importance: v })} /><//>
    </div>
    <${Field} label="标签"><${TagEditor} value=${form.tags} label="标签" onChange=${(v) => setForm({ ...form, tags: v })} /><//>
  <//>`;
}
