// @ts-check
/**
 * Bootstrap: token from URL → theme → live connection → shell + router.
 * Routes: /  /chat[/:id]  /memory[/:id]  /groups[/:id]  /tasks[/:id]  /capabilities  /devices[/:id]  /persona  /settings[/:section]  /setup
 */
import { html, render, useEffect, useErrorBoundary } from '/vendor/preact-htm.js';
import { consumeUrlToken, getToken } from './lib/api.js';
import { store, useStore, startLive, initTheme } from './lib/store.js';
import { useRoute } from './lib/hooks.js';
import { Nav, Topbar, Palette, PAGES } from './components/shell.js';
import { ApprovalTray, Toasts } from './components/live.js';
import { Button, Empty } from './components/ui.js';
import { Login } from './pages/login.js';
import { BridgePage } from './pages/bridge.js';
import { ChatPage } from './pages/chat.js';
import { MemoryPage } from './pages/memory.js';
import { TasksPage } from './pages/tasks.js';
import { CapabilitiesPage } from './pages/capabilities.js';
import { DevicesPage } from './pages/devices.js';
import { PersonaPage } from './pages/persona.js';
import { SettingsPage } from './pages/settings.js';
import { GroupsPage } from './pages/groups.js';
import { SetupWizard } from './pages/setup.js';

/** @type {Record<string, any>} */
export const ROUTES = {
  bridge: BridgePage, chat: ChatPage, memory: MemoryPage, groups: GroupsPage, tasks: TasksPage, capabilities: CapabilitiesPage,
  devices: DevicesPage, persona: PersonaPage, settings: SettingsPage,
};

/** Keeps one broken page from taking down the shell. @param {{children: any, page: string}} p */
function PageBoundary({ children, page }) {
  const [error, reset] = useErrorBoundary((e) => console.error('page crashed', page, e));
  if (error) {
    return html`<div class="page"><${Empty} icon="alert" title="这个页面出错了" hint=${String(error?.message || error)}>
      <${Button} variant="primary" icon="refresh" onClick=${reset}>重新加载页面<//>
    <//></div>`;
  }
  return children;
}

function NotFound() {
  return html`<div class="page"><${Empty} icon="compass" title="迷路了" hint="这片海域没有页面。"><${Button} variant="primary" href="/" icon="home">回到总览<//><//></div>`;
}

export function Shell() {
  const route = useRoute();
  const Page = ROUTES[route.page] || NotFound;
  const meta = PAGES.find((p) => p.id === route.page);
  const name = useStore((s) => s.status?.agent?.persona?.name || s.status?.agent?.name || '鲸鱼');
  // first-run wizard: /setup, or automatically while setup is incomplete (until 「稍后再说」)
  const wizard = useStore((s) => route.page === 'setup' || (!!s.setup && !s.setup.completed && !s.setup.unsupported && !s.setupLater));
  useEffect(() => {
    if (globalThis.document) document.title = `${wizard ? '首次设置 · ' : meta ? meta.label + ' · ' : ''}${name} · Whale`;
  }, [meta, name, wizard]);
  if (wizard) {
    return html`<div class="shell shell-wizard">
      <${PageBoundary} key="setup" page="setup"><${SetupWizard} /><//>
      <${Toasts} />
    </div>`;
  }
  return html`<div class="shell">
    <a class="skip-link" href="#content">跳到主要内容</a>
    <${Nav} page=${route.page} />
    <div class="main">
      <${Topbar} title=${meta?.label || ''} />
      <main id="content" class=${'content page-' + route.page} tabindex="-1">
        <${PageBoundary} key=${route.page} page=${route.page}><${Page} route=${route} /><//>
      </main>
    </div>
    <${ApprovalTray} />
    <${Palette} />
    <${Toasts} />
  </div>`;
}

export function App() {
  const token = useStore((s) => s.token);
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (store.get().token) store.set((s) => ({ paletteOpen: !s.paletteOpen }));
      } else if (e.key === 'Escape' && store.get().trayOpen) store.set({ trayOpen: false });
    };
    globalThis.addEventListener?.('keydown', onKey);
    return () => globalThis.removeEventListener?.('keydown', onKey);
  }, []);
  return token ? html`<${Shell} />` : html`<${Toasts} /><${Login} />`;
}

export function boot() {
  consumeUrlToken();
  initTheme();
  store.set({ token: getToken() });
  const root = typeof globalThis.document?.getElementById === 'function' ? document.getElementById('app') : null;
  if (!root) return;
  root.classList.remove('boot');
  root.textContent = '';
  render(html`<${App} />`, root);
  if (store.get().token) startLive();
}

if (typeof globalThis.document?.getElementById === 'function' && document.getElementById('app')) boot();
