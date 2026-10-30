import { useCallback, useEffect } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import Icon, { WhaleMark } from "./Icon";
export default function Overview({
  navigate,
}: {
  navigate: (
    tab: "chat" | "plugins" | "settings" | "tasks" | "approvals",
  ) => void;
}) {
  const status = useApi(useCallback(() => api.status(), []));
  const tasks = useApi(useCallback(() => api.tasks(), []));
  const plugins = useApi(useCallback(() => api.plugins(), []));
  useEffect(() => {
    const t = setInterval(() => status.reload(), 15000);
    return () => clearInterval(t);
  }, [status.reload]);
  const s = status.data;
  return (
    <div>
      <div className="page-head">
        <div>
          <h2>
            你好，欢迎回来<span className="title-dot">。</span>
          </h2>
          <p className="muted">鲸鱼的近况，与你的下一件事。</p>
        </div>
        <button
          className="icon-button"
          title="刷新总览"
          onClick={() => {
            status.reload();
            tasks.reload();
            plugins.reload();
          }}
        >
          <Icon name="refresh" />
        </button>
      </div>
      {status.error && (
        <div className="banner error" role="alert">
          {status.error}
        </div>
      )}
      <div className="hero">
        <div>
          <span className="eyebrow">A LITTLE CLOSER, EVERY DAY</span>
          <h3>
            记得你说过的话。
            <br />
            也期待你的新想法。
          </h3>
          <p>
            {s
              ? `当前 ${s.plugins.enabled} 项能力已启用，${s.memory.episodes + s.memory.preferences} 段记忆正在延续。`
              : "正在读取鲸鱼的状态…"}
          </p>
          <button className="primary" onClick={() => navigate("chat")}>
            开始一段对话
            <Icon name="arrow" size={17} />
          </button>
        </div>
        <div className="hero-graphic">
          <div className="halo" />
          <WhaleMark size={164} />
          <span className="floating-tag">
            <span className="status-dot" />{" "}
            {s?.profile === "cloud" ? "云端核心" : "本地核心"}
          </span>
        </div>
      </div>
      <div className="cards stats-grid">
        {[
          {
            label: "已启用能力",
            value: s ? `${s.plugins.enabled}` : "—",
            detail: s ? `共 ${s.plugins.loaded} 个插件` : "读取中",
            icon: "plugins",
            go: "plugins" as const,
          },
          {
            label: "连续记忆",
            value: s ? `${s.memory.episodes + s.memory.preferences}` : "—",
            detail: s
              ? `${s.memory.preferences} 项偏好 · ${s.memory.episodes} 段情节`
              : "读取中",
            icon: "memory",
            go: "chat" as const,
          },
          {
            label: "等待你决定",
            value: s ? `${s.drafts_pending}` : "—",
            detail: "查看执行前的授权请求",
            icon: "approvals",
            go: "approvals" as const,
          },
          {
            label: "运行时长",
            value: s ? `${Math.floor(s.uptime_s / 60)} min` : "—",
            detail: s ? `v${s.version} · ${s.profile}` : "读取中",
            icon: "events",
            go: "settings" as const,
          },
        ].map((c) => (
          <button
            key={c.label}
            className="card stat-card"
            onClick={() => navigate(c.go)}
          >
            <div className="card-title">
              {c.label}
              <Icon name={c.icon} />
            </div>
            <div className="card-value">{c.value}</div>
            <div className="muted small">{c.detail}</div>
          </button>
        ))}
      </div>
      <div className="overview-columns">
        <section className="panel">
          <div className="panel-head">
            <h3>最近的任务</h3>
            <button className="text-button" onClick={() => navigate("tasks")}>
              查看全部 ↗
            </button>
          </div>
          {tasks.error && <p className="banner error">{tasks.error}</p>}
          {tasks.loading ? (
            <div className="empty">正在读取…</div>
          ) : !tasks.data?.length ? (
            <div className="illustrated-empty">
              <Icon name="tasks" size={30} />
              <strong>故事从第一件事开始</strong>
              <p>搜索、创作或一句话，都会在这里留下进展。</p>
              <button onClick={() => navigate("chat")}>和鲸鱼说说</button>
            </div>
          ) : (
            tasks.data.slice(0, 4).map((t) => (
              <button
                key={t.task_id}
                className="activity-row"
                onClick={() => navigate("tasks")}
              >
                <span
                  className={`activity-dot ${["answered", "succeeded"].includes(t.status) ? "done" : ""}`}
                />
                <span>
                  <strong>{t.goal || "对话任务"}</strong>
                  <small>{new Date(t.at * 1000).toLocaleString("zh-CN")}</small>
                </span>
                <span className="badge">{t.status}</span>
              </button>
            ))
          )}
        </section>
        <section className="panel">
          <div className="panel-head">
            <h3>能力一览</h3>
            <button className="text-button" onClick={() => navigate("plugins")}>
              管理 ↗
            </button>
          </div>
          {plugins.data?.slice(0, 6).map((p) => (
            <div className="capability-row" key={p.plugin_id}>
              <div className={`capability-icon ${p.enabled ? "enabled" : ""}`}>
                <Icon
                  name={p.kind === "model" ? "chat" : "plugins"}
                  size={18}
                />
              </div>
              <div>
                <strong>{p.name}</strong>
                <small>{p.enabled ? "已启用" : "未启用"}</small>
              </div>
              <span className={`status-dot ${p.enabled ? "" : "off"}`} />
            </div>
          ))}
          {plugins.error && <div className="banner error">{plugins.error}</div>}
        </section>
      </div>
      <div className="setup-hint">
        <Icon name="settings" />
        <div>
          <strong>把鲸鱼调成你喜欢的样子</strong>
          <p>配置模型、切换人格，或检查服务连接。</p>
        </div>
        <button onClick={() => navigate("settings")}>
          打开设置
          <Icon name="arrow" size={16} />
        </button>
      </div>
    </div>
  );
}
