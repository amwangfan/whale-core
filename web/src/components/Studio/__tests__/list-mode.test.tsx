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
  fetchCalls,
  installDomMocks,
  mockMatchMedia,
  mockStudioFetch,
  jsonResult,
} from "./helpers";

installDomMocks();

beforeEach(() => {
  cleanup();
  setToken("test-token");
});

describe("窄屏列表模式（<768px 退化）", () => {
  it("max-width 命中时渲染列表模式，否则渲染画布", async () => {
    mockMatchMedia((q) => q.includes("max-width"));
    mockStudioFetch();
    const { unmount } = render(<Studio />);
    await screen.findByTestId("studio-list");
    expect(screen.queryByTestId("studio-canvas")).toBeNull();
    unmount();

    cleanup();
    mockMatchMedia(() => false);
    mockStudioFetch();
    render(<Studio />);
    await screen.findByTestId("studio-canvas");
    expect(screen.queryByTestId("studio-list")).toBeNull();
  });

  it("列表模式的关系编辑：下拉换角色走 PUT persona（带版本号）", async () => {
    mockMatchMedia((q) => q.includes("max-width"));
    const fetchMock = mockStudioFetch({
      "/studio/conversations/qq%3Agroup%3Ag1/persona": () =>
        jsonResult({
          key: "qq:group:g1",
          persona_id: "whale-playful",
          version: 1,
          changed: true,
        }),
    });
    render(<Studio />);
    await screen.findByTestId("list-qq:group:g1");

    const select = screen.getByLabelText("角色卡") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "whale-playful" } });

    await waitFor(() => {
      const put = fetchCalls(fetchMock).find(
        (c) => c.method === "PUT" && c.url.includes("/persona"),
      );
      expect(put?.body).toEqual({
        persona_id: "whale-playful",
        expected_version: 0,
      });
    });
  });

  it("列表模式勾选记忆副本走 PUT memories；vault 只有 Fork 按钮没有复选框", async () => {
    mockMatchMedia((q) => q.includes("max-width"));
    const fetchMock = mockStudioFetch({
      "/studio/conversations/qq%3Agroup%3Ag1/memories": () =>
        jsonResult({
          key: "qq:group:g1",
          space_id: "sp-copy",
          version: 1,
          changed: true,
        }),
    });
    render(<Studio />);
    const conv = await screen.findByTestId("list-qq:group:g1");

    // vault 不提供直接绑定入口
    expect(conv.textContent).toContain("主人原始库");
    expect(conv.textContent).toContain("永不可直接连线");

    const checkbox = screen.getByRole("checkbox", { name: /课程副本/ });
    fireEvent.click(checkbox);

    await waitFor(() => {
      const put = fetchCalls(fetchMock).find(
        (c) => c.method === "PUT" && c.url.includes("/memories"),
      );
      expect(put?.body).toMatchObject({
        space_id: "sp-copy",
        can_read: true,
        expected_version: 0,
      });
    });
  });
});
