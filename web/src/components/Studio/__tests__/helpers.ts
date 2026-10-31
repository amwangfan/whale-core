/**
 * Studio 测试公共工具：jsdom 环境补齐 + fetch 路由 mock（不依赖真实后端）。
 */

import { vi } from "vitest";

/** React Flow 在 jsdom 下需要的最小 DOM 能力补齐。 */
export function installDomMocks() {
  if (!("ResizeObserver" in window)) {
    class ResizeObserver {
      callback: globalThis.ResizeObserverCallback;
      constructor(cb: globalThis.ResizeObserverCallback) {
        this.callback = cb;
      }
      // RF 靠回调拿到初次测量；jsdom 不排版，这里立即回放一次。
      observe(target: Element) {
        this.callback(
          [
            {
              target,
              contentRect: { width: 500, height: 400 },
            } as globalThis.ResizeObserverEntry,
          ],
          this as unknown as globalThis.ResizeObserver,
        );
      }
      unobserve() {}
      disconnect() {}
    }
    (window as unknown as Record<string, unknown>).ResizeObserver =
      ResizeObserver;
  }
  if (!("DOMMatrixReadOnly" in window)) {
    class DOMMatrixReadOnly {
      m22: number;
      constructor(transform?: string) {
        const scale = transform?.match(/scale\(([\d.]+)\)/)?.[1];
        this.m22 = scale !== undefined ? Number(scale) : 1;
      }
    }
    (window as unknown as Record<string, unknown>).DOMMatrixReadOnly =
      DOMMatrixReadOnly;
  }
  // RF 边标签定位需要 SVG 文本测量；jsdom 不提供，返回零尺寸即可。
  if (typeof window.SVGElement !== "undefined") {
    (window.SVGElement.prototype as unknown as Record<string, unknown>).getBBox =
      () => ({ x: 0, y: 0, width: 0, height: 0 });
  }
  Object.defineProperties(window.HTMLElement.prototype, {
    offsetHeight: {
      configurable: true,
      get(this: HTMLElement) {
        return parseFloat(this.style.height) || 400;
      },
    },
    offsetWidth: {
      configurable: true,
      get(this: HTMLElement) {
        return parseFloat(this.style.width) || 600;
      },
    },
  });
}

/** matchMedia 可控 mock：matcher 决定每个 query 是否命中。 */
export function mockMatchMedia(matcher: (query: string) => boolean) {
  const impl = (query: string): MediaQueryList =>
    ({
      matches: matcher(query),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
  window.matchMedia = vi.fn(impl) as unknown as typeof window.matchMedia;
}

export interface FetchCall {
  method: string;
  url: string;
  body: unknown;
}

/** 把 mock fetch 的调用记录成 {method, url, body} 列表。 */
export function fetchCalls(mock: ReturnType<typeof vi.fn>): FetchCall[] {
  return mock.mock.calls.map(([input, init]) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    const raw = (init as RequestInit | undefined)?.body;
    return {
      method: ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase(),
      url,
      body: typeof raw === "string" && raw ? JSON.parse(raw) : undefined,
    };
  });
}

export function jsonResult(result: unknown, status = 200): Response {
  return new Response(JSON.stringify({ result }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function jsonError(
  status: number,
  code: string,
  message: string,
): Response {
  return new Response(JSON.stringify({ error: { code, message } }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 默认后端夹具：一个会话、一张角色卡、一个副本、一个 vault。 */
export const FIXTURE = {
  conversations: [
    {
      key: "qq:group:g1",
      channel: "qq",
      kind: "qq_group",
      conversation_id: "group:g1",
      persona_id: null,
      persona_version: 0,
      memory_spaces: [],
      memory_version: 0,
      capabilities: ["chat.basic"],
      capabilities_version: 1,
      ceiling: ["chat.basic", "outbox.submit"],
    },
  ],
  personas: [
    {
      id: "whale-playful",
      title: "活泼鲸鱼",
      name: "鲸鱼",
      summary: "有点傲娇。",
      builtin: true,
    },
  ],
  spaces: [
    {
      space_id: "sp-copy",
      space_type: "shareable_fork",
      name: "课程副本",
      version: 1,
      record_count: 3,
      bindable: true,
      writable: false,
    },
    {
      space_id: "vault",
      space_type: "protected_original",
      name: "主人原始库",
      version: 1,
      bindable: false,
      restricted: true,
    },
  ],
  graph: {
    nodes: [
      {
        id: "conv:qq:group:g1",
        kind: "conversation",
        key: "qq:group:g1",
        label: "qq:group:g1",
        channel: "qq",
        conversation_kind: "qq_group",
        capabilities: ["chat.basic"],
        ceiling: ["chat.basic", "outbox.submit"],
      },
      {
        id: "persona:whale-playful",
        kind: "persona",
        label: "活泼鲸鱼",
        builtin: true,
      },
      {
        id: "space:sp-copy",
        kind: "memory_space",
        label: "课程副本",
        space_type: "shareable_fork",
        version: 1,
        bindable: true,
        record_count: 3,
        writable: false,
      },
      {
        id: "space:vault",
        kind: "memory_space",
        label: "主人原始库",
        space_type: "protected_original",
        version: 1,
        bindable: false,
        restricted: true,
      },
      {
        id: "device:dev-1",
        kind: "device",
        label: "我的工作手机",
        status: "active",
        capabilities: ["phone.notification.post"],
        queued_events: 2,
      },
    ],
    edges: [],
    explanations: {},
    generated_at: "2026-10-31T00:00:00+00:00",
  },
};

/** 装好 Studio 首屏所需的 6 个端点；overrides 可按 url 片段覆盖。 */
export function mockStudioFetch(
  overrides: Record<
    string,
    (call: FetchCall) => Response
  > = {},
): ReturnType<typeof vi.fn> {
  const routes: Record<string, (call: FetchCall) => Response> = {
    "/studio/graph": () => jsonResult(structuredClone(FIXTURE.graph)),
    "/studio/conversations": () =>
      jsonResult({ conversations: structuredClone(FIXTURE.conversations) }),
    "/studio/personas": () =>
      jsonResult({ personas: structuredClone(FIXTURE.personas) }),
    "/studio/memory-spaces": () =>
      jsonResult({ memory_spaces: structuredClone(FIXTURE.spaces) }),
    "/auth/temporary/codes": () => jsonResult({ codes: [] }),
    "/auth/temporary/grants": () => jsonResult({ grants: [] }),
    ...overrides,
  };
  const mock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    const method = (init?.method ?? "GET").toUpperCase();
    const raw = init?.body;
    const call: FetchCall = {
      method,
      url,
      body: typeof raw === "string" && raw ? JSON.parse(raw) : undefined,
    };
    const entries = Object.entries(routes).sort(
      (a, b) => b[0].length - a[0].length, // 最长片段优先，避免集合路由遮蔽子资源
    );
    for (const [frag, handler] of entries) {
      if (url.includes(frag)) return Promise.resolve(handler(call));
    }
    return Promise.resolve(jsonError(404, "not_found", `no route: ${url}`));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}
