// @ts-check
/** App shell: navigation rail, top bar (connection, approvals, theme), command palette. */
import { html, useEffect, useMemo, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Orb } from './live.js';
import { cx } from './ui.js';
import { store, useStore, applyTheme, tryAct, logout, link } from '../lib/store.js';
import { navigate, linkClick, useNow } from '../lib/hooks.js';
import { PHASES } from '../lib/format.js';

export const PAGES = [
  { id: 'bridge', path: '/', label: '总览', icon: 'bridge', hint: 'Agent 状态、实时活动、四块磁贴' },
  { id: 'chat', path: '/chat', label: '对话', icon: 'chat', hint: '流式对话与思维轨迹' },
  { id: 'memory', path: '/memory', label: '记忆', icon: 'memory', hint: '关系图、列表、候选知识' },
  { id: 'groups', path: '/groups', label: '群聊', icon: 'group', hint: 'QQ 群记忆空间：话题、群友画像与喜好' },
  { id: 'tasks', path: '/tasks', label: '任务', icon: 'tasks', hint: '目标、流程视图、调度' },
  { id: 'capabilities', path: '/capabilities', label: '能力', icon: 'bolt', hint: '能力、策略与插件' },
  { id: 'devices', path: '/devices', label: '设备', icon: 'phone', hint: '手机控制面板与隐私' },
  { id: 'persona', path: '/persona', label: '人格', icon: 'user', hint: '人格卡与相处习惯' },
  { id: 'settings', path: '/settings', label: '设置', icon: 'gear', hint: '模型、智能体、记忆、自主' },
];

/** @param {{page: string}} p */
export function Nav({ page }) {
  const status = useStore((s) => s.status);
  const phase = useStore((s) => s.agent.phase);
  const open = useStore((s) => s.navOpen);
  const approvals = useStore((s) => s.approvals.length);
  const persona = status?.agent?.persona || {};
  const counts = {
    memory: status?.memory?.candidates || 0,
    tasks: status?.tasks?.active || 0,
    devices: status?.devices?.online || 0,
  };
  const close = () => store.set({ navOpen: false });
  return html`<nav class=${cx('nav', open && 'is-open')} aria-label="主导航">
    <a class="nav-brand" href="/" onClick=${(e) => { linkClick(e); close(); }}>
      <${Orb} phase=${phase} size="sm" avatar=${persona.avatar} />
      <span class="nav-brand-text"><strong>${persona.name || status?.agent?.name || '鲸鱼'}</strong><small>${PHASES[phase]?.label || '空闲'}</small></span>
    </a>
    <ul class="nav-list">
      ${PAGES.map((p) => html`<li key=${p.id}>
        <a href=${p.path} class=${cx('nav-item', page === p.id && 'is-active')} aria-current=${page === p.id ? 'page' : undefined}
          title=${p.hint} onClick=${(e) => { linkClick(e); close(); }}>
          <${Icon} name=${p.icon} size=${19} /><span class="nav-label">${p.label}</span>
          ${counts[p.id] ? html`<span class=${cx('nav-count', p.id === 'memory' && 'tone-warn')} title=${p.id === 'memory' ? '待审核的候选知识' : ''}>${counts[p.id]}</span>` : null}
        </a></li>`)}
    </ul>
    <div class="nav-foot">
      <button type="button" class="nav-item" onClick=${() => store.set({ trayOpen: true })}>
        <${Icon} name="shield" size=${19} /><span class="nav-label">待批准</span>${approvals ? html`<span class="nav-count tone-warn">${approvals}</span>` : null}
      </button>
      <button type="button" class="nav-item" onClick=${() => store.set({ paletteOpen: true })}>
        <${Icon} name="command" size=${19} /><span class="nav-label">命令面板</span><kbd class="nav-kbd">Ctrl K</kbd>
      </button>
    </div>
  </nav>
  <div class=${cx('nav-scrim', open && 'is-open')} onClick=${close}></div>`;
}

export function ConnIndicator() {
  const conn = useStore((s) => s.conn);
  const info = useStore((s) => s.connInfo);
  const now = useNow(1000);
  const map = {
    online: { tone: 'ok', label: '实时连接' }, connecting: { tone: 'warn', label: '连接中…' },
    reconnecting: { tone: 'warn', label: '重连中…' }, offline: { tone: 'danger', label: '已断开' },
    unauthorized: { tone: 'danger', label: '令牌无效' }, idle: { tone: 'muted', label: '未连接' },
  };
  const m = map[conn] || map.idle;
  const secs = conn === 'reconnecting' && info?.retryAt ? Math.max(0, Math.ceil((info.retryAt - now) / 1000)) : 0;
  return html`<button type="button" class=${cx('conn', `tone-${m.tone}`)} aria-live="polite"
    title=${conn === 'online' ? '事件实时推送中' : '点击立即重连'} onClick=${() => conn !== 'online' && link.retryNow()}>
    <span class="conn-dot"></span><span class="conn-label">${m.label}${secs ? ` ${secs}s` : ''}</span>
  </button>`;
}

/** @param {{title: string}} p */
export function Topbar({ title }) {
  const approvals = useStore((s) => s.approvals.length);
  const theme = useStore((s) => s.theme);
  const next = { auto: 'dark', dark: 'light', light: 'auto' }[theme] || 'auto';
  const themeLabel = { auto: '跟随系统', dark: '深色', light: '浅色' };
  return html`<header class="topbar">
    <button type="button" class="icon-btn topbar-menu" aria-label="打开导航" onClick=${() => store.set({ navOpen: true })}><${Icon} name="menu" /></button>
    <span class="topbar-title">${title}</span>
    <button type="button" class="search-trigger" onClick=${() => store.set({ paletteOpen: true })} aria-label="打开命令面板">
      <${Icon} name="search" size=${16} /><span>搜索或执行命令…</span><kbd>Ctrl K</kbd>
    </button>
    <div class="topbar-right">
      <${ConnIndicator} />
      <button type="button" class=${cx('icon-btn', 'approvals-btn', approvals && 'has-items')} aria-label=${`待批准 ${approvals} 项`}
        title="待批准" onClick=${() => store.set({ trayOpen: true })}>
        <${Icon} name="shield" />${approvals ? html`<span class="badge-count">${approvals}</span>` : null}
      </button>
      <button type="button" class="icon-btn" aria-label=${`主题：${themeLabel[theme]}，切换为${themeLabel[next]}`} title=${`主题：${themeLabel[theme]}`}
        onClick=${() => applyTheme(/** @type {any} */ (next))}><${Icon} name=${theme === 'light' ? 'sun' : theme === 'dark' ? 'moon' : 'auto'} /></button>
    </div>
  </header>`;
}

/** @typedef {{id: string, label: string, hint?: string, icon: string, group: string, run: () => any, keywords?: string}} Command */

/** @returns {Command[]} */
function baseCommands() {
  /** @type {Command[]} */
  const cmds = PAGES.map((p) => ({ id: 'go:' + p.id, label: `前往 ${p.label}`, hint: p.hint, icon: p.icon, group: '页面', run: () => navigate(p.path), keywords: p.id }));
  cmds.push(
    { id: 'new-chat', label: '新对话', icon: 'chat', group: '动作', run: () => navigate('/chat/new'), keywords: 'chat new' },
    { id: 'new-task', label: '新建任务', icon: 'flag', group: '动作', run: () => navigate('/tasks?new=1'), keywords: 'task goal' },
    { id: 'add-mem', label: '添加一条记忆', icon: 'plus', group: '动作', run: () => navigate('/memory?add=1'), keywords: 'remember memory' },
    { id: 'inbox', label: '审核候选知识', icon: 'inbox', group: '动作', run: () => navigate('/memory?view=inbox'), keywords: 'candidate' },
    { id: 'maintain', label: '立即整理记忆', icon: 'broom', group: '动作', run: () => tryAct('memory.maintain', {}, { ok: '记忆整理完成' }), keywords: 'maintain decay' },
    { id: 'explore', label: '立即自主探索', icon: 'compass', group: '动作', run: () => tryAct('autonomy.explore', {}, { ok: '已开始探索，结果会出现在活动流' }), keywords: 'explore' },
    { id: 'pair', label: '配对新设备', icon: 'link', group: '动作', run: () => navigate('/devices?pair=1'), keywords: 'pair phone' },
    { id: 'digest', label: '把手机经历整理成记忆', icon: 'sparkles', group: '动作', run: () => tryAct('devices.digest_now', {}, { ok: '手机经历整理完成' }), keywords: 'digest phone' },
    { id: 'models', label: '配置模型', icon: 'gear', group: '设置', run: () => navigate('/settings/models'), keywords: 'model provider api key' },
    { id: 'setup', label: '重新运行首次设置引导', icon: 'wand', group: '设置', run: () => navigate('/setup'), keywords: 'setup wizard onboarding 引导' },
    { id: 'new-group', label: '新建群空间', icon: 'group', group: '动作', run: () => navigate('/groups?new=1'), keywords: 'group qq 群' },
    { id: 'approvals', label: '打开待批准', icon: 'shield', group: '动作', run: () => store.set({ trayOpen: true }), keywords: 'approval' },
    { id: 'theme', label: '切换深色/浅色主题', icon: 'moon', group: '设置', run: () => applyTheme(store.get().theme === 'dark' ? 'light' : 'dark'), keywords: 'theme dark light' },
    { id: 'logout', label: '退出（清除本机令牌）', icon: 'logout', group: '设置', run: () => logout(), keywords: 'logout token' },
  );
  return cmds;
}

export function Palette() {
  const open = useStore((s) => s.paletteOpen);
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const input = useRef(/** @type {HTMLInputElement|null} */ (null));
  const close = () => { store.set({ paletteOpen: false }); setQ(''); setSel(0); };
  useEffect(() => { if (open) setTimeout(() => input.current?.focus(), 10); }, [open]);
  const items = useMemo(() => {
    const all = baseCommands();
    const t = q.trim().toLowerCase();
    const found = t ? all.filter((c) => (c.label + ' ' + (c.hint || '') + ' ' + (c.keywords || '')).toLowerCase().includes(t)) : all;
    if (t) {
      found.push(
        { id: 'ask', label: `问鲸鱼：「${q.trim()}」`, icon: 'send', group: '快速', run: () => navigate('/chat/new?draft=' + encodeURIComponent(q.trim())) },
        { id: 'mem-search', label: `在记忆中搜索「${q.trim()}」`, icon: 'search', group: '快速', run: () => navigate('/memory?q=' + encodeURIComponent(q.trim())) },
      );
    }
    return found;
  }, [q]);
  if (!open) return null;
  const run = (c) => { close(); c.run(); };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((sel + 1) % Math.max(1, items.length)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((sel - 1 + items.length) % Math.max(1, items.length)); }
    else if (e.key === 'Enter' && items[sel]) { e.preventDefault(); run(items[sel]); }
  };
  let group = '';
  return html`<div class="overlay overlay-top" onClick=${(e) => { if (e.target === e.currentTarget) close(); }}>
    <div class="palette" role="dialog" aria-modal="true" aria-label="命令面板">
      <div class="palette-input"><${Icon} name="search" />
        <input ref=${input} value=${q} placeholder="输入页面、动作，或直接问鲸鱼…" aria-label="命令" role="combobox" aria-expanded="true"
          aria-controls="palette-list" aria-activedescendant=${items[sel] ? 'pal-' + items[sel].id : undefined}
          onInput=${(e) => { setQ(e.currentTarget.value); setSel(0); }} onKeyDown=${onKey} />
        <kbd>Esc</kbd></div>
      <ul class="palette-list" id="palette-list" role="listbox">
        ${items.map((c, i) => {
          const head = c.group !== group ? (group = c.group) : '';
          return html`${head ? html`<li class="palette-group" role="presentation" key=${'g' + head}>${head}</li>` : null}
            <li key=${c.id} id=${'pal-' + c.id} role="option" aria-selected=${i === sel ? 'true' : 'false'}
              class=${cx('palette-item', i === sel && 'is-sel')} onMouseEnter=${() => setSel(i)} onClick=${() => run(c)}>
              <${Icon} name=${c.icon} size=${16} /><span>${c.label}</span>${c.hint ? html`<small>${c.hint}</small>` : null}
            </li>`;
        })}
        ${!items.length ? html`<li class="palette-empty">没有匹配的命令</li>` : null}
      </ul>
    </div>
  </div>`;
}
