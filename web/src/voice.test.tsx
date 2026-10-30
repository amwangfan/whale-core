// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import RealtimeVoice, { pcm16Base64 } from "./components/RealtimeVoice";
import { api } from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      voiceOpen: vi.fn(),
      voiceAppend: vi.fn(),
      voiceReceive: vi.fn(),
      voiceCommit: vi.fn(),
      voiceCancel: vi.fn(),
      voiceClose: vi.fn(),
    },
  };
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("realtime PCM transport", () => {
  it("encodes clipped signed PCM16 little endian at 24 kHz", () => {
    const raw = atob(
      pcm16Base64(new Float32Array([-2, -0.5, 0, 0.5, 2]), 24000),
    );
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    expect(
      Array.from({ length: 5 }, (_, i) => view.getInt16(i * 2, true)),
    ).toEqual([-32768, -16384, 0, 16384, 32767]);
  });
  it("resamples 48 kHz input to a 24 kHz mono output and rejects invalid rates", () => {
    expect(
      atob(pcm16Base64(new Float32Array([0, 0.1, 0.2, 0.3]), 48000)).length,
    ).toBe(4);
    expect(() => pcm16Base64(new Float32Array([0]), 0)).toThrow();
  });
  it("requests microphone access only after clicking start and stops it when the page leaves", async () => {
    const stop = vi.fn();
    const disconnect = vi.fn();
    const close = vi.fn().mockResolvedValue(undefined);
    const mic = { getTracks: () => [{ stop }] };
    const getUserMedia = vi.fn().mockResolvedValue(mic);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const node = { connect: vi.fn(), disconnect, onaudioprocess: null };
    vi.stubGlobal(
      "AudioContext",
      class {
        sampleRate = 24000;
        destination = {};
        resume = vi.fn().mockResolvedValue(undefined);
        close = close;
        createMediaStreamSource = vi.fn(() => ({
          connect: vi.fn(),
          disconnect,
        }));
        createScriptProcessor = vi.fn(() => node);
      },
    );
    vi.mocked(api.voiceOpen).mockResolvedValue({
      session_id: "session-1",
      status: "connected",
    });
    vi.mocked(api.voiceReceive).mockImplementation(() => new Promise(() => {}));
    vi.mocked(api.voiceClose).mockResolvedValue({});
    const view = render(<RealtimeVoice available={true} />);
    expect(getUserMedia).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "开始语音" }));
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledOnce());
    await screen.findByRole("button", { name: "发送这段录音" });
    view.unmount();
    expect(stop).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
    expect(api.voiceClose).toHaveBeenCalledWith("session-1");
  });
});
