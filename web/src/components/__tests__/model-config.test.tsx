// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Plugins from "../Plugins";
import { api, type RuntimeSettings } from "../../api";
vi.mock("../../api", async original => ({ ...await original<typeof import("../../api")>(), api: {
  plugins: vi.fn(), settings: vi.fn(), saveSettings: vi.fn(), testPlugin: vi.fn(),
} }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const model = { plugin_id: "deepseek", name: "Model plugin", kind: "model", version: "1", status: "enabled", enabled: true };
const settings: RuntimeSettings = { profile: "cloud", model: { base_url: "https://model.example/v1", model: "existing", api_key_configured: true }, capabilities: [], notes: [], intent_model_enabled: false, models: { intent: { base_url: "https://intent.example/v1", model: "intent-existing" } } };
async function open() {
  vi.mocked(api.plugins).mockResolvedValue([model, { ...model, plugin_id: "llm_intent", name: "Intent plugin" }]);
  render(<Plugins />); fireEvent.click(await screen.findByText("Model plugin"));
}

it("does not render a writable empty form before existing configuration loads", async () => {
  vi.mocked(api.settings).mockReturnValue(new Promise(() => {})); await open();
  await screen.findByText("加载模型配置…");
  expect(screen.queryByRole("button", { name: "保存并生效" })).toBeNull();
  expect(api.saveSettings).not.toHaveBeenCalled();
});

it("shows load errors and retries before permitting save", async () => {
  vi.mocked(api.settings).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(settings); await open();
  fireEvent.click(await screen.findByRole("button", { name: "重试模型配置" }));
  expect(await screen.findByDisplayValue("https://model.example/v1")).toBeTruthy();
  expect((screen.getByLabelText("模型 API 密钥") as HTMLInputElement).value).toBe("");
});

it("cancels configuration loading on leaving the detail page", async () => {
  vi.mocked(api.settings).mockReturnValue(new Promise(() => {})); await open();
  await waitFor(() => expect(api.settings).toHaveBeenCalledOnce());
  const signal = vi.mocked(api.settings).mock.calls[0][0]!;
  fireEvent.click(screen.getByRole("button", { name: /返回插件列表/ }));
  expect(signal.aborted).toBe(true);
});

it("saves the selected role without overwriting its key and locks drafts during saving", async () => {
  vi.mocked(api.settings).mockResolvedValue(settings);
  vi.mocked(api.saveSettings).mockReturnValue(new Promise(() => {})); await open();
  await screen.findByDisplayValue("existing");
  fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "new-model" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并生效" }));
  await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith({ models: { chat: { base_url: "https://model.example/v1", model: "new-model" } } }));
  expect((screen.getByLabelText("模型名称") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText("模型 API 密钥") as HTMLInputElement).disabled).toBe(true);
});

it("starts a new role editor without carrying the previous role's unsaved key", async () => {
  vi.mocked(api.settings).mockResolvedValue(settings); await open();
  fireEvent.change(await screen.findByLabelText("模型 API 密钥"), { target: { value: "temporary-draft" } });
  fireEvent.click(screen.getByRole("button", { name: /返回插件列表/ }));
  fireEvent.click(await screen.findByText("Intent plugin"));
  await screen.findByDisplayValue("intent-existing");
  expect((screen.getByLabelText("模型 API 密钥") as HTMLInputElement).value).toBe("");
  expect(api.saveSettings).not.toHaveBeenCalled();
});
