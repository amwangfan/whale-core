import { useEffect, useState } from 'react';
import { getToken, onUnauthorized, setToken } from './api';
import Overview from './components/Overview';
import Plugins from './components/Plugins';
import MemoryView from './components/Memory';
import Approvals from './components/Approvals';
import Chat from './components/Chat';
import Events from './components/Events';

type TabKey = 'overview' | 'plugins' | 'memory' | 'approvals' | 'chat' | 'events';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'overview', label: '总览' },
  { key: 'plugins', label: '插件' },
  { key: 'memory', label: '记忆' },
  { key: 'approvals', label: '审批' },
  { key: 'chat', label: '对话' },
  { key: 'events', label: '事件' },
];

export default function App() {
  const [tab, setTab] = useState<TabKey>('overview');
  const [tokenInput, setTokenInput] = useState(getToken());
  const [savedToken, setSavedToken] = useState(getToken());
  const [needsAuth, setNeedsAuth] = useState(false);

  useEffect(() => onUnauthorized(() => setNeedsAuth(true)), []);

  const saveToken = () => {
    setToken(tokenInput);
    setSavedToken(getToken());
    setNeedsAuth(!getToken());
  };

  const authed = Boolean(savedToken);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">🐋 Whale 控制台</div>
        <nav className="tabs">
          {TABS.map(t => (
            <button
              key={t.key}
              className={tab === t.key ? 'tab active' : 'tab'}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="token-box">
          <input
            type="password"
            placeholder="API token"
            value={tokenInput}
            onChange={e => setTokenInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') saveToken();
            }}
          />
          <button onClick={saveToken}>保存</button>
        </div>
      </header>

      {needsAuth && (
        <div className="banner warn">需要 token：请在右上角输入 API token 并保存（401 未授权）。</div>
      )}

      <main className="content" key={savedToken || 'anon'}>
        {!authed ? (
          <div className="empty">请先设置 API token 以连接 whale-core 管理 API。</div>
        ) : (
          <>
            <section hidden={tab !== 'overview'}>
              <Overview />
            </section>
            <section hidden={tab !== 'plugins'}>
              <Plugins />
            </section>
            <section hidden={tab !== 'memory'}>
              <MemoryView />
            </section>
            <section hidden={tab !== 'approvals'}>
              <Approvals />
            </section>
            <section hidden={tab !== 'chat'}>
              <Chat />
            </section>
            <section hidden={tab !== 'events'}>
              <Events />
            </section>
          </>
        )}
      </main>
    </div>
  );
}
