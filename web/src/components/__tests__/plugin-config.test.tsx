// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Plugins from "../Plugins";
import { api } from "../../api";
vi.mock("../../api", async original => ({ ...await original<typeof import("../../api")>(),
  api: { plugins: vi.fn(), plugin: vi.fn(), setPluginConfig: vi.fn() },
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const plugin = { plugin_id: "custom", name: "Test config", kind: "output", version: "1", status: "enabled", enabled: true };
async function open(config: Record<string, unknown>) {
  vi.mocked(api.plugins).mockResolvedValue([plugin]);
  vi.mocked(api.plugin).mockResolvedValue({ ...plugin, config });
  vi.mocked(api.setPluginConfig).mockResolvedValue({ plugin_id: "custom", saved: true });
  render(<Plugins />); fireEvent.click(await screen.findByText("Test config"));
  await screen.findByRole("button", { name: "保存配置" });
}

it("does not write an untouched form or coerce numeric-looking strings", async () => {
  await open({ code: "123", secret: "***" });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await screen.findByText("配置没有改动。");
  expect(api.setPluginConfig).not.toHaveBeenCalled();
});

it("sends only edited fields and explicit removals, preserving untouched secrets and strings", async () => {
  await open({ count: 2, code: "123", secret: "***", obsolete: true });
  fireEvent.change(screen.getByLabelText("配置值 count"), { target: { value: "4" } });
  fireEvent.click(screen.getByLabelText("删除配置字段 obsolete"));
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(api.setPluginConfig).toHaveBeenCalledWith("custom", { count: 4, obsolete: null }));
});

it("rejects duplicate trimmed keys before making a write", async () => {
  await open({ count: 2 });
  fireEvent.click(screen.getByRole("button", { name: "添加一行" }));
  fireEvent.change(screen.getByLabelText("配置字段 2"), { target: { value: " count " } });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await screen.findByText("配置字段重复：count");
  expect(api.setPluginConfig).not.toHaveBeenCalled();
});

it("serializes special property names as data", async () => {
  await open({});
  fireEvent.click(screen.getByRole("button", { name: "添加一行" }));
  fireEvent.change(screen.getByLabelText("配置字段 1"), { target: { value: "__proto__" } });
  fireEvent.change(screen.getByLabelText("配置值 __proto__"), { target: { value: '{"value":1}' } });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(api.setPluginConfig).toHaveBeenCalledOnce());
  const patch = vi.mocked(api.setPluginConfig).mock.calls[0][1];
  expect(JSON.stringify(patch)).toBe('{"__proto__":{"value":1}}');
});

it("supports retry after configuration loading fails", async () => {
  vi.mocked(api.plugins).mockResolvedValue([plugin]);
  vi.mocked(api.plugin).mockRejectedValueOnce(new Error("unavailable")).mockResolvedValue({ ...plugin, config: {} });
  render(<Plugins />); fireEvent.click(await screen.findByText("Test config"));
  fireEvent.click(await screen.findByRole("button", { name: "重试配置" }));
  expect(await screen.findByRole("button", { name: "保存配置" })).toBeTruthy();
});
