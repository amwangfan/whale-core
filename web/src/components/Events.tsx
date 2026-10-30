import { useEffect, useRef, useState } from 'react';
import { streamEvents, WhaleEvent } from '../api';

type ConnState = 'connecting' | 'open' | 'error';

const MAX_EVENTS = 500;

function formatAt(at: number | string): string {
  if (typeof at === 'number') {
    // 后端事件 at 为 epoch 秒
    return new Date(at * 1000).toLocaleTimeString();
  }
  return String(at);
}

export default function Events() {
  const [events, setEvents] = useState<WhaleEvent[]>([]);
  const [conn, setConn] = useState<ConnState>('connecting');
  const [errMsg, setErrMsg] = useState('');
  const [retry, setRetry] = useState(0);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setConn('connecting');
    setErrMsg('');
    const stream = streamEvents({
      onOpen: () => setConn('open'),
      onEvent: e => setEvents(prev => [...prev.slice(-(MAX_EVENTS - 1)), e]),
      onError: err => {
        setConn('error');
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
    <div>
      <div className="page-head">
        <h2>事件</h2>
        <div className="actions">
          <span
            className={
              conn === 'open' ? 'badge ok' : conn === 'connecting' ? 'badge' : 'badge bad'
            }
          >
            {conn === 'open' ? '已连接' : conn === 'connecting' ? '连接中…' : `断开：${errMsg}`}
          </span>
          {conn === 'error' && <button onClick={() => setRetry(r => r + 1)}>重连</button>}
          <button onClick={() => setEvents([])}>清空</button>
        </div>
      </div>

      <div className="event-log" ref={logRef}>
        {events.length === 0 && <div className="empty">暂无事件。</div>}
        {events.map((e, i) => (
          <div key={i} className="event-row">
            <span className="muted mono">{formatAt(e.at)}</span>
            <span className="badge">{e.kind}</span>
            <span className="event-detail">{e.detail}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
