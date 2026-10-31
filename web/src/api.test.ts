import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, apiUrl, isUnauthorized, setToken } from "./api";

/** 构造最小可用的 Response mock。 */
function mockResponse(
  status: number,
  body: unknown,
  opts: { json?: boolean } = {},
): Response {
  const isJson = opts.json !== false;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `Status ${status}`,
    json: async () => {
      if (!isJson) throw new Error("not json");
      return body;
    },
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
    headers: new Headers(),
  } as unknown as Response;
}

function stubFetch(res: Response) {
  const m = vi.fn().mockResolvedValue(res);
  vi.stubGlobal("fetch", m);
  return m;
}

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setToken("");
  });

  it("拼接 /api/v1 前缀并在请求头携带 Bearer token", async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: { ok: true } }));
    setToken("secret-token");
    const r = await api.health();
    expect(r.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/health");
    expect((init.headers as Headers).get("Authorization")).toBe(
      "Bearer secret-token",
    );
  });

  it("无 token 时不发送 Authorization 头", async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: { ok: true } }));
    await api.health();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).has("Authorization")).toBe(false);
  });

  it("401 时按 {error:{code,message}} 抛 ApiError 且 isUnauthorized 为真", async () => {
    stubFetch(
      mockResponse(401, {
        error: { code: "unauthorized", message: "bad token" },
      }),
    );
    const err: unknown = await api.status().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(401);
    expect(apiErr.code).toBe("unauthorized");
    expect(apiErr.message).toBe("bad token");
    expect(isUnauthorized(apiErr)).toBe(true);
  });

  it("错误体非 JSON 时回退为 http_<status>", async () => {
    stubFetch(mockResponse(502, "Bad Gateway", { json: false }));
    const err: unknown = await api.status().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("http_502");
    expect(isUnauthorized(err)).toBe(false);
  });

  it("记忆 key 经 encodeURIComponent 编码", async () => {
    const fetchMock = stubFetch(
      mockResponse(200, { result: { deleted: true } }),
    );
    const r = await api.deleteMemory("life:draft:a/b c");
    expect(r.deleted).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      `/api/v1/memory/${encodeURIComponent("life:draft:a/b c")}`,
    );
    expect(url).toBe("/api/v1/memory/life%3Adraft%3Aa%2Fb%20c");
    expect(init.method).toBe("DELETE");
  });

  it("chat 以 POST 发送 conversation_id 与 text", async () => {
    const fetchMock = stubFetch(
      mockResponse(200, {
        result: { reply: "hi", status: "ok", receipts: [] },
      }),
    );
    await api.chat("你好");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/chat");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      conversation_id: "console",
      text: "你好",
    });
    expect((init.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );
  });

  it("apiUrl 为相对路径补前导斜杠", () => {
    expect(apiUrl("/status")).toBe("/api/v1/status");
    expect(apiUrl("status")).toBe("/api/v1/status");
  });

  it("tasks 请求 /tasks 并解包 result.tasks", async () => {
    const tasks = [
      {
        task_id: "t-1",
        at: 1730000000,
        goal: "查天气",
        status: "answered",
        capabilities: ["weather.lookup"],
        receipts: ["weather:ok:sunny"],
      },
      {
        task_id: "t-2",
        at: 1729999999,
        goal: "发邮件",
        status: "denied",
        capabilities: ["mail.send"],
        receipts: ["plugin-denied:mail:policy"],
      },
    ];
    const fetchMock = stubFetch(mockResponse(200, { result: { tasks } }));
    const r = await api.tasks();
    expect(r).toHaveLength(2);
    expect(r[0].task_id).toBe("t-1");
    expect(r[1].status).toBe("denied");
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/tasks");
  });

  it("task 按 id 编码请求详情，404 抛 ApiError", async () => {
    const fetchMock = stubFetch(
      mockResponse(404, {
        error: { code: "not_found", message: "no such task" },
      }),
    );
    const err: unknown = await api.task("a/b c").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).code).toBe("not_found");
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/v1/tasks/${encodeURIComponent("a/b c")}`);
  });

  it("setPluginConfig 以 POST 提交 {config}，422 暴露校验消息", async () => {
    const fetchMock = stubFetch(
      mockResponse(200, { result: { plugin_id: "demo", saved: true } }),
    );
    const r = await api.setPluginConfig("demo", { api_key: "sk-x", limit: 3 });
    expect(r.saved).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/plugins/demo/config");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      config: { api_key: "sk-x", limit: 3 },
    });
    expect((init.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );

    stubFetch(
      mockResponse(422, {
        error: {
          code: "invalid_config",
          message: "config field 'limit' must be of type 'number'",
        },
      }),
    );
    const err: unknown = await api
      .setPluginConfig("demo", { limit: "x" })
      .catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(422);
    expect((err as ApiError).code).toBe("invalid_config");
    expect((err as ApiError).message).toContain("'limit'");
  });
});

describe("configuration and task contracts", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setToken("");
  });
  it("does not follow redirects carrying the authorization header", async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: { ok: true } }));
    setToken("secret");
    await api.health();
    expect(fetchMock.mock.calls[0][1].redirect).toBe("error");
  });
  it("settings submits a patch and preserves null secret deletion", async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: {} }));
    await api.saveSettings({
      model: { model: "test", api_key: null },
      services: { TAVILY_API_KEY: null },
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/settings");
    expect(JSON.parse(init.body)).toEqual({
      model: { model: "test", api_key: null },
      services: { TAVILY_API_KEY: null },
    });
  });
  it("records tool-task decisions separately from draft approvals", async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: {} }));
    await api.decideTask("a/b", "approve");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/tasks/a%2Fb/decision");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      decision: "approve",
    });
  });
});

describe("private artifact download", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setToken("");
  });
  it("refuses arbitrary URLs before sending an authorization header", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    setToken("secret");
    await expect(
      api.artifact("https://outside.example/media"),
    ).rejects.toMatchObject({ code: "invalid_artifact" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
