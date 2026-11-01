// @vitest-environment jsdom
// issue #45：Chat 流式——阶段反馈、delta 逐字、停止、错误事件、非流式降级。
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Chat, { type Msg } from "../Chat";
import { setToken } from "../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonError,
  jsonResult,
  mockStudioFetch,
} from "../Studio/__tests__/helpers";

installDomMocks();

/** 可控 SSE 流：测试逐帧推 data 行。 */
function controlledSse() {
  const encoder = new TextEncoder();
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    response: new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    push: (obj: unknown) =>
      ctrl.enqueue(encoder.encode(`data: ${JSON.stringify(obj)}\n`)),
    close: () => ctrl.close(),
  };
}

function renderChat() {
  function Wrapper() {
    const [msgs, setMsgs] = useState<Msg[]>([]);
    const [sending, setSending] = useState(false);
    return (
      <Chat
        msgs={msgs}
        setMsgs={setMsgs}
        sending={sending}
        setSending={setSending}
      />
    );
  }
  return render(<Wrapper />);
}

function typeAndSend(text: string) {
  fireEvent.change(screen.getByLabelText("消息内容"), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole("button", { name: /发送/ }));
}

beforeEach(() => {
  cleanup();
  setToken("test-token");
  Element.prototype.scrollIntoView = vi.fn();
});

describe("Chat 流式（issue #45）", () => {
  it("阶段 → delta → done：阶段文案、逐字追加、耗时行", async () => {
    const sse = controlledSse();
    const mock = mockStudioFetch({
      "/chat/stream": () => sse.response,
    });
    renderChat();
    typeAndSend("你好");

    // 发送后立即出现助手气泡（初始阶段「已收到」）
    await screen.findByText(/已收到/);
    // 阶段推进：正在整理上下文与记忆
    sse.push({ type: "phase", phase: "context" });
    await screen.findByText(/正在整理上下文与记忆/);
    sse.push({ type: "phase", phase: "model" });
    await screen.findByText(/正在请求模型/);
    // delta 逐字追加
    sse.push({ type: "delta", text: "你" });
    sse.push({ type: "delta", text: "好" });
    await screen.findByText(/你好/);
    // done：最终回复 + 耗时行
    sse.push({
      type: "done",
      reply: "你好呀，今天怎么样？",
      status: "answered",
      metrics: { ttft_ms: 120, total_ms: 450, chunks: 2, stopped: false },
    });
    sse.close();

    await screen.findByText("你好呀，今天怎么样？");
    await screen.findByText(/首 token 120 ms · 共 450 ms · 2 个增量/);
    // 阶段提示在 done 后消失
    expect(screen.queryByText(/正在请求模型/)).toBeNull();
    // 请求体正确
    const call = fetchCalls(mock).find((c) => c.url.includes("/chat/stream"));
    expect(call).toBeTruthy();
    expect(call!.method).toBe("POST");
    expect(call!.body).toEqual({ conversation_id: "console", text: "你好" });
  });

  it("停止：中断后保留部分文本并标注「已停止（部分内容）」", async () => {
    const sse = controlledSse();
    mockStudioFetch({ "/chat/stream": () => sse.response });
    renderChat();
    typeAndSend("讲个故事");

    sse.push({ type: "phase", phase: "model" });
    sse.push({ type: "delta", text: "从前有座山" });
    await screen.findByText(/从前有座山/);

    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    await screen.findByText("已停止（部分内容）");
    // 已收到的部分文本仍在
    expect(screen.getByText(/从前有座山/)).toBeTruthy();
    // 停止按钮消失（发送状态结束）
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "停止" })).toBeNull(),
    );
  });

  it("error 事件：渲染明确错误条（限流含重试建议）", async () => {
    const sse = controlledSse();
    mockStudioFetch({ "/chat/stream": () => sse.response });
    renderChat();
    typeAndSend("你好");

    sse.push({ type: "phase", phase: "received" });
    sse.push({ type: "error", error: "rate_limited" });
    sse.close();

    await screen.findByText("请求太频繁（已被限流），请稍后重试。");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "停止" })).toBeNull(),
    );
  });

  it("流通道失败：回落非流式 /chat 并标注降级", async () => {
    mockStudioFetch({
      "/chat/stream": () => jsonError(500, "server", "stream down"),
      "/chat": () =>
        jsonResult({ reply: "降级回复", status: "answered", receipts: [] }),
    });
    renderChat();
    typeAndSend("你好");

    await screen.findByText("降级回复");
    await screen.findByText(/已降级为非流式/);
  });
});
