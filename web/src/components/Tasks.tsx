import { useCallback, useEffect, useState } from "react";
import { api, Job, TaskRecord } from "../api";
import { useApi } from "../hooks";
import { JobResult, TERMINAL } from "./Media";
const LABELS: Record<string, string> = {
  answered: "已回答",
  succeeded: "已完成",
  failed: "执行失败",
  queued: "排队中",
  running: "执行中",
  pending_approval: "等待审批",
  cancelled: "已取消",
  cancelling: "正在停止",
  unknown: "结果待核对",
  denied: "已拒绝",
  clarify: "等待澄清",
};
export default function Tasks() {
  const tasks = useApi(useCallback(() => api.tasks(), []));
  const [expanded, setExpanded] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    const timer = setInterval(tasks.reload, 3000);
    return () => clearInterval(timer);
  }, [tasks.reload]);
  const refresh = async (id: string) => {
    try {
      await api.refreshJob(id);
      tasks.reload();
    } catch (e) {
      setError(String(e));
    }
  };
  const cancel = async (id: string) => {
    try {
      await api.cancelJob(id);
      tasks.reload();
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <div>
      <div className="page-head">
        <div>
          <h2>任务记录</h2>
          <p className="muted">看清每一个目标，核对每一步执行。</p>
        </div>
        <button onClick={tasks.reload}>刷新</button>
      </div>
      {(error || tasks.error) && (
        <div className="banner error">{error || tasks.error}</div>
      )}
      {tasks.loading && !tasks.data && <div className="empty">读取任务…</div>}
      {tasks.data?.length === 0 && (
        <div className="illustrated-empty">
          <strong>这里还没有任务</strong>
          <p>去对话或声音与创作页，发起第一件事。</p>
        </div>
      )}
      <ul className="list">
        {tasks.data?.map((t) => (
          <Task
            key={t.task_id}
            task={t}
            expanded={expanded === t.task_id}
            toggle={() => setExpanded(expanded === t.task_id ? "" : t.task_id)}
            refresh={() => void refresh(t.task_id)}
            cancel={() => void cancel(t.task_id)}
          />
        ))}
      </ul>
    </div>
  );
}
function Task({
  task: t,
  expanded,
  toggle,
  cancel,
  refresh,
}: {
  task: TaskRecord;
  expanded: boolean;
  toggle: () => void;
  cancel: () => void;
  refresh: () => void;
}) {
  const job = t.task_id.startsWith("job-");
  const active = job && !TERMINAL.includes(t.status);
  return (
    <li className="list-item">
      <div className="list-main">
        <div className="actions">
          <span
            className={`badge ${["answered", "succeeded"].includes(t.status) ? "ok" : ["failed", "unknown", "denied"].includes(t.status) ? "bad" : "warn"}`}
          >
            {LABELS[t.status] || t.status}
          </span>
          <span className="muted small">
            {new Date(t.at * 1000).toLocaleString("zh-CN")}
          </span>
        </div>
        <strong>{t.goal || "任务"}</strong>
        <div className="muted small">{t.capabilities.join(" · ")}</div>
        {expanded && (
          <div className="detail-panel">
            {job ? (
              <JobResult job={t as Job} />
            ) : (
              <p className="mono">{t.receipts.join("\n") || "没有执行回执"}</p>
            )}
            <details>
              <summary>请求与执行轨迹</summary>
              <pre className="result-text">{JSON.stringify(t, null, 2)}</pre>
            </details>
          </div>
        )}
      </div>
      <div className="actions">
        {job && t.status === "unknown" && Boolean(t.external_task_id) && (
          <button onClick={refresh}>核对远程状态</button>
        )}
        {active && (
          <button className="danger" onClick={cancel}>
            取消
          </button>
        )}
        <button onClick={toggle}>{expanded ? "收起" : "查看详情"}</button>
      </div>
    </li>
  );
}
