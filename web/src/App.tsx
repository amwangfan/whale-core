import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken, isUnauthorized, onUnauthorized, setToken, type RuntimeSettings } from "./api";
import Overview from "./components/Overview";
import Plugins from "./components/Plugins";
import MemoryView from "./components/Memory";
import Approvals from "./components/Approvals";
import Chat, { type Msg } from "./components/Chat";
import Tasks from "./components/Tasks";
import Events from "./components/Events";
import Icon from "./components/Icon";
import Settings from "./components/Settings";
import FirstRunSetup from "./components/FirstRunSetup";
import Media from "./components/Media";
import Workflows from "./components/Workflows";
import Studio from "./components/Studio";

type TabKey =
  | "overview"
  | "studio"
  | "plugins"
  | "memory"
  | "approvals"
  | "chat"
  | "tasks"
  | "events"
  | "settings"
  | "media"
  | "workflows";
const TABS: { key: TabKey; label: string; description: string }[] = [
  {
    key: "overview",
    label: "运行总览",
    description: "一眼看见鲸鱼的运行状态。",
  },
  {
    key: "chat",
    label: "与鲸鱼对话",
    description: "想法、日常与任务，都从这里开始。",
  },
  {
    key: "studio",
    label: "工作台",
    description: "像卡牌游戏一样配置会话的人格、记忆与能力。",
  },
  {
    key: "plugins",
    label: "插件与能力",
    description: "管理鲸鱼连接世界的方式。",
  },
  {
    key: "memory",
    label: "记忆空间",
    description: "查看、搜索和管理被记住的事情。",
  },
  {
    key: "tasks",
    label: "任务中心",
    description: "追踪每个目标，以及真正发生的动作。",
  },
  {
    key: "approvals",
    label: "待办审批",
    description: "在行动之前，确认每一个重要决定。",
  },
  {
    key: "events",
    label: "运行日志",
    description: "实时了解核心与插件的动态。",
  },
  {
    key: "media",
    label: "媒体与语音",
    description: "把灵感化为画面，让文字拥有声音。",
  },
  {
    key: "workflows",
    label: "探索与工作流",
    description: "连接能力，发现新事物，完成更大的目标。",
  },
  {
    key: "settings",
    label: "设置与诊断",
    description: "连接你的模型，调整鲸鱼的运行方式。",
  },
];

export default function App() {
  const [tab, setTab] = useState<TabKey>("overview");
  const [tokenInput, setTokenInput] = useState("");
  const [authed, setAuthed] = useState(false);
  const [connecting, setConnecting] = useState(Boolean(getToken()));
  const [error, setError] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [sending, setSending] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [session, setSession] = useState(0);
  const [setupSettings, setSetupSettings] = useState<RuntimeSettings | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const chatSession = useRef(new AbortController());

  const connect = useCallback(async (token: string) => {
    if (!token.trim()) {
      setError("请输入启动服务时生成的访问令牌。");
      return;
    }
    setConnecting(true);
    setError("");
    setToken(token);
    try {
      await api.health();
      try {
        const history = await api.conversation("console");
        setMsgs((current) =>
          current.length
            ? current
            : history.messages.map((message) => ({
                role: message.role,
                text: message.content,
              })),
        );
      } catch (historyError) {
        if (isUnauthorized(historyError))
          throw historyError; /* History errors must not prevent diagnostics. */
      }
      // Server-side completion is shared across browsers; never store keys here.
      try {
        const settings = await api.settings();
        setSetupSettings(settings);
        setShowSetup(settings.setup_completed === false);
      } catch (settingsError) {
        if (isUnauthorized(settingsError)) throw settingsError;
        setShowSetup(false); // Existing installations can still open Settings.
      }
      chatSession.current.abort();
      chatSession.current = new AbortController();
      setAuthed(true);
      setTokenInput("");
      setSession((n) => n + 1);
    } catch (e) {
      setAuthed(false);
      setError(
        isUnauthorized(e)
          ? "访问令牌无效或已过期，请重新连接。"
          : e instanceof Error
            ? e.message
            : "暂时无法连接，请稍后重试。",
      );
    } finally {
      setConnecting(false);
    }
  }, []);

  useEffect(() => {
    const token = getToken();
    if (token) void connect(token);
    return onUnauthorized(() => {
      chatSession.current.abort();
      setToken("");
      setAuthed(false);
      setError("访问令牌无效或已过期，请重新连接。");
    });
  }, [connect]);

  const reopenSetup = async () => {
    try {
      const latest = await api.settings();
      setSetupSettings(latest);
      setShowSetup(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取设置失败。");
    }
  };

  const disconnect = () => {
    chatSession.current.abort();
    setToken("");
    setAuthed(false);
    setShowSetup(false);
    setSetupSettings(null);
    setTokenInput("");
    setError("");
    setMsgs([]);
    setSending(false);
  };
  const active = TABS.find((t) => t.key === tab)!;

  if (!authed)
    return (
      <div className="connect-screen">
        <div className="connect-art" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="whale-mark">
            <Icon name="whale" size={110} />
          </div>
          <span className="art-caption">A LITTLE CLOSER, EVERY DAY.</span>
        </div>
        <main className="connect-card">
          <div className="wordmark">
            <span className="logo">
              <Icon name="whale" size={26} />
            </span>{" "}
            WHALE <span className="wordmark-small">CORE</span>
          </div>
          <p className="eyebrow">你的鲸鱼，在这里。</p>
          <h1>
            让想法，
            <br />
            成为生活的一部分。
          </h1>
          <p className="connect-intro">
            连接核心，和鲸鱼聊聊近况，整理记忆，
            <br className="desktop-only" />
            也让它帮你完成下一件事。
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void connect(tokenInput);
            }}
          >
            <label htmlFor="access-token">访问令牌</label>
            <div className="password-field">
              <input
                id="access-token"
                type={showToken ? "text" : "password"}
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                placeholder="粘贴你的 API token"
                autoComplete="off"
                spellCheck={false}
              />
              <button
                type="button"
                onClick={() => setShowToken((s) => !s)}
                aria-label={showToken ? "隐藏令牌" : "显示令牌"}
              >
                {showToken ? "隐藏" : "显示"}
              </button>
            </div>
            <p className="field-hint">
              令牌来自 <code>whale api</code> 的启动输出；也可在本机的{" "}
              <code>~/.whale/api_token</code> 中找到。
            </p>
            {error && (
              <div className="banner error" role="alert">
                {error}
              </div>
            )}
            <button
              className="primary connect-button"
              disabled={connecting || !tokenInput.trim()}
              type="submit"
            >
              {connecting ? "正在验证连接…" : "连接鲸鱼"}
              <Icon name="arrow" size={18} />
            </button>
            <p className="muted small">
              令牌仅用于连接当前服务，保存在此浏览器中。
            </p>
            {getToken() && !connecting && (
              <button
                className="text-button"
                type="button"
                onClick={() => void connect(getToken())}
              >
                使用已保存的令牌重试
              </button>
            )}
          </form>
        </main>
        <div className="connect-footer">
          WHALE CONSOLE <span>与你，一起发现。</span>
        </div>
      </div>
    );

  if (showSetup && setupSettings)
    return <FirstRunSetup initial={setupSettings}
      onComplete={() => { setShowSetup(false); setSession((n) => n + 1); }} />;

  return (
    <div className="app">
      <aside className="sidebar">
        <a
          className="wordmark"
          href="#overview"
          onClick={() => setTab("overview")}
        >
          <span className="logo">
            <Icon name="whale" size={25} />
          </span>{" "}
          WHALE <span className="wordmark-small">CORE</span>
        </a>
        <div className="sidebar-caption">工作空间</div>
        <nav className="navigation" aria-label="主导航">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={tab === t.key ? "nav-item active" : "nav-item"}
              aria-current={tab === t.key ? "page" : undefined}
              onClick={() => setTab(t.key)}
            >
              <Icon name={t.key} />
              <span>{t.label}</span>
              {tab === t.key && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection">
            <span className="status-dot" />
            <div>
              核心已连接<span>当前设备 · 同源连接</span>
            </div>
          </div>
          <button className="disconnect" onClick={disconnect}>
            <Icon name="logout" size={17} />
            断开连接
          </button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            工作空间<span>/</span>
            <strong>{active.label}</strong>
          </div>
          <span className="topbar-status">
            <span className="status-dot" />
            WHALE IS HERE
          </span>
        </header>
        <main className="content" key={session}>
          <div className="workspace-heading">
            <div>
              <p className="eyebrow">WHALE WORKSPACE</p>
              <h1>{active.label}</h1>
              <p>{active.description}</p>
            </div>
            <span className="workspace-tag">CORE CONSOLE</span>
          </div>
          {tab === "overview" && <Overview onNavigate={setTab} />}
          {tab === "studio" && <Studio />}
          {tab === "plugins" && <Plugins />}
          {tab === "memory" && <MemoryView />}
          {tab === "approvals" && <Approvals />}
          {tab === "chat" && (
            <Chat
              msgs={msgs}
              setMsgs={setMsgs}
              sending={sending}
              setSending={setSending}
              signal={chatSession.current.signal}
            />
          )}
          {tab === "tasks" && <Tasks />}
          {tab === "events" && <Events />}
          {tab === "settings" && <Settings onDisconnect={disconnect} onRunSetup={() => void reopenSetup()} />}
          {tab === "media" && <Media />}
          {tab === "workflows" && <Workflows />}
        </main>
      </div>
    </div>
  );
}
