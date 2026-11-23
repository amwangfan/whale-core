// @ts-check
/** First-run wizard steps that talk to the backend: 选择大脑 (models.presets / setup.quick_model) and 连接手机. */
import { html, useEffect, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Badge, Button, AsyncButton, Disclosure, Field, Skeleton, ErrorBox, cx } from './ui.js';
import { NetworkPanel } from './network.js';
import { act, refreshStatus, refreshSetup, useTopic, useStore } from '../lib/store.js';
import { rpc, errorText } from '../lib/api.js';
import { useRpc, useNow } from '../lib/hooks.js';
import { fmtDuration } from '../lib/format.js';

/** Radio-card keyboard: arrows move the selection. @param {any[]} list @param {string} cur @param {(id: string) => void} pick */
function arrowKeys(list, cur, pick) {
  return (e) => {
    if (!['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(e.key) || !list.length) return;
    e.preventDefault();
    const i = Math.max(0, list.findIndex((x) => x.id === cur));
    const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : list.length - 1;
    const next = list[(i + d) % list.length];
    pick(next.id);
    const esc = globalThis.CSS?.escape ? globalThis.CSS.escape(next.id) : String(next.id).replace(/"/g, '');
    const el = /** @type {any} */ (e.currentTarget).querySelector?.(`[data-id="${esc}"]`);
    el?.focus?.();
  };
}

/** @param {{p: any, on: boolean, onPick: () => void}} props */
function PresetCard({ p, on, onPick }) {
  return html`<button type="button" role="radio" aria-checked=${on ? 'true' : 'false'} tabindex=${on ? 0 : -1} data-id=${p.id}
    class=${cx('wiz-card', 'preset', on && 'is-on')} onClick=${onPick}>
    <span class="preset-top"><strong>${p.name}</strong>
      ${p.local ? html`<${Badge} tone="ok" icon="home">本地<//>` : html`<${Badge} tone="info" icon="globe">云端<//>`}</span>
    <span class="preset-note">${p.note}</span>
    <span class="preset-foot">${p.needs_key ? html`<span class="muted small"><${Icon} name="key" size=${12} /> 需要 API Key</span>` : html`<span class="muted small"><${Icon} name="check" size=${12} /> 不需要 Key</span>`}
      <code class="muted small">${p.chat_model}</code></span>
    ${on ? html`<span class="wiz-card-check" aria-hidden="true"><${Icon} name="check" size=${14} /></span>` : null}
  </button>`;
}

/** @param {{test: any}} p */
export function TestSuccess({ test }) {
  return html`<div class="wiz-result is-ok" role="status">
    <div class="wiz-result-head"><span class="wiz-result-icon"><${Icon} name="check" size=${18} /></span>
      <div><strong>连接成功，鲸鱼有大脑了</strong><p class="muted small">${test.model} · 回复用时 ${fmtDuration(test.latency_ms)}</p></div></div>
    <blockquote class="wiz-reply"><span class="muted small">它的第一句话：</span>${test.reply || '（空回复）'}</blockquote>
  </div>`;
}

/** @param {{error: string, onRetry: () => any, needsKey: boolean}} p */
export function TestError({ error, onRetry, needsKey }) {
  return html`<div class="wiz-result is-error" role="alert">
    <div class="wiz-result-head"><span class="wiz-result-icon"><${Icon} name="alert" size=${18} /></span>
      <div><strong>还没连上</strong><p class="wiz-err-msg">${error}</p></div></div>
    <ul class="wiz-tips muted small">
      ${needsKey ? html`<li>检查 API Key 是否完整复制（通常以 sk- 开头，前后没有空格）</li><li>确认账户里还有余额或免费额度</li>`
        : html`<li>确认 Ollama 正在运行：在终端执行 <code>ollama serve</code></li><li>确认已经下载了模型，例如 <code>ollama pull qwen2.5:7b</code></li>`}
      <li>如果电脑需要代理才能上网，Whale 也需要同样的网络环境</li>
    </ul>
    <${AsyncButton} variant="soft" icon="refresh" onClick=${onRetry}>重试<//>
  </div>`;
}

/** 选择大脑. @param {{w: any, set: (patch: any) => void, onNext: () => void}} p */
export function ModelStep({ w, set, onNext }) {
  const res = useRpc('models.presets');
  const setup = useStore((s) => s.setup);
  const presets = res.data?.presets || [];
  const p = presets.find((x) => x.id === w.preset);
  useEffect(() => { if (!w.preset && presets.length) set({ preset: presets[0].id }); }, [presets.length]);
  const pick = (id) => { if (id !== w.preset) set({ preset: id, test: null, testError: '', base_url: '', chat_model: '' }); };
  const run = async () => {
    if (!p) return;
    const params = { preset: p.id };
    if (w.key.trim()) params.api_key = w.key.trim();
    if (w.base_url.trim()) params.base_url = w.base_url.trim();
    if (w.chat_model.trim()) params.chat_model = w.chat_model.trim();
    set({ testing: true, test: null, testError: '' });
    try {
      const r = await rpc('setup.quick_model', params, { timeout: 90000 });
      if (r?.test?.ok) { set({ testing: false, test: r.test }); refreshStatus(); refreshSetup(); }
      else set({ testing: false, testError: r?.test?.error || '模型没有回应' });
    } catch (e) { set({ testing: false, testError: errorText(e) }); }
  };
  const canTest = !!p && (!p.needs_key || !!w.key.trim()) && !w.testing;
  return html`<div class="wiz-step-body">
    <p class="wiz-lead">鲸鱼需要一个大模型来思考。选一个你方便使用的服务，填上 Key，点「连接并测试」就好。以后随时可以在 设置 → 模型 里更换。</p>
    ${setup?.steps?.model && !w.test ? html`<p class="banner banner-info small"><${Icon} name="check" size=${16} />已经配置过对话模型。可以直接继续，或者在这里换一个。</p>` : null}
    ${res.error ? html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />` : !res.data ? html`<${Skeleton} rows=${3} height=${70} />` : html`
      <div class="wiz-cards presets" role="radiogroup" aria-label="选择模型服务" onKeyDown=${arrowKeys(presets, w.preset, pick)}>
        ${presets.map((x) => html`<${PresetCard} key=${x.id} p=${x} on=${x.id === w.preset} onPick=${() => pick(x.id)} />`)}
      </div>`}
    ${p ? html`<div class="wiz-form">
      ${p.local ? html`<p class="banner banner-info small"><${Icon} name="home" size=${16} /><span><strong>${p.name}</strong> 在你自己的电脑上运行模型，不需要 Key，也不联网。
        请先安装 Ollama 并保持它在运行（<code>ollama serve</code>），再下载模型：<code>ollama pull ${p.chat_model}</code>。</span></p>` : null}
      ${p.needs_key ? html`<${Field} label=${`${p.name} 的 API Key`} hint=${html`Key 只保存在这台电脑上的 secrets.json 里。${p.key_url ? html` <a href=${p.key_url} target="_blank" rel="noopener noreferrer" class="key-link">去获取 Key <${Icon} name="external" size=${12} /></a>` : ''}`}>
        <input class="input mono" type="password" autocomplete="off" spellcheck="false" value=${w.key} placeholder="sk-…" aria-label="API Key"
          onInput=${(e) => set({ key: e.currentTarget.value, testError: '' })} onKeyDown=${(e) => { if (e.key === 'Enter' && canTest) { e.preventDefault(); run(); } }} /><//>` : null}
      <${Disclosure} title="高级" hint="自定义接口地址或模型名">
        <div class="form-grid">
          <${Field} label="接口地址 base_url"><input class="input mono" value=${w.base_url} placeholder=${p.base_url} onInput=${(e) => set({ base_url: e.currentTarget.value })} /><//>
          <${Field} label="对话模型"><input class="input mono" value=${w.chat_model} placeholder=${p.chat_model} onInput=${(e) => set({ chat_model: e.currentTarget.value })} /><//>
        </div>
      <//>
      <div class="wiz-actions-inline">
        <${Button} variant="primary" icon="play" loading=${!!w.testing} disabled=${!canTest} onClick=${run}>${w.testing ? '正在连接…' : '连接并测试'}<//>
        ${p.needs_key && !w.key.trim() ? html`<span class="muted small">先填上 API Key</span>` : null}
      </div>
      ${w.test ? html`<${TestSuccess} test=${w.test} />` : null}
      ${w.testError ? html`<${TestError} error=${w.testError} onRetry=${run} needsKey=${!!p.needs_key} />` : null}
    </div>` : null}
    ${!w.test ? html`<p class="wiz-skip"><button type="button" class="linkish" onClick=${() => { set({ offline: true }); onNext(); }}>先跳过，用离线模式体验</button>
      <span class="muted small">离线模式下鲸鱼只能做简单的回答，之后随时可以配置。</span></p>` : null}
  </div>`;
}

/** Pairing code with countdown + 3-step instructions. @param {{initial?: any, urls: string[]}} p */
export function PairBlock({ initial = null, urls }) {
  const [code, setCode] = useState(/** @type {any} */ (initial));
  const [paired, setPaired] = useState(/** @type {string} */ (''));
  const now = useNow(1000);
  useTopic('device.paired', (ev) => { setPaired(ev.data?.device?.name || '新手机'); setCode(null); });
  const start = async () => setCode(await act('devices.pair_start', { name: '', ttl_s: 600 }));
  const left = code ? Math.max(0, Math.round((code.expires_at - now) / 1000)) : 0;
  if (paired) return html`<div class="wiz-result is-ok" role="status"><div class="wiz-result-head"><span class="wiz-result-icon"><${Icon} name="check" size=${18} /></span>
    <div><strong>已连接：${paired}</strong><p class="muted small">以后可以在「设备」页管理它的权限和隐私。</p></div></div></div>`;
  return html`<div class="wiz-pair">
    <ol class="wiz-howto">
      <li><span>1</span><div><strong>安装 App</strong><p class="muted small">在手机上安装 whale-mobile（日常版或 Root 版）。</p></div></li>
      <li><span>2</span><div><strong>填地址和配对码</strong><p class="muted small">打开 App 的设置，填入地址 ${urls?.[0] ? html`<code>${urls[0]}</code>` : '（见上方）'} 和下面的 6 位配对码。</p></div></li>
      <li><span>3</span><div><strong>完成</strong><p class="muted small">配对成功后这里会自动显示，手机就成了鲸鱼的眼睛和手。</p></div></li>
    </ol>
    ${code && left > 0 ? html`<div class="pair-code" aria-live="polite">
      <div class="pair-digits" aria-label=${'配对码 ' + String(code.code).split('').join(' ')}>${String(code.code).split('').map((c, i) => html`<span key=${i}>${c}</span>`)}</div>
      <div class="pair-timer"><span class="pair-ring" style=${{ '--p': left / (code.ttl_s || 600) }}></span>${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} 后过期 · 只能用一次</div>
    </div>` : html`<${AsyncButton} variant="primary" icon="link" onClick=${start}>${code ? '配对码已过期，重新生成' : '生成配对码'}<//>`}
  </div>`;
}

/** 连接手机 (optional). @param {{w: any, set: (patch: any) => void}} p */
export function PhoneStep({ w, set }) {
  return html`<div class="wiz-step-body">
    <p class="wiz-lead">装上 <strong>whale-mobile</strong> 后，手机就成了鲸鱼的眼睛和手：它会记住你在手机上看过的内容（锁屏、密码框、输入法等隐私界面永远不记录），也能在你允许时帮你操作手机。这一步可以跳过。</p>
    <${NetworkPanel} showOff=${false} onNetwork=${(n) => set({ lan: !!(n.lan_enabled && !n.restart_required) })}
      whenLive=${(net) => html`<${PairBlock} initial=${w.pair || null} urls=${net.urls} />`} />
  </div>`;
}
