// @vitest-environment jsdom
// issue #43：任务时间线——展开拉取渲染、failed 重试、unknown 引导、redeliver 禁用。
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Tasks from "../Tasks";
import { setToken } from "../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockStudioFetch,
} from "../Studio/__tests__/helpers";

installDomMocks();

const TASK = {
  task_id: "t-1",
  at: 1760000000,
  goal: "明早提醒我喝水",
  status: "failed",
  capabilities: ["reminder.set"],
  receipts: ["reminder.set:failed"],
};

const TL_FAILED = {
  task_id: "t-1",
  goal: "明早提醒我喝水",
  phase: "failed",
  status: "failed",
  current: {
    label: "执行失败",
    explanation: "执行器报告失败。",
    next_step: "可以重试；若反复失败请查看诊断。",
  },
  timeline: [
    { at: 1760000000, at_text: "10-09 10:00:00", kind: "running", text: "开始执行", detail: {} },
    { at: 1760000001, at_text: "10-09 10:00:01", kind: "failed", text: "执行失败", detail: { error: "boom" } },
  ],
  receipts: ["reminder.set:failed"],
  delivery: { state: "", attempts: 0, last_error: "", channel: "" },
  retry: {
    allowed: true,
    kind: "resume",
    reason: "失败任务可重试；重试走同一任务 ID（幂等键），不会产生重复外部动作。",
  },
  trace_ids: ["tr-1"],
  capabilities: ["reminder.set"],
};

const TL_UNKNOWN = {
  ...TL_FAILED,
  phase: "unknown",
  status: "unknown",
  current: {
    label: "状态未知",
    explanation: "执行结果未能确认（进程中断或渠道无回执）。",
    next_step: "先在诊断/Trace 里核实实际状态，再决定是否重试。",
  },
  timeline: [
    { at: 1760000000, at_text: "10-09 10:00:00", kind: "running", text: "开始执行", detail: {} },
  ],
  receipts: [],
  retry: {
    allowed: false,
    kind: "inspect",
    reason: "状态未知时不建议直接重试：先到诊断/Trace 核实。",
  },
  trace_ids: ["tr-7"],
};

const TL_REDELIVER = {
  ...TL_FAILED,
  phase: "delivery_failed",
  current: {
    label: "送达失败",
    explanation: "执行成功，但结果未能送达。",
    next_step: "可安全重发（只重投递，不重复执行工具）。",
  },
  delivery: { state: "failed", attempts: 2, last_error: "qq 通道超时", channel: "qq" },
  retry: {
    allowed: true,
    kind: "redeliver",
    reason: "只重新投递结果，不重复执行工具。",
  },
};

function mockTasks(timeline: unknown) {
  return mockStudioFetch({
    "/tasks": (call) => {
      if (call.url.includes("/timeline")) return jsonResult(timeline);
      if (call.method === "POST" && call.url.includes("/resume")) {
        return jsonResult({
          reply: "重试完成",
          status: "answered",
          receipts: [],
          task_id: "t-1",
        });
      }
      if (call.url.includes("/tasks/t-1")) return jsonResult(TASK);
      return jsonResult({ tasks: [TASK] });
    },
  });
}

beforeEach(() => {
  cleanup();
  setToken("test-token");
});

describe("Tasks 任务时间线（issue #43）", () => {
  it("展开时间线：拉取并渲染当前阶段、事件流水、回执与 Trace 链接", async () => {
    const mock = mockTasks(TL_FAILED);
    const onNavigate = vi.fn();
    render(<Tasks onNavigate={onNavigate} />);

    fireEvent.click(await screen.findByText("时间线"));

    // 当前阶段卡（label + 解释 + 下一步）；「执行失败」同时出现在徽标与事件行
    await waitFor(() =>
      expect(screen.getAllByText("执行失败").length).toBeGreaterThanOrEqual(2),
    );
    expect(screen.getByText("执行器报告失败。")).toBeTruthy();
    expect(screen.getByText(/下一步：可以重试/)).toBeTruthy();
    // 事件流水
    expect(screen.getByText("开始执行")).toBeTruthy();
    expect(screen.getByText(/boom/)).toBeTruthy();
    // 回执
    expect(screen.getAllByText("reminder.set:failed").length)
      .toBeGreaterThanOrEqual(1);
    // 拉取了正确端点
    const calls = fetchCalls(mock).filter((c) =>
      c.url.includes("/tasks/t-1/timeline"),
    );
    expect(calls.length).toBe(1);
    // Trace 链接跳事件页
    fireEvent.click(screen.getByText("tr-1"));
    expect(onNavigate).toHaveBeenCalledWith("events");
  });

  it("failed 任务：显示「重试」按钮并调用既有 resume 端点", async () => {
    const mock = mockTasks(TL_FAILED);
    render(<Tasks />);

    fireEvent.click(await screen.findByText("时间线"));
    const retryBtn = await screen.findByRole("button", { name: "重试" });
    fireEvent.click(retryBtn);

    await waitFor(() => {
      const post = fetchCalls(mock).find(
        (c) => c.method === "POST" && c.url.includes("/tasks/t-1/resume"),
      );
      expect(post).toBeTruthy();
    });
    await screen.findByText("重试完成");
    // 时间线重试理由说明
    expect(screen.getByText(/幂等键/)).toBeTruthy();
  });

  it("unknown 任务：不显示重试，给出「先到事件页核实」引导", async () => {
    mockTasks(TL_UNKNOWN);
    const onNavigate = vi.fn();
    render(<Tasks onNavigate={onNavigate} />);

    fireEvent.click(await screen.findByText("时间线"));
    await screen.findByText("状态未知：先到事件页核实。");
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
    fireEvent.click(screen.getByText("前往事件页"));
    expect(onNavigate).toHaveBeenCalledWith("events");
  });

  it("delivery_failed：last_error 红色展示，「重发结果」禁用并附说明", async () => {
    mockTasks(TL_REDELIVER);
    render(<Tasks />);

    fireEvent.click(await screen.findByText("时间线"));
    const btn = await screen.findByRole("button", {
      name: "重发结果（不重跑工具）",
    });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/只重新投递结果，不重复执行工具/)).toBeTruthy();
    expect(screen.getByText(/qq 通道超时/)).toBeTruthy();
    expect(screen.getByText(/尝试 2 次/)).toBeTruthy();
  });
});
