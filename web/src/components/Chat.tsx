import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { api } from "../api";
import Icon from "./Icon";
export interface Msg {
  role: "user" | "assistant";
  text: string;
  meta?: string;
  taskId?: string;
}
const LABELS: Record<string, string> = {
  answered: "已回复",
  failed: "执行失败",
  pending_approval: "等待审批",
  clarify: "需要补充",
  denied: "操作被拒绝",
};
export default function Chat({
  msgs,
  setMsgs,
  sending,
  setSending,
  signal,
}: {
  msgs: Msg[];
  setMsgs: Dispatch<SetStateAction<Msg[]>>;
  sending: boolean;
  setSending: Dispatch<SetStateAction<boolean>>;
  signal?: AbortSignal;
}) {
  const [input, setInput] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState("");
  const removeHistory = async () => {
    if (
      !window.confirm(
        "删除此会话的持久记录及关联记忆？此操作无法撤销。建议先在记忆空间导出备份；有未完成任务时会拒绝删除。",
      )
    )
      return;
    setDeleting(true);
    setNotice("");
    try {
      await api.deleteConversation("console");
      setMsgs([]);
      setNotice("此会话的持久记录已删除。");
    } catch (e) {
      setNotice(`删除失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDeleting(false);
    }
  };
  const bottomRef = useRef<HTMLDivElement>(null);
  const composing = useRef(false);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs]);
  const send = async (prompt?: string) => {
    const text = (prompt ?? input).trim();
    if (!text || sending || deleting) return;
    setMsgs((m) => [...m, { role: "user", text }]);
    setInput("");
    setSending(true);
    try {
      const r = await api.chat(text, "console", signal);
      if (signal?.aborted) return;
      setMsgs((m) => [
        ...m,
        {
          role: "assistant",
          text: r.reply || "模型返回了空回复，请检查模型配置。",
          meta: LABELS[r.status] ?? r.status,
          taskId: r.task_id,
        },
      ]);
    } catch (e) {
      if (signal?.aborted) return;
      setMsgs((m) => [
        ...m,
        {
          role: "assistant",
          text: `暂时没有完成：${e instanceof Error ? e.message : String(e)}`,
        },
      ]);
    } finally {
      if (!signal?.aborted) setSending(false);
    }
  };
  return (
    <div className="chat surface">
      <div className="chat-toolbar">
        <span className="status-dot" />
        <strong>鲸鱼</strong>
        <span className="muted small">与你的连续对话</span>
        <button
          className="text-button"
          disabled={sending || deleting || msgs.length === 0}
          onClick={() => setMsgs([])}
        >
          清空显示
        </button>
        <button
          className="text-button danger"
          disabled={sending || deleting}
          onClick={() => void removeHistory()}
        >
          {deleting ? "删除中…" : "删除会话记录"}
        </button>
      </div>
      {notice && (
        <div className="banner" role="status" style={{ margin: 12 }}>
          {notice}
        </div>
      )}
      <div className="chat-log" role="log" aria-live="polite">
        {msgs.length === 0 && (
          <div className="chat-welcome">
            <span className="chat-whale">
              <Icon name="whale" size={46} />
            </span>
            <h2>今天，想聊些什么？</h2>
            <p>说说近况，记住一个想法，或交给我一件事。</p>
            <div className="suggestions">
              {[
                "你现在能帮我做些什么？",
                "记住：我喜欢从原理开始理解问题",
                "搜索一下今天的科技新闻",
              ].map((s) => (
                <button
                  key={s}
                  disabled={sending || deleting}
                  onClick={() => void send(s)}
                >
                  {s}
                  <Icon name="arrow" size={14} />
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="msg-role">
              {m.role === "user" ? "我" : <Icon name="whale" size={22} />}
            </div>
            <div className="msg-body">
              {m.text}
              {m.meta && (
                <div className="message-meta">
                  {m.meta}
                  {m.taskId ? ` · 任务 ${m.taskId.slice(0, 8)}` : ""}
                </div>
              )}
            </div>
          </div>
        ))}
        {sending && (
          <div className="thinking">
            <span />
            <span />
            <span />
            鲸鱼正在思考…
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <form
        className="chat-composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          aria-label="消息内容"
          placeholder="和鲸鱼说点什么…"
          value={input}
          disabled={sending || deleting}
          rows={2}
          onChange={(e) => setInput(e.target.value)}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={() => {
            composing.current = false;
          }}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              !composing.current
            ) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-bottom">
          <span className="muted small">Enter 发送 · Shift + Enter 换行</span>
          <button
            className="primary"
            type="submit"
            disabled={sending || deleting || !input.trim()}
          >
            发送
            <Icon name="arrow" size={15} />
          </button>
        </div>
      </form>
    </div>
  );
}
