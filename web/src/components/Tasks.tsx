import { useCallback, useState } from "react";
import { api, TaskRecord, type TaskTimeline } from "../api";
import { useApi } from "../hooks";

/** status → 中文标签（徽标悬停可查看原始状态值）。 */
const STATUS_LABELS: Record<string, string> = {
  answered: "已回答",
  clarify: "需澄清",
  pending_approval: "待审批",
  denied: "已拒绝",
  failed: "失败",
  cancelled: "已取消",
  approved: "已批准",
  running: "执行中",
};

function statusBadgeClass(status: string): string {
  switch (status) {
    case "answered":
      return "badge ok";
    case "clarify":
      return "badge info";
    case "pending_approval":
      return "badge warn";
    case "denied":
    case "failed":
      return "badge bad";
    default:
      return "badge";
  }
}

function formatAt(at: number): string {
  // 后端 at 为 epoch 秒
  return new Date(at * 1000).toLocaleString();
}

function Capabilities({ capabilities }: { capabilities: string[] }) {
  if (capabilities.length === 0) return <span className="muted">无</span>;
  return (
    <>
      {capabilities.map((c) => (
        <span key={c} className="badge mono">
          {c}
        </span>
      ))}
    </>
  );
}

function ReceiptList({ receipts }: { receipts: string[] }) {
  if (receipts.length === 0) {
    return <div className="muted">无回执：该任务未实际执行任何动作。</div>;
  }
  return (
    <ul className="receipt-list">
      {receipts.map((r, i) => (
        <li key={i} className="mono">
          {r}
        </li>
      ))}
    </ul>
  );
}

type DetailState =
  | { kind: "loading" }
  | { kind: "ok"; task: TaskRecord }
  | { kind: "error"; message: string };

type TimelineState =
  | { kind: "loading" }
  | { kind: "ok"; tl: TaskTimeline }
  | { kind: "error"; message: string };

/** issue #43：阶段徽标配色（phase 语义真实：unknown 永不显示成成功）。 */
function phaseBadgeClass(phase: string): string {
  switch (phase) {
    case "succeeded":
    case "delivered":
      return "badge ok";
    case "failed":
    case "delivery_failed":
      return "badge bad";
    case "pending_approval":
      return "badge warn";
    case "running":
    case "acknowledged":
      return "badge info";
    default:
      return "badge";
  }
}

const DELIVERY_LABELS: Record<string, string> = {
  delivered: "已送达",
  failed: "送达失败",
  pending: "待送达",
  sending: "送达中",
};

/** issue #43：任务执行时间线面板（当前阶段 / 事件流水 / 回执 / 送达 / 重试建议）。 */
function TimelinePanel({
  tl,
  busy,
  onRetry,
  onNavigate,
}: {
  tl: TaskTimeline;
  busy: boolean;
  onRetry: (taskId: string) => void;
  onNavigate?: (tab: string) => void;
}) {
  return (
    <div className="task-timeline" aria-label="任务时间线">
      <div className="timeline-current">
        <span className={phaseBadgeClass(tl.phase)}>{tl.current.label}</span>
        <span className="muted">{tl.current.explanation}</span>
        <span className="muted small">下一步：{tl.current.next_step}</span>
      </div>

      <div className="section-label">执行过程</div>
      <ul className="timeline-list">
        {tl.timeline.map((e, i) => (
          <li key={i} className="timeline-event">
            <span className="muted mono">{e.at_text}</span>
            <span>{e.text}</span>
            {Object.keys(e.detail).length > 0 && (
              <span className="muted small mono">
                {JSON.stringify(e.detail)}
              </span>
            )}
          </li>
        ))}
      </ul>

      <div className="section-label">回执（实际执行）</div>
      <ReceiptList receipts={tl.receipts} />

      {(tl.delivery.state || tl.delivery.attempts > 0) && (
        <div className="timeline-delivery">
          <span className="section-label">送达</span>
          <span className="muted small">
            状态：{DELIVERY_LABELS[tl.delivery.state] ?? tl.delivery.state} ·
            尝试 {tl.delivery.attempts} 次
            {tl.delivery.channel ? ` · 渠道 ${tl.delivery.channel}` : ""}
          </span>
          {tl.delivery.last_error && (
            <div className="banner error">
              送达失败：{tl.delivery.last_error}
            </div>
          )}
        </div>
      )}

      <div className="timeline-retry">
        {tl.retry.allowed && tl.retry.kind === "resume" && (
          <button disabled={busy} onClick={() => onRetry(tl.task_id)}>
            {busy ? "重试中…" : "重试"}
          </button>
        )}
        {tl.retry.kind === "redeliver" && (
          <>
            {/* 后端尚无独立 redeliver 端点：只展示说明，不编动作。 */}
            <button disabled title={tl.retry.reason}>
              重发结果（不重跑工具）
            </button>
            <span className="muted small">{tl.retry.reason}</span>
          </>
        )}
        {tl.retry.kind === "inspect" && (
          <>
            <span className="muted">状态未知：先到事件页核实。</span>
            {onNavigate && (
              <button
                className="text-button"
                onClick={() => onNavigate("events")}
              >
                前往事件页
              </button>
            )}
          </>
        )}
        {tl.retry.kind !== "resume" &&
          tl.retry.kind !== "redeliver" &&
          tl.retry.kind !== "inspect" &&
          tl.retry.reason && (
            <span className="muted small">{tl.retry.reason}</span>
          )}
        {tl.retry.kind === "resume" && tl.retry.reason && (
          <span className="muted small">{tl.retry.reason}</span>
        )}
      </div>

      {tl.trace_ids.length > 0 && (
        <div className="timeline-traces">
          <span className="section-label">关联 Trace</span>
          {tl.trace_ids.map((id) => (
            <button
              key={id}
              className="text-button mono"
              title="在事件页查看该 trace"
              onClick={() => onNavigate?.("events")}
            >
              {id}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Tasks({
  onNavigate,
}: {
  onNavigate?: (tab: string) => void;
}) {
  const listFn = useCallback(() => api.tasks(), []);
  const { data: tasks, error, loading, reload } = useApi(listFn);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const action = async (
    id: string,
    kind: "cancel" | "resume" | "approve" | "reject",
  ) => {
    setBusy(id);
    setNotice("");
    setActionError("");
    try {
      if (kind === "cancel") {
        await api.cancelTask(id);
        setNotice("取消请求已记录。");
      } else if (kind === "resume") {
        const r = await api.resumeTask(id);
        setNotice(r.reply || `任务已恢复：${r.status}`);
      } else {
        await api.decideTask(id, kind);
        if (kind === "approve") {
          const r = await api.resumeTask(id);
          setNotice(r.reply || `任务已恢复：${r.status}`);
        } else setNotice("已拒绝任务。");
      }
      reload();
      setDetails({});
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };
  const [details, setDetails] = useState<Record<string, DetailState>>({});
  // issue #43：时间线展开状态（与「详情」相互独立，可同时展开）
  const [timelines, setTimelines] = useState<Record<string, TimelineState>>({});
  const [expandedTlId, setExpandedTlId] = useState<string | null>(null);

  const fetchTimeline = async (taskId: string) => {
    setTimelines((m) => ({ ...m, [taskId]: { kind: "loading" } }));
    try {
      const tl = await api.taskTimeline(taskId);
      setTimelines((m) => ({ ...m, [taskId]: { kind: "ok", tl } }));
    } catch (e) {
      setTimelines((m) => ({
        ...m,
        [taskId]: {
          kind: "error",
          message: e instanceof Error ? e.message : String(e),
        },
      }));
    }
  };

  const toggleTimeline = (taskId: string) => {
    if (expandedTlId === taskId) {
      setExpandedTlId(null);
      return;
    }
    setExpandedTlId(taskId);
    if (timelines[taskId]?.kind === "ok") return; // 已加载过，直接展开
    void fetchTimeline(taskId);
  };

  /** 时间线「重试」：走既有 resumeTask（幂等键保护），完成后刷新列表与时间线。 */
  const retryTask = async (taskId: string) => {
    setBusy(taskId);
    setNotice("");
    setActionError("");
    try {
      const r = await api.resumeTask(taskId);
      setNotice(r.reply || `任务已恢复：${r.status}`);
      reload();
      void fetchTimeline(taskId);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const toggle = async (taskId: string) => {
    if (expandedId === taskId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(taskId);
    if (details[taskId]?.kind === "ok") return; // 已加载过，直接展开
    setDetails((d) => ({ ...d, [taskId]: { kind: "loading" } }));
    try {
      const task = await api.task(taskId);
      setDetails((d) => ({ ...d, [taskId]: { kind: "ok", task } }));
    } catch (e) {
      setDetails((d) => ({
        ...d,
        [taskId]: {
          kind: "error",
          message: e instanceof Error ? e.message : String(e),
        },
      }));
    }
  };

  return (
    <div>
      <div className="page-head">
        <h2>任务</h2>
        <button onClick={reload}>刷新</button>
      </div>

      <div className="muted small page-note">
        「目标 /
        声明能力」是模型提出的计划，未必全部执行；「回执」才是实际执行（或被拒 /
        挂起）的记录。
      </div>

      {notice && (
        <div className="banner ok" role="status">
          {notice}
        </div>
      )}
      {actionError && (
        <div className="banner error" role="alert">
          {actionError}
        </div>
      )}
      {loading && <div className="muted">加载中…</div>}
      {error && <div className="banner error">{error}</div>}
      {tasks && tasks.length === 0 && (
        <div className="empty">
          暂无任务记录。通过「对话」页发起任务后，可在此核对模型提出的计划与实际执行的回执。
        </div>
      )}

      {tasks && tasks.length > 0 && (
        <ul className="list">
          {tasks.map((t) => {
            const expanded = expandedId === t.task_id;
            const detail = details[t.task_id];
            return (
              <li
                key={t.task_id}
                className="list-item clickable"
                onClick={() => void toggle(t.task_id)}
              >
                <div className="list-main">
                  <div>
                    <span
                      className={statusBadgeClass(t.status)}
                      title={t.status}
                    >
                      {STATUS_LABELS[t.status] ?? t.status}
                    </span>
                    <span className="muted mono">{formatAt(t.at)}</span>
                  </div>
                  <div>
                    {t.goal || <span className="muted">（无目标描述）</span>}
                  </div>
                  <div className="muted small">
                    声明能力（模型提出）：
                    <Capabilities capabilities={t.capabilities} />
                  </div>
                  <details
                    className="receipts"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <summary>
                      回执 {t.receipts.length} 条（实际执行，点击展开）
                    </summary>
                    <ReceiptList receipts={t.receipts} />
                  </details>

                  {expanded && (
                    <div
                      className="detail-panel"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {(!detail || detail.kind === "loading") && (
                        <div className="muted">加载详情…</div>
                      )}
                      {detail?.kind === "error" && (
                        <div className="banner error">
                          加载失败：{detail.message}
                        </div>
                      )}
                      {detail?.kind === "ok" && (
                        <>
                          <div className="mono muted">
                            task_id: {detail.task.task_id} · status:{" "}
                            {detail.task.status} · {formatAt(detail.task.at)}
                          </div>
                          <div>
                            <div className="section-label">
                              模型提出的计划（未必全部执行）
                            </div>
                            <div>目标：{detail.task.goal || "—"}</div>
                            <div>
                              声明能力：
                              <Capabilities
                                capabilities={detail.task.capabilities}
                              />
                            </div>
                          </div>
                          <div>
                            <div className="section-label">
                              实际执行的回执（真实发生）
                            </div>
                            <ReceiptList receipts={detail.task.receipts} />
                          </div>
                        </>
                      )}
                    </div>
                  )}

                  {expandedTlId === t.task_id && (
                    <div
                      className="detail-panel"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {(() => {
                        const tlState = timelines[t.task_id];
                        if (!tlState || tlState.kind === "loading")
                          return <div className="muted">加载时间线…</div>;
                        if (tlState.kind === "error")
                          return (
                            <div className="banner error">
                              加载失败：{tlState.message}
                            </div>
                          );
                        return (
                          <TimelinePanel
                            tl={tlState.tl}
                            busy={busy === t.task_id}
                            onRetry={(id) => void retryTask(id)}
                            onNavigate={onNavigate}
                          />
                        );
                      })()}
                    </div>
                  )}
                </div>

                <div className="actions" onClick={(e) => e.stopPropagation()}>
                  {t.status === "pending_approval" && (
                    <>
                      <button
                        className="primary"
                        disabled={busy === t.task_id}
                        onClick={() => void action(t.task_id, "approve")}
                      >
                        批准并执行
                      </button>
                      <button
                        className="danger"
                        disabled={busy === t.task_id}
                        onClick={() => void action(t.task_id, "reject")}
                      >
                        拒绝
                      </button>
                    </>
                  )}
                  {t.status === "approved" && (
                    <button
                      disabled={busy === t.task_id}
                      onClick={() => void action(t.task_id, "resume")}
                    >
                      继续执行
                    </button>
                  )}
                  {[
                    "pending_approval",
                    "approved",
                    "running",
                    "queued",
                  ].includes(t.status) && (
                    <button
                      disabled={busy === t.task_id}
                      onClick={() => void action(t.task_id, "cancel")}
                    >
                      取消
                    </button>
                  )}
                  <button
                    aria-expanded={expandedTlId === t.task_id}
                    onClick={() => toggleTimeline(t.task_id)}
                  >
                    {expandedTlId === t.task_id ? "收起时间线" : "时间线"}
                  </button>
                  <button
                    aria-expanded={expanded}
                    onClick={() => void toggle(t.task_id)}
                  >
                    {expanded ? "收起" : "详情"}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
