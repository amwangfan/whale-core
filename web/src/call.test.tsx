// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import RealtimeCall from "./components/RealtimeCall";
import { api } from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      voiceSession: vi.fn(),
    },
  };
});

interface FakeSocket {
  url: string;
  protocols: string[];
  readyState: number;
  bufferedAmount: number;
  binaryType: string;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
}

const sockets: FakeSocket[] = [];

class FakeWebSocket implements FakeSocket {
  static OPEN = 1;
  readyState = FakeWebSocket.OPEN;
  bufferedAmount = 0;
  binaryType = "";
  onmessage: FakeSocket["onmessage"] = null;
  onclose: FakeSocket["onclose"] = null;
  onerror: FakeSocket["onerror"] = null;
  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = 3;
  });
  constructor(
    public url: string,
    public protocols: string[],
  ) {
    sockets.push(this);
  }
}

const bufferSources: { start: ReturnType<typeof vi.fn> }[] = [];

class FakeAudioContext {
  sampleRate = 16000;
  currentTime = 0;
  destination = {};
  resume = vi.fn().mockResolvedValue(undefined);
  close = vi.fn().mockResolvedValue(undefined);
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
  createMediaStreamSource = vi.fn(() => ({ connect: vi.fn() }));
  createGain = vi.fn(() => ({ gain: { value: 1 }, connect: vi.fn() }));
  createBuffer = vi.fn((_channels: number, length: number, rate: number) => ({
    getChannelData: () => new Float32Array(length),
    duration: length / rate,
  }));
  createBufferSource = vi.fn(() => {
    const node = { connect: vi.fn(), start: vi.fn(), buffer: null };
    bufferSources.push(node);
    return node;
  });
}

const worklets: {
  port: { onmessage: ((event: { data: Float32Array }) => void) | null };
  connect: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
}[] = [];

class FakeAudioWorkletNode {
  port: { onmessage: ((event: { data: Float32Array }) => void) | null } = {
    onmessage: null,
  };
  connect = vi.fn();
  disconnect = vi.fn();
  constructor(
    public ctx: FakeAudioContext,
    public name: string,
  ) {
    worklets.push(this);
  }
}

const SESSION = {
  protocol: "whale-io-pcm",
  url: "ws://127.0.0.1:8081",
  ticket: "ticket-fixture-1",
  session_id: "session-fixture-1",
  sample_rate: 16000,
};

let trackStop: ReturnType<typeof vi.fn>;
let originalCreateObjectURL: typeof URL.createObjectURL | undefined;
let originalRevokeObjectURL: typeof URL.revokeObjectURL | undefined;

beforeEach(() => {
  sockets.length = 0;
  worklets.length = 0;
  bufferSources.length = 0;
  trackStop = vi.fn();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("AudioWorkletNode", FakeAudioWorkletNode);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: vi
        .fn()
        .mockResolvedValue({ getTracks: () => [{ stop: trackStop }] }),
    },
  });
  originalCreateObjectURL = URL.createObjectURL;
  originalRevokeObjectURL = URL.revokeObjectURL;
  URL.createObjectURL = vi.fn(() => "blob:mock-worklet");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  URL.createObjectURL = originalCreateObjectURL!;
  URL.revokeObjectURL = originalRevokeObjectURL!;
});

describe("RealtimeCall (whale-io PCM WebSocket transport)", () => {
  it("requests a ticket, presents it as a WebSocket subprotocol, and plays audio back", async () => {
    vi.mocked(api.voiceSession).mockResolvedValue({ ...SESSION });
    render(<RealtimeCall available={true} />);
    fireEvent.click(screen.getByRole("button", { name: /开始实时语音/ }));
    await waitFor(() => expect(sockets.length).toBe(1));
    const ws = sockets[0];
    // 票据经 subprotocol 出示，绝不拼进 URL
    expect(ws.url).toBe(SESSION.url);
    expect(ws.url).not.toContain(SESSION.ticket);
    expect(ws.protocols).toEqual(["whale", SESSION.ticket]);
    expect(ws.binaryType).toBe("arraybuffer");
    expect(screen.getAllByText("正在连接…").length).toBeGreaterThan(0);
    // 服务端 ready → 通话中
    ws.onmessage?.({ data: JSON.stringify({ type: "ready" }) });
    await screen.findByText("通话中");
    // 下行 PCM16 字节 → 排入播放
    ws.onmessage?.({ data: new Uint8Array([1, 0, 255, 127]).buffer });
    await waitFor(() => expect(bufferSources.length).toBe(1));
    expect(bufferSources[0].start).toHaveBeenCalled();
    // 上行：worklet 采样 → 重采样为 PCM16 LE 二进制帧
    expect(worklets.length).toBe(1);
    worklets[0].port.onmessage?.({
      data: new Float32Array([0, 0.5, -0.5, 1]),
    });
    expect(ws.send).toHaveBeenCalledTimes(1);
    const frame = ws.send.mock.calls[0][0] as ArrayBuffer;
    const view = new DataView(frame);
    expect(frame.byteLength).toBe(8);
    expect(view.getInt16(0, true)).toBe(0);
    expect(view.getInt16(2, true)).toBe(16383); // 0.5*32767 截断（PR#4 契约不四舍五入）
    expect(view.getInt16(4, true)).toBe(-16384); // -0.5*32768
    expect(view.getInt16(6, true)).toBe(32767);
    // 结束通话：关闭 WS、停麦克风、关音频上下文
    fireEvent.click(screen.getByRole("button", { name: "结束通话" }));
    expect(ws.close).toHaveBeenCalled();
    expect(trackStop).toHaveBeenCalled();
    await screen.findByRole("button", { name: /开始实时语音/ });
  });

  it("rejects a non-PCM session bridge without opening a WebSocket", async () => {
    vi.mocked(api.voiceSession).mockResolvedValue({
      ...SESSION,
      protocol: "http-session-bridge",
      ticket: "",
    });
    render(<RealtimeCall available={true} />);
    fireEvent.click(screen.getByRole("button", { name: /开始实时语音/ }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("PCM");
    expect(sockets.length).toBe(0);
    // 失败清理：麦克风轨道已停止
    expect(trackStop).toHaveBeenCalled();
  });

  it("keeps the start button disabled when the capability is unavailable", () => {
    render(<RealtimeCall available={false} />);
    const button = screen.getByRole("button", { name: /开始实时语音/ });
    expect(button).toHaveProperty("disabled", true);
    expect(api.voiceSession).not.toHaveBeenCalled();
  });
});
