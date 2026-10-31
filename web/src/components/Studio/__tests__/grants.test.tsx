// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import GrantsPanel from "../GrantsPanel";
import { setToken, type TemporaryGrant } from "../../../api";
import { fetchCalls, jsonResult } from "./helpers";

const NOW = 1_800_000_000;

const GRANT: TemporaryGrant = {
  grant_id: "tg-abc123",
  principal_id: "paired:qq:u1",
  channel: "qq",
  sender_id: "u1",
  conversation_id: "private:u1",
  scopes: ["task.read:self"],
  issued_at: NOW - 60,
  expires_at: NOW + 540,
  max_uses: 3,
  used_count: 1,
  remaining_uses: 2,
  proof_method: "otp",
  proof_strength: "high",
};

beforeEach(() => {
  cleanup();
  setToken("test-token");
});

describe("临时授权区", () => {
  it("创建口令：明文仅展示一次（复制按钮 + 仅显示一次提示），关闭后消失且不进清单", async () => {
    const fetchMock = vi.fn((input: unknown) => {
      const url = String(input);
      if (url.includes("/auth/temporary/codes")) {
        return Promise.resolve(
          jsonResult({
            code_id: "oc-1",
            code: "whale-secret-phrase",
            proof_strength: "high",
            scopes: ["task.read:self"],
            expires_at: NOW + 86400,
            note: "口令明文仅本次返回；服务端只保存校验值，无法找回。",
          }),
        );
      }
      return Promise.resolve(jsonResult({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GrantsPanel
        codes={[]}
        grants={[]}
        now={NOW}
        onChanged={() => {}}
        onError={() => {}}
      />,
    );

    fireEvent.click(screen.getByText("创建口令"));
    await waitFor(() => screen.getByTestId("code-plaintext"));

    // 明文 + 仅一次提示 + 复制按钮
    expect(screen.getByTestId("code-plaintext").textContent).toBe(
      "whale-secret-phrase",
    );
    expect(screen.getByText(/仅显示这一次/)).toBeTruthy();
    expect(screen.getByText("复制")).toBeTruthy();

    // 关闭后明文从 DOM 消失；清单里也不含明文（后端本就不返回）
    fireEvent.click(screen.getByText("我已保存，关闭"));
    expect(screen.queryByTestId("code-plaintext")).toBeNull();
    expect(screen.queryByText("whale-secret-phrase")).toBeNull();

    // 请求体包含 scopes 与租约参数
    const [create] = fetchCalls(fetchMock).filter(
      (c) => c.method === "POST" && c.url.includes("/auth/temporary/codes"),
    );
    expect(create.body).toMatchObject({
      scopes: ["task.read:self"],
      ttl_s: 86400,
      grant_ttl_s: 600,
      grant_max_uses: 1,
    });
    expect(create.body).not.toHaveProperty("custom_phrase");
  });

  it("自定义短语模式会提交 custom_phrase", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResult({
          code_id: "oc-2",
          code: "我的口令",
          proof_strength: "low",
          scopes: ["chat.basic"],
          expires_at: NOW + 10,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <GrantsPanel
        codes={[]}
        grants={[]}
        now={NOW}
        onChanged={() => {}}
        onError={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText(/自定义短语/));
    fireEvent.change(
      screen.getByPlaceholderText("长度与强度会被服务端校验"),
      {
      target: { value: "一段够长的自定义短语" },
    });
    fireEvent.click(screen.getByLabelText(/chat\.basic/));
    fireEvent.click(screen.getByLabelText(/task\.read:self/)); // 取消默认
    fireEvent.click(screen.getByText("创建口令"));
    await waitFor(() => screen.getByTestId("code-plaintext"));
    const [create] = fetchCalls(fetchMock).filter(
      (c) => c.method === "POST" && c.url.includes("/codes"),
    );
    expect(create.body).toMatchObject({
      custom_phrase: "一段够长的自定义短语",
      scopes: ["chat.basic"],
    });
  });

  it("撤销租约：POST revoke 并触发刷新回调", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResult({ grant_id: "tg-abc123", revoked: true })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn();
    render(
      <GrantsPanel
        codes={[]}
        grants={[GRANT]}
        now={NOW}
        onChanged={onChanged}
        onError={() => {}}
      />,
    );

    const card = screen.getByTestId("grant-tg-abc123");
    // 只读可视化：scopes、剩余次数、剩余时间、撤销按钮
    expect(card.textContent).toContain("task.read:self");
    expect(card.textContent).toContain("剩余次数：2/3");
    expect(card.textContent).toContain("剩余 9 分钟");

    fireEvent.click(screen.getByText("撤销授权"));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const [revoke] = fetchCalls(fetchMock).filter((c) =>
      c.url.includes("/grants/tg-abc123/revoke"),
    );
    expect(revoke.method).toBe("POST");
  });

  it("待使用口令可撤销；已核销口令不提供撤销", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResult({ code_id: "oc-9", revoked: true })),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <GrantsPanel
        codes={[
          {
            code_id: "oc-9",
            allowed_scopes: ["chat.basic"],
            expires_at: NOW + 1000,
          },
          {
            code_id: "oc-8",
            allowed_scopes: ["chat.basic"],
            expires_at: NOW + 1000,
            consumed_at: NOW - 10,
          },
        ]}
        grants={[]}
        now={NOW}
        onChanged={() => {}}
        onError={() => {}}
      />,
    );
    const usable = screen.getByTestId("code-oc-9");
    const consumed = screen.getByTestId("code-oc-8");
    expect(usable.textContent).toContain("撤销");
    expect(consumed.textContent).toContain("已核销");
    expect(consumed.textContent).not.toContain("撤销");

    fireEvent.click(screen.getByText("撤销"));
    await waitFor(() =>
      expect(
        fetchCalls(fetchMock).some((c) =>
          c.url.includes("/codes/oc-9/revoke"),
        ),
      ).toBe(true),
    );
  });
});
