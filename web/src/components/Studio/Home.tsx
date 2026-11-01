/**
 * Studio 首页（issue #44）：新用户先看到「今天发生了什么」。
 *
 * 四个区块，各自独立的 加载/空/错误 状态（互不影响）：
 * - 近期活动：/activity 的自然语言行；
 * - 需要处理：tasks 中 待审批/失败 的任务；
 * - 连接状态：health + status；
 * - 快捷入口：新建人物卡 / 查看记忆 / 主动行为 / 查看任务。
 */

import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ActivityItem,
  type StatusResult,
  type TaskRecord,
} from "../../api";

const DECISION_DOT: Record<string, string> = {
  replied: "replied",
  silent: "silent",
  failed: "failed",
  partial: "partial",
};

function fmtClock(epochS: number): string {
  const d = new Date(epochS * 1000);
  return Number.isNaN(d.getTime())
    ? String(epochS)
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtUptime(s: number): string {
  const m = Math.floor(s / 60);
  if (m < 1) return "刚启动";
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}

/** 任务需要处理的标准：phase（无则退回 status）为 待审批/失败。 */
function taskPhase(t: TaskRecord): string {
  const phase = (t as { phase?: unknown }).phase;
  return typeof phase === "string" && phase ? phase : t.status;
}

export default function StudioHome({
  onNavigate,
  onNewPersona,
}: {
  onNavigate?: (tab: string) => void;
  onNewPersona: () => void;
}) {
  const [activity, setActivity] = useState<ActivityItem[] | null>(null);
  const [activityErr, setActivityErr] = useState("");
  const [tasks, setTasks] = useState<TaskRecord[] | null>(null);
  const [tasksErr, setTasksErr] = useState("");
  const [conn, setConn] = useState<{
    ok: boolean;
    status: StatusResult | null;
  } | null>(null);
  const [connErr, setConnErr] = useState("");

  const load = useCallback(async () => {
    setActivityErr("");
    setTasksErr("");
    setConnErr("");
    void (async () => {
      try {
        const r = await api.activityList(5, 0);
        setActivity(r.items);
      } catch (e) {
        setActivityErr(e instanceof Error ? e.message : String(e));
      }
    })();
    void (async () => {
      try {
        setTasks(await api.tasks());
      } catch (e) {
        setTasksErr(e instanceof Error ? e.message : String(e));
      }
    })();
    void (async () => {
      try {
        const h = await api.health();
        let status: StatusResult | null = null;
        try {
          status = await api.status();
        } catch {
          /* status 缺失不阻断健康展示 */
        }
        setConn({ ok: h.ok, status });
      } catch (e) {
        setConnErr(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const pending = (tasks ?? []).filter((t) =>
    ["pending_approval", "failed"].includes(taskPhase(t)),
  );

  return (
    <div className="studio-home">
      <section className="home-block" aria-label="近期活动">
        <h3>近期活动</h3>
        {activityErr && <div className="banner error">{activityErr}</div>}
        {activity === null && !activityErr && (
          <p className="muted">加载中…</p>
        )}
        {activity && activity.length === 0 && (
          <div className="empty">暂无活动记录。</div>
        )}
        {activity && activity.length > 0 && (
          <ul className="activity-list">
            {activity.map((it) => (
              <li key={it.trace_id} className="activity-item">
                <span
                  className={`activity-dot ${DECISION_DOT[it.status] ?? ""}`}
                  aria-label={it.status}
                />
                <span className="muted mono activity-time">
                  {fmtClock(it.at)}
                </span>
                <div className="activity-lines">
                  {it.lines.map((line, i) => (
                    <div key={i}>{line}</div>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="home-block" aria-label="需要处理">
        <h3>需要处理</h3>
        {tasksErr && <div className="banner error">{tasksErr}</div>}
        {tasks === null && !tasksErr && <p className="muted">加载中…</p>}
        {tasks !== null && pending.length === 0 && (
          <div className="empty">没有待处理的事项。</div>
        )}
        {pending.length > 0 && (
          <ul className="list">
            {pending.map((t) => (
              <li key={t.task_id} className="list-item">
                <div className="list-main">
                  <div className="record-tags">
                    <span
                      className={
                        taskPhase(t) === "failed" ? "badge bad" : "badge warn"
                      }
                    >
                      {taskPhase(t) === "failed" ? "失败" : "待审批"}
                    </span>
                    <span>{t.goal || "（无目标描述）"}</span>
                  </div>
                </div>
                <div className="actions">
                  <button
                    className="text-button"
                    onClick={() => onNavigate?.("tasks")}
                  >
                    去处理
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="home-block" aria-label="连接状态">
        <h3>连接状态</h3>
        {connErr && <div className="banner error">{connErr}</div>}
        {conn === null && !connErr && <p className="muted">加载中…</p>}
        {conn && (
          <div className="home-conn">
            <span className={conn.ok ? "badge ok" : "badge bad"}>
              {conn.ok ? "核心已连接" : "连接异常"}
            </span>
            {conn.status && (
              <span className="muted small">
                版本 {conn.status.version} · 运行 {fmtUptime(conn.status.uptime_s)}{" "}
                · 插件 {conn.status.plugins.enabled}/{conn.status.plugins.loaded}{" "}
                启用 · 记忆 {conn.status.memory.episodes + conn.status.memory.preferences} 条
              </span>
            )}
          </div>
        )}
      </section>

      <section className="home-block" aria-label="快捷入口">
        <h3>快捷入口</h3>
        <div className="home-shortcuts">
          <button onClick={onNewPersona}>新建人物卡</button>
          <button onClick={() => onNavigate?.("memory")}>查看记忆</button>
          <button onClick={() => onNavigate?.("initiative")}>主动行为</button>
          <button onClick={() => onNavigate?.("tasks")}>查看任务</button>
        </div>
      </section>
    </div>
  );
}
