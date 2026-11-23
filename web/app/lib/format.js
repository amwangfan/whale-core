// @ts-check
/** Formatting and the console's vocabulary: every label shown to the owner lives here. */

/** @param {number|null|undefined} ms */
export function timeAgo(ms, now = Date.now()) {
  if (!ms) return '—';
  const d = Math.round((now - ms) / 1000);
  if (d < -60) return timeUntil(ms, now);
  if (d < 10) return '刚刚';
  if (d < 60) return `${d} 秒前`;
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
  return fmtDate(ms);
}
/** @param {number|null|undefined} ms */
export function timeUntil(ms, now = Date.now()) {
  if (!ms) return '—';
  const d = Math.round((ms - now) / 1000);
  if (d <= 0) return '即将';
  if (d < 60) return `${d} 秒后`;
  if (d < 3600) return `${Math.ceil(d / 60)} 分钟后`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时 ${Math.round((d % 3600) / 60)} 分后`;
  return `${Math.floor(d / 86400)} 天后`;
}
const pad = (n) => String(n).padStart(2, '0');
/** @param {number} ms */
export function fmtDate(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
/** @param {number|null|undefined} ms */
export function fmtTime(ms, withDate = true) {
  if (!ms) return '—';
  const d = new Date(ms);
  const today = new Date();
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (!withDate || d.toDateString() === today.toDateString()) return hm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}
/** @param {number|null|undefined} ms */
export function fmtDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
  const m = Math.floor(ms / 60000);
  return m < 60 ? `${m} 分 ${Math.round((ms % 60000) / 1000)} 秒` : `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}
/** @param {number} s */
export function fmtUptime(s) {
  if (!s && s !== 0) return '—';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
  return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时`;
}
/** @param {number} n */
export function fmtNum(n) {
  if (n == null) return '—';
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`;
  return String(n);
}
/** @param {string} s @param {number} n */
export function clip(s, n = 80) {
  const t = String(s ?? '');
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}
/** Compact JSON for step args/outputs. @param {any} v */
export function brief(v, n = 160) {
  if (v == null) return '';
  if (typeof v === 'string') return clip(v, n);
  try { return clip(JSON.stringify(v), n); } catch { return ''; }
}

// -- vocabulary --------------------------------------------------------------------------------------
export const PHASES = {
  idle: { label: '空闲', hint: '在等你开口，或在后台安静地整理记忆' },
  thinking: { label: '思考中', hint: '正在回忆与推理' },
  acting: { label: '执行中', hint: '正在调用能力做事' },
  waiting: { label: '等待批准', hint: '有动作需要你确认' },
};

export const KINDS = {
  fact: { label: '事实', color: 'var(--k-fact)', hint: '关于你的客观信息' },
  preference: { label: '偏好', color: 'var(--k-preference)', hint: '你喜欢/不喜欢什么' },
  episode: { label: '经历', color: 'var(--k-episode)', hint: '发生过的事' },
  knowledge: { label: '知识', color: 'var(--k-knowledge)', hint: '学到的外部知识' },
  diary: { label: '日记', color: 'var(--k-diary)', hint: '鲸鱼自己的探索日记' },
  person: { label: '人物', color: 'var(--k-person)', hint: '你生活中的人' },
  skill: { label: '技能', color: 'var(--k-skill)', hint: '学会的做事方法' },
  style: { label: '习惯', color: 'var(--k-style)', hint: '鲸鱼自己总结的性格与口癖' },
};
/** @param {string} kind */
export const kindLabel = (kind) => KINDS[kind]?.label || kind || '记忆';

export const MEM_STATUS = {
  active: { label: '有效', tone: 'ok' }, candidate: { label: '候选', tone: 'warn' },
  superseded: { label: '已被取代', tone: 'muted' }, archived: { label: '已归档', tone: 'muted' },
  forgotten: { label: '已遗忘', tone: 'danger' },
};

/** Scope category: owner | shared | group | person. @param {string} scope */
export function scopeCat(scope) {
  if (!scope || scope === 'owner') return 'owner';
  if (scope === 'shared') return 'shared';
  if (scope.startsWith('group:')) return 'group';
  return 'person';
}
export const SCOPES = {
  owner: { label: '主人', color: 'var(--sc-owner)', hint: '只在与你的对话里使用' },
  shared: { label: '共享', color: 'var(--sc-shared)', hint: '鲸鱼自己的学习与习惯，处处可用' },
  group: { label: '群聊', color: 'var(--sc-group)', hint: '群里学到的，单向流入主人会话' },
  person: { label: '私聊', color: 'var(--sc-person)', hint: '与他人的私聊，完全隔离' },
};
/** Port of memory/scopes.label(). @param {string} scope */
export function scopeLabel(scope) {
  const c = scopeCat(scope);
  if (c === 'group') return `群聊 ${scope.slice(6)}`;
  if (c === 'person') return `私聊 ${scope}`;
  return SCOPES[c].label;
}
export const SCOPE_RULE = '群聊记忆单向流入主人会话；其他人的私聊完全隔离；只有鲸鱼自己总结的性格与口癖共享';

export const TASK_STATUS = {
  draft: { label: '草稿', tone: 'muted' }, queued: { label: '排队中', tone: 'info' },
  scheduled: { label: '已调度', tone: 'violet' }, running: { label: '执行中', tone: 'accent' },
  waiting: { label: '等待批准', tone: 'warn' }, done: { label: '已完成', tone: 'ok' },
  failed: { label: '失败', tone: 'danger' }, cancelled: { label: '已取消', tone: 'muted' },
  interrupted: { label: '被中断', tone: 'warn' },
};
export const RUN_STATUS = {
  running: { label: '进行中', tone: 'accent' }, waiting: { label: '等待批准', tone: 'warn' },
  completed: { label: '完成', tone: 'ok' }, failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'muted' }, interrupted: { label: '被中断', tone: 'warn' },
};
export const STEP_STATUS = {
  running: { label: '进行中', tone: 'accent' }, done: { label: '完成', tone: 'ok' }, ok: { label: '成功', tone: 'ok' },
  error: { label: '出错', tone: 'danger' }, unknown: { label: '结果未知', tone: 'warn' },
  denied: { label: '已拒绝', tone: 'danger' }, interrupted: { label: '被中断', tone: 'warn' },
};
export const STEP_KINDS = {
  recall: { label: '回忆', icon: 'memory' }, think: { label: '思考', icon: 'spark' }, tool: { label: '调用能力', icon: 'bolt' },
  approval: { label: '等待批准', icon: 'shield' }, reply: { label: '回复', icon: 'chat' }, delegate: { label: '委派', icon: 'branch' },
  plan: { label: '计划', icon: 'list' }, note: { label: '备注', icon: 'note' },
};
export const RISKS = {
  read: { label: '只读', tone: 'ok', hint: '只读取信息，没有副作用' },
  write: { label: '写入', tone: 'info', hint: '修改鲸鱼自己的数据' },
  external: { label: '对外', tone: 'warn', hint: '对外发消息或操作设备' },
  dangerous: { label: '危险', tone: 'danger', hint: '执行命令、安装应用等' },
};
export const VERDICTS = { allow: { label: '允许', tone: 'ok' }, ask: { label: '询问', tone: 'warn' }, deny: { label: '禁止', tone: 'danger' } };
export const PLUGIN_STATUS = {
  active: { label: '运行中', tone: 'ok' }, needs_config: { label: '待配置', tone: 'warn' }, error: { label: '出错', tone: 'danger' },
  disabled: { label: '已停用', tone: 'muted' }, degraded: { label: '降级', tone: 'warn' },
};
export const DEVICE_ROLES = {
  agent: { label: '鲸鱼专用手机', short: '鲸鱼的手机', hint: '全部能力、不做隐私过滤：这是鲸鱼自己的身体' },
  personal: { label: '主人日常手机', short: '主人的手机', hint: '隐私过滤：系统隐私界面、输入法和你排除的应用永不记录' },
};
export const FILTER_REASONS = {
  system_private_screen: '系统隐私界面', system_private_app: '系统隐私应用/输入法', excluded_app: '你排除的应用',
  excluded_domain: '你排除的网站', phone_marked_sensitive: '手机标记为敏感', other: '其他',
};
export const CLIENT_ROLES = { console: '控制台', mobile: '手机 App', glasses: '眼镜', bridge: '渠道桥', cli: '命令行', device: '设备' };
export const PROVIDER_GROUPS = {
  builtin: { label: '内置', hint: '鲸鱼与生俱来的能力' }, plugin: { label: '插件', hint: '插件提供的能力' },
  device: { label: '设备', hint: '已连接手机的身体能力' }, client: { label: '客户端', hint: 'Link 客户端声明的能力' },
};

// -- activity humanisation -----------------------------------------------------------------------------
/**
 * @param {{topic: string, data: any}} ev
 * @returns {{icon: string, text: string, tone: string, href?: string}}
 */
export function humanize(ev) {
  const { topic, data = {} } = ev;
  const run = data.run || {};
  const item = data.item || {};
  const dev = data.device || {};
  const task = data.task || {};
  const modeName = { chat: '对话', goal: '任务', explore: '探索' }[run.mode] || '行动';
  switch (topic) {
    case 'run.started': return { icon: 'play', tone: 'accent', text: `开始${modeName}：${clip(run.input, 48)}`, href: runHref(run) };
    case 'run.completed': return { icon: 'check', tone: 'ok', text: `完成${modeName}${run.stats?.latency_ms ? `（${fmtDuration(run.stats.latency_ms)}）` : ''}：${clip(run.output || run.input, 48)}`, href: runHref(run) };
    case 'run.failed': return { icon: 'alert', tone: 'danger', text: `${modeName}失败：${clip(run.error, 60)}`, href: runHref(run) };
    case 'run.cancelled': return { icon: 'stop', tone: 'muted', text: `取消了${modeName}`, href: runHref(run) };
    case 'run.step': {
      const s = data.step || {};
      if (s.kind === 'tool') return { icon: 'bolt', tone: s.status === 'done' ? 'info' : 'warn', text: `调用能力「${s.title || s.input?.capability || ''}」${STEP_STATUS[s.status]?.label || ''}` };
      if (s.kind === 'recall') return { icon: 'memory', tone: 'violet', text: s.title || '回忆相关记忆' };
      if (s.kind === 'approval') return { icon: 'shield', tone: 'warn', text: s.title || '等待批准' };
      return { icon: STEP_KINDS[s.kind]?.icon || 'dot', tone: 'muted', text: s.title || STEP_KINDS[s.kind]?.label || '执行了一个步骤' };
    }
    case 'run.plan': return { icon: 'list', tone: 'info', text: `更新了计划（${(data.plan || []).length} 步）` };
    case 'memory.created': return { icon: 'memory', tone: 'violet', text: `记住了一条${kindLabel(item.kind)}：${clip(item.content, 52)}`, href: item.id ? `/memory/${item.id}` : undefined };
    case 'memory.updated': return { icon: 'edit', tone: 'muted', text: `更新记忆：${clip(item.content, 52)}`, href: item.id ? `/memory/${item.id}` : undefined };
    case 'memory.reinforced': return { icon: 'up', tone: 'violet', text: `强化了记忆：${clip(item.content, 52)}`, href: item.id ? `/memory/${item.id}` : undefined };
    case 'memory.superseded': return { icon: 'swap', tone: 'warn', text: '新记忆取代了一条旧记忆', href: data.new ? `/memory/${data.new}` : undefined };
    case 'memory.forgotten': return { icon: 'trash', tone: 'muted', text: '遗忘了一条记忆' };
    case 'memory.extracted': return { icon: 'sparkles', tone: 'violet', text: `从对话中提取了 ${data.count ?? 0} 条记忆` };
    case 'memory.maintained': return { icon: 'broom', tone: 'muted', text: `整理记忆：归档 ${data.archived ?? 0} 条，过期候选 ${data.expired_candidates ?? 0} 条` };
    case 'approval.requested': return { icon: 'shield', tone: 'warn', text: `请求批准：${data.title || data.capability || '一个动作'}` };
    case 'approval.resolved': return { icon: data.status === 'approved' ? 'check' : 'x', tone: data.status === 'approved' ? 'ok' : 'danger', text: `${data.status === 'approved' ? '已批准' : data.status === 'expired' ? '已过期' : '已拒绝'}：${data.title || data.capability || '一个动作'}` };
    case 'task.created': return { icon: 'flag', tone: 'info', text: `新任务：${clip(task.title, 48)}`, href: task.id ? `/tasks/${task.id}` : undefined };
    case 'task.updated': return { icon: 'flag', tone: TASK_STATUS[task.status]?.tone || 'muted', text: `任务「${clip(task.title, 32)}」${TASK_STATUS[task.status]?.label || task.status || '已更新'}`, href: task.id ? `/tasks/${task.id}` : undefined };
    case 'task.deleted': return { icon: 'trash', tone: 'muted', text: '删除了一个任务' };
    case 'schedule.fired': return { icon: 'clock', tone: 'info', text: `定时触发：${data.name || '调度任务'}` };
    case 'schedule.changed': return { icon: 'clock', tone: 'muted', text: data.removed ? '移除了一个定时任务' : `调度更新：${data.job?.name || ''}` };
    case 'notify': return { icon: 'bell', tone: 'accent', text: `主动告诉你：${clip(data.title, 24)} ${clip(data.body, 48)}` };
    case 'device.connected': return { icon: 'phone', tone: 'ok', text: `${dev.name || '设备'} 上线了`, href: '/devices' };
    case 'device.disconnected': return { icon: 'phone', tone: 'muted', text: `${data.name || '设备'} 离线了`, href: '/devices' };
    case 'device.paired': return { icon: 'link', tone: 'ok', text: `配对了新设备：${dev.name || ''}`, href: '/devices' };
    case 'device.revoked': return { icon: 'unlink', tone: 'danger', text: `撤销了设备：${dev.name || ''}`, href: '/devices' };
    case 'device.updated': return { icon: 'phone', tone: 'muted', text: `设备设置更新：${dev.name || ''}`, href: '/devices' };
    case 'device.event': return { icon: 'eye', tone: 'muted', text: `${data.device || '手机'}：${clip(data.event?.summary, 56)}` };
    case 'device.event_filtered': return { icon: 'lock', tone: 'muted', text: `隐私过滤了一条手机经历${data.reason ? `（${FILTER_REASONS[data.reason] || data.reason}）` : ''}` };
    case 'device.action': return { icon: 'bolt', tone: data.action?.status === 'succeeded' ? 'ok' : 'info', text: `手机动作 ${data.action?.capability || ''}：${data.phase === 'request' ? '已发送' : data.action?.status || '有回执'}` };
    case 'device.notification': return { icon: 'bell', tone: 'muted', text: `手机通知：${clip(data.title || data.app || '', 48)}` };
    case 'device.pair_requested': return { icon: 'link', tone: 'warn', text: `有手机请求配对${data.code ? `（${data.code}）` : ''}`, href: '/devices' };
    case 'device.digest': return { icon: 'sparkles', tone: 'violet', text: `把手机经历整理成了 ${data.episodes ?? 0} 段经历、${data.facts ?? 0} 条事实` };
    case 'autonomy.exploring': return { icon: 'compass', tone: 'accent', text: `开始自主探索：${clip(data.topic, 40)}` };
    case 'autonomy.explored': return { icon: 'compass', tone: 'violet', text: `探索完成：${clip(data.topic, 30)}，${(data.findings || []).length} 条候选知识` };
    case 'autonomy.proactive': return { icon: 'bell', tone: data.sent ? 'accent' : 'muted', text: data.sent ? `主动发了一条消息：${clip(data.text, 40)}` : `想主动说话但忍住了${data.why ? `（${data.why}）` : ''}` };
    case 'plugin.changed': return { icon: 'plug', tone: 'muted', text: `插件「${data.name || data.id || ''}」${PLUGIN_STATUS[data.status]?.label || data.status || '已更新'}`, href: '/capabilities' };
    case 'config.changed': return { icon: 'gear', tone: 'muted', text: `设置已更新（${(data.sections || []).join('、')}）` };
    case 'persona.activated': return { icon: 'user', tone: 'accent', text: '切换了人格', href: '/persona' };
    case 'persona.saved': return { icon: 'user', tone: 'muted', text: `保存了人格：${data.name || ''}`, href: '/persona' };
    case 'system.started': return { icon: 'power', tone: 'ok', text: `Whale ${data.version || ''} 已启动` };
    case 'group.updated': return { icon: 'group', tone: 'muted', text: `群空间「${data.name || data.id || ''}」设置已更新`, href: groupHref(data.id) };
    case 'group.refreshed': return { icon: 'sparkles', tone: 'violet', text: `整理了群「${data.name || data.id || ''}」${data.counts ? `：新增 ${data.counts.profiles_added ?? 0} 份画像、${data.counts.preferences_added ?? 0} 条喜好` : ''}`, href: groupHref(data.id) };
    case 'group.archived': return { icon: 'group', tone: 'muted', text: `群「${data.name || data.id || ''}」存档了 ${data.count ?? 0} 条新消息`, href: groupHref(data.id) };
    case 'setup.completed': return { icon: 'check', tone: 'ok', text: '完成了首次设置' };
    default: return { icon: 'dot', tone: 'muted', text: topic };
  }
}
/** @param {string} [id] */
export function groupHref(id) { return id ? `/groups/${encodeURIComponent(id)}` : '/groups'; }

// -- group memory spaces --------------------------------------------------------------------------------
export const AUTO_REFRESH = [
  { value: 0, label: '手动' }, { value: 30, label: '每 30 分钟' }, { value: 120, label: '每 2 小时' }, { value: 1440, label: '每天' },
];
/** @param {number} min */
export function autoRefreshLabel(min) {
  const hit = AUTO_REFRESH.find((o) => o.value === min);
  if (hit) return hit.label;
  return min >= 60 ? `每 ${Math.round(min / 60)} 小时` : `每 ${min} 分钟`;
}
export const TRAIT_KINDS = {
  expressions: { label: '口头禅', hint: '反复出现的说法', icon: 'chat' },
  topics: { label: '常聊', hint: '经常聊起的话题', icon: 'sparkles' },
  scenes: { label: '典型反应', hint: '在某类场景下的典型回应', icon: 'wave' },
};
/** keyword type → colour token */
export const KEYWORD_TYPES = {
  名词: 'var(--k-fact)', 动词: 'var(--k-knowledge)', 作品名: 'var(--k-preference)', 原文短语: 'var(--k-episode)',
  专名: 'var(--k-person)', 词组: 'var(--k-diary)',
};
export const EVIDENCE_REASONS = {
  unsupported_source: '这条记忆的来源不支持查看原文',
  source_missing: '原始消息已不在存档里（可能已被删除，或该成员已退出记录）',
};
/** "11-20 21:00 – 22:10" style span. @param {number} a @param {number} [b] */
export function fmtSpan(a, b) {
  if (!a) return '—';
  const s = fmtTime(a);
  if (!b || b === a) return s;
  const sameDay = new Date(a).toDateString() === new Date(b).toDateString();
  return `${s} – ${sameDay ? fmtTime(b, false) : fmtTime(b)}`;
}
/** Whole days covered by two ms timestamps (≥ 1). @param {number} a @param {number} b */
export function daySpan(a, b) {
  if (!a || !b) return 0;
  return Math.max(1, Math.round((b - a) / 86400000) + 1);
}
/** @param {any} run */
function runHref(run) {
  if (run.task_id) return `/tasks/${run.task_id}`;
  if (run.conversation_id) return `/chat/${run.conversation_id}`;
  return undefined;
}

/** Group key for a capability provider: builtin | plugin | device | client. @param {string} provider */
export function providerGroup(provider) {
  return String(provider || 'builtin').split(':')[0];
}
