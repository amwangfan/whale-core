// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Studio from "..";
import { setToken } from "../../../api";
import {
  FIXTURE,
  installDomMocks,
  mockMatchMedia,
  mockStudioFetch,
} from "./helpers";

installDomMocks();

beforeEach(() => {
  cleanup();
  setToken("test-token");
  mockMatchMedia(() => false); // 桌面画布模式
});

describe("Studio 画布", () => {
  it("节点完全来自后端 graph：会话 / 角色 / 记忆 / vault 卡都渲染", async () => {
    const fetchMock = mockStudioFetch();
    render(<Studio />);
    fireEvent.click(screen.getByRole("button", { name: "图谱" }));

    await waitFor(() =>
      expect(screen.getByTestId("card-conv:qq:group:g1")).toBeTruthy(),
    { timeout: 5000 },
    );
    expect(screen.getByTestId("card-persona:whale-playful")).toBeTruthy();
    expect(screen.getByTestId("card-space:sp-copy")).toBeTruthy();
    expect(screen.getByTestId("card-space:vault")).toBeTruthy();
    // issue #11：设备卡渲染（状态/能力/排队水位来自服务端）
    expect(screen.getByTestId("card-device:dev-1")).toBeTruthy();

    // 文案来自后端 label/name，不是前端编造
    // （人物卡管理面板也会列出同名卡标题，故用 getAllByText 不断言唯一）
    expect(screen.getByText("qq:group:g1")).toBeTruthy();
    expect(screen.getAllByText("活泼鲸鱼").length).toBeGreaterThan(0);
    expect(screen.getAllByText("课程副本").length).toBeGreaterThan(0);

    // INV-08：vault 卡只有「存在 + 名称」，不展示记录数等私密元数据
    const vault = screen.getByTestId("card-space:vault");
    expect(vault.textContent).toContain("主人原始库");
    expect(vault.textContent).toContain("受保护");
    expect(vault.textContent).not.toContain("记录");

    // 首屏拉取了服务端事实源
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls.some((u) => u.includes("/studio/graph"))).toBe(true);
    expect(urls.some((u) => u.includes("/studio/conversations"))).toBe(true);
  });

  it("普通副本卡展示后端给的记录数与只读属性", async () => {
    mockStudioFetch();
    render(<Studio />);
    fireEvent.click(screen.getByRole("button", { name: "图谱" }));
    await waitFor(() =>
      expect(screen.getByTestId("card-space:sp-copy")).toBeTruthy(),
    { timeout: 5000 },
    );
    const copy = screen.getByTestId("card-space:sp-copy");
    expect(copy.textContent).toContain("记录：3 条");
    expect(copy.textContent).toContain("只读");
  });

  it("画布边来自后端 graph（不本地虚构连线）", async () => {
    mockStudioFetch({
      "/studio/graph": () =>
        new Response(
          JSON.stringify({
            result: {
              ...structuredClone(FIXTURE.graph),
              edges: [
                {
                  id: "edge:mem:qq:group:g1:sp-copy",
                  kind: "memory",
                  from: "conv:qq:group:g1",
                  to: "space:sp-copy",
                  can_read: true,
                  can_write: false,
                  granted_by: "studio-api",
                  created_at: "2026-10-31T00:00:00+00:00",
                },
              ],
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    const { container } = render(<Studio />);
    fireEvent.click(screen.getByRole("button", { name: "图谱" }));
    await waitFor(() =>
      expect(screen.getByTestId("card-space:sp-copy")).toBeTruthy(),
    { timeout: 5000 },
    );
    await waitFor(() => {
      const edges = container.querySelectorAll(".react-flow__edge");
      expect(edges.length).toBe(1);
    }, { timeout: 5000 });
  });
});
