// @ts-check
/**
 * 总览 Bridge: who the agent is and what it is doing right now, a live activity stream,
 * and four peer tiles (记忆 / 能力 / 任务 / 设备). Clicking a tile expands it in place into a
 * working view; the others shrink into a slim rail (dynamic focus).
 */
import { html, useState } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Orb, ActivityStream } from '../components/live.js';
import { Badge, Button, Chip, Empty, Meter, Skeleton, cx, When, AsyncButton } from '../components/ui.js';
import { useStore, tryAct, act, useTopic } from '../lib/store.js';
import { useRpc, navigate, linkClick, useRoute, useDebouncedCallback } from '../lib/hooks.js';
import {
  PHASES, KINDS, SCOPES, SCOPE_RULE, TASK_STATUS, RISKS, DEVICE_ROLES, PROVIDER_GROUPS, kindLabel, clip, timeAgo, timeUntil, providerGroup, fmtUptime,
} from '../lib/format.js';

const TILES = [
  { id: 'memory', label: '记忆', icon: 'memory', href: '/memory' },
  { id: 'caps', label: '能力', icon: 'bolt', href: '/capabilities' },
  { id: 'tasks', label: '任务', icon: 'tasks', href: '/tasks' },
  { id: 'devices', label: '设备', icon: 'phone', href: '/devices' },
];

/** Gentle nudge while the first-run setup has no model yet. */
export function SetupCard() {
  const setup = useStore((s) => s.setup);
  if (!setup || setup.unsupported || setup.steps?.model) return null;
  const st = setup.steps || {};
  const steps = [['model', '选择大脑'], ['persona', '人格'], ['phone', '连接手机'], ['qq', '连接 QQ']];
  return html`<section class="setup-card card" aria-label="完成设置">
    <span class="setup-card-icon"><${Icon} name="wand" size=${22} /></span>
    <div class="setup-card-main">
      <strong>完成设置，让鲸鱼真正思考</strong>
      <p class="muted small">还差一步：给它选一个大脑（DeepSeek、通义千问、Kimi、本机 Ollama…）。只要一分钟。</p>
      <ol class="setup-card-steps">${steps.map(([k, l]) => html`<li key=${k} class=${st[k] ? 'is-done' : ''}><${Icon} name=${st[k] ? 'check' : 'dot'} size=${12} />${l}</li>`)}</ol>
    </div>
    <${Button} variant="primary" icon="chevron" href="/setup">继续设置<//>
  </section>`;
}

export function ModelsOfflineBanner() {
  const offline = useStore((s) => !!s.status?.models?.offline);
  const setupCard = useStore((s) => !!s.setup && !s.setup.unsupported && !s.setup.steps?.model);
  if (!offline || setupCard) return null;
  return html`<div class="banner banner-warn" role="status">
    <${Icon} name="alert" />
    <div><strong>还没有配置对话模型</strong><p>鲸鱼现在只能用离线脑回答。去 设置 → 模型 添加一个 OpenAI 兼容服务（如 DeepSeek、通义、Ollama），它就能真正思考了。</p></div>
    <${Button} variant="primary" size="sm" href="/settings/models" icon="gear">配置模型<//>
  </div>`;
}

function Hero() {
  const status = useStore((s) => s.status);
  const agent = useStore((s) => s.agent);
  const [text, setText] = useState('');
  const persona = status?.agent?.persona || {};
  const ph = PHASES[agent.phase] || PHASES.idle;
  const ask = (e) => {
    e.preventDefault();
    const t = text.trim();
    if (t) navigate('/chat/new?send=' + encodeURIComponent(t));
  };
  return html`<section class="hero" style=${{ '--persona': persona.color || 'var(--accent)' }}>
    <${Orb} phase=${agent.phase} size="lg" avatar=${persona.avatar} />
    <div class="hero-main">
      <p class="hero-kicker">${status ? `Whale ${status.version} · 已运行 ${fmtUptime(status.uptime_s)}` : '正在连接…'}</p>
      <h1 class="hero-name">${persona.name || status?.agent?.name || '鲸鱼'}</h1>
      ${persona.tagline ? html`<p class="hero-tagline">${persona.tagline}</p>` : null}
      <p class=${cx('hero-phase', `phase-${agent.phase}`)}><span class="pulse-dot"></span><strong>${ph.label}</strong><span>${ph.hint}</span></p>
      ${agent.active?.length ? html`<ul class="hero-active">${agent.active.map((r) => html`<li key=${r.run_id}>
        <${Badge} tone=${r.status === 'waiting' ? 'warn' : 'accent'} dot>${{ chat: '对话', goal: '任务', explore: '探索' }[r.mode] || r.mode}<//>
        <a href=${r.conversation_id ? `/chat/${r.conversation_id}` : '/tasks'} onClick=${linkClick}>${clip(r.title, 48)}</a>
        <span class="muted">${r.channel}</span></li>`)}</ul>` : null}
      <form class="hero-ask" onSubmit=${ask}>
        <input class="input" value=${text} placeholder="对鲸鱼说点什么…（回车发送到对话）" aria-label="快速提问" onInput=${(e) => setText(e.currentTarget.value)} />
        <${Button} variant="primary" icon="send" onClick=${ask} disabled=${!text.trim()} aria-label="发送" />
      </form>
    </div>
  </section>`;
}

/** @param {{by: Record<string, number>, colors?: Record<string, any>, labels?: (k: string) => string}} p */
function Bars({ by, colors = KINDS, labels = kindLabel }) {
  const entries = Object.entries(by || {}).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, n]) => n));
  if (!entries.length) return html`<p class="muted small">暂无</p>`;
  return html`<ul class="bars">${entries.map(([k, n]) => html`<li key=${k} style=${{ '--bar': colors[k]?.color || 'var(--accent)' }}>
    <span class="bar-label">${labels(k)}</span><span class="bar-track"><span style=${{ width: `${(n / max) * 100}%` }}></span></span><span class="bar-n">${n}</span></li>`)}</ul>`;
}

// -- memory --------------------------------------------------------------------------------------
function MemoryMini({ focused }) {
  const mem = useStore((s) => s.status?.memory);
  const recent = useRpc(focused ? 'memory.list' : null, { status: 'active', limit: 6, order: 'created_at DESC' }, [focused]);
  const cands = useRpc(focused ? 'memory.list' : null, { status: 'candidate', limit: 4, order: 'created_at DESC' }, [focused]);
  const refresh = useDebouncedCallback(() => { recent.reload(true); cands.reload(true); }, 600);
  useTopic('memory.*', () => focused && refresh());
  if (!mem) return html`<${Skeleton} rows=${3} />`;
  const scope = mem.by_scope || {};
  if (!focused) {
    return html`<div class="tile-sum">
      <span class="tile-big">${mem.active ?? 0}</span><span class="tile-unit">条有效记忆</span>
      <div class="kind-strip" aria-hidden="true">${Object.entries(mem.by_kind || {}).map(([k, n]) => html`<span key=${k} style=${{ flex: n, background: KINDS[k]?.color }}></span>`)}</div>
      <div class="chips">${Object.entries(scope).map(([k, n]) => html`<${Chip} key=${k} color=${SCOPES[k]?.color}>${SCOPES[k]?.label || k} ${n}<//>`)}
        ${mem.candidates ? html`<${Badge} tone="warn">${mem.candidates} 条候选<//>` : null}</div>
    </div>`;
  }
  return html`<div class="tile-work">
    <div class="tile-cols">
      <div><h4>按类型</h4><${Bars} by=${mem.by_kind} /></div>
      <div><h4>按范围</h4><${Bars} by=${scope} colors=${SCOPES} labels=${(k) => SCOPES[k]?.label || k} />
        <p class="rule-hint"><${Icon} name="info" size=${13} />${SCOPE_RULE}</p></div>
    </div>
    <div class="tile-cols">
      <div><h4>最近记住的</h4>
        ${recent.data ? (recent.data.items.length ? html`<ul class="mini-list">${recent.data.items.map((m) => html`<li key=${m.id}>
          <a href=${`/memory/${m.id}`} onClick=${linkClick}><i class="kind-dot" style=${{ background: KINDS[m.kind]?.color }}></i>${clip(m.content, 60)}</a>
          <span class="muted"><${When} ms=${m.created_at} fmt=${timeAgo} /></span></li>`)}</ul>` : html`<p class="muted small">还没有记忆。和鲸鱼聊聊天吧。</p>`) : html`<${Skeleton} rows=${3} />`}
      </div>
      <div><h4>候选知识 ${mem.candidates ? html`<${Badge} tone="warn">${mem.candidates}<//>` : null}</h4>
        ${cands.data?.items?.length ? html`<ul class="mini-list">${cands.data.items.map((m) => html`<li key=${m.id}>
          <span>${clip(m.content, 54)}</span>
          <${AsyncButton} size="sm" variant="ghost" icon="check" title="采纳为有效记忆" onClick=${async () => { await act('memory.update', { id: m.id, status: 'active' }, { ok: '已采纳' }); cands.reload(true); }} />
        </li>`)}</ul>` : html`<p class="muted small">没有待审核的候选知识。自主探索的发现会先进这里。</p>`}
      </div>
    </div>
    <div class="tile-foot"><span class="muted">${mem.links ?? 0} 条关系${mem.queue_processed != null ? ` · 已从 ${mem.queue_processed} 轮对话中学习` : ''}</span>
      <${AsyncButton} size="sm" variant="ghost" icon="broom" onClick=${() => tryAct('memory.maintain', {}, { ok: '记忆整理完成' })}>立即整理<//></div>
  </div>`;
}

// -- capabilities ------------------------------------------------------------------------------------
function CapsMini({ focused }) {
  const caps = useStore((s) => s.status?.capabilities);
  const list = useRpc(focused ? 'capabilities.list' : null, {}, [focused]);
  useTopic(['capability.*', 'plugin.changed', 'config.changed'], () => focused && list.reload(true));
  if (!caps) return html`<${Skeleton} rows=${3} />`;
  if (!focused) {
    return html`<div class="tile-sum"><span class="tile-big">${caps.total}</span><span class="tile-unit">项能力</span>
      <div class="chips">${Object.entries(caps.by_provider || {}).map(([k, n]) => html`<${Chip} key=${k}>${PROVIDER_GROUPS[k]?.label || k} ${n}<//>`)}</div></div>`;
  }
  const all = list.data?.capabilities || [];
  const verdicts = { allow: 0, ask: 0, deny: 0 };
  all.forEach((c) => { verdicts[c.policy] = (verdicts[c.policy] || 0) + 1; });
  const used = [...all].filter((c) => c.stats?.calls).sort((a, b) => b.stats.calls - a.stats.calls).slice(0, 6);
  const byGroup = {};
  all.forEach((c) => { const g = providerGroup(c.provider); byGroup[g] = (byGroup[g] || 0) + 1; });
  return html`<div class="tile-work">
    <div class="tile-cols">
      <div><h4>来源</h4><${Bars} by=${byGroup} colors=${{}} labels=${(k) => PROVIDER_GROUPS[k]?.label || k} /></div>
      <div><h4>策略</h4><div class="verdicts">
        <div class="tone-ok"><strong>${verdicts.allow}</strong><span>自动允许</span></div>
        <div class="tone-warn"><strong>${verdicts.ask}</strong><span>先问你</span></div>
        <div class="tone-danger"><strong>${verdicts.deny}</strong><span>已禁止</span></div></div>
        <p class="rule-hint"><${Icon} name="info" size=${13} />只读和写入鲸鱼自己的数据默认允许；对外发消息、操作设备默认先问你。</p></div>
    </div>
    <h4>最常用</h4>
    ${list.data ? (used.length ? html`<ul class="mini-list">${used.map((c) => html`<li key=${c.name}><code>${c.name}</code><span>${c.title}</span>
      <${Badge} tone=${RISKS[c.risk]?.tone}>${RISKS[c.risk]?.label}<//><span class="muted">${c.stats.calls} 次</span></li>`)}</ul>` : html`<p class="muted small">还没有调用记录。</p>`) : html`<${Skeleton} rows=${3} />`}
  </div>`;
}

// -- tasks --------------------------------------------------------------------------------------------
function TasksMini({ focused }) {
  const t = useStore((s) => s.status?.tasks);
  const tasks = useRpc(focused ? 'tasks.list' : null, { limit: 8 }, [focused]);
  const jobs = useRpc(focused ? 'schedule.list' : null, {}, [focused]);
  const [goal, setGoal] = useState('');
  const refresh = useDebouncedCallback(() => { tasks.reload(true); jobs.reload(true); }, 500);
  useTopic(['task.*', 'schedule.*'], () => focused && refresh());
  if (!t) return html`<${Skeleton} rows=${3} />`;
  if (!focused) {
    return html`<div class="tile-sum"><span class="tile-big">${t.active}</span><span class="tile-unit">进行中</span>
      <div class="chips"><${Chip}>已调度 ${t.scheduled}<//><${Chip}>共 ${t.total}<//></div></div>`;
  }
  const create = async (e) => {
    e?.preventDefault?.();
    if (!goal.trim()) return;
    const task = await act('tasks.create', { goal: goal.trim() }, { ok: '任务已创建，鲸鱼开始工作' }).catch(() => null);
    if (task) { setGoal(''); navigate(`/tasks/${task.id}`); }
  };
  const next = (jobs.data?.jobs || []).filter((j) => j.enabled && j.next_at).sort((a, b) => a.next_at - b.next_at).slice(0, 4);
  return html`<div class="tile-work">
    <form class="inline-form" onSubmit=${create}>
      <input class="input" value=${goal} placeholder="交给鲸鱼一个目标，例如：整理本周 AI 新闻要点" aria-label="新任务目标" onInput=${(e) => setGoal(e.currentTarget.value)} />
      <${Button} variant="primary" icon="flag" onClick=${create} disabled=${!goal.trim()}>开始<//>
    </form>
    <div class="tile-cols">
      <div><h4>最近任务</h4>
        ${tasks.data ? (tasks.data.tasks.length ? html`<ul class="mini-list">${tasks.data.tasks.map((x) => html`<li key=${x.id}>
          <${Badge} tone=${TASK_STATUS[x.status]?.tone} dot=${x.status === 'running'}>${TASK_STATUS[x.status]?.label || x.status}<//>
          <a href=${`/tasks/${x.id}`} onClick=${linkClick}>${clip(x.title, 40)}</a></li>`)}</ul>` : html`<p class="muted small">还没有任务。</p>`) : html`<${Skeleton} rows=${3} />`}
      </div>
      <div><h4>接下来</h4>
        ${jobs.data ? (next.length ? html`<ul class="mini-list">${next.map((j) => html`<li key=${j.id}><${Icon} name="clock" size=${14} /><span>${clip(j.name, 30)}</span>
          <span class="muted">${timeUntil(j.next_at)}</span></li>`)}</ul>` : html`<p class="muted small">没有待触发的调度。</p>`) : html`<${Skeleton} rows=${2} />`}
      </div>
    </div>
  </div>`;
}

// -- devices ---------------------------------------------------------------------------------------------
function DevicesMini({ focused }) {
  const d = useStore((s) => s.status?.devices);
  const clients = useStore((s) => s.status?.clients || []);
  const list = useRpc(focused ? 'devices.list' : null, {}, [focused]);
  const refresh = useDebouncedCallback(() => list.reload(true), 500);
  useTopic(['device.connected', 'device.disconnected', 'device.status', 'device.updated', 'device.paired', 'device.revoked', 'device.pair_requested'], () => focused && refresh());
  if (!d) return html`<${Skeleton} rows=${3} />`;
  if (!focused) {
    return html`<div class="tile-sum"><span class="tile-big">${d.online}</span><span class="tile-unit">/ ${d.total} 台在线</span>
      <div class="chips"><${Chip}>${clients.length} 个客户端<//></div></div>`;
  }
  const devices = (list.data?.devices || []).filter((x) => !x.revoked);
  return html`<div class="tile-work">
    ${list.data ? (devices.length ? html`<ul class="dev-mini">${devices.map((x) => html`<li key=${x.id} class=${cx(x.online && 'is-online')}>
      <span class="dev-mini-phone" aria-hidden="true"><span></span></span>
      <div><a href=${`/devices/${x.id}`} onClick=${linkClick}><strong>${x.name}</strong></a>
        <span class="muted small">${DEVICE_ROLES[x.role]?.short} · ${x.online ? (x.foreground ? clip(x.foreground, 24) : '在线') : `离线 · ${timeAgo(x.last_seen)}`}</span></div>
      ${x.battery != null ? html`<span class="dev-mini-bat"><${Meter} value=${x.battery} max=${100} tone=${x.battery < 20 ? 'danger' : 'ok'} label="电量" />${x.battery}%</span>` : null}
    </li>`)}</ul>` : html`<${Empty} compact icon="phone" title="还没有配对手机" hint="配对后，手机就成了鲸鱼的眼睛和手。"><${Button} size="sm" variant="primary" href="/devices?pair=1" icon="link">配对新设备<//><//>`) : html`<${Skeleton} rows=${2} />`}
    <h4>已连接的客户端</h4>
    <div class="chips">${clients.map((c) => html`<${Chip} key=${c.id}>${c.name} · ${c.role}<//>`)}</div>
  </div>`;
}

const MINIS = { memory: MemoryMini, caps: CapsMini, tasks: TasksMini, devices: DevicesMini };

function Peers() {
  const route = useRoute();
  const focus = TILES.some((t) => t.id === route.query.focus) ? route.query.focus : '';
  const setFocus = (id) => navigate(id ? `/?focus=${id}` : '/', { replace: true });
  return html`<section class=${cx('peers', focus && 'has-focus')} aria-label="四块磁贴">
    ${TILES.map((t) => {
      const Mini = MINIS[t.id];
      const on = focus === t.id;
      const railed = focus && !on;
      return html`<article key=${t.id} class=${cx('peer', `peer-${t.id}`, on && 'is-focus', railed && 'is-rail')}>
        <button type="button" class="peer-head" aria-expanded=${on ? 'true' : 'false'} onClick=${() => setFocus(on ? '' : t.id)}
          title=${on ? '收起' : `展开${t.label}`}>
          <span class="peer-icon"><${Icon} name=${t.icon} size=${20} /></span>
          <span class="peer-label">${t.label}</span>
          ${railed ? html`<span class="peer-rail-n"><${RailNumber} id=${t.id} /></span>` : null}
          <${Icon} name=${on ? 'x' : 'chevron'} size=${16} class="peer-chev" />
        </button>
        ${railed ? null : html`<div class="peer-body"><${Mini} focused=${on} /></div>`}
        ${on ? html`<div class="peer-foot"><${Button} size="sm" variant="ghost" href=${t.href} icon="external">打开完整的${t.label}页<//></div>` : null}
      </article>`;
    })}
  </section>`;
}

function RailNumber({ id }) {
  const s = useStore((st) => st.status);
  if (!s) return '…';
  return { memory: s.memory?.active, caps: s.capabilities?.total, tasks: s.tasks?.active, devices: s.devices?.online }[id] ?? '—';
}

export function BridgePage() {
  const activity = useStore((s) => s.activity);
  const conn = useStore((s) => s.conn);
  return html`<div class="page bridge">
    <${SetupCard} />
    <${ModelsOfflineBanner} />
    <div class="bridge-grid">
      <div class="bridge-main">
        <${Hero} />
        <${Peers} />
      </div>
      <aside class="bridge-side card">
        <header class="card-head"><span class="card-icon"><${Icon} name="wave" /></span>
          <div class="card-titles"><h3 class="card-title">实时活动</h3><p class="card-sub">${conn === 'online' ? '每一步都在这里发生' : '连接恢复后继续更新'}</p></div></header>
        <div class="card-body-flush scroll"><${ActivityStream} items=${activity} limit=${60} /></div>
      </aside>
    </div>
  </div>`;
}
