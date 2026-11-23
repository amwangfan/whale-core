// @ts-check
/**
 * 设备 Devices: a visual control panel. Device cards (phone illustration, online pulse, battery,
 * network, foreground app, role) · detail with role switch, quick actions, capability switches,
 * recent events/actions, privacy panel for personal phones · pairing · digest & search · clients.
 */
import { html, useEffect, useState } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Badge, Button, AsyncButton, Card, Chip, ConfirmButton, Empty, Field, PageHead, Segmented, Skeleton, Switch, ErrorBox, Modal, cx, When, Disclosure } from '../components/ui.js';
import { Phone, BatteryGauge, appName } from '../components/phone.js';
import { PrivacyPanel } from '../components/device-privacy.js';
import { PhoneAddressHint } from '../components/network.js';
import { act, tryAct, useTopic, toast } from '../lib/store.js';
import { useRpc, navigate, linkClick, useNow, useDebouncedCallback } from '../lib/hooks.js';
import { DEVICE_ROLES, CLIENT_ROLES, FILTER_REASONS, timeAgo, fmtTime, clip, brief } from '../lib/format.js';

const ACTION_STATUS = { succeeded: ['成功', 'ok'], failed: ['失败', 'danger'], timeout: ['超时', 'warn'], cancelled: ['已取消', 'muted'], unknown: ['结果未知', 'warn'], pending: ['已发送', 'info'], received: ['已送达', 'info'] };
const CAP_LABELS = {
  'device.status': '状态', 'device.app.list': '应用列表', 'screen.context.read': '读屏幕', 'screen.history.search': '屏幕记忆搜索', 'screen.evidence.read': '读取证据',
  'device.observe': '观察界面', 'device.wait': '等待', 'phone.notification.read': '读通知', 'device.tap': '点击', 'device.long_press': '长按', 'device.swipe': '滑动',
  'device.input_text': '输入文字', 'device.key': '按键', 'device.app.launch': '打开应用', 'device.media.seek_to': '媒体跳转', 'device.screen.record': '录屏', 'device.stop': '停止',
  'phone.notification.post': '发通知', 'phone.call.ring': '响铃', 'phone.call.audio': '通话音频', 'phone.device.safe_action': '安全动作', 'device.shell': 'Root Shell',
  'device.file.read': '读文件', 'device.file.write': '写文件', 'device.file.list': '列目录', 'device.logcat': '日志', 'device.app.install': '安装应用', 'device.app.uninstall': '卸载应用',
  'glasses.card.show': '眼镜卡片', 'glasses.card.update': '更新卡片', 'glasses.card.dismiss': '关闭卡片', 'screen.live.subscribe': '实时屏幕', 'device.output.read': '读输出',
};

/** @param {{d: any, selected: boolean}} p */
function DeviceCard({ d, selected }) {
  const role = DEVICE_ROLES[d.role] || DEVICE_ROLES.personal;
  return html`<a href=${`/devices/${d.id}`} onClick=${linkClick} class=${cx('device-card', selected && 'is-sel', d.online ? 'is-online' : 'is-offline', d.revoked && 'is-revoked')}
    aria-current=${selected ? 'true' : undefined}>
    <${Phone} device=${d} size="sm" />
    <div class="device-card-main">
      <strong>${d.name}</strong>
      <span class="muted small">${d.model || '未知型号'}${d.android ? ` · Android ${d.android}` : ''}</span>
      <span class="device-card-badges">
        ${d.revoked ? html`<${Badge} tone="danger">已撤销<//>` : html`<${Badge} tone=${d.online ? 'ok' : 'muted'} dot=${d.online}>${d.online ? '在线' : '离线'}<//>`}
        <${Badge} tone=${d.role === 'agent' ? 'accent' : 'violet'}>${role.short}<//>
      </span>
      <span class="device-card-live">${d.online ? html`<${BatteryGauge} level=${d.battery} />${d.network ? html`<span class="muted small"><${Icon} name="wifi" size=${12} /> ${d.network}</span>` : null}`
        : html`<span class="muted small">最后在线 <${When} ms=${d.last_seen} fmt=${timeAgo} /></span>`}</span>
    </div>
  </a>`;
}

function PairCard({ autoStart, onApproved }) {
  const [code, setCode] = useState(/** @type {any} */ (null));
  const [name, setName] = useState('');
  const net = useRpc('system.network');
  const now = useNow(1000);
  const start = async () => setCode(await act('devices.pair_start', { name: name.trim(), ttl_s: 600 }));
  useEffect(() => { if (autoStart) start().catch(() => {}); }, [autoStart]);
  useTopic('device.paired', (ev) => { if (code) { toast({ kind: 'ok', title: `已配对：${ev.data.device?.name || '新设备'}` }); setCode(null); onApproved(); } });
  const left = code ? Math.max(0, Math.round((code.expires_at - now) / 1000)) : 0;
  return html`<${Card} title="配对新设备" icon="link" class="pair-card" subtitle="手机安装 whale-mobile 后，在 App 里输入这个 6 位码。配对使用 Ed25519 设备身份，码只能用一次。">
    ${net.data ? html`<${PhoneAddressHint} net=${net.data} />` : null}
    ${code && left > 0 ? html`<div class="pair-code" aria-live="polite">
      <div class="pair-digits" aria-label=${'配对码 ' + code.code.split('').join(' ')}>${code.code.split('').map((c, i) => html`<span key=${i}>${c}</span>`)}</div>
      <div class="pair-timer"><span class="pair-ring" style=${{ '--p': left / (code.ttl_s || 600) }}></span>${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} 后过期</div>
      <p class="muted small">等待手机输入… 配对成功会自动出现在上方。</p>
      <${Button} size="sm" variant="ghost" onClick=${() => setCode(null)}>取消<//>
    </div>` : html`<form class="inline-form" onSubmit=${(e) => { e.preventDefault(); start(); }}>
      <input class="input" value=${name} placeholder="给它起个名字（可选）" aria-label="设备名称" onInput=${(e) => setName(e.currentTarget.value)} />
      <${AsyncButton} variant="primary" icon="link" onClick=${start}>${code ? '重新生成配对码' : '生成配对码'}<//>
    </form>`}
  <//>`;
}

/** @param {{d: any, all: string[], onChanged: () => void}} p */
function QuickActions({ d, onChanged }) {
  const [result, setResult] = useState(/** @type {any} */ (null));
  const [notify, setNotify] = useState(false);
  const [note, setNote] = useState({ title: '来自鲸鱼', body: '' });
  const has = (c) => d.capabilities?.includes(c);
  const run = async (capability, args, label) => {
    const res = await act('devices.action', { id: d.id, capability, arguments: args, timeout_s: capability === 'device.observe' ? 60 : 15 });
    setResult({ label, res });
    onChanged();
  };
  const disabled = !d.online;
  const acts = [
    { cap: 'device.observe', icon: 'eye', label: '观察屏幕', args: { ui_tree: true, max_elements: 120 } },
    { cap: 'device.key', icon: 'home', label: '主屏幕', args: { key: 'home' } },
    { cap: 'device.key', icon: 'back', label: '返回', args: { key: 'back' } },
  ];
  // devices.action returns the ledger view; an `observation` is included once BACKEND_REQUESTS #8 lands
  const obs = result?.res?.observation;
  return html`<${Card} title="快捷动作" icon="bolt" subtitle=${disabled ? '设备离线，动作暂不可用' : '由你直接发起，会被记录、带幂等键；结果如实显示（成功 / 失败 / 未知）'}>
    <div class="quick-actions">
      ${acts.map((a) => html`<${AsyncButton} key=${a.label} variant="soft" icon=${a.icon} disabled=${disabled || !has(a.cap)} title=${has(a.cap) ? a.cap : '设备未开放此能力'}
        onClick=${() => run(a.cap, a.args, a.label)}>${a.label}<//>`)}
      <${Button} variant="soft" icon="bell" disabled=${disabled || !d.scopes?.includes('phone.notification.post')} onClick=${() => setNotify(true)}>发通知<//>
    </div>
    ${result ? html`<div class="action-result">
      <div class="action-result-head"><strong>${result.label}</strong>
        <${Badge} tone=${(ACTION_STATUS[result.res?.status] || [])[1] || 'muted'}>${(ACTION_STATUS[result.res?.status] || [result.res?.status])[0]}<//>
        <button type="button" class="icon-btn" aria-label="关闭结果" onClick=${() => setResult(null)}><${Icon} name="x" size=${14} /></button></div>
      ${result.res?.summary ? html`<p>${result.res.summary}</p>` : null}
      ${result.res?.status === 'unknown' ? html`<p class="step-warn">设备没有在时限内回执，动作可能执行了也可能没有。不会自动重试。</p>` : null}
      ${result.res?.output_ref ? html`<p class="muted small">完整输出：<code>${result.res.output_ref}</code>（鲸鱼可用 device.output.read 读取）</p>` : null}
      ${obs?.elements ? html`<p class="muted small">前台：${appName(obs.foreground_package)} · ${obs.elements.length} 个界面元素</p>
        <ul class="obs-list">${obs.elements.slice(0, 12).map((el, i) => html`<li key=${i}><code>${el.element_id || i}</code>${el.text || el.content_desc || el.class_name}</li>`)}</ul>` : null}
    </div>` : null}
    <${Modal} open=${notify} onClose=${() => setNotify(false)} title=${`发通知到 ${d.name}`} footer=${html`<${Button} variant="ghost" onClick=${() => setNotify(false)}>取消<//>
      <${AsyncButton} variant="primary" icon="send" disabled=${!note.title.trim()} onClick=${async () => {
        const r = await act('devices.notify', { id: d.id, title: note.title.trim(), body: note.body });
        toast({ kind: r.status === 'delivered' || r.status === 'shown' || r.status === 'succeeded' ? 'ok' : 'info', title: '通知已发送', body: `回执：${r.status}` });
        setNotify(false);
      }}>发送<//>`}>
      <${Field} label="标题"><input class="input" value=${note.title} onInput=${(e) => setNote({ ...note, title: e.currentTarget.value })} /><//>
      <${Field} label="内容"><textarea class="input" rows="3" value=${note.body} onInput=${(e) => setNote({ ...note, body: e.currentTarget.value })}></textarea><//>
    <//>
  <//>`;
}

/** @param {{id: string, all: string[], onChanged: () => void}} p */
function DeviceDetail({ id, all, onChanged }) {
  const res = useRpc('devices.get', { id }, [id]);
  const [renaming, setRenaming] = useState('');
  const refresh = useDebouncedCallback(() => res.reload(true), 500);
  useTopic(['device.*'], (ev) => {
    const did = ev.data.device?.id || ev.data.device_id || ev.data.id || ev.data.action?.device_id;
    if (did === id) refresh();
  });
  if (res.error) return html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />`;
  const d = res.data;
  if (!d) return html`<${Skeleton} rows=${8} />`;
  const role = DEVICE_ROLES[d.role] || DEVICE_ROLES.personal;
  const setRole = async (r) => { await act('devices.set_role', { id, role: r }, { ok: `已设为${DEVICE_ROLES[r].label}` }); res.reload(true); onChanged(); };
  const toggleScope = async (cap, on) => {
    const scopes = on ? [...new Set([...(d.scopes || []), cap])] : (d.scopes || []).filter((x) => x !== cap);
    await act('devices.set_scopes', { id, scopes }, { ok: on ? `已开放「${CAP_LABELS[cap] || cap}」` : `已关闭「${CAP_LABELS[cap] || cap}」` });
    res.reload(true);
  };
  return html`<div class="device-detail">
    <section class="device-hero card">
      <${Phone} device=${d} />
      <div class="device-hero-main">
        ${renaming ? html`<form class="inline-form" onSubmit=${async (e) => { e.preventDefault(); await act('devices.rename', { id, name: renaming.trim() }, { ok: '已重命名' }); setRenaming(''); res.reload(true); onChanged(); }}>
          <input class="input" value=${renaming} aria-label="设备名称" onInput=${(e) => setRenaming(e.currentTarget.value)} /><${Button} size="sm" variant="primary" icon="check" type="submit" aria-label="保存名称" /></form>`
          : html`<h2>${d.name} <button type="button" class="icon-btn" aria-label="重命名" onClick=${() => setRenaming(d.name)}><${Icon} name="edit" size=${15} /></button></h2>`}
        <p class="muted small">${d.model || ''}${d.android ? ` · Android ${d.android}` : ''}${d.app_version ? ` · App ${d.app_version}` : ''}${d.flavor ? ` · ${d.flavor === 'root' ? 'Root 版' : '日常版'}` : ''}</p>
        <div class="device-vitals">
          <span><${Badge} tone=${d.online ? 'ok' : 'muted'} dot=${d.online}>${d.online ? '在线' : '离线'}<//></span>
          <span><${BatteryGauge} level=${d.battery} /></span>
          <span><${Icon} name="wifi" size=${14} /> ${d.network || '—'}</span>
          <span title=${d.foreground || ''}><${Icon} name="eye" size=${14} /> ${d.foreground ? appName(d.foreground) : '—'}</span>
          <span class="muted small">${d.online ? html`连接于 ${fmtTime(d.connected_at)}` : html`最后在线 <${When} ms=${d.last_seen} fmt=${timeAgo} />`}</span>
        </div>
        <div class="role-switch">
          <${Segmented} label="设备角色" value=${d.role} onChange=${setRole}
            options=${[{ value: 'agent', label: html`<${Icon} name="sparkles" size=${14} /> 鲸鱼专用手机` }, { value: 'personal', label: html`<${Icon} name="user" size=${14} /> 主人日常手机` }]} />
          <p class="muted small">${role.hint}${!d.role_explicit ? '（根据安装版本自动判断，可手动更改）' : ''}</p>
        </div>
      </div>
    </section>
    <${QuickActions} d=${d} onChanged=${() => res.reload(true)} />
    ${d.role === 'personal' ? html`<${PrivacyPanel} id=${id} />` : html`<div class="banner banner-info small"><${Icon} name="sparkles" size=${16} />这是鲸鱼自己的手机：它在上面的一切经历都会被完整记住（不做隐私过滤），并以第一人称写进记忆。</div>`}
    <${Card} title="能力开关" icon="bolt" subtitle="设备声明的能力中，你允许鲸鱼使用哪些。关闭后立即从能力列表中移除。">
      <div class="scope-grid">${all.map((cap) => {
        const declared = d.capabilities?.includes(cap);
        const on = d.scopes?.includes(cap);
        return html`<div key=${cap} class=${cx('scope', on && 'is-on', !declared && d.online && 'is-undeclared')}>
          <div><strong>${CAP_LABELS[cap] || cap}</strong><code>${cap}</code></div>
          <${Switch} size="sm" checked=${!!on} label=${cap} disabled=${d.revoked} onChange=${(v) => toggleScope(cap, v)} /></div>`;
      })}</div>
    <//>
    <div class="two-col">
      <${Card} title="最近经历" icon="eye" subtitle=${`共 ${d.event_count} 条`} pad=${false}>
        ${d.events?.length ? html`<ul class="event-list">${d.events.map((e) => html`<li key=${e.event_id} class=${e.kind === 'dropped' ? 'is-dropped' : ''}>
          ${e.kind === 'dropped' ? html`<span class="muted"><${Icon} name="lock" size=${13} /> 已过滤：${FILTER_REASONS[e.dropped] || e.dropped}</span>`
            : html`<span>${clip(e.summary, 90)}</span><span class="muted small">${e.extras?.app_name || appName(e.extras?.package)}</span>`}
          <span class="muted small">${fmtTime(e.occurred_at || e.received_at)}</span></li>`)}</ul>` : html`<p class="muted small pad">还没有经历。</p>`}
      <//>
      <${Card} title="最近动作" icon="bolt" pad=${false}>
        ${d.actions?.length ? html`<ul class="event-list">${d.actions.map((a) => {
          const st = ACTION_STATUS[a.status] || [a.status, 'muted'];
          return html`<li key=${a.action_id}><span><code>${a.capability}</code> ${brief(a.arguments, 40)}</span>
            <${Badge} tone=${st[1]}>${st[0]}<//><span class="muted small">${a.origin === 'owner' ? '你' : '鲸鱼'} · ${fmtTime(a.created_at)}</span>
            ${a.summary ? html`<span class="muted small">${clip(a.summary, 60)}</span>` : null}</li>`;
        })}</ul>` : html`<p class="muted small pad">还没有动作。</p>`}
      <//>
    </div>
    <div class="danger-zone">
      ${d.revoked ? html`<span class="muted small">已撤销，需要重新配对才能使用。</span>` : html`<${ConfirmButton} icon="unlink" confirmText="撤销后需重新配对，确定？"
        onConfirm=${async () => { await act('devices.revoke', { id }, { ok: '已撤销设备' }); res.reload(true); onChanged(); }}>撤销设备<//>`}
    </div>
  </div>`;
}

function PhoneMemory() {
  const [q, setQ] = useState('');
  const [hours, setHours] = useState('72');
  const [found, setFound] = useState(/** @type {any[]|null} */ (null));
  const search = async () => { if (q.trim()) setFound((await act('devices.search_events', { query: q.trim(), hours: Number(hours) || 72, limit: 30 })).events); };
  return html`<${Card} title="手机经历 → 记忆" icon="sparkles" subtitle="手机经历会定时整理成“经历”和有原话证据的“事实”。也可以现在就整理，或直接搜索。">
    <div class="inline-form">
      <${AsyncButton} variant="primary" icon="sparkles" onClick=${async () => {
        const r = await act('devices.digest_now', {});
        toast({ kind: 'ok', title: '整理完成', body: `${r.devices} 台设备 · ${r.segments} 段 → ${r.episodes} 条经历、${r.facts} 条事实` });
      }}>立即整理成记忆<//>
      <${Button} variant="ghost" href="/memory?q=手机" icon="memory">查看手机记忆<//>
    </div>
    <form class="inline-form" onSubmit=${(e) => { e.preventDefault(); search().catch(() => {}); }}>
      <input class="input" type="search" value=${q} placeholder="我刚才在手机上看的那个视频…" aria-label="搜索手机经历" onInput=${(e) => setQ(e.currentTarget.value)} />
      <select class="input input-sm" value=${hours} aria-label="时间范围" onChange=${(e) => setHours(e.currentTarget.value)}>
        <option value="6">6 小时</option><option value="24">1 天</option><option value="72">3 天</option><option value="168">7 天</option><option value="720">30 天</option></select>
      <${AsyncButton} variant="soft" icon="search" onClick=${search}>搜索<//>
    </form>
    ${found ? (found.length ? html`<ul class="event-list">${found.map((e, i) => html`<li key=${i}><span>${e.summary}</span><span class="muted small">${e.device} · ${e.app || ''}</span><span class="muted small">${e.time}</span></li>`)}</ul>`
      : html`<p class="muted small">没有找到。被隐私过滤的内容不会出现在这里。</p>`) : null}
  <//>`;
}

function Clients() {
  const res = useRpc('link.clients');
  useTopic('clients.changed', (ev) => res.setData({ clients: ev.data.clients || [] }));
  return html`<${Card} title="已连接的客户端" icon="link" subtitle="通过 Link 协议连接的一切：控制台、手机 App、眼镜、渠道桥、脚本。" pad=${false}>
    ${!res.data ? html`<div class="pad"><${Skeleton} rows=${2} /></div>` : res.data.clients.length ? html`<ul class="event-list">${res.data.clients.map((c) => html`<li key=${c.id}>
      <span><strong>${c.name}</strong> <${Badge} tone="info">${CLIENT_ROLES[c.role] || c.role}<//></span>
      <span class="muted small">${c.capabilities?.length ? `声明 ${c.capabilities.length} 项能力` : '无能力声明'} · 订阅 ${(c.subscriptions || []).join(', ')}</span>
      <span class="muted small">连接于 <${When} ms=${c.connected_at} fmt=${timeAgo} /></span></li>`)}</ul>` : html`<p class="muted small pad">没有客户端连接。</p>`}
  <//>`;
}

export function DevicesPage({ route }) {
  const list = useRpc('devices.list');
  const refresh = useDebouncedCallback(() => list.reload(true), 500);
  useTopic(['device.connected', 'device.disconnected', 'device.status', 'device.updated', 'device.paired', 'device.revoked', 'device.pair_requested'], refresh);
  const [showRevoked, setShowRevoked] = useState(false);
  const devices = list.data?.devices || [];
  const visible = devices.filter((d) => showRevoked || !d.revoked);
  const selected = route.id || (visible.length === 1 ? visible[0].id : '');
  return html`<div class="page devices">
    <${PageHead} title="设备" icon="phone" subtitle="手机是鲸鱼的身体：眼睛、手和通知。上线即注册能力，下线即注销。"
      actions=${html`<${Button} variant="primary" icon="link" href="/devices?pair=1">配对新设备<//>`} />
    ${list.error ? html`<${ErrorBox} error=${list.error} onRetry=${() => list.reload()} />` : null}
    ${!list.data ? html`<${Skeleton} rows=${3} height=${80} />` : html`<div class="device-grid">
      ${visible.map((d) => html`<${DeviceCard} key=${d.id} d=${d} selected=${d.id === selected} />`)}
      ${!visible.length ? html`<${Empty} icon="phone" title="还没有配对设备" hint="安装 whale-mobile，生成配对码，在手机上输入即可。日常手机会做隐私过滤；也可以给鲸鱼一台专用手机。" compact />` : null}
    </div>`}
    ${devices.some((d) => d.revoked) ? html`<button type="button" class="linkish small" onClick=${() => setShowRevoked(!showRevoked)}>${showRevoked ? '隐藏已撤销的设备' : '显示已撤销的设备'}</button>` : null}
    ${selected ? html`<${DeviceDetail} key=${selected} id=${selected} all=${list.data?.capabilities || []} onChanged=${() => list.reload(true)} />` : null}
    <div class="two-col">
      <${PairCard} autoStart=${route.query.pair === '1'} onApproved=${() => list.reload(true)} />
      <${PhoneMemory} />
    </div>
    <${Clients} />
    <${Disclosure} title="连接说明"><p class="muted small">手机经设备线协议连接 <code>${list.data?.ws_path || '/api/devices/ws'}</code>（Ed25519 设备身份、序号、五终态回执、幂等键）。核心在边缘把设备能力翻译成 <code>${'device.<名字>.<能力>'}</code>。</p><//>
  </div>`;
}
