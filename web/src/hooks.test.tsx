// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { API_READ_TIMEOUT_MS, useApi } from "./hooks";

afterEach(() => { cleanup(); vi.useRealTimers(); });
const flush = async () => { await act(async () => {}); };
function pending<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

it("cancels superseded reads and ignores late completion from an abort-ignoring loader", async () => {
  const first = pending<string>(), second = pending<string>();
  const signals: AbortSignal[] = [];
  const fn = vi.fn((signal: AbortSignal) => {
    signals.push(signal);
    return signals.length === 1 ? first.promise : second.promise;
  });
  const { result } = renderHook(() => useApi(fn));
  await flush();
  act(() => result.current.reload());
  await flush();
  expect(signals[0].aborted).toBe(true);
  second.resolve("new"); await flush();
  first.resolve("old"); await flush();
  expect(result.current.data).toBe("new");
  expect(result.current.loading).toBe(false);
});

it("times out a hanging read, retains the last success and recovers on reload", async () => {
  vi.useFakeTimers();
  const hanging = pending<string>();
  const signals: AbortSignal[] = [];
  const fn = vi.fn((signal: AbortSignal) => {
    signals.push(signal);
    return signals.length === 2 ? hanging.promise : Promise.resolve(signals.length === 1 ? "saved" : "fresh");
  });
  const { result } = renderHook(() => useApi(fn)); await flush();
  act(() => result.current.reload()); await flush();
  await act(async () => { vi.advanceTimersByTime(API_READ_TIMEOUT_MS); });
  expect(signals[1].aborted).toBe(true);
  expect(result.current.loading).toBe(false);
  expect(result.current.error).toContain("超时");
  expect(result.current.data).toBe("saved");
  hanging.resolve("late"); await flush();
  expect(result.current.data).toBe("saved");
  act(() => result.current.reload()); await flush();
  expect(result.current.data).toBe("fresh");
  expect(result.current.error).toBeNull();
  expect(vi.getTimerCount()).toBe(0);
});

it("aborts and releases timers on unmount, ignoring late reload calls", async () => {
  vi.useFakeTimers();
  const job = pending<string>();
  const fn = vi.fn((_signal: AbortSignal) => job.promise);
  const { result, unmount } = renderHook(() => useApi(fn)); await flush();
  const reload = result.current.reload;
  const signal = fn.mock.calls[0][0];
  unmount();
  expect(signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
  reload(); job.reject(new Error("late")); await flush();
  expect(fn).toHaveBeenCalledOnce();
});

it("turns synchronous loader exceptions into recoverable errors", async () => {
  const fn = vi.fn((): Promise<string> => { throw new Error("sync failure"); });
  const { result } = renderHook(() => useApi(fn)); await flush();
  expect(result.current.error).toBe("sync failure");
  expect(result.current.loading).toBe(false);
  fn.mockResolvedValue("recovered");
  act(() => result.current.reload()); await flush();
  expect(result.current.data).toBe("recovered");
});

it("cancels the old loader when dependencies change", async () => {
  const old = pending<string>();
  const fn = vi.fn((_signal: AbortSignal) => old.promise);
  const newer = vi.fn((_signal: AbortSignal) => Promise.resolve("changed"));
  const { result, rerender } = renderHook(({ load }) => useApi(load), { initialProps: { load: fn } });
  await flush(); rerender({ load: newer }); await flush();
  expect(fn.mock.calls[0][0].aborted).toBe(true);
  old.reject(new Error("obsolete failure")); await flush();
  expect(result.current.data).toBe("changed");
  expect(result.current.error).toBeNull();
});
