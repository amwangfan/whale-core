// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Tasks from "../Tasks";
import { api, type TaskTimeline } from "../../api";
vi.mock("../../api", async original => ({ ...await original<typeof import("../../api")>(), api: {
  tasks: vi.fn(), task: vi.fn(), taskTimeline: vi.fn(), cancelTask: vi.fn(),
} }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const task = { task_id: "one", at: 1, goal: "list goal", status: "running", capabilities: [], receipts: [] };
const timeline = (label: string): TaskTimeline => ({ task_id: "one", goal: "list goal", status: "running", capabilities: [], phase: "running", current: { label, explanation: "", next_step: "" }, timeline: [], receipts: [], delivery: { state: "", attempts: 0, last_error: "", channel: "" }, retry: { allowed: false, kind: "none", reason: "" }, trace_ids: [] });
function setup() { vi.mocked(api.tasks).mockResolvedValue([task]); }

it("cancels pending details when collapsed and ignores late data", async () => {
  setup(); let finish!: (value: typeof task) => void;
  vi.mocked(api.task).mockReturnValue(new Promise(resolve => { finish = resolve; }));
  render(<Tasks />); fireEvent.click(await screen.findByRole("button", { name: "详情" }));
  await waitFor(() => expect(api.task).toHaveBeenCalledOnce());
  const signal = vi.mocked(api.task).mock.calls[0][1]!;
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  expect(signal.aborted).toBe(true);
  await act(async () => { finish({ ...task, goal: "late detail" }); });
  expect(screen.queryByText("目标：late detail")).toBeNull();
});

it("refreshes open details and timeline together instead of retaining cached data", async () => {
  setup(); vi.mocked(api.task).mockResolvedValueOnce({ ...task, goal: "old detail" }).mockResolvedValue({ ...task, goal: "new detail" });
  vi.mocked(api.taskTimeline).mockResolvedValueOnce(timeline("old timeline")).mockResolvedValue(timeline("new timeline"));
  render(<Tasks />); fireEvent.click(await screen.findByRole("button", { name: "详情" }));
  fireEvent.click(screen.getByRole("button", { name: "时间线" }));
  await screen.findByText("目标：old detail"); await screen.findByText("old timeline");
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("目标：new detail"); await screen.findByText("new timeline");
  expect(api.task).toHaveBeenCalledTimes(2); expect(api.taskTimeline).toHaveBeenCalledTimes(2);
});

it("invalidates the open timeline after a task action", async () => {
  setup(); vi.mocked(api.cancelTask).mockResolvedValue({ ...task, status: "cancelled" });
  vi.mocked(api.taskTimeline).mockResolvedValueOnce(timeline("running before action")).mockResolvedValue(timeline("cancelled after action"));
  render(<Tasks />); fireEvent.click(await screen.findByRole("button", { name: "时间线" }));
  await screen.findByText("running before action"); fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(await screen.findByText("cancelled after action")).toBeTruthy();
});

it("retries failed details without issuing an action or refreshing the task list", async () => {
  setup(); vi.mocked(api.task).mockRejectedValueOnce(new Error("unavailable")).mockResolvedValue(task);
  render(<Tasks />); fireEvent.click(await screen.findByRole("button", { name: "详情" }));
  fireEvent.click(await screen.findByRole("button", { name: "重试详情" }));
  expect(await screen.findByText("目标：list goal")).toBeTruthy();
  expect(api.tasks).toHaveBeenCalledOnce(); expect(api.cancelTask).not.toHaveBeenCalled();
});
