// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "../../../api";
import { executeIntent, resolveDrop } from "../connect";
import { fetchCalls, jsonError, jsonResult } from "./helpers";

const CONV = {
  key: "qq:group:g1",
  persona_version: 2,
  memory_version: 5,
};

beforeEach(() => {
  setToken("test-token");
});

describe("resolveDrop（纯决策，不发请求）", () => {
  it("角色卡 → 会话 = persona 意图", () => {
    const intent = resolveDrop(
      { kind: "persona", id: "persona:whale-playful" },
      { kind: "conversation", conversationKey: "qq:group:g1" },
    );
    expect(intent).toEqual({
      type: "persona",
      conversationKey: "qq:group:g1",
      personaId: "whale-playful",
    });
  });

  it("vault（protected_original）→ 会话 = fork-wizard 意图，且不发任何请求", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const intent = resolveDrop(
      {
        kind: "memory_space",
        id: "space:vault",
        restricted: true,
        spaceType: "protected_original",
      },
      { kind: "conversation", conversationKey: "qq:group:g1" },
    );
    expect(intent).toEqual({ type: "fork-wizard", spaceId: "vault" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("共享副本 → 会话 = memory 意图，且默认只读", () => {
    const intent = resolveDrop(
      { kind: "memory_space", id: "space:sp-copy", spaceType: "shareable_fork" },
      { kind: "conversation", conversationKey: "qq:group:g1" },
    );
    expect(intent).toMatchObject({
      type: "memory",
      spaceId: "sp-copy",
      canWrite: false,
    });
  });

  it("目标不是会话 = invalid", () => {
    const intent = resolveDrop(
      { kind: "persona", id: "persona:x" },
      { kind: "persona" },
    );
    expect(intent.type).toBe("invalid");
  });
});

describe("executeIntent（连线结果以后端为准）", () => {
  it("成功：PUT persona 携带 expected_version，返回 ok", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResult({ key: "qq:group:g1", persona_id: "whale-playful", version: 3, changed: true }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const outcome = await executeIntent(
      { type: "persona", conversationKey: "qq:group:g1", personaId: "whale-playful" },
      CONV,
    );
    expect(outcome).toEqual({ status: "ok", changed: true, version: 3 });

    const [call] = fetchCalls(fetchMock);
    expect(call.method).toBe("PUT");
    expect(call.url).toContain(
      "/studio/conversations/qq%3Agroup%3Ag1/persona",
    );
    expect(call.body).toEqual({ persona_id: "whale-playful", expected_version: 2 });
  });

  it("403 vault_not_bindable：denied + forkRequired，不假装成功", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          jsonError(
            403,
            "vault_not_bindable",
            "个人原始库不能直接连接任何会话；请先创建受控副本（Fork）",
          ),
        ),
      ),
    );
    const outcome = await executeIntent(
      { type: "memory", conversationKey: "qq:group:g1", spaceId: "vault", canWrite: false },
      CONV,
    );
    expect(outcome).toMatchObject({
      status: "denied",
      code: "vault_not_bindable",
      forkRequired: true,
    });
  });

  it("409 version_conflict：conflict，提示刷新后重试", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          jsonError(409, "version_conflict", "绑定已被并发修改；请读取最新版本后重试"),
        ),
      ),
    );
    const outcome = await executeIntent(
      { type: "memory", conversationKey: "qq:group:g1", spaceId: "sp-copy", canWrite: false },
      CONV,
    );
    expect(outcome.status).toBe("conflict");
  });

  it("网络失败：error，不产生乐观成功", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("offline"))),
    );
    const outcome = await executeIntent(
      { type: "persona", conversationKey: "qq:group:g1", personaId: "x" },
      CONV,
    );
    expect(outcome.status).toBe("error");
  });
});
