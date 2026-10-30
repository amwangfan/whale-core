// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import App from "./App";
import Chat from "./components/Chat";
import { PluginConfigPanel } from "./components/Plugins";
import { api, ApiError, setToken } from "./api";

beforeEach(() => {
  localStorage.clear();
  setToken("");
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  setToken("");
});

describe("console behavior", () => {
  it("validates the token before exposing the workspace and reports rejection", async () => {
    vi.spyOn(api, "health").mockRejectedValue(
      new ApiError("unauthorized", "凭据不正确", 401),
    );
    render(<App />);
    fireEvent.change(screen.getByLabelText("访问凭据"), {
      target: { value: "invalid-test-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "进入工作空间" }));
    await screen.findByText("凭据不正确");
    expect(screen.queryByRole("navigation", { name: "主导航" })).toBeNull();
  });

  it("mounts only the selected page after successful connection", async () => {
    vi.spyOn(api, "health").mockResolvedValue({ ok: true });
    vi.spyOn(api, "status").mockResolvedValue({
      ok: true,
      version: "0.1.0",
      profile: "local",
      uptime_s: 0,
      plugins: { loaded: 12, enabled: 2 },
      memory: { episodes: 0, preferences: 0 },
      drafts_pending: 0,
    });
    vi.spyOn(api, "tasks").mockResolvedValue([]);
    vi.spyOn(api, "plugins").mockResolvedValue([]);
    const history = vi.spyOn(api, "history").mockResolvedValue({ history: [] });
    render(<App />);
    fireEvent.change(screen.getByLabelText("访问凭据"), {
      target: { value: "test-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "进入工作空间" }));
    await screen.findByRole("navigation", { name: "主导航" });
    expect(history).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "与鲸鱼对话" }));
    await waitFor(() => expect(history).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("你好，欢迎回来。")).toBeNull();
  });

  it("preserves an untouched masked key and sends schema-typed values", async () => {
    vi.spyOn(api, "plugin").mockResolvedValue({
      plugin_id: "local_model",
      name: "local",
      version: "1",
      kind: "model",
      status: "enabled",
      enabled: true,
      config: {
        base_url: "http://127.0.0.1/v1",
        model: "old",
        timeout: 25,
        api_key: "***",
      },
      config_schema: {
        properties: {
          base_url: { type: "string" },
          model: { type: "string" },
          timeout: { type: "number" },
          api_key: { type: "string", format: "secret" },
        },
      },
    });
    const save = vi
      .spyOn(api, "setPluginConfig")
      .mockResolvedValue({ saved: true, plugin_id: "local_model" });
    const onSaved = vi.fn();
    render(<PluginConfigPanel pluginId="local_model" onSaved={onSaved} />);
    await screen.findByLabelText("模型名称");
    fireEvent.change(screen.getByLabelText("模型名称"), {
      target: { value: "new-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(save).toHaveBeenCalledWith("local_model", {
      base_url: "http://127.0.0.1/v1",
      model: "new-model",
      timeout: 25,
    });
    expect(screen.getByLabelText(/API 密钥/).getAttribute("type")).toBe(
      "password",
    );
  });

  it("does not show success if applying a provider configuration fails", async () => {
    vi.spyOn(api, "plugin").mockResolvedValue({
      plugin_id: "local_model",
      name: "local",
      version: "1",
      kind: "model",
      status: "enabled",
      enabled: true,
      config: { model: "old" },
    });
    vi.spyOn(api, "setPluginConfig").mockRejectedValue(
      new Error("activation failed"),
    );
    const saved = vi.fn();
    render(<PluginConfigPanel pluginId="local_model" onSaved={saved} />);
    await screen.findByLabelText("模型名称");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByRole("alert");
    expect(saved).not.toHaveBeenCalled();
    expect(screen.queryByText("配置已保存，并已应用到后续调用。")).toBeNull();
  });

  it("does not submit while a Chinese IME composition is active", async () => {
    vi.spyOn(api, "history").mockResolvedValue({ history: [] });
    const chat = vi
      .spyOn(api, "chat")
      .mockResolvedValue({ reply: "你好", status: "answered", receipts: [] });
    render(<Chat />);
    const field = screen.getByLabelText("发送给鲸鱼的消息");
    await waitFor(() =>
      expect((field as HTMLTextAreaElement).disabled).toBe(false),
    );
    fireEvent.change(field, { target: { value: "你好" } });
    fireEvent.keyDown(field, { key: "Enter", isComposing: true });
    expect(chat).not.toHaveBeenCalled();
    fireEvent.keyDown(field, { key: "Enter", isComposing: false });
    await waitFor(() => expect(chat).toHaveBeenCalledWith("你好", "console"));
  });
});
