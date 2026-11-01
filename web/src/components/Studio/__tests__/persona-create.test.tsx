// @vitest-environment jsdom
// issue #37：人物卡快速创建（表单分区/模板）、导入预览、即时试玩、草稿恢复。
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import PersonaPanel from "../PersonaPanel";
import { setToken, type StudioPersona } from "../../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockStudioFetch,
} from "./helpers";

installDomMocks();

const PERSONAS: StudioPersona[] = [
  { id: "default", title: "冷静鲸鱼（内置默认）", name: "鲸鱼", builtin: true },
];

function renderPanel() {
  const onChanged = vi.fn();
  const onError = vi.fn();
  const rendered = render(
    <PersonaPanel
      personas={PERSONAS}
      busy={false}
      onChanged={onChanged}
      onError={onError}
    />,
  );
  return { onChanged, onError, container: rendered.container };
}

beforeEach(() => {
  cleanup();
  setToken("test-token");
  localStorage.clear();
});

describe("PersonaPanel 快速创建 / 导入 / 试玩 / 草稿（issue #37）", () => {
  it("新建表单：默认区只有 5+1 个字段，无预制角色模板", async () => {
    mockStudioFetch();
    const { container } = renderPanel();
    fireEvent.click(screen.getByText("新建人物卡"));

    const basic = container.querySelector(".persona-form-basic");
    expect(basic).toBeTruthy();
    const zone = within(basic as HTMLElement);
    // 默认区：标题 + 角色名/简短介绍/性格特点/相处方式/示例对话
    expect(zone.getByLabelText(/标题/)).toBeTruthy();
    expect(zone.getByLabelText(/角色名/)).toBeTruthy();
    expect(zone.getByLabelText(/简短介绍/)).toBeTruthy();
    expect(zone.getByLabelText(/性格特点/)).toBeTruthy();
    expect(zone.getByLabelText(/相处方式/)).toBeTruthy();
    expect(zone.getByLabelText(/示例对话/)).toBeTruthy();
    // 进阶字段不在默认区
    expect(zone.queryByLabelText(/卡 ID/)).toBeNull();
    expect(zone.queryByLabelText(/音色提示/)).toBeNull();
    expect(zone.queryByLabelText(/人设原文/)).toBeNull();

    // 进阶设置折叠（details 无 open），字段仍在 DOM 中
    const adv = container.querySelector("details.persona-form-advanced");
    expect(adv).toBeTruthy();
    expect(adv!.hasAttribute("open")).toBe(false);
    expect(screen.getByText("进阶设置")).toBeTruthy();
    expect(screen.getByLabelText(/卡 ID/)).toBeTruthy();

    // 核心不提供任何预制人格或起步模板。
    expect(screen.queryByLabelText("从模板开始")).toBeNull();
  });

  it("导入预览：解析展示映射/字段值，确认导入后填进表单并随保存提交", async () => {
    const mock = mockStudioFetch({
      "/studio/personas/import-preview": () =>
        jsonResult({
          parsed: {
            name: "小鲸",
            summary: "来自导入的简介",
            temperament: "安静温和",
            relationship_style: "陪伴型",
            example_dialogue: "用户：嗨\n小鲸：嗨呀",
            external_prompt: "原文……",
          },
          suggested_card_id: "xiao-jing",
          mapping: {
            "data.description": "summary",
            "data.personality": "temperament",
          },
          unmapped: [
            {
              field: "data.creator_notes",
              note: "无对应字段，未导入",
              preview: "创作者备注…",
            },
          ],
          missing: [],
          conflicts: [],
          warnings: [],
          requires_confirmation: true,
        }),
      "/studio/personas": (call) => {
        if (call.method === "POST") {
          const body = (call.body ?? {}) as Record<string, unknown>;
          return jsonResult({
            persona: { id: String(body.card_id ?? "auto"), title: body.title },
          });
        }
        return jsonResult({ personas: PERSONAS });
      },
    });
    const { onChanged } = renderPanel();
    fireEvent.click(screen.getByText("新建人物卡"));

    fireEvent.change(screen.getByLabelText("导入文本"), {
      target: { value: '{"data":{"description":"来自导入的简介"}}' },
    });
    fireEvent.click(screen.getByText("解析预览"));

    // 预览：映射表 + 解析值 + 未映射列表
    await screen.findByText("data.description");
    expect(screen.getByText("data.personality")).toBeTruthy();
    expect(screen.getAllByText("summary").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("来自导入的简介")).toBeTruthy();
    expect(screen.getByText("data.creator_notes")).toBeTruthy();
    expect(screen.getByText(/无对应字段，未导入/)).toBeTruthy();

    // 预览请求体
    const previewCall = fetchCalls(mock).find((c) =>
      c.url.includes("/studio/personas/import-preview"),
    );
    expect(previewCall).toBeTruthy();
    expect(
      ((previewCall!.body ?? {}) as Record<string, unknown>).text,
    ).toContain("来自导入的简介");

    // 确认导入 → 表单填入
    fireEvent.click(screen.getByText("确认导入"));
    expect((screen.getByLabelText(/角色名/) as HTMLInputElement).value).toBe(
      "小鲸",
    );
    expect((screen.getByLabelText(/简短介绍/) as HTMLTextAreaElement).value).toBe(
      "来自导入的简介",
    );
    expect(
      (screen.getByLabelText(/示例对话/) as HTMLTextAreaElement).value,
    ).toBe("用户：嗨\n小鲸：嗨呀");

    // 保存 → POST body 带导入字段与建议卡 ID
    fireEvent.change(screen.getByLabelText(/标题/), {
      target: { value: "导入的卡" },
    });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(onChanged).toHaveBeenCalled(), {
      timeout: 5000,
    });
    const post = fetchCalls(mock).find(
      (c) =>
        c.method === "POST" &&
        c.url.includes("/studio/personas") &&
        !c.url.includes("import-preview"),
    );
    expect(post).toBeTruthy();
    const body = post!.body as Record<string, unknown>;
    expect(body.name).toBe("小鲸");
    expect(body.summary).toBe("来自导入的简介");
    expect(body.example_dialogue).toBe("用户：嗨\n小鲸：嗨呀");
    expect(body.relationship_style).toBe("陪伴型");
    expect(body.card_id).toBe("xiao-jing");
  });

  it("试玩：以表单草稿为 card 调 playtest，渲染默认人格 vs 当前卡对比", async () => {
    const mock = mockStudioFetch({
      "/studio/personas/playtest": (call) => {
        const body = (call.body ?? {}) as Record<string, unknown>;
        return jsonResult({
          input: String(body.input ?? "固定场景"),
          baseline: {
            persona_id: "default",
            source: "builtin_default",
            content_sha: "sha-base-1",
            reply: "基线回复：听起来不错。",
            fields_used: ["name"],
          },
          trial: {
            persona_id: "playtest",
            source: "unsaved_draft",
            content_sha: "sha-trial-1",
            reply: "试玩回复：好呀，几点出发？",
            fields_used: ["name", "summary", "example_dialogue"],
          },
          note: "两侧均无记忆/历史注入；试玩不写记忆、不进会话历史",
        });
      },
    });
    renderPanel();
    fireEvent.click(screen.getByText("新建人物卡"));

    fireEvent.change(screen.getByLabelText("试玩输入"), {
      target: { value: "周末一起去爬山吗？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "试玩" }));

    await screen.findByText("试玩回复：好呀，几点出发？");
    expect(screen.getByText("基线回复：听起来不错。")).toBeTruthy();
    expect(screen.getByText("当前生效配置（可能无角色）")).toBeTruthy();
    expect(screen.getByText("当前卡（草稿）")).toBeTruthy();
    expect(screen.getByText("sha-base-1")).toBeTruthy();
    expect(screen.getByText("sha-trial-1")).toBeTruthy();
    // 生效字段标签
    expect(screen.getByText("example_dialogue")).toBeTruthy();
    expect(screen.getAllByText("summary").length).toBeGreaterThanOrEqual(1);

    // 请求体：card 草稿 + 自定义输入
    const call = fetchCalls(mock).find((c) =>
      c.url.includes("/studio/personas/playtest"),
    );
    expect(call).toBeTruthy();
    const body = call!.body as Record<string, unknown>;
    expect(body.input).toBe("周末一起去爬山吗？");
    const card = body.card as Record<string, unknown>;
    expect(card).toBeTruthy();
    expect(card.name).toBe(""); // 新建卡不预选角色身份
  });

  it("草稿：编辑中途取消后重开表单提示恢复/放弃，恢复后内容回填", async () => {
    mockStudioFetch();
    renderPanel();

    fireEvent.click(screen.getByText("新建人物卡"));
    fireEvent.change(screen.getByLabelText(/简短介绍/), {
      target: { value: "草稿内容：还没写完" },
    });
    // 自动存草稿
    await waitFor(() =>
      expect(
        localStorage.getItem("whale.persona-draft.new") ?? "",
      ).toContain("草稿内容"),
    );
    fireEvent.click(screen.getByText("取消"));

    // 重新打开 → 提示恢复草稿
    fireEvent.click(screen.getByText("新建人物卡"));
    await screen.findByText(/检测到未保存的草稿/);
    fireEvent.click(screen.getByText("恢复草稿"));
    expect(
      (screen.getByLabelText(/简短介绍/) as HTMLTextAreaElement).value,
    ).toBe("草稿内容：还没写完");

    // 放弃草稿路径：改内容产生新草稿 → 重开 → 放弃 → 回到初始空表单
    fireEvent.change(screen.getByLabelText(/性格特点/), {
      target: { value: "另一笔草稿" },
    });
    await waitFor(() =>
      expect(
        localStorage.getItem("whale.persona-draft.new") ?? "",
      ).toContain("另一笔草稿"),
    );
    fireEvent.click(screen.getByText("取消"));
    fireEvent.click(screen.getByText("新建人物卡"));
    await screen.findByText(/检测到未保存的草稿/);
    fireEvent.click(screen.getByText("放弃草稿"));
    expect(localStorage.getItem("whale.persona-draft.new")).toBeNull();
    expect(
      (screen.getByLabelText(/简短介绍/) as HTMLTextAreaElement).value,
    ).toBe("");
  });
});
