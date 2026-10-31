// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import ForkWizard from "../ForkWizard";
import Studio from "..";
import { setToken } from "../../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockMatchMedia,
  mockStudioFetch,
} from "./helpers";

installDomMocks();

beforeEach(() => {
  cleanup();
  setToken("test-token");
});

describe("Fork 向导", () => {
  it("preview → 勾选 → confirmed 勾选 → POST forks(confirmed:true)；不自动连线", async () => {
    const fetchMock = vi.fn((input: unknown) => {
      const url = String(input);
      if (url.includes("/studio/forks/preview")) {
        return Promise.resolve(
          jsonResult({
            source_space: "vault",
            redact: "builtin",
            count: 2,
            items: [
              { key: "mem:a", kind: "episode", tags: [], content: { text: "138****1234" } },
              { key: "mem:b", kind: "episode", tags: [], content: { text: "课程表" } },
            ],
          }),
        );
      }
      if (url.includes("/studio/forks")) {
        return Promise.resolve(
          jsonResult({
            fork: {
              space_id: "sp-new",
              space_type: "shareable_fork",
              name: "课程副本",
              version: 1,
              bindable: true,
              auto_bound: false,
            },
          }),
        );
      }
      return Promise.resolve(jsonResult({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    const onDone = vi.fn();
    render(<ForkWizard sourceSpace="vault" onClose={() => {}} onDone={onDone} />);

    fireEvent.change(screen.getByLabelText(/记录 key/), {
      target: { value: "mem:a\nmem:b" },
    });
    fireEvent.click(screen.getByText("预览脱敏结果"));

    await waitFor(() => screen.getByText(/已脱敏/));
    // 取消勾选 mem:b，只复制 mem:a
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    // 未勾选确认时不能提交
    expect(
      (screen.getByText("确认 Fork") as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByLabelText(/我已审核脱敏预览/));
    fireEvent.click(screen.getByText("确认 Fork"));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("sp-new"));

    const calls = fetchCalls(fetchMock);
    const preview = calls.find((c) => c.url.includes("/forks/preview"));
    const fork = calls.find(
      (c) => c.url.includes("/studio/forks") && !c.url.includes("preview"),
    );
    expect(preview?.body).toEqual({
      source_space: "vault",
      record_keys: ["mem:a", "mem:b"],
      redact: "builtin",
    });
    expect(fork?.body).toMatchObject({
      source_space: "vault",
      record_keys: ["mem:a"],
      confirmed: true,
    });
    // Fork 成功后没有任何绑定请求（不自动连线）
    expect(calls.some((c) => c.url.includes("/memories"))).toBe(false);
  });

  it("窄屏列表模式下点 vault 的「创建受控副本」：只开向导，不发绑定请求", async () => {
    mockMatchMedia((q) => q.includes("max-width")); // 触发列表模式
    const fetchMock = mockStudioFetch();
    render(<Studio />);

    const conv = await screen.findByTestId("list-qq:group:g1");
    expect(conv).toBeTruthy();
    const forkButtons = await screen.findAllByText("创建受控副本");
    fireEvent.click(forkButtons[0]);

    // Fork 向导打开
    await waitFor(() =>
      expect(screen.getByRole("dialog")).toBeTruthy(),
    );
    expect(screen.getByText(/个人原始库不能直接连接/)).toBeTruthy();

    // 全程没有任何 memories/persona 绑定请求
    const calls = fetchCalls(fetchMock);
    expect(
      calls.some((c) => c.method === "PUT" && c.url.includes("/memories")),
    ).toBe(false);
    expect(
      calls.some((c) => c.method === "PUT" && c.url.includes("/persona")),
    ).toBe(false);
  });
});
