import { afterEach, expect, it, vi } from "vitest";
import { MAX_SSE_LINE_CHARS, readJsonEvents } from "./sse";
import { chatStream, streamEvents } from "./api";

afterEach(() => vi.unstubAllGlobals());
const encoder = new TextEncoder();
function stream(text: string, bytewise = false) {
  const bytes = encoder.encode(text);
  return new ReadableStream<Uint8Array>({ start(c) {
    if (bytewise) for (const byte of bytes) c.enqueue(new Uint8Array([byte]));
    else c.enqueue(bytes);
    c.close();
  } });
}
async function collect(body: ReadableStream<Uint8Array>) {
  const events = [];
  for await (const event of readJsonEvents(body)) events.push(event);
  return events;
}

it("preserves split UTF-8 and CRLF, ignores comments and non-object JSON, and flushes EOF", async () => {
  const body = stream(': ping\r\ndata: null\ndata: []\ndata: invalid\rdata: {"text":"鲸鱼"}\r\ndata: {"last":true}', true);
  expect(await collect(body)).toEqual([{ text: "鲸鱼" }, { last: true }]);
  expect(body.locked).toBe(false);
});

it.each(["", "\n"])("bounds an unterminated or terminated line (%j) and releases its reader", async (end) => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ start(c) {
    c.enqueue(encoder.encode("data: " + "x".repeat(MAX_SSE_LINE_CHARS) + end));
  }, cancel });
  await expect(collect(body)).rejects.toThrow("exceeds limit");
  expect(cancel).toHaveBeenCalledOnce();
  expect(body.locked).toBe(false);
});

it("cancels the source and unlocks when a consumer leaves early", async () => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ start(c) { c.enqueue(encoder.encode('data: {"n":1}\n')); }, cancel });
  for await (const _event of readJsonEvents(body)) break;
  expect(cancel).toHaveBeenCalledOnce();
  expect(body.locked).toBe(false);
});

it("cancels pending reads on abort and delivers no later events", async () => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ cancel });
  const controller = new AbortController();
  const iterator = readJsonEvents(body, controller.signal);
  const next = iterator.next(); controller.abort();
  expect((await next).done).toBe(true);
  expect(cancel).toHaveBeenCalledOnce();
  expect(body.locked).toBe(false);
});

it("cleans up an already aborted stream", async () => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ cancel });
  const controller = new AbortController(); controller.abort();
  expect((await readJsonEvents(body, controller.signal).next()).done).toBe(true);
  expect(cancel).toHaveBeenCalledOnce();
  expect(body.locked).toBe(false);
});

it("stops event callbacks after close even if fetch ignores abort", async () => {
  let finish!: (res: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
  const handlers = { onEvent: vi.fn(), onOpen: vi.fn(), onError: vi.fn() };
  const events = streamEvents(handlers); events.close();
  const body = stream('data: {"kind":"late"}\n');
  finish(new Response(body));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(handlers.onOpen).not.toHaveBeenCalled();
  expect(handlers.onEvent).not.toHaveBeenCalled();
  expect(handlers.onError).not.toHaveBeenCalled();
});

it("does not swallow chat consumer failures as malformed JSON", async () => {
  const body = stream('data: {"type":"delta","text":"hello"}\n');
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body)));
  await expect(chatStream("x", "c", { onDelta() { throw new Error("consumer failed"); } })).rejects.toThrow("consumer failed");
  expect(body.locked).toBe(false);
});

it("cancels the event body when opening fails before acquiring a reader", async () => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ cancel });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body)));
  const onError = vi.fn();
  streamEvents({ onEvent: vi.fn(), onOpen() { throw new Error("open failed"); }, onError });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(onError).toHaveBeenCalledOnce();
  expect(cancel).toHaveBeenCalledOnce();
  expect(body.locked).toBe(false);
});
