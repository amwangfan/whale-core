import { useCallback, useState } from 'react';
import { api } from '../api';
import { useApi } from '../hooks';

export default function Approvals() {
  const listFn = useCallback(() => api.approvals(), []);
  const { data: approvals, error, loading, reload } = useApi(listFn);
  const [busy, setBusy] = useState<string | null>(null);

  const decide = async (draftId: string, decision: 'approve' | 'reject') => {
    setBusy(draftId);
    try {
      await api.decideApproval(draftId, decision);
      reload();
    } catch (e) {
      alert(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <div className="page-head">
        <h2>审批</h2>
        <button onClick={reload}>刷新</button>
      </div>

      {loading && <div className="muted">加载中…</div>}
      {error && <div className="banner error">{error}</div>}
      {approvals && approvals.length === 0 && <div className="empty">没有待审批的草稿。</div>}

      {approvals && approvals.length > 0 && (
        <ul className="list">
          {approvals.map(a => (
            <li key={a.draft_id} className="list-item">
              <div className="list-main">
                <div className="mono">{a.draft_id}</div>
                <div>{a.text}</div>
                <div className="muted">
                  渠道：{a.channel || '—'} · 状态：{a.status}
                </div>
              </div>
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy === a.draft_id}
                  onClick={() => void decide(a.draft_id, 'approve')}
                >
                  批准
                </button>
                <button
                  className="danger"
                  disabled={busy === a.draft_id}
                  onClick={() => void decide(a.draft_id, 'reject')}
                >
                  拒绝
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
