// @ts-check
/**
 * First-run setup wizard (full screen): 欢迎 → 选择大脑 → 人格 → 连接手机 → 连接 QQ → 完成.
 * Shown after login while `setup.status.completed` is false (unless the owner chose 「稍后再说」),
 * and at /setup (设置 → 服务 「重新运行引导」, command palette). Every step can be skipped; the
 * progress dots jump anywhere; nothing here is a dead end.
 */
import { html, useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Button, AsyncButton, Field, Skeleton, ErrorBox, cx } from '../components/ui.js';
import { ModelStep, PhoneStep } from '../components/setup-steps.js';
import { act, toast, useStore, snoozeSetup, refreshSetup, refreshStatus, store } from '../lib/store.js';
import { useRpc, navigate, useRoute } from '../lib/hooks.js';

export const STEPS = [
  { id: 'welcome', label: '欢迎', title: '你好，我是鲸鱼' },
  { id: 'model', label: '大脑', title: '给它选一个大脑' },
  { id: 'persona', label: '人格', title: '它是什么性格？' },
  { id: 'phone', label: '手机', title: '连接你的手机', optional: true },
  { id: 'qq', label: 'QQ', title: '连接 QQ', optional: true },
  { id: 'done', label: '完成', title: '一切就绪' },
];

/** @param {string} a */
function isImage(a) { return /^(data:image\/|https?:\/\/)/.test(a || ''); }
/** @param {{avatar?: string, name?: string}} p */
export function AvatarMark({ avatar, name }) {
  if (isImage(avatar || '')) return html`<span class="wiz-avatar"><img src=${avatar} alt="" /></span>`;
  return html`<span class="wiz-avatar">${avatar || (name || '鲸').slice(0, 1)}</span>`;
}

/** @param {{w: any, set: (p: any) => void, onNext: () => void}} p */
function WelcomeStep({ w, set, onNext }) {
  return html`<div class="wiz-step-body">
    <p class="wiz-lead">一个会<strong>记住你</strong>、能<strong>帮你做事</strong>、也能<strong>连接手机和 QQ</strong> 的私人 AI。它运行在你自己的电脑上，你们聊过的事、它学到的东西，都保存在这里。</p>
    <ul class="wiz-features">
      <li><span class="wiz-feat-icon tone-violet"><${Icon} name="memory" size=${20} /></span><div><strong>记得你</strong><p>你说过的喜好、发生过的事，它会整理成记忆，下次自然地用上。</p></div></li>
      <li><span class="wiz-feat-icon tone-info"><${Icon} name="tasks" size=${20} /></span><div><strong>帮你做事</strong><p>查资料、整理信息、定时提醒；有风险的动作会先问你。</p></div></li>
      <li><span class="wiz-feat-icon tone-ok"><${Icon} name="phone" size=${20} /></span><div><strong>连接手机和 QQ</strong><p>在手机上、在 QQ 私聊和群里，都是同一个它。</p></div></li>
    </ul>
    <form class="wiz-name" onSubmit=${(e) => { e.preventDefault(); onNext(); }}>
      <${Field} label="给它起个名字" hint="以后可以在 设置 → 智能体 里改">
        <input class="input input-lg" value=${w.name} maxlength="24" placeholder="鲸鱼" aria-label="AI 的名字" onInput=${(e) => set({ name: e.currentTarget.value })} /><//>
    </form>
  </div>`;
}

/** @param {{w: any, set: (p: any) => void}} p */
function PersonaStep({ w, set }) {
  const res = useRpc('personas.list');
  const list = res.data?.personas || [];
  const active = w.persona || res.data?.active || 'default';
  return html`<div class="wiz-step-body">
    <p class="wiz-lead">人格决定了它说话的语气和性格。先用默认的也完全可以——相处久了，它会慢慢形成自己的习惯。</p>
    ${res.error ? html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />` : !res.data ? html`<${Skeleton} rows=${2} height=${60} />` : html`
      <div class="wiz-cards personas" role="radiogroup" aria-label="选择人格">
        ${list.map((p) => {
          const on = p.id === active;
          return html`<button type="button" key=${p.id} role="radio" aria-checked=${on ? 'true' : 'false'} class=${cx('wiz-card', 'persona-pick', on && 'is-on')}
            style=${{ '--persona': p.color || 'var(--accent)' }} onClick=${() => set({ persona: p.id })}>
            <${AvatarMark} avatar=${p.avatar} name=${p.name} />
            <span><strong>${p.name}</strong><span class="muted small">${p.tagline || (p.builtin ? '默认人格' : '自定义人格')}</span></span>
            ${on ? html`<span class="wiz-card-check" aria-hidden="true"><${Icon} name="check" size=${14} /></span>` : null}
          </button>`;
        })}
      </div>`}
    <p class="wiz-skip"><button type="button" class=${cx('linkish', w.landing === '/persona' && 'is-on')} onClick=${() => set({ landing: w.landing === '/persona' ? '' : '/persona' })}>
      <${Icon} name="upload" size=${14} />${w.landing === '/persona' ? '好的，完成后带你去「人格」页导入' : '我有 SillyTavern 角色卡，完成后去导入'}</button></p>
  </div>`;
}

function QqStep() {
  return html`<div class="wiz-step-body">
    <p class="wiz-lead">鲸鱼可以通过 QQ 机器人（OneBot v11，例如 NapCat、Lagrange）和你私聊，也能在你允许的群里聊天。这一步可以跳过，随时再配。</p>
    <ul class="wiz-features">
      <li><span class="wiz-feat-icon tone-info"><${Icon} name="chat" size=${20} /></span><div><strong>私聊</strong><p>在 QQ 上找它聊天，和在这里一样记得你。</p></div></li>
      <li><span class="wiz-feat-icon tone-violet"><${Icon} name="group" size=${20} /></span><div><strong>群聊记忆空间</strong><p>把一个群设为记忆空间后，它会在你点「更新」时整理话题、群友的说话方式和明确说过的喜好；原始聊天只存在本机。</p></div></li>
    </ul>
    <p class="banner banner-info small"><${Icon} name="plug" size=${16} /><span>QQ 在 <strong>能力 → 插件 → QQ</strong> 里配置：填 OneBot 的地址和 Access Token，打开开关即可。</span></p>
    <div class="wiz-actions-inline"><${Button} variant="soft" icon="external" onClick=${() => { snoozeSetup(); navigate('/capabilities'); }}>现在去配置 QQ<//>
      <span class="muted small">引导会先收起来，之后在 总览 或 设置 里可以继续。</span></div>
  </div>`;
}

/** @param {{w: any}} p */
function DoneStep({ w }) {
  const setup = useStore((s) => s.setup);
  const st = setup?.steps || {};
  const rows = [
    { ok: true, label: '名字', value: w.name.trim() || '鲸鱼' },
    { ok: !!(w.test || st.model), label: '大脑', value: w.test ? `${w.test.model}` : st.model ? '已配置' : '离线模式（之后在 设置 → 模型 配置）' },
    { ok: true, label: '人格', value: w.personaName || '默认' },
    { ok: !!(w.lan || st.phone), label: '手机', value: st.phone ? '已连接' : w.lan ? '已允许局域网连接' : '跳过了（之后在「设备」页配对）' },
    { ok: !!st.qq, label: 'QQ', value: st.qq ? '已配置' : '跳过了（之后在 能力 → 插件 配置）' },
  ];
  return html`<div class="wiz-step-body">
    <p class="wiz-lead">${w.name.trim() || '鲸鱼'} 已经准备好了。去 总览 跟它打个招呼吧。</p>
    <ul class="wiz-summary">${rows.map((r) => html`<li key=${r.label} class=${r.ok ? 'is-ok' : 'is-skip'}>
      <${Icon} name=${r.ok ? 'check' : 'clock'} size=${16} /><span class="wiz-sum-label">${r.label}</span><span>${r.value}</span></li>`)}</ul>
  </div>`;
}

/**
 * @param {{initial?: any, onExit?: () => void}} p  initial: state overrides (tests, deep links)
 */
export function SetupWizard({ initial = {}, onExit }) {
  const status = useStore((s) => s.status);
  const route = useRoute();
  const [w, setW] = useState(() => ({
    step: 'welcome', name: status?.agent?.name || '鲸鱼', preset: '', key: '', base_url: '', chat_model: '',
    test: null, testError: '', testing: false, persona: '', personaName: '', lan: false, landing: '', offline: false, pair: null, ...initial,
  }));
  const set = (patch) => setW((cur) => ({ ...cur, ...patch }));
  const idx = Math.max(0, STEPS.findIndex((s) => s.id === w.step));
  const step = STEPS[idx];
  const head = useRef(/** @type {HTMLElement|null} */ (null));
  useEffect(() => { head.current?.focus?.(); }, [w.step]);
  const go = (i) => set({ step: STEPS[Math.max(0, Math.min(STEPS.length - 1, i))].id });
  const personas = useRpc(w.step === 'persona' ? 'personas.list' : null, {}, [w.step]);

  const next = async () => {
    if (step.id === 'persona') {
      const active = personas.data?.active || 'default';
      const chosen = w.persona || active;
      const p = (personas.data?.personas || []).find((x) => x.id === chosen);
      if (chosen !== active) await act('personas.activate', { id: chosen }, { ok: `已切换为「${p?.name || chosen}」` }).catch(() => null);
      set({ personaName: p?.name || w.personaName });
    }
    go(idx + 1);
  };
  const exit = () => { snoozeSetup(); onExit?.(); if (route.page === 'setup') navigate('/', { replace: true }); };
  const finish = async () => {
    const name = w.name.trim() || '鲸鱼';
    await act('setup.complete', { name });
    store.set({ setupLater: false });
    await refreshSetup();
    refreshStatus();
    toast({ kind: 'ok', title: `设置完成，${name} 已经准备好了`, body: '有任何想法，直接在下面的输入框里对它说。' });
    navigate(w.landing || '/', { replace: true });
  };

  let body;
  if (step.id === 'welcome') body = html`<${WelcomeStep} w=${w} set=${set} onNext=${next} />`;
  else if (step.id === 'model') body = html`<${ModelStep} w=${w} set=${set} onNext=${() => go(idx + 1)} />`;
  else if (step.id === 'persona') body = html`<${PersonaStep} w=${w} set=${set} />`;
  else if (step.id === 'phone') body = html`<${PhoneStep} w=${w} set=${set} />`;
  else if (step.id === 'qq') body = html`<${QqStep} />`;
  else body = html`<${DoneStep} w=${w} />`;

  const last = step.id === 'done';
  const nextLabel = step.id === 'welcome' ? '开始' : '继续';
  return html`<div class="wizard" role="dialog" aria-modal="true" aria-labelledby="wiz-title">
    <div class="wiz-sea" aria-hidden="true"><span class="wiz-wave w1"></span><span class="wiz-wave w2"></span><span class="wiz-orb"></span></div>
    <header class="wiz-top">
      <span class="wiz-brand"><span class="wiz-brand-mark"><${Icon} name="wave" size=${18} /></span>Whale · 首次设置</span>
      <button type="button" class="linkish wiz-later" onClick=${exit}>稍后再说</button>
    </header>
    <nav class="wiz-progress" aria-label="设置进度">
      <ol class="wiz-dots">${STEPS.map((s, i) => html`<li key=${s.id}>
        <button type="button" class=${cx('wiz-dot', i === idx && 'is-current', i < idx && 'is-done')} aria-current=${i === idx ? 'step' : undefined}
          aria-label=${`第 ${i + 1} 步：${s.label}${s.optional ? '（可选）' : ''}`} onClick=${() => go(i)}>
          <span class="wiz-dot-mark">${i < idx ? html`<${Icon} name="check" size=${11} />` : i + 1}</span><span class="wiz-dot-label">${s.label}</span></button></li>`)}</ol>
    </nav>
    <main class="wiz-main">
      <section class=${cx('wiz-panel', `wiz-${step.id}`)} key=${step.id}>
        <p class="wiz-kicker">第 ${idx + 1} / ${STEPS.length} 步${step.optional ? ' · 可选' : ''}</p>
        <h1 id="wiz-title" class="wiz-title" tabindex="-1" ref=${head}>${step.title}</h1>
        ${body}
      </section>
    </main>
    <footer class="wiz-foot">
      ${idx > 0 ? html`<${Button} variant="ghost" icon="left" onClick=${() => go(idx - 1)}>上一步<//>` : html`<span></span>`}
      <div class="wiz-foot-right">
        ${step.optional ? html`<${Button} variant="plain" onClick=${() => go(idx + 1)}>跳过这一步<//>` : null}
        ${last ? html`<${AsyncButton} variant="primary" size="lg" icon="check" onClick=${finish}>开始使用<//>`
          : html`<${AsyncButton} variant="primary" size="lg" icon="chevron" onClick=${next} disabled=${!!w.testing}>${nextLabel}<//>`}
      </div>
    </footer>
  </div>`;
}

export function SetupPage() {
  return html`<${SetupWizard} />`;
}
