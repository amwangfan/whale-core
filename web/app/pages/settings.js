// @ts-check
/**
 * 设置 Settings: grouped cards — 模型 · 智能体 · 记忆 · 自主 · 设备 · 服务.
 * Every card keeps a local draft and saves a deep-merge patch with `config.update`.
 */
import { html, useEffect, useState } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Badge, Button, AsyncButton, Card, ConfirmButton, Field, Meter, PageHead, Skeleton, Switch, TagEditor, ErrorBox, Segmented, cx, When } from '../components/ui.js';
import { ActivityStream } from '../components/live.js';
import { NetworkPanel, useRestart, RestartState } from '../components/network.js';
import { act, tryAct, useTopic, useStore, applyTheme, logout, store } from '../lib/store.js';
import { useRpc, linkClick, navigate } from '../lib/hooks.js';
import { fmtUptime, timeAgo, CLIENT_ROLES } from '../lib/format.js';

const SECTIONS = [
  { id: 'models', label: '模型', icon: 'spark' }, { id: 'agent', label: '智能体', icon: 'user' }, { id: 'memory', label: '记忆', icon: 'memory' },
  { id: 'autonomy', label: '自主', icon: 'compass' }, { id: 'devices', label: '设备', icon: 'phone' }, { id: 'service', label: '服务', icon: 'power' },
];
const ROLES = [
  { id: 'chat', label: '对话', hint: '主力模型：回答、推理、调用能力' }, { id: 'fast', label: '快速', hint: '记忆提取、群聊判断等后台小活；留空则用对话模型' },
  { id: 'vision', label: '视觉', hint: '带图片的消息；留空则用对话模型' }, { id: 'embedding', label: '向量', hint: '语义检索；留空则用内置哈希向量（只有关键词效果）' },
];
const PRESETS = [
  { id: 'deepseek', label: 'DeepSeek', base_url: 'https://api.deepseek.com/v1' },
  { id: 'dashscope', label: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { id: 'moonshot', label: 'Kimi', base_url: 'https://api.moonshot.cn/v1' },
  { id: 'siliconflow', label: '硅基流动', base_url: 'https://api.siliconflow.cn/v1' },
  { id: 'openai', label: 'OpenAI', base_url: 'https://api.openai.com/v1' },
  { id: 'ollama', label: 'Ollama（本机）', base_url: 'http://127.0.0.1:11434/v1' },
];
const ZONES = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Taipei', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'UTC'];
const ID_RE = /^[a-z0-9_-]{1,32}$/;

/** Local draft of one config section. @param {any} value */
function useDraft(value) {
  const [d, setD] = useState(value);
  useEffect(() => { setD(value); }, [JSON.stringify(value)]);
  return [d, setD, JSON.stringify(d) !== JSON.stringify(value)];
}

/** @param {{label: any, value: any, onChange: (v: any) => void, min?: number, max?: number, step?: number, unit?: string, hint?: any}} p */
function NumField({ label, value, onChange, min, max, step = 1, unit, hint }) {
  return html`<${Field} label=${label} hint=${hint}><span class="num-input"><input class="input" type="number" min=${min} max=${max} step=${step} value=${value}
    onInput=${(e) => onChange(e.currentTarget.value === '' ? '' : Number(e.currentTarget.value))} />${unit ? html`<span class="muted small">${unit}</span>` : null}</span><//>`;
}

function SaveBar({ dirty, onSave, onReset }) {
  if (!dirty) return null;
  return html`<div class="form-actions"><${AsyncButton} variant="primary" icon="check" onClick=${onSave}>保存<//><${Button} variant="ghost" onClick=${onReset}>撤销<//></div>`;
}

/** @param {{cfg: any, status: any, onCfg: (c: any) => void, reloadStatus: () => void}} p */
function Providers({ cfg, status, onCfg, reloadStatus }) {
  const [form, setForm] = useState(/** @type {any} */ (null));
  const providers = cfg.models.providers || [];
  const save = async () => {
    const body = { id: form.id.trim(), base_url: form.base_url.trim(), label: form.label.trim() };
    if (form.api_key) body.api_key = form.api_key;
    await act('models.upsert_provider', body, { ok: `已保存服务商「${body.label || body.id}」` });
    onCfg(await act('config.get', {}, { quiet: true })); reloadStatus(); setForm(null);
  };
  const idOk = form && ID_RE.test(form.id || '');
  return html`<div class="providers">
    <h4>服务商 <span class="muted small">任何 OpenAI 兼容接口</span></h4>
    ${providers.length ? html`<ul class="provider-list">${providers.map((p) => {
      const st = status?.providers?.[p.id];
      const keySet = cfg.secrets?.[`provider.${p.id}`];
      return html`<li key=${p.id} class="provider">
        <div class="provider-main"><strong>${p.label || p.id}</strong><code>${p.id}</code><span class="muted small mono">${p.base_url}</span></div>
        <div class="provider-meta">${keySet ? html`<${Badge} tone="ok" icon="key">密钥已设置<//>` : html`<${Badge} tone="muted">无密钥<//>`}
          ${st ? html`<span class="muted small">${st.requests} 次 · ${st.errors} 错 · 平均 ${Math.round(st.avg_ms)} ms</span>` : null}
          ${st?.last_error ? html`<span class="tone-text-danger small" title=${st.last_error}>最近错误</span>` : null}</div>
        <div class="row-actions">
          <${Button} size="sm" variant="ghost" icon="edit" onClick=${() => setForm({ ...p, api_key: '', editing: true })}>编辑<//>
          <${ConfirmButton} icon="trash" confirmText="移除？" onConfirm=${async () => { await act('models.remove_provider', { id: p.id }, { ok: '已移除' }); onCfg(await act('config.get', {}, { quiet: true })); reloadStatus(); }}>移除<//>
        </div></li>`;
    })}</ul>` : html`<p class="muted small">还没有服务商。选一个预设开始：</p>`}
    ${form ? html`<div class="provider-form card">
      <div class="form-grid">
        <${Field} label="ID" hint="小写字母、数字、- _" error=${form.id && !idOk ? '格式不对' : ''}><input class="input mono" value=${form.id} disabled=${form.editing} onInput=${(e) => setForm({ ...form, id: e.currentTarget.value })} /><//>
        <${Field} label="名称"><input class="input" value=${form.label} onInput=${(e) => setForm({ ...form, label: e.currentTarget.value })} /><//>
      </div>
      <${Field} label="接口地址 base_url"><input class="input mono" value=${form.base_url} placeholder="https://…/v1" onInput=${(e) => setForm({ ...form, base_url: e.currentTarget.value })} /><//>
      <${Field} label="API Key" hint=${form.editing && cfg.secrets?.[`provider.${form.id}`] ? '已设置；留空不修改' : '只保存在服务器的 secrets.json；本机模型可留空'}>
        <input class="input mono" type="password" autocomplete="new-password" value=${form.api_key} placeholder="sk-…" onInput=${(e) => setForm({ ...form, api_key: e.currentTarget.value })} /><//>
      <div class="form-actions"><${AsyncButton} variant="primary" icon="check" disabled=${!idOk || !form.base_url.trim()} onClick=${save}>保存服务商<//><${Button} variant="ghost" onClick=${() => setForm(null)}>取消<//></div>
    </div>` : html`<div class="chips">${PRESETS.filter((x) => !providers.some((p) => p.id === x.id)).map((x) => html`<${Button} key=${x.id} size="sm" variant="ghost" icon="plus"
      onClick=${() => setForm({ ...x, api_key: '' })}>${x.label}<//>`)}<${Button} size="sm" variant="ghost" icon="plus" onClick=${() => setForm({ id: '', label: '', base_url: '', api_key: '' })}>自定义<//></div>`}
  </div>`;
}

/** @param {{role: any, cfg: any, status: any, onSaved: (c: any) => void}} p */
function RoleRow({ role, cfg, status, onSaved }) {
  const route = cfg.models[role.id] || {};
  const [d, setD, dirty] = useDraft({ provider: route.provider || '', model: route.model || '',
    reasoning_effort: route.reasoning_effort || '' });
  const [models, setModels] = useState(/** @type {string[]} */ ([]));
  const [test, setTest] = useState(/** @type {any} */ (null));
  const st = status?.roles?.[role.id];
  const list = async () => { if (d.provider) setModels((await act('models.list_remote', { provider: d.provider }, { timeout: 15000 })).models || []); };
  return html`<div class="role-row">
    <div class="role-name"><strong>${role.label}</strong>${st?.configured ? html`<${Badge} tone="ok" dot>已配置<//>` : html`<${Badge} tone="muted">未配置<//>`}<p class="muted small">${role.hint}</p></div>
    <div class="role-controls">
      <select class="input" value=${d.provider} aria-label=${role.label + '服务商'} onChange=${(e) => { setD({ ...d, provider: e.currentTarget.value }); setModels([]); }}>
        <option value="">未设置</option>${cfg.models.providers.map((p) => html`<option key=${p.id} value=${p.id}>${p.label || p.id}</option>`)}</select>
      <input class="input mono" list=${'models-' + role.id} value=${d.model} placeholder="模型名，如 deepseek-chat" aria-label=${role.label + '模型'} onInput=${(e) => setD({ ...d, model: e.currentTarget.value })} />
      <datalist id=${'models-' + role.id}>${models.map((m) => html`<option key=${m} value=${m}></option>`)}</datalist>
      <select class="input" style=${{ maxWidth: '7.5em' }} value=${d.reasoning_effort} aria-label=${role.label + '思考强度'} title="思考强度（仅支持的模型生效，如 kimi-k3）"
        onChange=${(e) => setD({ ...d, reasoning_effort: e.currentTarget.value })}>
        <option value="">默认强度</option><option value="high">high</option><option value="medium">medium</option><option value="low">low</option>
      </select>
      <${AsyncButton} size="sm" variant="ghost" icon="list" disabled=${!d.provider} title="从服务商拉取模型列表" onClick=${list}>${models.length ? `${models.length} 个模型` : '列出模型'}<//>
      ${dirty ? html`<${AsyncButton} size="sm" variant="primary" icon="check" onClick=${async () => onSaved(await act('config.update', { patch: { models: { [role.id]: d } } }, { ok: `${role.label}模型已保存` }))}>保存<//>` : null}
      ${role.id !== 'embedding' ? html`<${AsyncButton} size="sm" variant="soft" icon="play" disabled=${dirty} title=${dirty ? '先保存' : '发送一条探测消息'} onClick=${async () => setTest(await act('models.test', { role: role.id }, { timeout: 60000 }))}>测试连接<//>` : null}
    </div>
    ${test ? html`<p class=${cx('role-test', test.ok ? 'tone-text-ok' : 'tone-text-danger')} role="status"><${Icon} name=${test.ok ? 'check' : 'alert'} size=${14} />
      ${test.ok ? `连接正常 · ${test.model} · ${test.latency_ms} ms · 回复「${test.reply}」` : `连接失败：${test.error}`}</p>` : null}
  </div>`;
}

function ModelsCard({ cfg, onCfg }) {
  const status = useRpc('models.status');
  const [budget, setBudget, dirty] = useDraft(cfg.models.daily_request_budget);
  const b = status.data?.budget;
  return html`<${Card} id="models" title="模型" icon="spark" subtitle="鲸鱼的大脑。按角色路由到不同服务商：对话、快速、视觉、向量。">
    ${status.data?.offline ? html`<div class="banner banner-warn small"><${Icon} name="alert" size=${16} />对话模型未配置，鲸鱼正在用离线脑。添加服务商并为“对话”选择模型即可。</div>` : null}
    <${Providers} cfg=${cfg} status=${status.data} onCfg=${onCfg} reloadStatus=${() => status.reload(true)} />
    <h4>角色路由</h4>
    <div class="roles">${ROLES.map((r) => html`<${RoleRow} key=${r.id} role=${r} cfg=${cfg} status=${status.data} onSaved=${(c) => { onCfg(c); status.reload(true); }} />`)}</div>
    <h4>每日请求预算</h4>
    <div class="budget">
      ${b ? html`<${Meter} value=${b.used} max=${b.limit} tone=${b.used / b.limit > 0.9 ? 'danger' : b.used / b.limit > 0.7 ? 'warn' : 'accent'} label="今日用量" /><span class="muted small">今日已用 ${b.used} / ${b.limit} 次（超出后返回 rate_limited）</span>` : null}
      <span class="inline-form"><${NumField} label="上限" value=${budget} min=${1} unit="次/天" onChange=${setBudget} />
        ${dirty ? html`<${AsyncButton} size="sm" variant="primary" icon="check" onClick=${async () => { onCfg(await act('config.update', { patch: { models: { daily_request_budget: Number(budget) } } }, { ok: '预算已保存' })); status.reload(true); }}>保存<//>` : null}</span>
    </div>
  <//>`;
}

function AgentCard({ cfg, onCfg }) {
  const pick = (a) => ({ name: a.name, timezone: a.timezone, max_steps: a.max_steps, history_turns: a.history_turns,
    reply_gear: a.reply_gear || 'fast' });
  const [d, setD, dirty] = useDraft(pick(cfg.agent));
  return html`<${Card} id="agent" title="智能体" icon="user" subtitle="名字与时区出现在每一次提示词里；步数上限防止一次行动停不下来。">
    <div class="form-grid">
      <${Field} label="名字"><input class="input" value=${d.name} onInput=${(e) => setD({ ...d, name: e.currentTarget.value })} /><//>
      <${Field} label="时区" hint="影响时间感知与调度"><input class="input" list="tz-list" value=${d.timezone} onInput=${(e) => setD({ ...d, timezone: e.currentTarget.value })} />
        <datalist id="tz-list">${ZONES.map((z) => html`<option key=${z} value=${z}></option>`)}</datalist><//>
      <${NumField} label="每次行动最多步数" value=${d.max_steps} min=${1} max=${50} onChange=${(v) => setD({ ...d, max_steps: v })} hint="思考 ⇄ 调用能力的轮数" />
      <${NumField} label="携带历史轮数" value=${d.history_turns} min=${0} max=${100} onChange=${(v) => setD({ ...d, history_turns: v })} hint="对话上下文里带多少轮历史" />
      <${Field} label="回复档位" hint="慢档=完整缓冲想完一次性发（不配流式增量）"><select class="input" value=${d.reply_gear} onChange=${(e) => setD({ ...d, reply_gear: e.currentTarget.value })}>
        <option value="fast">快档（流式）</option><option value="slow">慢档（缓冲一次性发）</option>
      </select><//>
    </div>
    <${SaveBar} dirty=${dirty} onReset=${() => setD(pick(cfg.agent))} onSave=${async () => onCfg(await act('config.update', { patch: { agent: { ...d, max_steps: Number(d.max_steps), history_turns: Number(d.history_turns) } } }, { ok: '已保存' }))} />
  <//>`;
}

function MemoryCard({ cfg, onCfg }) {
  const [d, setD, dirty] = useDraft(cfg.memory);
  const [res, setRes] = useState(/** @type {any} */ (null));
  return html`<${Card} id="memory" title="记忆" icon="memory" subtitle="每轮对话后自动提取（必须有原话），整理时去重、取代、建立关系；强度按半衰期衰减，被想起即强化。">
    <div class="switch-rows"><div class="switch-row"><div><strong>自动提取记忆</strong><p class="muted small">对话结束后，用快速模型从你的原话里提取事实、偏好与经历。</p></div>
      <${Switch} checked=${d.extraction} label="自动提取记忆" onChange=${(v) => setD({ ...d, extraction: v })} /></div></div>
    <div class="form-grid">
      <${NumField} label="半衰期" value=${d.half_life_days} min=${1} step=${0.5} unit="天" onChange=${(v) => setD({ ...d, half_life_days: v })} hint="多久不被想起，强度减半" />
      <${NumField} label="每次召回条数" value=${d.recall_limit} min=${1} max=${50} onChange=${(v) => setD({ ...d, recall_limit: v })} />
      <${NumField} label="整理间隔" value=${d.consolidation_interval_min} min=${5} unit="分钟" onChange=${(v) => setD({ ...d, consolidation_interval_min: v })} hint="衰减、归档、过期候选" />
    </div>
    <${SaveBar} dirty=${dirty} onReset=${() => setD(cfg.memory)} onSave=${async () => onCfg(await act('config.update', { patch: { memory: { ...d, half_life_days: Number(d.half_life_days), recall_limit: Number(d.recall_limit), consolidation_interval_min: Number(d.consolidation_interval_min) } } }, { ok: '已保存' }))} />
    <div class="inline-form"><${AsyncButton} variant="soft" icon="broom" onClick=${async () => setRes(await act('memory.maintain', {}))}>立即整理<//>
      ${res ? html`<span class="muted small">归档 ${res.archived} 条衰退的经历，过期 ${res.expired_candidates} 条候选知识</span>` : null}
      <${Button} variant="ghost" href="/memory" icon="external">打开记忆<//></div>
  <//>`;
}

function AutonomyCard({ cfg, onCfg }) {
  const st = useRpc('autonomy.status');
  useTopic('autonomy.*', () => st.reload(true));
  const [d, setD, dirty] = useDraft(cfg.autonomy);
  const [topic, setTopic] = useState('');
  const hours = Array.from({ length: 24 }, (_, i) => i);
  const recent = (st.data?.recent || []).map((e) => ({ id: e.id, topic: e.topic, ts: e.ts, data: e.data })).reverse();
  return html`<${Card} id="autonomy" title="自主" icon="compass" subtitle="空闲时按兴趣探索（只读能力），写日记、发现候选知识；值得分享时主动找你——但受静默时段、每日额度和冷却约束。">
    <div class="switch-rows">
      <div class="switch-row"><div><strong>自主探索</strong><p class="muted small">按间隔从兴趣中选题搜索与阅读。</p></div><${Switch} checked=${d.explore} label="自主探索" onChange=${(v) => setD({ ...d, explore: v })} /></div>
      <div class="switch-row"><div><strong>主动消息</strong><p class="muted small">${st.data ? (st.data.proactive_ready ? '现在可以主动发消息' : `当前不会主动发：${st.data.proactive_reason}`) : ''}</p></div><${Switch} checked=${d.proactive} label="主动消息" onChange=${(v) => setD({ ...d, proactive: v })} /></div>
    </div>
    <div class="form-grid">
      <${NumField} label="探索间隔" value=${d.explore_interval_min} min=${15} unit="分钟" onChange=${(v) => setD({ ...d, explore_interval_min: v })} />
      <${NumField} label="每日主动消息上限" value=${d.proactive_daily_limit} min=${0} max=${50} unit="条" onChange=${(v) => setD({ ...d, proactive_daily_limit: v })} />
      <${Field} label="静默时段" hint="这段时间里不会主动打扰你"><span class="inline-form">
        <select class="input input-sm" value=${d.quiet_hours[0]} aria-label="静默开始" onChange=${(e) => setD({ ...d, quiet_hours: [Number(e.currentTarget.value), d.quiet_hours[1]] })}>${hours.map((h) => html`<option key=${h} value=${h}>${h}:00</option>`)}</select>
        <span class="muted">至</span>
        <select class="input input-sm" value=${d.quiet_hours[1]} aria-label="静默结束" onChange=${(e) => setD({ ...d, quiet_hours: [d.quiet_hours[0], Number(e.currentTarget.value)] })}>${hours.map((h) => html`<option key=${h} value=${h}>${h}:00</option>`)}</select></span><//>
    </div>
    <${Field} label="兴趣" hint="除了这些，鲸鱼也会从你的偏好记忆里找话题"><${TagEditor} value=${d.interests || []} label="兴趣" placeholder="例如 深海生物、AI 新闻" onChange=${(v) => setD({ ...d, interests: v })} /><//>
    <${SaveBar} dirty=${dirty} onReset=${() => setD(cfg.autonomy)} onSave=${async () => onCfg(await act('config.update', { patch: { autonomy: { ...d, explore_interval_min: Number(d.explore_interval_min), proactive_daily_limit: Number(d.proactive_daily_limit) } } }, { ok: '已保存' }))} />
    <form class="inline-form" onSubmit=${(e) => { e.preventDefault(); tryAct('autonomy.explore', topic.trim() ? { topic: topic.trim() } : {}, { ok: '已开始探索' }); }}>
      <input class="input" value=${topic} placeholder=${'主题（留空则随机选：' + ((st.data?.topics || []).slice(0, 3).join('、') || '最近的新知') + '）'} aria-label="探索主题" onInput=${(e) => setTopic(e.currentTarget.value)} />
      <${AsyncButton} variant="soft" icon="compass" onClick=${() => tryAct('autonomy.explore', topic.trim() ? { topic: topic.trim() } : {}, { ok: '已开始探索' })}>立即探索<//>
    </form>
    <h4>最近的自主行为</h4>
    ${st.error ? html`<${ErrorBox} error=${st.error} onRetry=${() => st.reload()} />` : !st.data ? html`<${Skeleton} rows=${2} />` : html`<${ActivityStream} items=${recent} limit=${12} dense />`}
  <//>`;
}

function DevicesCard({ cfg, onCfg }) {
  const [v, setV, dirty] = useDraft(cfg.devices.digest_interval_min);
  return html`<${Card} id="devices" title="设备" icon="phone" subtitle="手机经历多久整理一次成记忆。">
    <span class="inline-form"><${NumField} label="整理间隔" value=${v} min=${5} unit="分钟" onChange=${setV} hint="保存后在下次服务重启时生效（也可随时在设备页“立即整理”）" />
      ${dirty ? html`<${AsyncButton} size="sm" variant="primary" icon="check" onClick=${async () => onCfg(await act('config.update', { patch: { devices: { digest_interval_min: Number(v) } } }, { ok: '已保存' }))}>保存<//>` : null}</span>
    <${Button} variant="ghost" href="/devices" icon="external">打开设备<//>
  <//>`;
}

function ServiceCard() {
  const status = useStore((s) => s.status);
  const theme = useStore((s) => s.theme);
  const conn = useStore((s) => s.conn);
  const setup = useStore((s) => s.setup);
  const clients = useRpc('link.clients');
  const restart = useRestart(() => clients.reload(true));
  useTopic('clients.changed', (ev) => clients.setData({ clients: ev.data.clients || [] }));
  return html`<${Card} id="service" title="服务" icon="power">
    <dl class="kv kv-meta">
      <div><dt>版本</dt><dd>Whale ${status?.version || '—'}</dd></div><div><dt>Python</dt><dd>${status?.python || '—'}</dd></div>
      <div><dt>已运行</dt><dd>${status ? fmtUptime(status.uptime_s) : '—'}</dd></div><div><dt>实时连接</dt><dd>${conn === 'online' ? '正常' : conn}</dd></div>
    </dl>
    <div class="inline-form">
      <${ConfirmButton} variant="soft" icon="power" confirmText="确定重启？正在进行的回复会中断" disabled=${restart.phase === 'restarting'} onConfirm=${restart.start}>重启服务<//>
      <${Button} variant="ghost" icon="wand" onClick=${() => navigate('/setup')}>重新运行引导<//>
      ${setup && !setup.completed && !setup.unsupported ? html`<${Badge} tone="warn">首次设置还没完成<//>` : null}
    </div>
    <${RestartState} phase=${restart.phase} />
    <h4>手机连接</h4>
    <${NetworkPanel} confirmRestart />
    <h4>已连接的客户端</h4>
    ${clients.data ? html`<ul class="mini-list">${clients.data.clients.map((c) => html`<li key=${c.id}><strong>${c.name}</strong><${Badge} tone="info">${CLIENT_ROLES[c.role] || c.role}<//>
      <span class="muted small"><${When} ms=${c.connected_at} fmt=${timeAgo} /></span></li>`)}</ul>` : html`<${Skeleton} rows=${2} />`}
    <h4>外观</h4>
    <${Segmented} label="主题" value=${theme} onChange=${applyTheme} options=${[{ value: 'auto', label: '跟随系统' }, { value: 'dark', label: '深海' }, { value: 'light', label: '晴空' }]} />
    <h4>访问令牌</h4>
    <p class="muted small">令牌保存在本浏览器。轮换请在服务器上运行 <code>whale token --rotate</code>，之后所有客户端需要重新输入。</p>
    <${ConfirmButton} icon="logout" confirmText="确定退出？" onConfirm=${() => logout()}>退出（清除本机令牌）<//>
  <//>`;
}

export function SettingsPage({ route }) {
  const res = useRpc('config.get');
  const [cfgLocal, setCfg] = useState(/** @type {any} */ (null));
  const cfg = cfgLocal || res.data;
  useEffect(() => { if (res.data) setCfg(res.data); }, [res.data]);
  useTopic('config.changed', () => res.reload(true));
  const section = route.id || '';
  useEffect(() => {
    if (!section || !cfg) return undefined;
    const t = setTimeout(() => globalThis.document?.getElementById(section)?.scrollIntoView({ behavior: store.get().reduceMotion ? 'auto' : 'smooth', block: 'start' }), 60);
    return () => clearTimeout(t);
  }, [section, !!cfg]);
  return html`<div class="page settings">
    <${PageHead} title="设置" icon="gear" subtitle="改动即时生效，并广播给所有客户端" />
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="设置分组">${SECTIONS.map((s) => html`<a key=${s.id} href=${`/settings/${s.id}`} onClick=${linkClick}
        class=${cx('settings-link', section === s.id && 'is-active')}><${Icon} name=${s.icon} size=${16} />${s.label}</a>`)}</nav>
      <div class="settings-cards">
        ${res.error ? html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />` : null}
        ${!cfg ? html`<${Skeleton} rows=${8} height=${40} />` : html`
          <${ModelsCard} cfg=${cfg} onCfg=${setCfg} />
          <${AgentCard} cfg=${cfg} onCfg=${setCfg} />
          <${MemoryCard} cfg=${cfg} onCfg=${setCfg} />
          <${AutonomyCard} cfg=${cfg} onCfg=${setCfg} />
          <${DevicesCard} cfg=${cfg} onCfg=${setCfg} />
          <${ServiceCard} />`}
      </div>
    </div>
  </div>`;
}

