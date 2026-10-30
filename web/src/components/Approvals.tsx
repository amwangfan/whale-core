import { useCallback, useState } from 'react';
import { api } from '../api';
import { useApi } from '../hooks';

export default function Approvals() {
  const listFn = useCallback(() => api.approvals(), []);
  const { data: approvals, error, loading, reload } = useApi(listFn);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState('');

  const decide = async (draftId: string, decision: 'approve' | 'reject') => {
    setBusy(draftId);
    try {
      await api.decideApproval(draftId, decision);
      setNotice(decision === 'approve'
        ? '批准已记录，等待发送执行器后续派发；当前并未发送。'
        : '拒绝已记录，之后的派发将被阻止。');
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

      {notice && <div className="banner ok">{notice}</div>}
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
                  渠道：{a.channel || '—'} · 状态：{a.status} · 审批：{a.decision || '待决定'} · 派发：{a.execution_status || '未确认'}
                </div>
              </div>
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy === a.draft_id || Boolean(a.decision)}
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
