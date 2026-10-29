import { useCallback, useState } from 'react';
import { api, TaskRecord } from '../api';
import { useApi } from '../hooks';

/** status → 中文标签（徽标悬停可查看原始状态值）。 */
const STATUS_LABELS: Record<string, string> = {
  answered: '已回答',
  clarify: '需澄清',
  pending_approval: '待审批',
  denied: '已拒绝',
  failed: '失败',
};

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'answered':
      return 'badge ok';
    case 'clarify':
      return 'badge info';
    case 'pending_approval':
      return 'badge warn';
    case 'denied':
    case 'failed':
      return 'badge bad';
    default:
      return 'badge';
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
      {capabilities.map(c => (
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
  | { kind: 'loading' }
  | { kind: 'ok'; task: TaskRecord }
  | { kind: 'error'; message: string };

export default function Tasks() {
  const listFn = useCallback(() => api.tasks(), []);
  const { data: tasks, error, loading, reload } = useApi(listFn);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, DetailState>>({});

  const toggle = async (taskId: string) => {
    if (expandedId === taskId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(taskId);
    if (details[taskId]?.kind === 'ok') return; // 已加载过，直接展开
    setDetails(d => ({ ...d, [taskId]: { kind: 'loading' } }));
    try {
      const task = await api.task(taskId);
      setDetails(d => ({ ...d, [taskId]: { kind: 'ok', task } }));
    } catch (e) {
      setDetails(d => ({
        ...d,
        [taskId]: { kind: 'error', message: e instanceof Error ? e.message : String(e) },
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
        「目标 / 声明能力」是模型提出的计划，未必全部执行；「回执」才是实际执行（或被拒 / 挂起）的记录。
      </div>

      {loading && <div className="muted">加载中…</div>}
      {error && <div className="banner error">{error}</div>}
      {tasks && tasks.length === 0 && (
        <div className="empty">
          暂无任务记录。通过「对话」页发起任务后，可在此核对模型提出的计划与实际执行的回执。
        </div>
      )}

      {tasks && tasks.length > 0 && (
        <ul className="list">
          {tasks.map(t => {
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
                    <span className={statusBadgeClass(t.status)} title={t.status}>
                      {STATUS_LABELS[t.status] ?? t.status}
                    </span>
                    <span className="muted mono">{formatAt(t.at)}</span>
                  </div>
                  <div>{t.goal || <span className="muted">（无目标描述）</span>}</div>
                  <div className="muted small">
                    声明能力（模型提出）：<Capabilities capabilities={t.capabilities} />
                  </div>
                  <details className="receipts" onClick={e => e.stopPropagation()}>
                    <summary>
                      回执 {t.receipts.length} 条（实际执行，点击展开）
                    </summary>
                    <ReceiptList receipts={t.receipts} />
                  </details>

                  {expanded && (
                    <div className="detail-panel" onClick={e => e.stopPropagation()}>
                      {(!detail || detail.kind === 'loading') && (
                        <div className="muted">加载详情…</div>
                      )}
                      {detail?.kind === 'error' && (
                        <div className="banner error">加载失败：{detail.message}</div>
                      )}
                      {detail?.kind === 'ok' && (
                        <>
                          <div className="mono muted">
                            task_id: {detail.task.task_id} · status: {detail.task.status} ·{' '}
                            {formatAt(detail.task.at)}
                          </div>
                          <div>
                            <div className="section-label">模型提出的计划（未必全部执行）</div>
                            <div>目标：{detail.task.goal || '—'}</div>
                            <div>
                              声明能力：<Capabilities capabilities={detail.task.capabilities} />
                            </div>
                          </div>
                          <div>
                            <div className="section-label">实际执行的回执（真实发生）</div>
                            <ReceiptList receipts={detail.task.receipts} />
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>

                <button>{expanded ? '收起' : '详情'}</button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
