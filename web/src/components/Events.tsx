/**
 * 事件页（issue #39）：两个视图。
 * - 「活动记录」（用户视图）：GET /activity 的自然语言解释，
 *   回答「鲸鱼最近为什么回复/没回复」，按状态配色点。
 * - 「事件流」（诊断视图）：既有 SSE 实时事件列表 + Trace 查询
 *   （/traces 列表筛选，点 trace_id 展开 /traces/{id} 的 spans 明细）。
 */
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  streamEvents,
  type ActivityItem,
  type TraceDetail,
  type TracesListResult,
  type WhaleEvent,
} from "../api";

type ConnState = "connecting" | "open" | "error";

const MAX_EVENTS = 500;

const STATUS_TEXT: Record<string, string> = {
  open: "进行中",
  replied: "已回复",
  silent: "静默",
  failed: "失败",
  partial: "部分完成",
};

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

function statusBadge(status: string): string {
  switch (status) {
    case "replied":
      return "badge ok";
    case "failed":
      return "badge bad";
    case "partial":
      return "badge warn";
    case "silent":
      return "badge";
    default:
      return "badge info";
  }
}

function formatAt(at: number | string): string {
  if (typeof at === "number") {
    // 后端事件 at 为 epoch 秒
    return new Date(at * 1000).toLocaleTimeString();
  }
  return String(at);
}

/** epoch 秒 → HH:MM:SS（24 小时制）。 */
function fmtClock(epochS: number): string {
  const d = new Date(epochS * 1000);
  return Number.isNaN(d.getTime())
    ? String(epochS)
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

// ---------------------------------------------------------------- 活动记录

function ActivityView() {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.activityList(50, 0);
      setItems(r.items);
      setTotal(r.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-label="活动记录">
      <p className="muted">这里解释鲸鱼最近为什么回复/没回复。</p>
      {error && <div className="banner error">{error}</div>}
      <div className="actions" style={{ margin: "8px 0" }}>
        <button onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
        {items && (
          <span className="muted small">共 {total} 条活动记录</span>
        )}
      </div>
      {items && items.length === 0 && (
        <div className="empty">暂无活动记录。</div>
      )}
      {items && items.length > 0 && (
        <ul className="activity-list">
          {items.map((it) => (
            <li key={it.trace_id} className="activity-item">
              <span
                className={`activity-dot ${it.status}`}
                title={statusText(it.status)}
                aria-label={statusText(it.status)}
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
  );
}

// ---------------------------------------------------------------- Trace 查询

const TRACE_STATUS_OPTIONS = ["replied", "silent", "failed", "partial"];

function TracePanel() {
  const [status, setStatus] = useState("");
  const [conv, setConv] = useState("");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<TracesListResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(
    async (off: number) => {
      setLoading(true);
      setError(null);
      try {
        const r = await api.tracesList({
          status: status || undefined,
          conversation_id: conv.trim() || undefined,
          limit,
          offset: off,
        });
        setResult(r);
        setOffset(off);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [status, conv, limit],
  );

  useEffect(() => {
    void load(0);
    // 仅在挂载时自动查询一次；筛选变化由「查询」按钮触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = async (traceId: string) => {
    if (openId === traceId) {
      setOpenId(null);
      setDetail(null);
      return;
    }
    setOpenId(traceId);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await api.tracesDetail(traceId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetailLoading(false);
    }
  };

  return (
    <section className="trace-panel" aria-label="Trace 查询">
      <div className="section-head">
        <h2>Trace 查询</h2>
        <span className="muted small">
          结构化诊断：阶段 / 耗时 / reason_code / 回执
        </span>
      </div>

      <div className="memory-toolbar">
        <select
          aria-label="状态筛选"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">全部状态</option>
          {TRACE_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {statusText(s)}
            </option>
          ))}
        </select>
        <input
          aria-label="会话 ID"
          placeholder="conversation_id，如 group:g1…"
          value={conv}
          onChange={(e) => setConv(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void load(0);
          }}
        />
        <select
          aria-label="每页条数"
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
        >
          {[20, 50, 100, 200].map((n) => (
            <option key={n} value={n}>
              每页 {n} 条
            </option>
          ))}
        </select>
        <button disabled={loading} onClick={() => void load(0)}>
          {loading ? "查询中…" : "查询"}
        </button>
      </div>

      {error && <div className="banner error">{error}</div>}
      {result && result.traces.length === 0 && (
        <div className="empty">没有匹配的 trace。</div>
      )}
      {result && result.traces.length > 0 && (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>时间</th>
                <th>kind</th>
                <th>会话</th>
                <th>状态</th>
                <th>summary</th>
                <th>trace_id</th>
              </tr>
            </thead>
            <tbody>
              {result.traces.map((t) => (
                <Fragment key={t.trace_id}>
                  <tr>
                    <td className="mono">{fmtClock(t.started_at)}</td>
                    <td>{t.kind}</td>
                    <td className="mono">{t.conversation_id || "—"}</td>
                    <td>
                      <span className={statusBadge(t.status)}>
                        {statusText(t.status)}
                      </span>
                    </td>
                    <td>{t.summary}</td>
                    <td>
                      <button
                        className="text-button mono"
                        aria-expanded={openId === t.trace_id}
                        onClick={() => void toggle(t.trace_id)}
                      >
                        {t.trace_id}
                      </button>
                    </td>
                  </tr>
                  {openId === t.trace_id && (
                    <tr className="expand-row">
                      <td colSpan={6}>
                        {detailLoading && (
                          <span className="muted">正在读取详情…</span>
                        )}
                        {detail && detail.trace_id === t.trace_id && (
                          <>
                            <span className="section-label">
                              spans（{detail.spans.length}）
                            </span>
                            <table className="table span-table">
                              <thead>
                                <tr>
                                  <th>阶段</th>
                                  <th>状态</th>
                                  <th>reason_code</th>
                                  <th>耗时</th>
                                  <th>detail</th>
                                </tr>
                              </thead>
                              <tbody>
                                {detail.spans.map((s) => (
                                  <tr key={s.span_id}>
                                    <td>{s.phase}</td>
                                    <td>{s.status}</td>
                                    <td className="mono">
                                      {s.reason_code || "—"}
                                    </td>
                                    <td>{s.duration_ms} ms</td>
                                    <td>{s.detail || "—"}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {result && (
        <div className="memory-pager">
          <button
            disabled={offset === 0 || loading}
            onClick={() => void load(Math.max(0, offset - limit))}
          >
            上一页
          </button>
          <span className="muted small">
            {result.total === 0
              ? "共 0 条"
              : `第 ${offset + 1}–${Math.min(offset + limit, result.total)} 条 / 共 ${result.total} 条`}
          </span>
          <button
            disabled={offset + limit >= result.total || loading}
            onClick={() => void load(offset + limit)}
          >
            下一页
          </button>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- 事件流（SSE）

function StreamView() {
  const [events, setEvents] = useState<WhaleEvent[]>([]);
  const [conn, setConn] = useState<ConnState>("connecting");
  const [errMsg, setErrMsg] = useState("");
  const [filter, setFilter] = useState("");
  const [retry, setRetry] = useState(0);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setConn("connecting");
    setErrMsg("");
    const stream = streamEvents({
      onOpen: () => setConn("open"),
      onEvent: (e) =>
        setEvents((prev) => [...prev.slice(-(MAX_EVENTS - 1)), e]),
      onError: (err) => {
        setConn("error");
        setErrMsg(err.message);
      },
    });
    return () => stream.close();
  }, [retry]);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events]);

  return (
    <section aria-label="事件流">
      <div className="actions" style={{ margin: "8px 0" }}>
        <span
          className={
            conn === "open"
              ? "badge ok"
              : conn === "connecting"
                ? "badge"
                : "badge bad"
          }
        >
          {conn === "open"
            ? "已连接"
            : conn === "connecting"
              ? "连接中…"
              : `断开：${errMsg}`}
        </span>
        {conn === "error" && (
          <button onClick={() => setRetry((r) => r + 1)}>重连</button>
        )}
        <button onClick={() => setEvents([])}>清空</button>
      </div>

      <div className="searchbar">
        <input
          aria-label="筛选运行日志"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="按事件类型或内容筛选…"
        />
      </div>
      <div className="event-log" role="log" aria-live="polite" ref={logRef}>
        {events.length === 0 && <div className="empty">暂无事件。</div>}
        {events
          .filter((e) =>
            `${e.kind} ${e.detail}`
              .toLowerCase()
              .includes(filter.toLowerCase()),
          )
          .map((e, i) => (
            <div key={i} className="event-row">
              <span className="muted mono">{formatAt(e.at)}</span>
              <span className="badge">{e.kind}</span>
              <span className="event-detail">{e.detail}</span>
            </div>
          ))}
      </div>

      <TracePanel />
    </section>
  );
}

// ---------------------------------------------------------------- 页面

export default function Events() {
  const [view, setView] = useState<"activity" | "stream">("activity");

  return (
    <div>
      <div className="page-head">
        <h2>事件</h2>
        <div className="view-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={view === "activity"}
            className={view === "activity" ? "active" : ""}
            onClick={() => setView("activity")}
          >
            活动记录
          </button>
          <button
            role="tab"
            aria-selected={view === "stream"}
            className={view === "stream" ? "active" : ""}
            onClick={() => setView("stream")}
          >
            事件流
          </button>
        </div>
      </div>

      {view === "activity" ? <ActivityView /> : <StreamView />}
    </div>
  );
}
