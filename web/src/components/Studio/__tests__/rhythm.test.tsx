// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Studio from "..";
import { setToken } from "../../../api";
import {
  FIXTURE,
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockMatchMedia,
  mockStudioFetch,
} from "./helpers";

installDomMocks();

const CONV_WITH_RHYTHM = {
  ...structuredClone(FIXTURE.conversations[0]),
  rhythm: { activity: "balanced", max_segments: 0, dnd: false },
  rhythm_version: 2,
};

beforeEach(() => {
  cleanup();
  setToken("test-token");
  mockMatchMedia((q) => q.includes("max-width")); // 列表模式
});

describe("会话节奏设置（G4，只影响回复节奏）", () => {
  it("列表模式渲染节奏编辑器，初值来自服务端会话视图", async () => {
    mockStudioFetch({
      "/studio/conversations": () =>
        jsonResult({ conversations: [structuredClone(CONV_WITH_RHYTHM)] }),
    });
    render(<Studio />);
    await screen.findByTestId("rhythm-editor");
    const activity = screen.getByLabelText(/活跃模式/) as HTMLSelectElement;
    expect(activity.value).toBe("balanced");
    const dnd = screen.getByLabelText(/免打扰/) as HTMLInputElement;
    expect(dnd.checked).toBe(false);
  });

  it("保存节奏走 PUT /rhythm（带版本号），成功后整体刷新", async () => {
    const fetchMock = mockStudioFetch({
      "/studio/conversations": () =>
        jsonResult({ conversations: [structuredClone(CONV_WITH_RHYTHM)] }),
      "/studio/conversations/qq%3Agroup%3Ag1/rhythm": () =>
        jsonResult({
          key: "qq:group:g1",
          rhythm: { activity: "quiet", max_segments: 1, dnd: true },
          version: 3,
          changed: true,
        }),
    });
    render(<Studio />);
    await screen.findByTestId("rhythm-editor");

    fireEvent.change(screen.getByLabelText(/活跃模式/), {
      target: { value: "quiet" },
    });
    fireEvent.click(screen.getByLabelText(/免打扰/));
    fireEvent.click(screen.getByText("保存节奏设置"));

    await waitFor(() => {
      const put = fetchCalls(fetchMock).find(
        (c) => c.method === "PUT" && c.url.includes("/rhythm"),
      );
      expect(put?.body).toEqual({
        activity: "quiet",
        max_segments: 0,
        dnd: true,
        expected_version: 2,
      });
    });
  });

  it("409 版本冲突提示并刷新（不乐观假成功）", async () => {
    const fetchMock = mockStudioFetch({
      "/studio/conversations": () =>
        jsonResult({ conversations: [structuredClone(CONV_WITH_RHYTHM)] }),
      "/studio/conversations/qq%3Agroup%3Ag1/rhythm": () =>
        new Response(
          JSON.stringify({
            error: {
              code: "version_conflict",
              message: "绑定已被并发修改；请读取最新版本后重试",
              current_version: 3,
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
    });
    render(<Studio />);
    await screen.findByTestId("rhythm-editor");
    fireEvent.click(screen.getByLabelText(/免打扰/));
    fireEvent.click(screen.getByText("保存节奏设置"));

    await screen.findByText(/并发修改/);
    // 冲突后重新拉取服务端事实源
    const calls = fetchCalls(fetchMock);
    expect(
      calls.some((c) => c.method === "GET" && c.url.includes("/studio/conversations")),
    ).toBe(true);
  });
});
