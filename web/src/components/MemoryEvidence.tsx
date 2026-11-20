import { useEffect, useState } from 'react';
import { studioApi, type MemoryEvidence as Evidence } from '../api';

export default function MemoryEvidence({ recordKey, evidenceId }: {recordKey: string; evidenceId: string}) {
  const [result, setResult] = useState<Evidence | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let stale = false;
    setResult(null); setError('');
    studioApi.memoryEvidence(recordKey, evidenceId).then(r => { if (!stale) setResult(r); })
      .catch(e => { if (!stale) setError(String(e)); });
    return () => { stale = true; };
  }, [recordKey, evidenceId]);
  return <section aria-label="群聊原始证据">
    <h4>群聊原始证据</h4>
    {error ? <p role="alert">无法读取证据：{error}</p> : !result ? <p role="status">正在读取原话…</p> : !result.available ? <p>{result.message}</p> : <>
      <dl><dt>发送者</dt><dd>{result.sender_name || '未记录昵称'}（{result.sender_id || '未记录账号'}）</dd>
        <dt>发送时间（北京时间）</dt><dd>{new Date(result.sent_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false})}</dd>
        <dt>群号</dt><dd>{result.group_id}</dd><dt>消息 ID</dt><dd>{result.message_id}</dd></dl>
      <blockquote style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',margin:'12px 0',padding:12,borderLeft:'3px solid #886f43'}}>{result.text}</blockquote>
      <p className="muted small">{result.archive_note}</p>
    </>}
  </section>;
}
