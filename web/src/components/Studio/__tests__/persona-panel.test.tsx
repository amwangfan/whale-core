// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import PersonaPanel from "../PersonaPanel";
import { setToken, type StudioPersona } from "../../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonError,
  jsonResult,
  mockStudioFetch,
} from "./helpers";

installDomMocks();

const PERSONAS: StudioPersona[] = [
  { id: "default", title: "冷静鲸鱼（内置默认）", name: "鲸鱼", builtin: true },
  {
    id: "custom-abc",
    title: "我的卡",
    name: "小鲸",
    builtin: false,
    summary: "旧设定",
  },
];

function renderPanel(overrides: Record<string, unknown> = {}) {
  const onChanged = vi.fn();
  const onError = vi.fn();
  render(
    <PersonaPanel
      personas={PERSONAS}
      busy={false}
      onChanged={onChanged}
      onError={onError}
      {...overrides}
    />,
  );
  return { onChanged, onError };
}

beforeEach(() => {
  cleanup();
  setToken("test-token");
});

describe("PersonaPanel 人物卡管理", () => {
  it("列表渲染：内置卡无「编辑」，自定义卡有「编辑」，都有「复制为新卡」", () => {
    mockStudioFetch();
    renderPanel();
    expect(screen.getByText("冷静鲸鱼（内置默认）")).toBeTruthy();
    expect(screen.getByText("我的卡")).toBeTruthy();
    expect(screen.getAllByText("复制为新卡")).toHaveLength(2);
    expect(screen.getAllByText("编辑")).toHaveLength(1); // 只有自定义卡可编辑
    expect(screen.getByText("新建人物卡")).toBeTruthy();
  });

  it("新建：填标题后保存 → POST /studio/personas，成功后回调刷新", async () => {
    const mock = mockStudioFetch({
      "/studio/personas": (call) => {
        const body = (call.body ?? {}) as Record<string, unknown>;
        if (call.method === "POST") {
          return jsonResult({
            persona: { id: "custom-xyz", title: body.title },
          });
        }
        return jsonResult({ personas: PERSONAS });
      },
    });
    const { onChanged } = renderPanel();

    fireEvent.click(screen.getByText("新建人物卡"));
    const titleInput = screen.getByLabelText(/标题/);
    fireEvent.change(titleInput, { target: { value: "深夜陪伴卡" } });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(onChanged).toHaveBeenCalled(), {
      timeout: 5000,
    });
    const post = fetchCalls(mock).find(
      (c) => c.method === "POST" && c.url.includes("/studio/personas"),
    );
    expect(post).toBeTruthy();
    const postBody = post!.body as Record<string, unknown>;
    expect(postBody.title).toBe("深夜陪伴卡");
    expect(postBody.card_id).toBeUndefined(); // 留空 → 服务端自动生成
    expect(onChanged.mock.calls[0][0]).toContain("custom-xyz");
  });

  it("新建：标题为空时本地拦截，不发请求", async () => {
    const mock = mockStudioFetch();
    renderPanel();
    fireEvent.click(screen.getByText("新建人物卡"));
    // 清空角色名以外的字段，标题留空（required + 本地校验）
    fireEvent.change(screen.getByLabelText(/标题/), { target: { value: "  " } });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() =>
      expect(screen.getByText("标题不能为空。")).toBeTruthy(),
    );
    expect(
      fetchCalls(mock).filter((c) => c.method === "POST"),
    ).toHaveLength(0);
  });

  it("编辑：先拉详情回填，保存走 PUT，字段原样提交", async () => {
    const detail = {
      id: "custom-abc",
      title: "我的卡",
      name: "小鲸",
      builtin: false,
      summary: "旧设定",
      tone: "温柔",
      speaking_rate: 1.1,
      style_rules: ["短句"],
      extra_taboos: [],
      external_prompt: "原文",
    };
    const mock = mockStudioFetch({
      "/studio/personas/custom-abc": (call) => {
        if (call.method === "GET") return jsonResult({ persona: detail });
        if (call.method === "PUT") {
          return jsonResult({
            persona: { ...detail, ...(call.body as Record<string, unknown>) },
          });
        }
        return jsonError(405, "method_not_allowed", "x");
      },
    });
    const { onChanged } = renderPanel();

    fireEvent.click(screen.getByText("编辑"));
    // 等详情回填
    await waitFor(
      () =>
        expect(
          (screen.getByLabelText(/简短介绍/) as HTMLTextAreaElement).value,
        ).toBe("旧设定"),
      { timeout: 5000 },
    );
    fireEvent.change(screen.getByLabelText(/简短介绍/), {
      target: { value: "新设定" },
    });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(onChanged).toHaveBeenCalled(), {
      timeout: 5000,
    });
    const put = fetchCalls(mock).find((c) => c.method === "PUT");
    expect(put).toBeTruthy();
    expect(put!.url).toContain("/studio/personas/custom-abc");
    const putBody = put!.body as Record<string, unknown>;
    expect(putBody.summary).toBe("新设定");
    expect(putBody.tone).toBe("温柔"); // 未改字段随表单原样提交
  });

  it("保存被拒（如内置卡 403）时错误显示在表单内，不静默", async () => {
    mockStudioFetch({
      "/studio/personas/custom-abc": (call) => {
        if (call.method === "GET") {
          return jsonResult({
            persona: { id: "custom-abc", title: "我的卡", builtin: false },
          });
        }
        return jsonError(403, "builtin_immutable", "内置卡不可修改");
      },
    });
    renderPanel();
    fireEvent.click(screen.getByText("编辑"));
    await waitFor(
      () => expect(screen.getByText("保存")).toBeTruthy(),
      { timeout: 5000 },
    );
    fireEvent.click(screen.getByText("保存"));
    await waitFor(
      () => expect(screen.getByText("内置卡不可修改")).toBeTruthy(),
      { timeout: 5000 },
    );
  });

  it("生效预览：拉取 effective 快照并展示模型上下文与字段徽标", async () => {
    mockStudioFetch({
      "/studio/personas/custom-abc/effective": () =>
        jsonResult({
          effective: {
            persona_id: "custom-abc",
            card_title: "我的卡",
            source: "conversation_bound",
            model_context: "你是「小鲸」。\n设定：旧设定\n表达规则：\n- 先给结论",
            voice: { timbre_hint: "bright", speaking_rate: 1.1 },
            content_sha: "abc123def456",
            fallback_reason: "",
            dispositions: [
              { field: "name", destination: "model_context", applied: "", used_value: true },
              { field: "voice_hint", destination: "tts_only", applied: "", used_value: true },
            ],
          },
        }),
    });
    renderPanel();
    const buttons = screen.getAllByText("生效预览");
    fireEvent.click(buttons[1]); // custom-abc 行
    await waitFor(() =>
      expect(screen.getByText(/你是「小鲸」/)).toBeTruthy(),
    );
    expect(screen.getByText("abc123def456")).toBeTruthy();
    expect(screen.getByText(/会话绑定卡/)).toBeTruthy();
  });

  it("编辑表单：每个字段标注去向徽标（模型上下文/仅语音/元数据）", async () => {
    mockStudioFetch({
      "/studio/personas/custom-abc": () =>
        jsonResult({
          persona: {
            id: "custom-abc",
            title: "我的卡",
            name: "小鲸",
            builtin: false,
            summary: "旧设定",
          },
        }),
    });
    renderPanel();
    fireEvent.click(screen.getByText("编辑"));
    await waitFor(() => screen.getByText("保存"));
    expect(screen.getAllByText("模型上下文").length).toBeGreaterThanOrEqual(8);
    expect(screen.getAllByText("仅语音").length).toBe(2); // 语速 + 音色提示
    expect(screen.getAllByText("元数据").length).toBeGreaterThanOrEqual(6);
  });

});
