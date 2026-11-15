// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import MemoryView from "../Memory";
import { api, studioApi, type MemoryBrowseResult } from "../../api";

vi.mock("../../api", async original => ({
  ...await original<typeof import("../../api")>(),
  studioApi: { memorySubjects: vi.fn(), memoryBrowse: vi.fn() },
  api: { searchMemory: vi.fn() },
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
function pending<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; }
const space = (name: string) => ({ space_id: name, name, space_type: "owner_main", paused: false, record_count: 100, scope_note: "owner" });
const subjects = { owner: { spaces: [space("alpha"), space("beta")], vault_present: false }, members: [], groups: [], forks: [], legacy_unscoped_records: 0 };
const rows = (summary: string): MemoryBrowseResult => ({ space_ids: ["alpha"], total: 100, offset: 0, limit: 50,
  metadata_only: false, items: [{ key: summary, kind: "preference", summary, confidence: 0, superseded: false }] } as MemoryBrowseResult);
function setup() { vi.mocked(studioApi.memorySubjects).mockResolvedValue(subjects); }

it("cancels old subject browsing and ignores its late result after switching", async () => {
  setup(); const old = pending<MemoryBrowseResult>();
  vi.mocked(studioApi.memoryBrowse).mockImplementation(params => params.space_id === "alpha" ? old.promise : Promise.resolve(rows("beta record")));
  render(<MemoryView />);
  fireEvent.click(await screen.findByRole("button", { name: /alpha 100/ }));
  await waitFor(() => expect(studioApi.memoryBrowse).toHaveBeenCalledOnce());
  const signal = vi.mocked(studioApi.memoryBrowse).mock.calls[0][1]!;
  fireEvent.click(screen.getByRole("button", { name: /beta 100/ }));
  expect((await screen.findAllByText("beta record")).length).toBeGreaterThan(0);
  expect(signal.aborted).toBe(true);
  await act(async () => { old.resolve(rows("alpha late")); });
  expect(screen.queryByText("alpha late")).toBeNull();
});

it("hides previous-page controls and records while the new page loads", async () => {
  setup(); const second = pending<MemoryBrowseResult>();
  vi.mocked(studioApi.memoryBrowse).mockResolvedValueOnce(rows("page one"))
    .mockReturnValueOnce(second.promise).mockResolvedValue(rows("page two"));
  render(<MemoryView />); fireEvent.click(await screen.findByRole("button", { name: /alpha 100/ }));
  await screen.findAllByText("page one");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(screen.queryByText("page one")).toBeNull();
  expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
  await act(async () => { second.resolve(rows("page two")); });
  expect((await screen.findAllByText("page two")).length).toBeGreaterThan(0);
  expect(vi.mocked(studioApi.memoryBrowse).mock.calls[1][0].offset).toBe(50);
});

it("can refresh the subject catalog without redundantly reading unchanged selected records", async () => {
  setup(); vi.mocked(studioApi.memoryBrowse).mockResolvedValue(rows("saved record"));
  render(<MemoryView />); fireEvent.click(await screen.findByRole("button", { name: /alpha 100/ }));
  await screen.findAllByText("saved record");
  fireEvent.click(screen.getByRole("button", { name: "刷新主体" }));
  await waitFor(() => expect(studioApi.memorySubjects).toHaveBeenCalledTimes(2));
  await act(async () => {});
  expect(studioApi.memoryBrowse).toHaveBeenCalledOnce();
});

it("clears failed browsing and retries the same query", async () => {
  setup(); vi.mocked(studioApi.memoryBrowse).mockRejectedValueOnce(new Error("read unavailable"))
    .mockResolvedValue(rows("restored"));
  render(<MemoryView />); fireEvent.click(await screen.findByRole("button", { name: /alpha 100/ }));
  fireEvent.click(await screen.findByRole("button", { name: "重试浏览" }));
  await screen.findAllByText("restored");
  expect(screen.queryByText("read unavailable")).toBeNull();
});

it("aborts advanced search requests when unmounted", async () => {
  setup(); vi.mocked(api.searchMemory).mockReturnValue(new Promise(() => {}));
  const view = render(<MemoryView />); await screen.findByRole("button", { name: /alpha 100/ });
  fireEvent.click(screen.getByText("高级：全局关键词搜索（跨主体）"));
  fireEvent.change(screen.getByLabelText("全局搜索记忆"), { target: { value: "find me" } });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  await waitFor(() => expect(api.searchMemory).toHaveBeenCalledOnce());
  const signal = vi.mocked(api.searchMemory).mock.calls[0][1]!;
  view.unmount(); expect(signal.aborted).toBe(true);
});

it("aborts a pending catalog request when unmounted", async () => {
  vi.mocked(studioApi.memorySubjects).mockReturnValue(new Promise(() => {}));
  const view = render(<MemoryView />);
  await waitFor(() => expect(studioApi.memorySubjects).toHaveBeenCalledOnce());
  const signal = vi.mocked(studioApi.memorySubjects).mock.calls[0][0]!;
  view.unmount(); expect(signal.aborted).toBe(true);
});
