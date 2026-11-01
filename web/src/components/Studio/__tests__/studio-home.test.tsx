// @vitest-environment jsdom
// issue #44：Studio 首页四区块 + 空状态 + 导航分层（默认组 / 高级组）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Studio from "..";
import { setToken } from "../../../api";
import {
  installDomMocks,
  jsonResult,
  mockMatchMedia,
  mockStudioFetch,
} from "./helpers";

installDomMocks();

const HOME_ROUTES = {
  "/activity": () =>
    jsonResult({
      total: 1,
      items: [
        {
          trace_id: "tr-1",
          at: 1760000000,
          status: "replied",
          lines: ["10:00:00 · 私聊里来了一条消息。", "结果：已回复。"],
        },
      ],
    }),
  "/tasks": () =>
    jsonResult({
      tasks: [
        {
          task_id: "t-1",
          at: 1760000000,
          goal: "审阅明早的提醒",
          status: "pending_approval",
          capabilities: [],
          receipts: [],
        },
        {
          task_id: "t-2",
          at: 1760000000,
          goal: "已完成的旧任务",
          status: "answered",
          capabilities: [],
          receipts: [],
        },
      ],
    }),
  "/health": () => jsonResult({ ok: true }),
  "/status": () =>
    jsonResult({
      ok: true,
      version: "0.2.0",
      profile: "local",
      uptime_s: 3660,
      plugins: { loaded: 2, enabled: 1 },
      memory: { episodes: 3, preferences: 2 },
      drafts_pending: 0,
    }),
};

beforeEach(() => {
  cleanup();
  setToken("test-token");
  mockMatchMedia(() => false); // 桌面
});

describe("Studio 首页与信息架构（issue #44）", () => {
  it("首页渲染四区块：近期活动 / 需要处理 / 连接状态 / 快捷入口", async () => {
    mockStudioFetch(HOME_ROUTES);
    const onNavigate = vi.fn();
    render(<Studio onNavigate={onNavigate} />);

    // 近期活动（自然语言行）
    await screen.findByText("结果：已回复。");
    expect(screen.getByText(/私聊里来了一条消息/)).toBeTruthy();

    // 需要处理：只有待审批/失败任务
    await screen.findByText("审阅明早的提醒");
    expect(screen.getByText("待审批")).toBeTruthy();
    expect(screen.queryByText("已完成的旧任务")).toBeNull();

    // 连接状态
    await screen.findByText("核心已连接");
    expect(screen.getByText(/0\.2\.0/)).toBeTruthy();
    expect(screen.getByText(/记忆 5 条/)).toBeTruthy();

    // 快捷入口 → 跨页跳转 / 打开人物新建
    fireEvent.click(screen.getByRole("button", { name: "查看任务" }));
    expect(onNavigate).toHaveBeenCalledWith("tasks");
    fireEvent.click(screen.getByRole("button", { name: "查看记忆" }));
    expect(onNavigate).toHaveBeenCalledWith("memory");
    fireEvent.click(screen.getByRole("button", { name: "主动行为" }));
    expect(onNavigate).toHaveBeenCalledWith("initiative");

    // 新建人物卡 → 切到人物视图并自动打开新建表单
    fireEvent.click(screen.getByRole("button", { name: "新建人物卡" }));
    await waitFor(() =>
      expect(screen.getByLabelText(/标题/)).toBeTruthy(),
    );
    expect(screen.getByLabelText(/简短介绍/)).toBeTruthy();
  });

  it("空状态：无活动、无待处理时各有明确文案", async () => {
    mockStudioFetch({
      "/activity": () => jsonResult({ total: 0, items: [] }),
      "/tasks": () => jsonResult({ tasks: [] }),
      "/health": () => jsonResult({ ok: true }),
      "/status": HOME_ROUTES["/status"],
    });
    render(<Studio />);

    await screen.findByText("暂无活动记录。");
    await screen.findByText("没有待处理的事项。");
    await screen.findByText("核心已连接");
  });

  it("导航分层：默认组七个入口 + 高级组工程项；图谱/会话视图可达", async () => {
    mockStudioFetch(HOME_ROUTES);
    const onNavigate = vi.fn();
    render(<Studio onNavigate={onNavigate} />);

    // 默认导航组
    for (const name of ["概览", "人物", "会话", "记忆", "主动性", "任务", "事件"]) {
      expect(screen.getByRole("button", { name })).toBeTruthy();
    }
    // 高级分组（默认折叠，项仍在 DOM）
    expect(screen.getByText("高级")).toBeTruthy();
    for (const name of ["图谱", "记忆空间", "设备", "插件", "系统设置"]) {
      expect(screen.getByRole("button", { name })).toBeTruthy();
    }

    // 跨页项走 onNavigate
    fireEvent.click(screen.getByRole("button", { name: "事件" }));
    expect(onNavigate).toHaveBeenCalledWith("events");
    fireEvent.click(screen.getByRole("button", { name: "插件" }));
    expect(onNavigate).toHaveBeenCalledWith("plugins");

    // 图谱（高级组）→ 画布仍可达（现有功能不丢）
    fireEvent.click(screen.getByRole("button", { name: "图谱" }));
    await screen.findByTestId("studio-canvas");

    // 会话（默认组）→ 列表模式
    fireEvent.click(screen.getByRole("button", { name: "会话" }));
    await screen.findByTestId("studio-list");
  });
});
