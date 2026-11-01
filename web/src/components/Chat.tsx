import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { api, chatStream } from "../api";
import Icon from "./Icon";

export interface Msg {
  role: "user" | "assistant";
  text: string;
  meta?: string;
  taskId?: string;
  /** issue #45：流式阶段反馈 / 停止标注 / 错误条 */
  phase?: string;
  streaming?: boolean;
  error?: boolean;
}

const LABELS: Record<string, string> = {
  answered: "已回复",
  failed: "执行失败",
  pending_approval: "等待审批",
  clarify: "需要补充",
  denied: "操作被拒绝",
};

/** issue #45：流式阶段 → 用户可读文案（真实阶段，无伪造进度）。 */
const PHASE_TEXT: Record<string, string> = {
  received: "已收到",
  context: "正在整理上下文与记忆",
  model: "正在请求模型",
  compose: "正在整理回复",
};

const STREAM_ERROR_TEXT: Record<string, string> = {
  config: "模型配置有问题，请到「设置与诊断」页检查后再试。",
  rate_limited: "请求太频繁（已被限流），请稍后重试。",
  server: "服务内部错误，请稍后重试。",
  network: "网络异常，请检查连接后重试。",
  unavailable: "服务暂不可用，请稍后重试。",
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
  /** 本轮发送的停止控制器（仅停止接收，不声称撤销任何外部动作）。 */
  const sendAbort = useRef<AbortController | null>(null);

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
    const abort = new AbortController();
    sendAbort.current = abort;
    // 助手占位气泡：先显示真实阶段文案，delta 到达后逐字追加
    setMsgs((m) => [
      ...m,
      { role: "assistant", text: "", phase: "received", streaming: true },
    ]);
    const patchLast = (patch: Partial<Msg>) =>
      setMsgs((m) => {
        const next = [...m];
        const i = next.length - 1;
        if (i >= 0 && next[i].role === "assistant")
          next[i] = { ...next[i], ...patch };
        return next;
      });
    const stoppedPatch: Partial<Msg> = {
      streaming: false,
      phase: undefined,
      meta: "已停止（部分内容）",
    };
    let accumulated = "";
    let streamError = "";
    try {
      const done = await chatStream(
        text,
        "console",
        {
          onPhase: (p) => patchLast({ phase: p }),
          onDelta: (d) => {
            accumulated += d;
            patchLast({ text: accumulated });
          },
          onError: (kind) => {
            streamError = kind;
          },
        },
        abort.signal,
      );
      if (signal?.aborted) return;
      if (abort.signal.aborted) {
        patchLast(stoppedPatch);
        return;
      }
      if (done) {
        const mt = done.metrics;
        const metricText = mt
          ? `首 token ${mt.ttft_ms ?? "-"} ms · 共 ${mt.total_ms ?? "-"} ms · ${mt.chunks ?? 0} 个增量`
          : "";
        patchLast({
          streaming: false,
          phase: undefined,
          text:
            done.reply || accumulated || "模型返回了空回复，请检查模型配置。",
          taskId: done.task_id,
          meta:
            (LABELS[done.status] ?? done.status) +
            (done.stopped ? " · 已停止（部分内容）" : "") +
            (metricText ? ` · ${metricText}` : ""),
        });
        return;
      }
      if (streamError) {
        patchLast({
          streaming: false,
          phase: undefined,
          error: true,
          text:
            STREAM_ERROR_TEXT[streamError] ??
            `出错了（${streamError}），请稍后重试。`,
        });
        return;
      }
      // 流正常结束但没有 done 帧：按流失败处理，回落非流式
      throw new Error("stream_ended_without_done");
    } catch (e) {
      if (signal?.aborted) return;
      if (abort.signal.aborted) {
        patchLast(stoppedPatch);
        return;
      }
      // 流式通道失败：回落原非流式 /chat，并标注降级
      try {
        const r = await api.chat(text, "console", signal);
        if (signal?.aborted) return;
        patchLast({
          streaming: false,
          phase: undefined,
          text: r.reply || "模型返回了空回复，请检查模型配置。",
          taskId: r.task_id,
          meta: `${LABELS[r.status] ?? r.status} · 已降级为非流式`,
        });
      } catch (e2) {
        if (signal?.aborted) return;
        patchLast({
          streaming: false,
          phase: undefined,
          error: true,
          text: `暂时没有完成：${e2 instanceof Error ? e2.message : String(e2)}`,
        });
      }
    } finally {
      if (!signal?.aborted) setSending(false);
      if (sendAbort.current === abort) sendAbort.current = null;
    }
  };

  const streamingNow = msgs.some((m) => m.streaming);

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
            <div className={`msg-body${m.error ? " msg-error" : ""}`}>
              {m.streaming && (
                <div className="phase-hint">
                  {PHASE_TEXT[m.phase ?? ""] ?? "正在处理"}…
                </div>
              )}
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
        {sending && !streamingNow && (
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
          <span className="muted small">
            {sending
              ? "停止仅中断回复接收，不会撤销已发生的动作"
              : "Enter 发送 · Shift + Enter 换行"}
          </span>
          {sending ? (
            <button
              type="button"
              className="danger"
              onClick={() => sendAbort.current?.abort()}
            >
              停止
            </button>
          ) : (
            <button
              className="primary"
              type="submit"
              disabled={deleting || !input.trim()}
            >
              发送
              <Icon name="arrow" size={15} />
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
