import { useCallback } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}
export default function Overview({
  onNavigate,
}: {
  onNavigate: (
    tab: "chat" | "plugins" | "memory" | "tasks" | "approvals" | "settings",
  ) => void;
}) {
  const statusFn = useCallback(() => api.status(), []);
  const { data, loading, error, reload } = useApi(statusFn);
  const taskFn = useCallback(() => api.tasks(), []);
  const tasks = useApi(taskFn);
  return (
    <div className="overview">
      <section className="welcome-panel">
        <div>
          <span className="badge soft">
            WHALE CORE · {data?.version ?? "—"}
          </span>
          <h2>每一个想法，都有回声。</h2>
          <p>对话、记忆与行动，在同一个地方相连。</p>
          <button className="primary" onClick={() => onNavigate("chat")}>
            开始对话
            <Icon name="arrow" size={16} />
          </button>
        </div>
        <div className="welcome-visual" aria-hidden="true">
          <div className="welcome-orbit" />
          <Icon name="whale" size={110} />
          <span>山有木兮木有枝</span>
        </div>
      </section>
      <div className="section-head">
        <h2>核心状态</h2>
        <button
          className="text-button"
          disabled={loading}
          onClick={() => {
            reload();
            tasks.reload();
          }}
        >
          <Icon name="refresh" size={15} />
          {loading ? "刷新中" : "刷新状态"}
        </button>
      </div>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div className="cards">
        <div className="card stat-card">
          <div className="stat-icon">
            <Icon name="overview" />
          </div>
          <div className="card-title">运行时长</div>
          <div className="card-value">
            {data ? formatUptime(data.uptime_s) : "—"}
          </div>
          <div className="stat-caption">
            <span className="status-dot" />
            {data
              ? `${data.profile === "cloud" ? "云端" : "本地"}配置 · 正在运行`
              : "正在读取状态"}
          </div>
        </div>
        <button
          className="card stat-card"
          onClick={() => onNavigate("plugins")}
        >
          <div className="stat-icon lilac">
            <Icon name="plugins" />
          </div>
          <div className="card-title">已启用能力插件</div>
          <div className="card-value">
            {data?.plugins.enabled ?? "—"}
            <small> / {data?.plugins.loaded ?? "—"}</small>
          </div>
          <div className="stat-caption">连接模型、工具与渠道</div>
        </button>
        <button className="card stat-card" onClick={() => onNavigate("memory")}>
          <div className="stat-icon blue">
            <Icon name="memory" />
          </div>
          <div className="card-title">记忆空间</div>
          <div className="card-value">
            {data ? data.memory.episodes + data.memory.preferences : "—"}
            <small> 条</small>
          </div>
          <div className="stat-caption">
            {data
              ? `${data.memory.episodes} 段经历 · ${data.memory.preferences} 项偏好`
              : "经历与偏好"}
          </div>
        </button>
        <button
          className="card stat-card"
          onClick={() => onNavigate("approvals")}
        >
          <div className="stat-icon amber">
            <Icon name="approvals" />
          </div>
          <div className="card-title">等待你确认</div>
          <div className="card-value">
            {data?.drafts_pending ?? "—"}
            <small> 项</small>
          </div>
          <div className="stat-caption">重要的行动，由你决定</div>
        </button>
      </div>
      <div className="overview-grid">
        <section className="surface">
          <div className="section-head">
            <h2>最近任务</h2>
            <button className="text-button" onClick={() => onNavigate("tasks")}>
              查看全部
              <Icon name="arrow" size={15} />
            </button>
          </div>
          {tasks.error && <div className="banner error">{tasks.error}</div>}
          {tasks.loading && <div className="empty">正在读取任务…</div>}
          {tasks.data?.length === 0 && (
            <div className="empty illustrated">
              <Icon name="tasks" size={32} />
              <strong>你的下一件事，从这里开始</strong>
              <span>在对话中提出目标，进度会出现在这里。</span>
            </div>
          )}
          {tasks.data?.slice(0, 4).map((task) => (
            <button
              className="recent-task"
              key={task.task_id}
              onClick={() => onNavigate("tasks")}
            >
              <span className="task-bullet" />
              <div>
                <strong>{task.goal || "未命名任务"}</strong>
                <span>
                  {new Date(task.at * 1000).toLocaleString("zh-CN")} ·{" "}
                  {task.receipts.length} 条回执
                </span>
              </div>
              <span
                className={
                  task.status === "failed"
                    ? "badge bad"
                    : task.status === "answered"
                      ? "badge ok"
                      : "badge"
                }
              >
                {(
                  {
                    answered: "已完成",
                    failed: "失败",
                    pending_approval: "待审批",
                    clarify: "待补充",
                    denied: "已拒绝",
                  } as Record<string, string>
                )[task.status] ?? task.status}
              </span>
            </button>
          ))}
        </section>
        <section className="surface getting-started">
          <p className="eyebrow">MAKE IT YOURS</p>
          <h2>让鲸鱼更懂你</h2>
          <p>
            接入你常用的模型和能力，
            <br />
            再从一次简单的对话开始。
          </p>
          <button className="shortcut" onClick={() => onNavigate("settings")}>
            <Icon name="plugins" />
            <span>
              配置模型与服务<small>设置端点、凭据与扩展能力</small>
            </span>
            <Icon name="arrow" size={16} />
          </button>
          <button className="shortcut" onClick={() => onNavigate("memory")}>
            <Icon name="memory" />
            <span>
              整理你的记忆<small>搜索、查看或导出已保存内容</small>
            </span>
            <Icon name="arrow" size={16} />
          </button>
        </section>
      </div>
    </div>
  );
}
