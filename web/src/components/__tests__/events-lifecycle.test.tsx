// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Events from "../Events";
import { api } from "../../api";

vi.mock("../../api", async original => ({
  ...await original<typeof import("../../api")>(),
  streamEvents: vi.fn(() => ({ close: vi.fn() })),
  api: { activityList: vi.fn(), tracesList: vi.fn(), tracesDetail: vi.fn() },
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}
const row = (id: string) => ({ trace_id: id, kind: "test", conversation_id: "c", started_at: 0, status: "replied", summary: id });
const list = { total: 150, limit: 50, offset: 0, traces: [row("one"), row("two")] };
const detail = (id: string) => ({ trace_id: id, spans: [{ span_id: id, phase: id + " phase", status: "ok", duration_ms: 1 }] });
function setup() {
  vi.mocked(api.activityList).mockResolvedValue({ total: 0, items: [] });
  vi.mocked(api.tracesList).mockResolvedValue(list as Awaited<ReturnType<typeof api.tracesList>>);
}

it("cancels activity reads when switching view", async () => {
  setup(); const job = pending<Awaited<ReturnType<typeof api.activityList>>>();
  vi.mocked(api.activityList).mockReturnValue(job.promise);
  render(<Events />);
  await waitFor(() => expect(api.activityList).toHaveBeenCalledOnce());
  const signal = vi.mocked(api.activityList).mock.calls[0][2]!;
  fireEvent.click(screen.getByRole("tab", { name: "事件流" }));
  expect(signal.aborted).toBe(true);
  await act(async () => { job.resolve({ total: 0, items: [] }); });
  expect(screen.queryByText("暂无活动记录。")).toBeNull();
});

it("isolates detail state when switching and collapsing rows", async () => {
  setup(); const old = pending<Awaited<ReturnType<typeof api.tracesDetail>>>();
  vi.mocked(api.tracesDetail).mockImplementation(id => id === "one" ? old.promise : Promise.resolve(detail(id) as Awaited<ReturnType<typeof api.tracesDetail>>));
  render(<Events />); fireEvent.click(screen.getByRole("tab", { name: "事件流" }));
  fireEvent.click(await screen.findByRole("button", { name: "one" }));
  await waitFor(() => expect(api.tracesDetail).toHaveBeenCalledOnce());
  const signal = vi.mocked(api.tracesDetail).mock.calls[0][1]!;
  fireEvent.click(screen.getByRole("button", { name: "two" }));
  expect(await screen.findByText("two phase")).toBeTruthy();
  expect(signal.aborted).toBe(true);
  await act(async () => { old.resolve(detail("one") as Awaited<ReturnType<typeof api.tracesDetail>>); });
  expect(screen.queryByText("one phase")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "two" }));
  expect(screen.queryByText("two phase")).toBeNull();
});

it("keeps paging on applied filters and page size until a new query is submitted", async () => {
  setup(); render(<Events />); fireEvent.click(screen.getByRole("tab", { name: "事件流" }));
  await screen.findByRole("button", { name: "one" });
  fireEvent.change(screen.getByLabelText("会话 ID"), { target: { value: "draft" } });
  fireEvent.change(screen.getByLabelText("每页条数"), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(api.tracesList).toHaveBeenLastCalledWith({ limit: 50, offset: 50 }, expect.any(AbortSignal)));
  await screen.findByRole("button", { name: "one" });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() => expect(api.tracesList).toHaveBeenLastCalledWith({ status: undefined, conversation_id: "draft", limit: 20, offset: 0 }, expect.any(AbortSignal)));
});

it("shows detail failures locally and can retry without reloading the list", async () => {
  setup(); vi.mocked(api.tracesDetail).mockRejectedValueOnce(new Error("detail unavailable"))
    .mockResolvedValue(detail("one") as Awaited<ReturnType<typeof api.tracesDetail>>);
  render(<Events />); fireEvent.click(screen.getByRole("tab", { name: "事件流" }));
  fireEvent.click(await screen.findByRole("button", { name: "one" }));
  fireEvent.click(await screen.findByRole("button", { name: "重试详情" }));
  expect(await screen.findByText("one phase")).toBeTruthy();
  expect(api.tracesList).toHaveBeenCalledOnce();
});
