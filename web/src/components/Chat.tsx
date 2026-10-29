import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

interface Msg {
  role: 'user' | 'assistant';
  text: string;
  meta?: string;
}

const CONVERSATION_ID = 'console';

export default function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [msgs]);

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setMsgs(m => [...m, { role: 'user', text }]);
    setInput('');
    setSending(true);
    try {
      const r = await api.chat(text, CONVERSATION_ID);
      setMsgs(m => [
        ...m,
        { role: 'assistant', text: r.reply || '（空回复）', meta: `status: ${r.status}` },
      ]);
    } catch (e) {
      setMsgs(m => [
        ...m,
        { role: 'assistant', text: `错误：${e instanceof Error ? e.message : String(e)}` },
      ]);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="chat">
      <div className="page-head">
        <h2>对话</h2>
        <span className="muted">conversation_id: {CONVERSATION_ID}</span>
      </div>

      <div className="chat-log">
        {msgs.length === 0 && <div className="empty">向 whale-core 说点什么…</div>}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'msg user' : 'msg assistant'}>
            <div className="msg-role">{m.role === 'user' ? '我' : 'whale'}</div>
            <div className="msg-body">
              {m.text}
              {m.meta && <div className="muted small">{m.meta}</div>}
            </div>
          </div>
        ))}
        {sending && <div className="muted">思考中…</div>}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input">
        <input
          placeholder="输入消息，Enter 发送"
          value={input}
          disabled={sending}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') void send();
          }}
        />
        <button className="primary" disabled={sending || !input.trim()} onClick={() => void send()}>
          发送
        </button>
      </div>
    </div>
  );
}
