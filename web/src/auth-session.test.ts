import { afterEach, expect, it, vi } from "vitest";
import { api, chatStream, onUnauthorized, setToken } from "./api";

afterEach(() => { vi.unstubAllGlobals(); setToken(""); });
function pending() {
  let resolve!: (r: Response) => void;
  const promise = new Promise<Response>(yes => { resolve = yes; });
  return { promise, resolve };
}
const unauthorized = () => new Response(JSON.stringify({ error: { code: "unauthorized", message: "expired" } }), { status: 401 });

it.each(["different", "same"])("ignores old 401 responses after a %s token session is established", async mode => {
  const old = pending(); vi.stubGlobal("fetch", vi.fn(() => old.promise));
  const notify = vi.fn(), unsubscribe = onUnauthorized(notify);
  try {
    setToken("first"); const request = api.status().catch(e => e);
    setToken(mode === "same" ? "first" : "second"); old.resolve(unauthorized());
    expect((await request).status).toBe(401); expect(notify).not.toHaveBeenCalled();
  } finally { unsubscribe(); }
});

it("does not disconnect when an obsolete aborted read returns 401", async () => {
  const old = pending(); vi.stubGlobal("fetch", vi.fn(() => old.promise));
  const notify = vi.fn(), unsubscribe = onUnauthorized(notify);
  try {
    setToken("current"); const controller = new AbortController();
    const request = api.status(controller.signal).catch(e => e);
    controller.abort(); old.resolve(unauthorized()); await request;
    expect(notify).not.toHaveBeenCalled();
  } finally { unsubscribe(); }
});

it("uses the shared authorization/error path for streaming chat", async () => {
  const fetchMock = vi.fn().mockResolvedValue(unauthorized()); vi.stubGlobal("fetch", fetchMock);
  const notify = vi.fn(), unsubscribe = onUnauthorized(notify);
  try {
    setToken("current"); await expect(chatStream("hello", "console", {})).rejects.toMatchObject({ status: 401 });
    expect(notify).toHaveBeenCalledOnce();
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Headers).get("Authorization")).toBe("Bearer current");
    expect(init.redirect).toBe("error");
  } finally { unsubscribe(); }
});
