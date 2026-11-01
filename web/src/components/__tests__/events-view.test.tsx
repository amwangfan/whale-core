// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Events from "../Events";
import { setToken } from "../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockStudioFetch,
} from "../Studio/__tests__/helpers";

// 事件流视图的 SSE 不在本测试覆盖；打桩为空实现避免真实连接。
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    streamEvents: vi.fn(() => ({ close: vi.fn() })),
  };
});

installDomMocks();

const ACTIVITY = {
  total: 1,
  items: [
    {
      trace_id: "tr-1",
      at: 1760000000,
      status: "replied",
      lines: [
        "10:00:00 · 私聊里来了一条消息。",
        "判断：需要回复。",
        "结果：已回复。",
      ],
    },
  ],
};

const TRACE_ROW = {
  trace_id: "tr-1",
  kind: "message",
  conversation_id: "group:g1",
  principal_id: "qq:user:1001",
  message_id: "m-1",
  task_id: "",
  action_id: "",
  receipt_id: "r-1",
  started_at: 1760000000,
  ended_at: 1760000002,
  status: "replied",
  summary: "回复了群聊消息",
};

const TRACE_DETAIL = {
  ...TRACE_ROW,
  spans: [
    {
      span_id: "sp-1",
      phase: "decide",
      status: "executed",
      reason_code: "mentioned",
      detail: "",
      started_at: 1760000000,
      duration_ms: 3,
      attempt: 1,
    },
    {
      span_id: "sp-2",
      phase: "model",
      status: "executed",
      reason_code: "",
      detail: "模型调用成功",
      started_at: 1760000000,
      duration_ms: 1200,
      attempt: 1,
    },
    {
      span_id: "sp-3",
      phase: "send",
      status: "executed",
      reason_code: "",
      detail: "",
      started_at: 1760000001,
      duration_ms: 40,
      attempt: 1,
    },
  ],
};

beforeEach(() => {
  cleanup();
  setToken("test-token");
});

describe("Events 事件页（活动记录 / 事件流 + Trace 查询）", () => {
  it("活动记录：渲染自然语言解释行与状态色点", async () => {
    mockStudioFetch({
      "/activity": () => jsonResult(ACTIVITY),
    });
    const { container } = render(<Events />);

    expect(
      screen.getByText("这里解释鲸鱼最近为什么回复/没回复。"),
    ).toBeTruthy();
    await screen.findByText("判断：需要回复。");
    expect(screen.getByText("结果：已回复。")).toBeTruthy();
    expect(screen.getByText("私聊里来了一条消息。", { exact: false }))
      .toBeTruthy();
    // replied → 绿色状态点
    expect(container.querySelector(".activity-dot.replied")).toBeTruthy();
    expect(screen.getByLabelText("已回复")).toBeTruthy();
  });

  it("Trace 查询：列表渲染，筛选条件正确组参", async () => {
    const mock = mockStudioFetch({
      "/activity": () => jsonResult({ total: 0, items: [] }),
      "/traces": () =>
        jsonResult({ total: 1, offset: 0, limit: 50, traces: [TRACE_ROW] }),
    });
    render(<Events />);

    fireEvent.click(screen.getByRole("tab", { name: "事件流" }));
    // 进入事件流后自动查询一次
    await screen.findByText("回复了群聊消息");
    expect(screen.getByText("kind")).toBeTruthy();
    expect(screen.getByText("trace_id")).toBeTruthy();
    expect(screen.getByText("group:g1")).toBeTruthy();

    // 设置筛选：status=replied + conversation_id=group:g1
    fireEvent.change(screen.getByLabelText("状态筛选"), {
      target: { value: "replied" },
    });
    fireEvent.change(screen.getByLabelText("会话 ID"), {
      target: { value: "group:g1" },
    });
    fireEvent.click(screen.getByText("查询"));

    await waitFor(() => {
      const calls = fetchCalls(mock).filter(
        (c) => c.url.includes("/traces?") && c.url.includes("status=replied"),
      );
      expect(calls.length).toBeGreaterThan(0);
      expect(calls[0].url).toContain("conversation_id=group%3Ag1");
      expect(calls[0].url).toContain("limit=50");
      expect(calls[0].url).toContain("offset=0");
    });
  });

  it("Trace 详情：点击 trace_id 展开并渲染 spans 表格", async () => {
    const mock = mockStudioFetch({
      "/activity": () => jsonResult({ total: 0, items: [] }),
      "/traces": (call) => {
        if (call.url.includes("/traces/tr-1")) {
          return jsonResult({ trace: TRACE_DETAIL });
        }
        return jsonResult({
          total: 1,
          offset: 0,
          limit: 50,
          traces: [TRACE_ROW],
        });
      },
    });
    render(<Events />);

    fireEvent.click(screen.getByRole("tab", { name: "事件流" }));
    await screen.findByText("回复了群聊消息");

    fireEvent.click(screen.getByText("tr-1"));

    await waitFor(() => {
      const calls = fetchCalls(mock).filter((c) =>
        c.url.includes("/traces/tr-1"),
      );
      expect(calls.length).toBe(1);
    });
    // spans 表头与内容
    await screen.findByText("阶段");
    expect(screen.getByText("reason_code")).toBeTruthy();
    expect(screen.getByText("耗时")).toBeTruthy();
    expect(screen.getByText("mentioned")).toBeTruthy();
    expect(screen.getByText("模型调用成功")).toBeTruthy();
    expect(screen.getByText("1200 ms")).toBeTruthy();
    expect(screen.getByText("decide")).toBeTruthy();
  });
});
