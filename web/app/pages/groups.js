// @ts-check
/**
 * 群聊 Groups: QQ groups the owner chose as memory spaces (port of PR #95's Studio).
 * /groups                     list (cards) · 新建群空间
 * /groups/:id?tab=&member=&sender=   detail: header actions + 话题时间线 / 关键词 / 群友 / 成员发言
 * Dynamic focus: with a group open, the list shrinks into a slim rail beside the detail.
 */
import { html, useState } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Badge, Button, Chip, Empty, PageHead, Skeleton, Switch, ErrorBox, Tabs, When, cx } from '../components/ui.js';
import { GroupForm, ImportChat, DeleteGroup } from '../components/group-form.js';
import { Timeline, Keywords, Members, Transcript, GroupFigures } from '../components/group-views.js';
import { act, toast, useTopic } from '../lib/store.js';
import { rpc, errorText } from '../lib/api.js';
import { useRpc, navigate, linkClick, useDebouncedCallback, useMedia } from '../lib/hooks.js';
import { AUTO_REFRESH, autoRefreshLabel, groupHref, timeAgo, fmtNum } from '../lib/format.js';

const TABS = [
  { id: 'timeline', label: '话题时间线', icon: 'clock' }, { id: 'keywords', label: '关键词', icon: 'hash' },
  { id: 'members', label: '群友', icon: 'group' }, { id: 'transcript', label: '成员发言', icon: 'rows' },
];

/** @param {{g: any, selected?: boolean, rail?: boolean}} p */
function GroupCard({ g, selected, rail }) {
  return html`<li class=${cx('group-card', selected && 'is-sel', g.paused && 'is-paused', rail && 'is-rail')}>
    <a href=${groupHref(g.id)} onClick=${linkClick} aria-current=${selected ? 'page' : undefined}>
      <span class="group-mark" aria-hidden="true">${(g.name || '群').slice(0, 1)}${g.pending ? html`<i class="group-dot"></i>` : null}</span>
      <span class="group-text">
        <span class="group-title"><strong>${g.name}</strong>${g.pending ? html`<${Badge} tone="warn" title="存档了但还没整理的新消息">${fmtNum(g.pending)} 待整理<//>` : null}</span>
        ${rail ? null : html`<span class="group-desc muted small">${g.description || `QQ 群 ${g.group_id}`}</span>
        <span class="group-stats small"><span><${Icon} name="group" size=${13} />${g.members} 位群友${g.bots ? ` · ${g.bots} 个机器人` : ''}</span>
          <span><${Icon} name="chat" size=${13} />${fmtNum(g.messages)} 条消息</span></span>
        <span class="chips chips-dense">
          ${g.paused ? html`<${Chip} color="var(--warn)">已暂停<//>` : null}
          ${g.auto_refresh_min ? html`<${Chip} color="var(--accent)">自动 · ${autoRefreshLabel(g.auto_refresh_min)}<//>` : html`<${Chip}>手动更新<//>`}
          <span class="muted small">${g.refreshed_at ? html`<${When} ms=${g.refreshed_at} fmt=${(ms) => `${timeAgo(ms)}整理`} />` : '还没整理过'}</span>
        </span>`}
      </span>
    </a>
  </li>`;
}

/** @param {{onCreate: () => void}} p */
function GroupsEmpty({ onCreate }) {
  return html`<div class="groups-empty card">
    <${Empty} icon="group" title="还没有群空间"
      hint="把一个 QQ 群设为记忆空间后，鲸鱼会在你点「更新」时整理话题、群友的说话方式和明确说过的喜好；原始聊天只存在本机。">
      <${Button} variant="primary" icon="plus" onClick=${onCreate}>新建群空间<//>
    <//>
    <ul class="groups-explain">
      <li><${Icon} name="clock" size=${18} /><div><strong>话题时间线</strong><p>按时间切成一段段话题，每段有关键词，点开能看原始消息。</p></div></li>
      <li><${Icon} name="user" size=${18} /><div><strong>群友画像</strong><p>口头禅、常聊的话题、典型反应，每一条都能追溯到真实的消息。</p></div></li>
      <li><${Icon} name="lock" size=${18} /><div><strong>你说了算</strong><p>只整理名单里的人；任何群友都可以退出记录，已有画像会被遗忘。</p></div></li>
    </ul>
    <p class="muted small"><${Icon} name="plug" size=${13} /> 需要先在 <a href="/capabilities" onClick=${linkClick}>能力 → 插件 → QQ</a> 连接 QQ 机器人，群消息才会进来；也可以先导入 QQ Chat Exporter 导出的聊天记录。</p>
  </div>`;
}

/** @param {{id: string, tab: string, member: string, sender: string, bump: number, onListChanged: () => void}} p */
function GroupDetail({ id, tab, member, sender, bump, onListChanged }) {
  const res = useRpc('groups.get', { id }, [id, bump]);
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState(/** @type {''|'edit'|'import'|'delete'} */ (''));
  const [dropFile, setDropFile] = useState(/** @type {any} */ (null));
  const [dragging, setDragging] = useState(false);
  const reload = () => { res.reload(true); onListChanged(); };
  useTopic('group.refreshed', (ev) => { if (ev.data?.id === id && !busy) res.reload(true); });
  const go = (q) => {
    const params = new URLSearchParams({ tab, ...(member ? { member } : {}), ...(sender ? { sender } : {}), ...q });
    [...params.keys()].forEach((k) => { if (!params.get(k)) params.delete(k); });
    navigate(`${groupHref(id)}?${params.toString()}`, { replace: true });
  };
  if (res.error) return html`<div class="group-detail"><${ErrorBox} error=${res.error} onRetry=${() => res.reload()} /></div>`;
  const g = res.data?.group;
  const digest = res.data?.digest;
  if (!g) return html`<div class="group-detail card"><${Skeleton} rows=${6} height=${28} /></div>`;

  const refresh = async () => {
    setBusy(true);
    try {
      const r = await rpc('groups.refresh', { id }, { timeout: 300000 });
      const c = r.counts || {};
      toast({ kind: 'ok', title: `整理完成：处理了 ${r.processed} 条消息`,
        body: `画像 新增 ${c.profiles_added ?? 0} · 更新 ${c.profiles_updated ?? 0}；喜好 新增 ${c.preferences_added ?? 0} · 更新 ${c.preferences_updated ?? 0} · 过期 ${c.preferences_expired ?? 0}${r.pending ? `；还有 ${r.pending} 条待整理` : ''}` });
      reload();
    } catch (e) {
      if (/** @type {any} */ (e).code === 'conflict') toast({ kind: 'warn', title: '正在更新', body: '上一次整理还没结束，稍等片刻再看。' });
      else toast({ kind: 'error', title: '更新失败', body: errorText(e) });
    } finally { setBusy(false); }
  };
  const setPaused = async (paused) => { await act('groups.set_paused', { id, paused }, { ok: paused ? '已暂停存档' : '已恢复存档' }); reload(); };
  const setAuto = async (min) => { await act('groups.update', { id, auto_refresh_min: min }, { ok: `自动更新：${autoRefreshLabel(min)}` }); reload(); };
  const counts = { timeline: digest?.segments?.length, keywords: digest?.keywords?.length, members: digest?.members?.length };
  const onDrop = (e) => {
    e.preventDefault(); setDragging(false);
    const f = e.dataTransfer?.files?.[0];
    if (f) { setDropFile(f); setModal('import'); }
  };

  let body;
  if (tab === 'keywords') body = html`<${Keywords} group=${g} digest=${digest} />`;
  else if (tab === 'members') body = html`<${Members} group=${g} digest=${digest} member=${member} onMember=${(m) => go({ member: m })} onTranscript=${(s) => go({ tab: 'transcript', sender: s, member: '' })} onChanged=${reload} />`;
  else if (tab === 'transcript') body = html`<${Transcript} group=${g} digest=${digest} sender=${sender} onSender=${(s) => go({ sender: s })} />`;
  else body = html`<${Timeline} group=${g} digest=${digest} />`;

  return html`<section class=${cx('group-detail', dragging && 'is-dragging')} aria-label=${g.name}
    onDragOver=${(e) => { if (e.dataTransfer?.types?.includes?.('Files')) { e.preventDefault(); setDragging(true); } }}
    onDragLeave=${(e) => { if (e.currentTarget === e.target) setDragging(false); }} onDrop=${onDrop}>
    <header class="group-head card">
      <div class="group-head-main">
        <span class="group-mark lg" aria-hidden="true">${(g.name || '群').slice(0, 1)}</span>
        <div class="group-head-text">
          <h2>${g.name}</h2>
          <p class="muted small">QQ 群 <code>${g.group_id}</code> · ${g.members} 位群友${g.bots ? ` · ${g.bots} 个机器人` : ''} · ${g.refreshed_at ? html`<${When} ms=${g.refreshed_at} fmt=${(ms) => `上次整理 ${timeAgo(ms)}`} />` : '还没整理过'}</p>
          ${g.description ? html`<p class="group-head-desc">${g.description}</p>` : null}
        </div>
        <div class="group-head-actions">
          <${Button} variant="primary" icon="sparkles" loading=${busy} disabled=${g.paused} title=${g.paused ? '已暂停：恢复存档后才能更新' : '整理新消息：话题、关键词、画像和喜好'} onClick=${refresh}>${busy ? '正在整理…' : '更新'}<//>
          <${Button} variant="soft" icon="upload" onClick=${() => { setDropFile(null); setModal('import'); }}>导入聊天记录<//>
          <${Button} variant="ghost" icon="edit" onClick=${() => setModal('edit')} aria-label="编辑群空间" title="编辑名称、描述和名单" />
          <${Button} variant="ghost" icon="trash" onClick=${() => setModal('delete')} aria-label="删除群空间" title="删除群空间" />
        </div>
      </div>
      ${g.pending && !g.paused ? html`<p class="banner banner-info small"><${Icon} name="inbox" size=${16} />有 ${fmtNum(g.pending)} 条新消息还没整理。点「更新」让鲸鱼整理它们。</p>` : null}
      ${g.paused ? html`<p class="banner banner-warn small"><${Icon} name="pause" size=${16} />已暂停：新消息不会存档，也不能更新。已有的整理结果仍然可用。</p>` : null}
      <div class="group-head-bar">
        <${GroupFigures} group=${g} digest=${digest} />
        <div class="group-head-controls">
          <label class="inline-form small"><span>暂停存档</span><${Switch} size="sm" checked=${!!g.paused} label="暂停存档" onChange=${setPaused} /></label>
          <label class="inline-form small"><span>自动更新</span>
            <select class="input input-sm" value=${String(g.auto_refresh_min || 0)} onChange=${(e) => setAuto(Number(e.currentTarget.value))}>
              ${AUTO_REFRESH.map((o) => html`<option key=${o.value} value=${String(o.value)}>${o.label}</option>`)}
              ${AUTO_REFRESH.some((o) => o.value === (g.auto_refresh_min || 0)) ? null : html`<option value=${String(g.auto_refresh_min)}>${autoRefreshLabel(g.auto_refresh_min)}</option>`}
            </select></label>
        </div>
      </div>
    </header>
    <${Tabs} tabs=${TABS.map((t) => ({ ...t, count: counts[t.id] }))} value=${tab} onChange=${(t) => go({ tab: t })} />
    <div class="group-body card">${body}</div>
    ${dragging ? html`<div class="drop-overlay" aria-hidden="true"><${Icon} name="upload" size=${32} /><p>松开即可导入聊天记录</p></div>` : null}
    <${GroupForm} open=${modal === 'edit'} onClose=${() => setModal('')} group=${{ ...g, roster: g.roster }} onSaved=${reload} />
    <${ImportChat} open=${modal === 'import'} onClose=${() => setModal('')} group=${g} initialFile=${dropFile} onDone=${reload} />
    <${DeleteGroup} open=${modal === 'delete'} onClose=${() => setModal('')} group=${g} onDeleted=${() => { onListChanged(); navigate('/groups'); }} />
  </section>`;
}

export function GroupsPage({ route }) {
  const list = useRpc('groups.list');
  const [creating, setCreating] = useState(route.query.new === '1');
  const [bump, setBump] = useState(0);
  const wide = useMedia('(min-width: 900px)');
  const id = route.id || '';
  const tab = TABS.some((t) => t.id === route.query.tab) ? route.query.tab : 'timeline';
  const live = useDebouncedCallback(() => { list.reload(true); setBump((b) => b + 1); }, 1200);
  useTopic(['group.updated', 'group.archived', 'group.refreshed'], live);
  const groups = list.data?.groups || [];
  const open = !!id;
  return html`<div class=${cx('page', 'groups', open && 'has-detail')}>
    <${PageHead} title="群聊" icon="group" subtitle="把 QQ 群设为记忆空间：话题、群友画像与喜好，都带着真实消息作依据"
      actions=${html`<${Button} variant="primary" icon="plus" onClick=${() => setCreating(true)}>新建群空间<//>`} />
    ${list.error ? html`<${ErrorBox} error=${list.error} onRetry=${() => list.reload()} />` : null}
    ${!list.data && !list.error ? html`<${Skeleton} rows=${3} height=${72} />`
      : list.data && !groups.length && !id ? html`<${GroupsEmpty} onCreate=${() => setCreating(true)} />` : html`
      <div class="groups-layout">
        ${!id || wide ? html`<nav class="groups-list-wrap" aria-label="群空间">
          <ul class=${cx('groups-list', id && 'is-rail')}>${groups.map((g) => html`<${GroupCard} key=${g.id} g=${g} selected=${g.id === id} rail=${!!id} />`)}</ul>
        </nav>` : null}
        ${id ? html`<div class="groups-detail-wrap">
          ${!wide ? html`<${Button} size="sm" variant="ghost" icon="left" href="/groups">全部群空间<//>` : null}
          <${GroupDetail} key=${id} id=${id} tab=${tab} member=${route.query.member || ''} sender=${route.query.sender || ''} bump=${bump} onListChanged=${() => list.reload(true)} />
        </div>` : null}
      </div>`}
    <${GroupForm} open=${creating} onClose=${() => { setCreating(false); if (route.query.new) navigate('/groups', { replace: true }); }}
      onSaved=${(g) => { list.reload(true); if (g?.id) navigate(groupHref(g.id)); }} />
  </div>`;
}
