// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import RuntimeDiagnostics from "../RuntimeDiagnostics";
import { api } from "../../api";
vi.mock("../../api", () => ({ api: { devices: vi.fn(), diagnostics: vi.fn() } }));
const devices = { devices: [{ device_id: "dev1", label: "手机一号", status: "active", online: true }], retention: { state: "ok", scanned: 500, deleted: 2, more: true } };
const diagnostics = { checks: [], summary: {}, memory_worker: { state: "ok", queue_depth: 3, completed: 10 } };
beforeEach(() => {
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  vi.mocked(api.devices).mockResolvedValue(devices);
  vi.mocked(api.diagnostics).mockResolvedValue(diagnostics);
});
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers(); });
it("shows devices and real backend states", async () => {
  render(<RuntimeDiagnostics />);
  expect(await screen.findByText("手机一号")).toBeTruthy();
  expect(screen.getByText("仍有后续批次")).toBeTruthy();
  expect(screen.getByText("3 / 10")).toBeTruthy();
});
it("keeps one section useful when the other fails", async () => {
  vi.mocked(api.diagnostics).mockRejectedValue(new Error("服务暂不可用"));
  render(<RuntimeDiagnostics />);
  expect(await screen.findByText("服务暂不可用")).toBeTruthy();
  expect(screen.getByText("手机一号")).toBeTruthy();
});
it("does not block memory diagnostics on a hanging device query", async () => {
  vi.mocked(api.devices).mockReturnValue(new Promise(() => {}));
  render(<RuntimeDiagnostics />);
  expect(await screen.findByText("3 / 10")).toBeTruthy();
});
it("retains and labels stale data after a refresh failure", async () => {
  render(<RuntimeDiagnostics />);
  await screen.findByText("手机一号");
  vi.mocked(api.devices).mockRejectedValue(new Error("连接失败"));
  fireEvent.click(screen.getByRole("button", { name: /刷新状态|重新读取/ }));
  expect(await screen.findByText(/显示上次成功数据/)).toBeTruthy();
  expect(screen.getByText("手机一号")).toBeTruthy();
});
it("ignores late responses from an aborted refresh", async () => {
  let oldResolve!: (value: typeof devices) => void;
  vi.mocked(api.devices).mockReturnValueOnce(new Promise(resolve => { oldResolve = resolve; }));
  render(<RuntimeDiagnostics />);
  fireEvent.click(screen.getByRole("button", { name: /刷新状态|重新读取/ }));
  await screen.findByText("手机一号");
  await act(async () => oldResolve({ ...devices, devices: [] }));
  expect(screen.getByText("手机一号")).toBeTruthy();
});
it("bounds hung requests and stops polling on unmount", async () => {
  vi.useFakeTimers();
  vi.mocked(api.devices).mockReturnValue(new Promise(() => {}));
  const view = render(<RuntimeDiagnostics />);
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(screen.getByText("读取超时，请重试")).toBeTruthy();
  const calls = vi.mocked(api.devices).mock.calls.length;
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(api.devices).toHaveBeenCalledTimes(calls);
});
it("pauses while hidden and refreshes once on return", async () => {
  vi.useFakeTimers();
  render(<RuntimeDiagnostics />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  Object.defineProperty(document, "hidden", { configurable: true, value: true });
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(api.devices).toHaveBeenCalledTimes(1);
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(api.devices).toHaveBeenCalledTimes(2);
});
it("does not poll when automatic refresh is disabled", async () => {
  vi.useFakeTimers();
  render(<RuntimeDiagnostics />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.click(screen.getByRole("checkbox"));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const calls = vi.mocked(api.devices).mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(api.devices).toHaveBeenCalledTimes(calls);
});
