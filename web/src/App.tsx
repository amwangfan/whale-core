import { useEffect, useState } from "react";
import { api, getToken, onUnauthorized, setToken } from "./api";
import Icon, { WhaleMark } from "./components/Icon";
import Overview from "./components/Overview";
import Plugins from "./components/Plugins";
import MemoryView from "./components/Memory";
import Approvals from "./components/Approvals";
import Chat from "./components/Chat";
import Tasks from "./components/Tasks";
import Events from "./components/Events";
import Settings from "./components/Settings";
import Media from "./components/Media";
import Life from "./components/Life";

type TabKey =
  | "overview"
  | "chat"
  | "plugins"
  | "memory"
  | "approvals"
  | "tasks"
  | "events"
  | "settings"
  | "media"
  | "life";
const NAV: { key: TabKey; label: string; en: string; group: string }[] = [
  { key: "overview", label: "总览", en: "Overview", group: "工作空间" },
  { key: "chat", label: "与鲸鱼对话", en: "Conversation", group: "工作空间" },
  { key: "media", label: "声音与创作", en: "Studio", group: "能力与生活" },
  {
    key: "plugins",
    label: "插件与能力",
    en: "Capabilities",
    group: "能力与生活",
  },
  { key: "life", label: "主动生活", en: "Life", group: "能力与生活" },
  { key: "memory", label: "记忆", en: "Memory", group: "管理" },
  { key: "tasks", label: "任务记录", en: "Tasks", group: "管理" },
  { key: "approvals", label: "待办审批", en: "Approvals", group: "管理" },
  { key: "events", label: "运行事件", en: "Events", group: "管理" },
  { key: "settings", label: "设置与诊断", en: "Settings", group: "管理" },
];
export default function App() {
  const [tab, setTab] = useState<TabKey>("overview");
  const [input, setInput] = useState(getToken());
  const [connected, setConnected] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const connect = async () => {
    if (!input.trim()) return;
    setToken(input);
    setChecking(true);
    setError("");
    try {
      await api.health();
      setConnected(true);
    } catch (e) {
      setConnected(false);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  };
  useEffect(() => {
    if (getToken()) void connect();
  }, []);
  useEffect(
    () =>
      onUnauthorized(() => {
        setConnected(false);
        setError("连接凭据已失效，请重新输入。");
      }),
    [],
  );
  const selected = NAV.find((n) => n.key === tab)!;
  const page = {
    overview: <Overview navigate={setTab} />,
    chat: <Chat />,
    media: <Media />,
    plugins: <Plugins />,
    life: <Life />,
    memory: <MemoryView />,
    tasks: <Tasks />,
    approvals: <Approvals />,
    events: <Events />,
    settings: <Settings />,
  }[tab];
  if (!connected)
    return (
      <main className="login-screen">
        <div className="login-art">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="login-whale">
            <WhaleMark size={160} />
          </div>
          <span className="eyebrow">WHALE / YOUR EVERYDAY COMPANION</span>
          <h1>
            让想法有回应。
            <br />
            让陪伴有记忆。
          </h1>
          <p>
            对话、创造、发现。
            <br />
            在同一个空间，与你的鲸鱼保持连接。
          </p>
          <span className="login-foot">山有木兮木有枝</span>
        </div>
        <div className="login-form">
          <div className="brand-lockup">
            <WhaleMark />
            <div>
              <strong>whale</strong>
              <span>CORE CONSOLE</span>
            </div>
          </div>
          <span className="eyebrow">WELCOME BACK</span>
          <h2>连接你的鲸鱼</h2>
          <p className="muted">
            输入服务启动时生成的访问凭据，进入你的工作空间。
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void connect();
            }}
          >
            <label htmlFor="api-token">访问凭据</label>
            <input
              id="api-token"
              type="password"
              autoComplete="off"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="粘贴 API token"
            />
            <button className="primary" disabled={checking || !input.trim()}>
              {checking ? "正在连接…" : "进入工作空间"}
              <Icon name="arrow" size={18} />
            </button>
          </form>
          {error && (
            <div role="alert" className="banner error">
              {error}
            </div>
          )}
          <div className="login-help">
            <span className="status-dot" />
            连接后才会读取你的记忆与任务。
            <br />
            <code>whale api</code> 的首次启动输出中可找到凭据。
          </div>
        </div>
      </main>
    );
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand-lockup">
          <WhaleMark />
          <div>
            <strong>
              whale<span className="brand-dot">.</span>
            </strong>
            <span>CORE CONSOLE</span>
          </div>
        </div>
        <div className="workspace-label">
          个人空间 <span>PERSONAL</span>
        </div>
        <nav aria-label="主导航">
          {NAV.map((n, i) => (
            <div key={n.key}>
              {(i === 0 || NAV[i - 1].group !== n.group) && (
                <div className="nav-group">{n.group}</div>
              )}
              <button
                className={`nav-item ${tab === n.key ? "active" : ""}`}
                aria-current={tab === n.key ? "page" : undefined}
                onClick={() => setTab(n.key)}
              >
                <Icon name={n.key} />
                {n.label}
                {tab === n.key && <span className="nav-indicator" />}
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection">
            <span className="status-dot" />
            核心已连接<span className="mono">API v1</span>
          </div>
          <button
            className="logout"
            onClick={() => {
              setToken("");
              setInput("");
              setConnected(false);
            }}
          >
            断开连接
          </button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            工作空间 <span>/</span>
            <strong>{selected.label}</strong>
          </div>
          <div className="topbar-right">
            <span className="desktop-only">
              {new Date().toLocaleDateString("zh-CN", {
                month: "long",
                day: "numeric",
                weekday: "long",
              })}
            </span>
            <div className="avatar">我</div>
          </div>
        </header>
        <main className="content">
          <div className="page-kicker">
            {selected.en.toUpperCase()}
            <span>WHALE CORE</span>
          </div>
          <div key={tab} className="page-enter">
            {page}
          </div>
          <footer className="page-footer">
            <span>Whale · 每一份记忆，都让下一次相见更熟悉。</span>
            <span>你的核心，你来掌控。</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
