// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";
import { api, setToken, type RuntimeSettings } from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    streamEvents: vi.fn(() => ({ close: vi.fn() })),
    api: {
      ...actual.api,
      health: vi.fn(),
      settings: vi.fn(),
      saveSettings: vi.fn(),
      testProvider: vi.fn(),
      conversation: vi.fn(),
      status: vi.fn(),
      plugins: vi.fn(),
      diagnostics: vi.fn(),
      tasks: vi.fn(),
    },
  };
});

const initial: RuntimeSettings = {
  setup_completed: false,
  profile: "cloud",
  model: {
    base_url: "https://api.deepseek.com",
    model: "deepseek-chat",
    api_key_configured: false,
  },
  intent_model_enabled: false,
  capabilities: [],
  notes: [],
};

beforeEach(() => {
  setToken("");
  vi.mocked(api.health).mockResolvedValue({ ok: true });
  vi.mocked(api.settings).mockResolvedValue({ ...initial });
  vi.mocked(api.saveSettings).mockResolvedValue({ ...initial, setup_completed: true });
  vi.mocked(api.conversation).mockResolvedValue({ conversation_id: "console", messages: [] });
  vi.mocked(api.status).mockResolvedValue({
    ok: true, version: "0.1.0", profile: "cloud", uptime_s: 1,
    plugins: { loaded: 0, enabled: 0 },
    memory: { episodes: 0, preferences: 0 },
    drafts_pending: 0,
  });
  vi.mocked(api.plugins).mockResolvedValue([]);
  vi.mocked(api.diagnostics).mockResolvedValue({ checks: [], summary: {} });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  setToken("");
});

async function connect() {
  render(<App />);
  fireEvent.change(screen.getByLabelText("访问令牌"), {
    target: { value: "valid-console-token" },
  });
  fireEvent.click(screen.getByRole("button", { name: "连接鲸鱼" }));
  await screen.findByRole("heading", { name: "先连接你的 AI 模型" });
}

describe("first run setup", () => {
  it("prompts to configure model URL, API key, and optional settings once", async () => {
    await connect();
    expect(api.settings).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("API 接口地址")).toHaveProperty(
      "value", "https://api.deepseek.com",
    );
    fireEvent.change(screen.getByLabelText("API Key（必填）"), {
      target: { value: "private-model-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "下一步：扩展能力" }));
    expect(await screen.findByRole("heading", { name: "再选择需要的能力" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("搜索服务（可选）"), {
      target: { value: "tavily" },
    });
    fireEvent.change(screen.getByLabelText("搜索 API Key"), {
      target: { value: "search-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledOnce());
    expect(vi.mocked(api.saveSettings).mock.calls[0][0]).toMatchObject({
      setup_completed: true,
      profile: "cloud",
      model: {
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        api_key: "private-model-key",
      },
      services: { WHALE_SEARCH_PROVIDER: "tavily", TAVILY_API_KEY: "search-secret" },
    });
    expect(await screen.findByText("配置已保存。你可以继续配置，也可以开始使用。")).toBeTruthy();
    expect(api.testProvider).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    expect(await screen.findByRole("heading", { name: "运行总览" })).toBeTruthy();
  });

  it("skips existing installations without prompting", async () => {
    vi.mocked(api.settings).mockResolvedValueOnce({ ...initial, setup_completed: true });
    render(<App />);
    fireEvent.change(screen.getByLabelText("访问令牌"), {
      target: { value: "valid-console-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "连接鲸鱼" }));
    await screen.findByRole("heading", { name: "运行总览" });
    expect(screen.queryByRole("heading", { name: "先连接你的 AI 模型" })).toBeNull();
  });

  it("allows skipping without posting any API secrets or enabling features", async () => {
    await connect();
    fireEvent.click(screen.getByRole("button", { name: "暂时跳过引导" }));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith({ setup_completed: true }));
    expect(await screen.findByRole("heading", { name: "运行总览" })).toBeTruthy();
  });
});
