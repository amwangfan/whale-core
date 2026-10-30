import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { WhaleMark } from "./Icon";
interface Msg {
  role: "user" | "assistant";
  text: string;
  meta?: string;
}
export default function Chat() {
  const [cid, setCid] = useState(
    () => localStorage.getItem("whale.conversation") || "console",
  );
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let active = true;
    setMsgs([]);
    setLoadingHistory(true);
    void api
      .history(cid)
      .then((r) => {
        if (active)
          setMsgs(r.history.map((m) => ({ role: m.role, text: m.content })));
      })
      .catch((e) => {
        if (active) setError(String(e));
      })
      .finally(() => {
        if (active) setLoadingHistory(false);
      });
    return () => {
      active = false;
    };
  }, [cid]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs, sending]);
  const send = async () => {
    const text = input.trim();
    if (!text || sending || loadingHistory) return;
    setMsgs((m) => [...m, { role: "user", text }]);
    setInput("");
    setSending(true);
    setError("");
    try {
      const r = await api.chat(text, cid);
      setMsgs((m) => [
        ...m,
        {
          role: "assistant",
          text: r.reply,
          meta: r.status === "answered" ? undefined : r.status,
        },
      ]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };
  const fresh = () => {
    const id = "console-" + crypto.randomUUID().slice(0, 8);
    localStorage.setItem("whale.conversation", id);
    setCid(id);
    setError("");
  };
  return (
    <div className="chat">
      <div className="page-head">
        <div>
          <h2>与鲸鱼对话</h2>
          <p className="muted">从一句话开始，继续你们的故事。</p>
        </div>
        <button disabled={sending || loadingHistory} onClick={fresh}>
          新对话
        </button>
      </div>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div className="chat-log" aria-live="polite">
        {!msgs.length && (
          <div className="illustrated-empty">
            <WhaleMark size={68} />
            <strong>今天，有什么想和我说的？</strong>
            <p>
              聊聊近况，试试「记住：我喜欢…」，
              <br />
              或者「搜索：我想了解的事」。
            </p>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <span className="msg-role">{m.role === "user" ? "我" : "鲸"}</span>
            <div className="msg-body">
              {m.text}
              {m.meta && <div className="muted small">{m.meta}</div>}
            </div>
          </div>
        ))}
        {sending && (
          <div className="msg">
            <span className="msg-role">鲸</span>
            <div className="msg-body muted">正在想怎么回答你…</div>
          </div>
        )}
        <div ref={bottom} />
      </div>
      <form
        className="chat-input"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          aria-label="发送给鲸鱼的消息"
          rows={2}
          placeholder="和鲸鱼说点什么…"
          maxLength={4000}
          value={input}
          disabled={sending || loadingHistory}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button
          className="primary"
          disabled={sending || loadingHistory || !input.trim()}
        >
          {sending ? "思考中" : "发送 ↗"}
        </button>
      </form>
      <div
        className="muted small"
        style={{ textAlign: "center", paddingTop: 8, fontSize: 9 }}
      >
        Enter 发送 · Shift + Enter 换行 · 会话记忆已连接
      </div>
    </div>
  );
}
