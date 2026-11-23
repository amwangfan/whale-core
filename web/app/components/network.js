// @ts-check
/**
 * Phone connectivity: LAN switch (`system.set_lan`), phone addresses (`system.network.urls`) and a
 * service restart (`system.restart`) that follows the live link down and back up.
 * Shared by the first-run wizard, 设置 → 服务 and 设备 → 配对新设备.
 */
import { html, useEffect, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Button, AsyncButton, ConfirmButton, Switch, Skeleton, ErrorBox, cx } from './ui.js';
import { act, toast, useStore, link } from '../lib/store.js';
import { useRpc, linkClick } from '../lib/hooks.js';

/** @param {string} text */
export async function copyText(text) {
  try {
    await globalThis.navigator?.clipboard?.writeText(text);
    toast({ kind: 'ok', title: '已复制', body: text, timeout: 2500 });
  } catch {
    toast({ kind: 'warn', title: '无法自动复制', body: `请手动选中：${text}` });
  }
}

/** Big copyable phone addresses. @param {{urls: string[], big?: boolean}} p */
export function PhoneUrls({ urls, big }) {
  if (!urls?.length) return html`<p class="muted small">没有找到这台电脑的局域网地址。请确认电脑已连上 Wi-Fi 或网线。</p>`;
  return html`<ul class=${cx('phone-urls', big && 'is-big')}>${urls.map((u) => html`<li key=${u}>
    <code class="phone-url" title="在手机 App 的设置里填这个地址">${u}</code>
    <${Button} size="sm" variant="ghost" icon="copy" onClick=${() => copyText(u)} aria-label=${'复制 ' + u}>复制<//>
  </li>`)}</ul>`;
}

/**
 * Restart the service and watch the live link reconnect.
 * @param {() => void} [onBack] @returns {{phase: 'idle'|'restarting'|'back'|'slow', start: () => Promise<void>}}
 */
export function useRestart(onBack) {
  const conn = useStore((s) => s.conn);
  const [phase, setPhase] = useState(/** @type {'idle'|'restarting'|'back'|'slow'} */ ('idle'));
  const dropped = useRef(false);
  const timers = useRef(/** @type {any[]} */ ([]));
  const back = useRef(onBack);
  back.current = onBack;
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  useEffect(() => {
    if (phase !== 'restarting' && phase !== 'slow') return;
    if (conn !== 'online') dropped.current = true;
    else if (dropped.current) { setPhase('back'); back.current?.(); toast({ kind: 'ok', title: '服务已重启，重新连上了' }); }
  }, [conn, phase]);
  const start = async () => {
    await act('system.restart', {});
    dropped.current = false;
    setPhase('restarting');
    timers.current.push(setTimeout(() => { if (!link.online) link.retryNow(); }, 3000));
    // the socket may survive a very fast restart: treat "still online after 6 s" as back
    timers.current.push(setTimeout(() => { if (!dropped.current && link.online) { setPhase('back'); back.current?.(); } }, 6000));
    timers.current.push(setTimeout(() => setPhase((p) => (p === 'restarting' ? 'slow' : p)), 30000));
  };
  return { phase, start };
}

/** @param {{phase: string}} p */
export function RestartState({ phase }) {
  if (phase === 'restarting') return html`<p class="restart-state" role="status"><span class="spinner"></span>正在重启服务… 大约需要几秒，控制台会自动重新连接。</p>`;
  if (phase === 'slow') return html`<p class="restart-state tone-text-warn" role="status"><${Icon} name="alert" size=${14} />服务还没有回来。请确认 Whale 仍在运行（如果是手动启动的，可能需要重新运行 <code>whale serve</code>）。</p>`;
  if (phase === 'back') return html`<p class="restart-state tone-text-ok" role="status"><${Icon} name="check" size=${14} />已重启并重新连上。</p>`;
  return null;
}

/**
 * LAN switch + addresses + restart.
 * @param {{confirmRestart?: boolean, showOff?: boolean, onNetwork?: (n: any) => void, intro?: any, whenLive?: (n: any) => any}} p
 *   confirmRestart: two-step restart button (settings); showOff: allow switching LAN off again.
 *   whenLive: extra content once phones can actually connect (LAN on and applied).
 */
export function NetworkPanel({ confirmRestart = false, showOff = true, onNetwork, intro, whenLive }) {
  const res = useRpc('system.network');
  const [local, setNet] = useState(/** @type {any} */ (null));
  useEffect(() => { if (res.data) { setNet(res.data); onNetwork?.(res.data); } }, [res.data]);
  const net = local || res.data;
  const reload = async () => { const n = await res.reload(true); if (n) { setNet(n); onNetwork?.(n); } };
  const restart = useRestart(reload);
  if (res.error && !net) return html`<${ErrorBox} error=${res.error} onRetry=${() => res.reload()} />`;
  if (!net) return html`<${Skeleton} rows=${2} />`;
  const setLan = async (enabled) => {
    const n = await act('system.set_lan', { enabled });
    setNet(n); onNetwork?.(n);
    if (n.restart_required) toast({ kind: 'info', title: enabled ? '已允许局域网连接' : '已关闭局域网连接', body: '重启服务后生效' });
  };
  const live = net.lan_enabled && !net.restart_required;
  const busy = restart.phase === 'restarting';
  return html`<div class="net-panel">
    ${intro}
    <div class="switch-row">
      <div><strong>允许局域网里的手机连接</strong>
        <p class="muted small">${net.lan_enabled
          ? '同一个 Wi-Fi 下的手机可以连上鲸鱼；每台手机仍需配对码或访问令牌。'
          : `现在只有这台电脑能访问（${net.host}:${net.port}）。开启后，同一个 Wi-Fi 下的手机就能连上。只在你信任的网络里开启。`}</p></div>
      <${Switch} checked=${!!net.lan_enabled} label="允许局域网里的手机连接" disabled=${busy || (net.lan_enabled && !showOff)} onChange=${setLan} />
    </div>
    ${net.restart_required ? html`<div class="banner banner-info small"><${Icon} name="refresh" size=${16} />
      <div><strong>需要重启服务才能生效</strong><p>重启只要几秒，正在进行的对话不会丢失。</p></div>
      ${confirmRestart
        ? html`<${ConfirmButton} size="sm" variant="primary" icon="power" confirmText="确定重启？" disabled=${busy} onConfirm=${restart.start}>立即重启服务<//>`
        : html`<${AsyncButton} size="sm" variant="primary" icon="power" disabled=${busy} onClick=${restart.start}>立即重启服务<//>`}
    </div>` : null}
    <${RestartState} phase=${restart.phase} />
    ${live ? html`<div class="net-urls"><p class="small"><${Icon} name="phone" size=${14} /> 在手机 App 里填这个地址：</p><${PhoneUrls} urls=${net.urls} big /></div>` : null}
    ${live && whenLive ? whenLive(net) : null}
  </div>`;
}

/** Compact hint for the pairing card. @param {{net: any}} p */
export function PhoneAddressHint({ net }) {
  if (!net) return null;
  if (!net.lan_enabled || net.restart_required) {
    return html`<p class="banner banner-warn small"><${Icon} name="wifi" size=${16} />
      <span>${net.lan_enabled ? '局域网连接已开启，重启服务后手机才能连上。' : '服务目前只允许本机访问，手机连不上。'}
        去 <a href="/settings/service" onClick=${linkClick}>设置 → 服务</a> ${net.lan_enabled ? '重启服务' : '打开「允许局域网里的手机连接」'}。</span></p>`;
  }
  return html`<div class="pair-addr"><span class="muted small">手机 App 里填的地址</span><${PhoneUrls} urls=${net.urls} /></div>`;
}
