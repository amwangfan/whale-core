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
import Plugins from "./components/Plugins";
import Tasks from "./components/Tasks";
import Settings from "./components/Settings";
import ShellTerminal from "./components/ShellTerminal";
import Media, { artifactPaths, ArtifactPreview } from "./components/Media";
import Workflows from "./components/Workflows";
import { api, setToken } from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    streamEvents: vi.fn(() => ({ close: vi.fn() })),
    api: {
      ...actual.api,
      health: vi.fn(),
      conversation: vi.fn(),
      deleteConversation: vi.fn(),
      status: vi.fn(),
      tasks: vi.fn(),
      plugins: vi.fn(),
      plugin: vi.fn(),
      setPluginConfig: vi.fn(),
      chat: vi.fn(),
      decideTask: vi.fn(),
      resumeTask: vi.fn(),
      settings: vi.fn(),
      saveSettings: vi.fn(),
      diagnostics: vi.fn(),
      runCapability: vi.fn(),
      artifact: vi.fn(),
      skills: vi.fn(),
      workflowSettings: vi.fn(),
      saveWorkflowSettings: vi.fn(),
      life: vi.fn(),
      addInterest: vi.fn(),
      removeInterest: vi.fn(),
      lifeFeedback: vi.fn(),
    },
  };
});

beforeEach(() => {
  setToken("");
  vi.mocked(api.health).mockResolvedValue({ ok: true });
  vi.mocked(api.conversation).mockResolvedValue({
    conversation_id: "console",
    messages: [],
  });
  vi.mocked(api.status).mockResolvedValue({
    ok: true,
    version: "0.1.0",
    profile: "local",
    uptime_s: 42,
    plugins: { loaded: 1, enabled: 1 },
    memory: { episodes: 0, preferences: 0 },
    drafts_pending: 0,
  });
  vi.mocked(api.tasks).mockResolvedValue([]);
  vi.mocked(api.plugins).mockResolvedValue([]);
  vi.mocked(api.diagnostics).mockResolvedValue({ checks: [], summary: {} });
  vi.mocked(api.life).mockResolvedValue({ interests: [], diary: [] });
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  setToken("");
});

describe("console user flows", () => {
  it("validates the token before opening the workspace and only fetches active-page data", async () => {
    render(<App />);
    expect(api.status).not.toHaveBeenCalled();
    expect(api.plugins).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("访问令牌"), {
      target: { value: "test-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "连接鲸鱼" }));
    await screen.findByRole("heading", { level: 1, name: "运行总览" });
    expect(api.health).toHaveBeenCalledOnce();
    expect(api.plugins).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "插件与能力" }));
    await waitFor(() => expect(api.plugins).toHaveBeenCalledOnce());
  });

  it("shows connection failure instead of treating an arbitrary saved token as connected", async () => {
    vi.mocked(api.health).mockRejectedValueOnce(new Error("连接失败"));
    render(<App />);
    fireEvent.change(screen.getByLabelText("访问令牌"), {
      target: { value: "bad-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "连接鲸鱼" }));
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "连接失败",
    );
    expect(api.status).not.toHaveBeenCalled();
  });

  it("does not submit an Enter keystroke used to finish Chinese IME composition", async () => {
    vi.mocked(api.chat).mockResolvedValueOnce({
      reply: "收到",
      status: "answered",
      receipts: [],
    });
    render(
      <Chat msgs={[]} setMsgs={vi.fn()} sending={false} setSending={vi.fn()} />,
    );
    const input = screen.getByLabelText("消息内容");
    fireEvent.change(input, { target: { value: "你好" } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    expect(api.chat).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    await waitFor(() =>
      expect(api.chat).toHaveBeenCalledWith("你好", "console", undefined),
    );
  });

  it("retains untouched masked plugin secrets while saving configuration", async () => {
    vi.mocked(api.plugins).mockResolvedValue([
      {
        plugin_id: "search_web",
        name: "网页搜索",
        kind: "output",
        version: "1",
        status: "enabled",
        enabled: true,
      },
    ]);
    vi.mocked(api.plugin).mockResolvedValue({
      plugin_id: "search_web",
      name: "网页搜索",
      kind: "output",
      version: "1",
      status: "enabled",
      enabled: true,
      config: { api_key: "***", limit: 2 },
    });
    vi.mocked(api.setPluginConfig).mockResolvedValue({
      plugin_id: "search_web",
      saved: true,
    });
    render(<Plugins />);
    fireEvent.click(await screen.findByText("网页搜索"));  // 点卡片进详情页
    const key = await screen.findByLabelText("配置值 api_key");
    expect(key.getAttribute("type")).toBe("password");
    expect((key as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() =>
      expect(api.setPluginConfig).toHaveBeenCalledWith("search_web", { limit: 2 }),
    );
  });

  it("model 插件配置走专用界面（供应商/端点/模型/密钥）", async () => {
    vi.mocked(api.plugins).mockResolvedValue([
      {
        plugin_id: "deepseek",
        name: "回复模型（互动对话）",
        kind: "model",
        version: "1",
        status: "enabled",
        enabled: true,
      },
    ]);
    vi.mocked(api.settings).mockResolvedValue({
      profile: "cloud",
      model: { base_url: "https://api.deepseek.com", model: "deepseek-chat", api_key_configured: true },
      intent_model_enabled: false,
      capabilities: [],
      notes: [],
      models: {},
    });
    render(<Plugins />);
    fireEvent.click(await screen.findByText("回复模型（互动对话）"));  // 点卡片进详情页
    expect(await screen.findByText(/供应商与模型由你决定/)).toBeTruthy();
    expect(screen.getByPlaceholderText(/api.deepseek.com/)).toBeTruthy();
  });

  it("records approval before resuming a pending task", async () => {
    const task = {
      task_id: "task-1",
      at: 1,
      goal: "执行请求",
      status: "pending_approval",
      capabilities: ["shell.execute"],
      receipts: [],
    };
    vi.mocked(api.tasks).mockResolvedValue([task]);
    vi.mocked(api.decideTask).mockResolvedValue({
      ...task,
      status: "approved",
    });
    vi.mocked(api.resumeTask).mockResolvedValue({
      task_id: "task-1",
      reply: "执行成功",
      status: "answered",
      receipts: [],
    });
    render(<Tasks />);
    fireEvent.click(await screen.findByRole("button", { name: "批准并执行" }));
    await screen.findByText("执行成功");
    expect(api.decideTask).toHaveBeenCalledWith("task-1", "approve");
    expect(api.resumeTask).toHaveBeenCalledWith("task-1");
    expect(vi.mocked(api.decideTask).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(api.resumeTask).mock.invocationCallOrder[0],
    );
  });

  it("saves settings without overwriting an existing model secret with an empty string", async () => {
    const value = {
      profile: "cloud" as const,
      model: {
        base_url: "https://example.com/v1",
        model: "model",
        api_key_configured: true,
      },
      intent_model_enabled: false,
      capabilities: [],
      notes: [],
      services: { WHALE_IMAGE_API_KEY: true, WHALE_IMAGE_MODEL: "image" },
    };
    vi.mocked(api.settings).mockResolvedValue(value);
    vi.mocked(api.saveSettings).mockResolvedValue(value);
    render(<Settings onDisconnect={vi.fn()} />);
    await screen.findByDisplayValue("https://example.com/v1");
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledOnce());
    expect(
      vi.mocked(api.saveSettings).mock.calls[0][0].model,
    ).not.toHaveProperty("api_key");
  });
});

describe("media and workflow operations", () => {
  it("sends image parameters only when the configured capability is available", async () => {
    vi.mocked(api.settings).mockResolvedValue({
      profile: "local",
      model: {
        base_url: "http://localhost/v1",
        model: "test",
        api_key_configured: false,
      },
      intent_model_enabled: false,
      capabilities: ["media.image.generate"],
      notes: [],
    });
    vi.mocked(api.runCapability).mockResolvedValue({
      output: "generated",
      reply: "generated",
      status: "answered",
      receipts: [],
    });
    render(<Media />);
    await screen.findByText("已就绪");
    fireEvent.change(screen.getByLabelText("画面描述"), {
      target: { value: "一只鲸鱼" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成图片" }));
    await waitFor(() =>
      expect(api.runCapability).toHaveBeenCalledWith("media.image.generate", {
        prompt: "一只鲸鱼",
        size: "1024x1024",
      }),
    );
  });

  it("uses authenticated blob fetches for artifacts and revokes object URLs on unmount", async () => {
    const path = "/api/v1/artifacts/" + "a".repeat(32);
    const create = vi.fn().mockReturnValue("blob:private-image");
    const revoke = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }),
    );
    vi.mocked(api.artifact).mockResolvedValue(
      new Blob(["image"], { type: "image/png" }),
    );
    const rendered = render(<ArtifactPreview path={path} />);
    const image = await screen.findByAltText("鲸鱼生成的图片");
    expect(image.getAttribute("src")).toBe("blob:private-image");
    expect(api.artifact).toHaveBeenCalledWith(path, expect.any(AbortSignal));
    rendered.unmount();
    expect(revoke).toHaveBeenCalledWith("blob:private-image");
    vi.unstubAllGlobals();
  });

  it("only extracts local artifact references and deduplicates repeated references", () => {
    const path = "/api/v1/artifacts/" + "b".repeat(32);
    expect(
      artifactPaths(`https://outside.example/image.png ${path} ${path}`),
    ).toEqual([path]);
    expect(artifactPaths("/api/v1/artifacts/not-valid")).toEqual([]);
  });

  it("saves HH:MM quiet hours and scheduled exploration minutes as seconds", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    vi.mocked(api.workflowSettings).mockResolvedValue({
      paused: false,
      daily_limit: 5,
      quiet_start: "22:00",
      quiet_end: "07:00",
      timezone: "Asia/Shanghai",
      schedule_enabled: false,
      interval_s: 3600,
    });
    vi.mocked(api.saveWorkflowSettings).mockResolvedValue({
      paused: false,
      daily_limit: 5,
      quiet_start: "23:30",
      quiet_end: "07:00",
      timezone: "Asia/Shanghai",
      schedule_enabled: true,
      interval_s: 1800,
    });
    render(<Workflows />);
    fireEvent.change(await screen.findByLabelText("静默开始"), {
      target: { value: "23:30" },
    });
    fireEvent.click(screen.getByLabelText("定期自动探索"));
    fireEvent.change(screen.getByLabelText("探索间隔（分钟）"), {
      target: { value: "30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存主动行为设置" }));
    await waitFor(() =>
      expect(api.saveWorkflowSettings).toHaveBeenCalledWith({
        paused: false,
        daily_limit: 5,
        quiet_start: "23:30",
        quiet_end: "07:00",
        timezone: "Asia/Shanghai",
        schedule_enabled: true,
        interval_s: 1800,
      }),
    );
  });

  it("manages life interests and renders the exploration diary", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    vi.mocked(api.workflowSettings).mockResolvedValue({
      paused: false,
      daily_limit: 5,
      quiet_start: "22:00",
      quiet_end: "07:00",
      timezone: "Asia/Shanghai",
      schedule_enabled: false,
      interval_s: 3600,
    });
    vi.mocked(api.life).mockResolvedValue({
      interests: [{ topic: "AR 眼镜", weight: 1.2 }],
      diary: [
        {
          day: "2026-10-30",
          note: "探索窗口：审查外部供给内容 2 条。",
          sources: ["https://example.com/a"],
        },
      ],
    });
    vi.mocked(api.addInterest).mockResolvedValue({ topic: "机器人", weight: 1 });
    vi.mocked(api.lifeFeedback).mockResolvedValue({ saved: true });
    render(<Workflows />);
    await screen.findByText("AR 眼镜");
    expect(screen.getByText("兴趣权重 1.2")).toBeTruthy();
    expect(screen.getByText("探索窗口：审查外部供给内容 2 条。")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "阅读来源 ↗" }).getAttribute("href"),
    ).toBe("https://example.com/a");
    fireEvent.click(screen.getByRole("button", { name: "喜欢" }));
    await waitFor(() =>
      expect(api.lifeFeedback).toHaveBeenCalledWith("AR 眼镜", "liked"),
    );
    fireEvent.change(screen.getByLabelText("兴趣话题"), {
      target: { value: "机器人" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() =>
      expect(api.addInterest).toHaveBeenCalledWith("机器人"),
    );
  });

  it("removes a life interest through the same endpoint with the remove flag", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    vi.mocked(api.workflowSettings).mockResolvedValue({
      paused: false,
      daily_limit: 5,
      quiet_start: "22:00",
      quiet_end: "07:00",
      timezone: "Asia/Shanghai",
      schedule_enabled: false,
      interval_s: 3600,
    });
    vi.mocked(api.life).mockResolvedValue({
      interests: [{ topic: "电子音乐", weight: 0.8 }],
      diary: [],
    });
    vi.mocked(api.removeInterest).mockResolvedValue({ removed: true });
    render(<Workflows />);
    fireEvent.click(
      await screen.findByRole("button", { name: "移除" }),
    );
    await waitFor(() =>
      expect(api.removeInterest).toHaveBeenCalledWith("电子音乐"),
    );
  });
});

describe("persistent conversations and credential revocation", () => {
  it("restores persisted conversation history and retains it when switching pages", async () => {
    vi.mocked(api.conversation).mockResolvedValue({
      conversation_id: "console",
      messages: [
        { role: "user", content: "昨天的想法" },
        { role: "assistant", content: "我记得这个想法" },
      ],
    });
    render(<App />);
    fireEvent.change(screen.getByLabelText("访问令牌"), {
      target: { value: "token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "连接鲸鱼" }));
    await screen.findByRole("heading", { level: 1, name: "运行总览" });
    fireEvent.click(screen.getByRole("button", { name: "与鲸鱼对话" }));
    expect(await screen.findByText("我记得这个想法")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "插件与能力" }));
    fireEvent.click(screen.getByRole("button", { name: "与鲸鱼对话" }));
    expect(screen.getByText("昨天的想法")).toBeTruthy();
    expect(api.conversation).toHaveBeenCalledOnce();
  });

  it("asks for confirmation before deleting persisted conversation data", async () => {
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    vi.mocked(api.deleteConversation).mockResolvedValue({});
    render(
      <Chat msgs={[]} setMsgs={vi.fn()} sending={false} setSending={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "删除会话记录" }));
    expect(api.deleteConversation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除会话记录" }));
    await waitFor(() =>
      expect(api.deleteConversation).toHaveBeenCalledWith("console"),
    );
    expect(confirm.mock.calls[0][0]).toContain("导出备份");
    confirm.mockRestore();
  });

  it("only sends a null model key after explicitly selecting revocation", async () => {
    const value = {
      profile: "cloud" as const,
      model: {
        base_url: "https://example.com/v1",
        model: "model",
        api_key_configured: true,
      },
      intent_model_enabled: false,
      capabilities: [],
      notes: [],
      services: { TAVILY_API_KEY: true },
    };
    vi.mocked(api.settings).mockResolvedValue(value);
    vi.mocked(api.saveSettings).mockResolvedValue(value);
    render(<Settings onDisconnect={vi.fn()} />);
    fireEvent.click(await screen.findByLabelText("保存时清除当前模型密钥"));
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledOnce());
    expect(
      vi.mocked(api.saveSettings).mock.calls[0][0].model?.api_key,
    ).toBeNull();
  });
});

describe("controlled shell requests", () => {
  it("submits command parameters and clearly marks pending approval as not executed", async () => {
    vi.mocked(api.runCapability).mockResolvedValue({
      status: "pending_approval",
      output: "",
      reply: "需要审批",
      receipts: [],
      task_id: "shell-1",
    });
    render(<ShellTerminal available={true} />);
    fireEvent.change(screen.getByLabelText("待执行命令"), {
      target: { value: "pwd" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交执行请求" }));
    await screen.findByText(
      "请求已登记，命令尚未执行。请前往「任务中心」审批后继续执行。",
    );
    expect(api.runCapability).toHaveBeenCalledWith("shell.execute", {
      command: "pwd",
    });
  });
  it("keeps managed as the default and only submits personal after explicit selection", async () => {
    const settings = {
      profile: "local" as const,
      model: {
        base_url: "http://127.0.0.1/v1",
        model: "test",
        api_key_configured: false,
      },
      intent_model_enabled: false,
      capabilities: [],
      notes: [],
      services: { WHALE_SHELL_ENABLED: "1" },
    };
    vi.mocked(api.settings).mockResolvedValue(settings);
    vi.mocked(api.saveSettings).mockResolvedValue(settings);
    render(<Settings onDisconnect={vi.fn()} />);
    const tier = await screen.findByLabelText("Shell 权限档位");
    expect((tier as HTMLSelectElement).value).toBe("managed");
    fireEvent.change(tier, { target: { value: "personal" } });
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledOnce());
    expect(
      vi.mocked(api.saveSettings).mock.calls[0][0].services?.WHALE_SHELL_TIER,
    ).toBe("personal");
  });
});
