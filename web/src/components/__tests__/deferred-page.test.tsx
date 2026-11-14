// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { deferredPage } from "../DeferredPage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("waits until selected, shows loading, then forwards page props", async () => {
  let finish!: (module: { default: typeof Content }) => void;
  function Content({ label }: { label: string }) { return <p>{label}</p>; }
  const load = vi.fn(() => new Promise<{ default: typeof Content }>(resolve => { finish = resolve; }));
  const Page = deferredPage(load);
  expect(load).not.toHaveBeenCalled();
  render(<Page label="loaded content" />);
  expect(screen.getByRole("status").textContent).toContain("正在加载");
  finish({ default: Content });
  expect(await screen.findByText("loaded content")).toBeTruthy();
  expect(load).toHaveBeenCalledOnce();
});

it("keeps navigation usable on chunk failure and retries with a fresh loader", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const load = vi.fn()
    .mockRejectedValueOnce(new Error("private internal URL"))
    .mockResolvedValue({ default: () => <p>recovered</p> });
  const Page = deferredPage(load);
  const navigate = vi.fn();
  render(<><button onClick={navigate}>其他页面</button><Page /></>);
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.queryByText("private internal URL")).toBeNull();
  fireEvent.click(screen.getByText("其他页面"));
  expect(navigate).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText("重试页面"));
  expect(await screen.findByText("recovered")).toBeTruthy();
  expect(load).toHaveBeenCalledTimes(2);
});

it("contains page render failures and permits recovery", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  function Broken() { throw new Error("render failed"); return null; }
  const load = vi.fn()
    .mockResolvedValueOnce({ default: Broken })
    .mockResolvedValue({ default: () => <p>working again</p> });
  const Page = deferredPage(load);
  render(<Page />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByText("重试页面"));
  expect(await screen.findByText("working again")).toBeTruthy();
});
