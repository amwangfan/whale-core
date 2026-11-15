/** The Core wire format is one JSON `data:` line per event, not multiline data. */
export const MAX_SSE_LINE_CHARS = 1_048_576;

export async function* readJsonEvents(body: ReadableStream<Uint8Array>, signal?: AbortSignal) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal?.addEventListener("abort", cancel);
  if (signal?.aborted) cancel();
  function parse(line: string): Record<string, unknown> | null {
    if (line.length > MAX_SSE_LINE_CHARS) throw new Error("Event stream line exceeds limit");
    if (!line.startsWith("data:")) return null;
    try {
      const value: unknown = JSON.parse(line.slice(5).trim());
      return value !== null && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown> : null;
    } catch { return null; }
  }
  try {
    while (!signal?.aborted) {
      const { done, value } = await reader.read();
      if (signal?.aborted) return;
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let start = 0;
      for (let i = 0; i < buffer.length; i++) {
        if (buffer[i] !== "\n" && buffer[i] !== "\r") continue;
        const event = parse(buffer.slice(start, i));
        start = i + 1;
        if (event) {
          if (signal?.aborted) return;
          yield event;
        }
      }
      buffer = buffer.slice(start);
      if (buffer.length > MAX_SSE_LINE_CHARS) throw new Error("Event stream line exceeds limit");
      if (done) {
        const event = parse(buffer);
        if (event && !signal?.aborted) yield event;
        return;
      }
    }
  } finally {
    signal?.removeEventListener("abort", cancel);
    cancel();
    reader.releaseLock();
  }
}
