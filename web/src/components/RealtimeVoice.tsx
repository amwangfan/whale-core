import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import Icon from "./Icon";

/** Interleaving-free PCM16 little endian, resampled to the bridge's 24 kHz. */
export function pcm16Base64(samples: Float32Array, inputRate: number): string {
  if (!Number.isFinite(inputRate) || inputRate <= 0)
    throw new Error("音频采样率无效");
  const length = Math.floor((samples.length * 24000) / inputRate);
  const bytes = new Uint8Array(length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < length; i++) {
    const position = (i * inputRate) / 24000;
    const a = Math.floor(position);
    const fraction = position - a;
    const sample = Math.max(
      -1,
      Math.min(
        1,
        (samples[a] ?? 0) * (1 - fraction) +
          (samples[a + 1] ?? samples[a] ?? 0) * fraction,
      ),
    );
    view.setInt16(
      i * 2,
      sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767),
      true,
    );
  }
  let text = "";
  for (let i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
  return btoa(text);
}

function pcmAudio(base64: string, context: AudioContext): AudioBuffer {
  const text = atob(base64);
  const bytes = Uint8Array.from(text, (c) => c.charCodeAt(0));
  const view = new DataView(bytes.buffer);
  const audio = context.createBuffer(1, Math.floor(bytes.length / 2), 24000);
  const channel = audio.getChannelData(0);
  for (let i = 0; i < channel.length; i++)
    channel[i] = view.getInt16(i * 2, true) / 32768;
  return audio;
}

type VoiceState =
  | "idle"
  | "connecting"
  | "listening"
  | "waiting"
  | "speaking"
  | "ready";
const LABELS: Record<VoiceState, string> = {
  idle: "尚未连接",
  connecting: "正在连接…",
  listening: "正在聆听",
  waiting: "等待鲸鱼回复…",
  speaking: "鲸鱼正在说话",
  ready: "会话已就绪",
};

export default function RealtimeVoice({ available }: { available: boolean }) {
  const [state, setState] = useState<VoiceState>("idle");
  const [error, setError] = useState("");
  const [transcript, setTranscript] = useState("");
  const [instructions, setInstructions] = useState("");
  const generation = useRef(0);
  const session = useRef("");
  const context = useRef<AudioContext | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const processor = useRef<ScriptProcessorNode | null>(null);
  const source = useRef<MediaStreamAudioSourceNode | null>(null);
  const playback = useRef<Set<AudioBufferSourceNode>>(new Set());
  const nextPlayback = useRef(0);
  const appendQueue = useRef<Promise<void>>(Promise.resolve());
  const pendingChunks = useRef(0);
  const pollAbort = useRef<AbortController | null>(null);

  const stopCapture = useCallback(() => {
    if (processor.current) {
      processor.current.onaudioprocess = null;
      processor.current.disconnect();
      processor.current = null;
    }
    source.current?.disconnect();
    source.current = null;
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  }, []);
  const stopPlayback = useCallback(() => {
    playback.current.forEach((node) => {
      try {
        node.stop();
      } catch {
        /* already ended */
      }
    });
    playback.current.clear();
    nextPlayback.current = 0;
  }, []);
  const shutdown = useCallback(
    (update = true) => {
      generation.current++;
      pollAbort.current?.abort();
      pollAbort.current = null;
      stopCapture();
      stopPlayback();
      const current = session.current;
      session.current = "";
      const audio = context.current;
      context.current = null;
      if (audio) void audio.close().catch(() => {});
      if (current) void api.voiceClose(current).catch(() => {});
      if (update) setState("idle");
    },
    [stopCapture, stopPlayback],
  );
  useEffect(() => () => shutdown(false), [shutdown]);

  const fail = useCallback(
    (message: string) => {
      shutdown();
      setError(message);
    },
    [shutdown],
  );
  const capture = async (id: string, epoch: number) => {
    if (!navigator.mediaDevices?.getUserMedia)
      throw new Error(
        "此浏览器无法使用麦克风，请通过 HTTPS 或 localhost 打开控制台。",
      );
    const mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
      video: false,
    });
    if (generation.current !== epoch) {
      mic.getTracks().forEach((t) => t.stop());
      return;
    }
    const audio = context.current;
    if (!audio) {
      mic.getTracks().forEach((t) => t.stop());
      return;
    }
    stream.current = mic;
    source.current = audio.createMediaStreamSource(mic);
    // ScriptProcessor provides a small compatibility fallback without serving
    // dynamic worklet JavaScript; all recorded buffers remain in this component.
    const node = audio.createScriptProcessor(4096, 1, 1);
    processor.current = node;
    appendQueue.current = Promise.resolve();
    pendingChunks.current = 0;
    node.onaudioprocess = (event) => {
      if (generation.current !== epoch || !session.current) return;
      if (pendingChunks.current >= 12) {
        fail("音频上传速度跟不上录音，已关闭麦克风。请检查网络后重新连接。");
        return;
      }
      const encoded = pcm16Base64(
        event.inputBuffer.getChannelData(0),
        audio.sampleRate,
      );
      pendingChunks.current++;
      appendQueue.current = appendQueue.current
        .then(async () => {
          if (generation.current === epoch) await api.voiceAppend(id, encoded);
        })
        .catch((e) => {
          if (generation.current === epoch)
            fail(e instanceof Error ? e.message : "音频上传失败。");
        })
        .finally(() => {
          if (generation.current === epoch) pendingChunks.current--;
        });
    };
    source.current.connect(node);
    node.connect(audio.destination);
    setState("listening");
  };
  const receive = async (id: string, epoch: number, signal: AbortSignal) => {
    while (generation.current === epoch && !signal.aborted) {
      try {
        const event = await api.voiceReceive(id, signal);
        if (generation.current !== epoch || signal.aborted) return;
        const type = String(event.type ?? "");
        if (
          ["response.audio.delta", "response.output_audio.delta"].includes(
            type,
          ) &&
          typeof event.delta === "string" &&
          context.current
        ) {
          const audio = context.current;
          if (nextPlayback.current > audio.currentTime + 60)
            throw new Error("语音输出缓存超过限制，已关闭会话。");
          const node = audio.createBufferSource();
          node.buffer = pcmAudio(event.delta, audio);
          node.connect(audio.destination);
          playback.current.add(node);
          node.onended = () => {
            playback.current.delete(node);
            if (!playback.current.size && !stream.current) setState("ready");
          };
          const start = Math.max(
            audio.currentTime + 0.02,
            nextPlayback.current,
          );
          node.start(start);
          nextPlayback.current = start + node.buffer.duration;
          setState("speaking");
        } else if (
          type.includes("transcript.delta") &&
          typeof event.delta === "string"
        )
          setTranscript((t) => (t + event.delta).slice(-30000));
        else if (
          type === "conversation.item.input_audio_transcription.completed" &&
          typeof event.transcript === "string"
        )
          setTranscript((t) =>
            (t + "\n我：" + event.transcript + "\n鲸鱼：").slice(-30000),
          );
        else if (type === "response.done")
          setState(
            stream.current
              ? "listening"
              : playback.current.size
                ? "speaking"
                : "ready",
          );
        else if (type === "error") {
          const detail = event.error as { message?: unknown } | undefined;
          setError(
            typeof detail?.message === "string"
              ? detail.message.slice(0, 500)
              : "实时语音服务报告了错误。",
          );
        }
        await new Promise((resolve) => setTimeout(resolve, 30));
      } catch (e) {
        if (signal.aborted || generation.current !== epoch) return;
        fail(e instanceof Error ? e.message : "语音连接已断开。");
        return;
      }
    }
  };
  const start = async () => {
    setError("");
    setState("connecting");
    setTranscript("");
    const epoch = ++generation.current;
    try {
      let audio: AudioContext;
      try {
        audio = new AudioContext({ sampleRate: 24000 });
      } catch {
        audio = new AudioContext();
      }
      context.current = audio;
      await audio.resume();
      const result = await api.voiceOpen(instructions.trim());
      if (generation.current !== epoch) {
        void api.voiceClose(result.session_id).catch(() => {});
        return;
      }
      session.current = result.session_id;
      await capture(result.session_id, epoch);
      if (generation.current !== epoch) return;
      const controller = new AbortController();
      pollAbort.current = controller;
      void receive(result.session_id, epoch, controller.signal);
    } catch (e) {
      if (generation.current === epoch)
        fail(e instanceof Error ? e.message : "无法打开实时语音。");
    }
  };
  const commit = async () => {
    stopCapture();
    setState("waiting");
    const epoch = generation.current,
      id = session.current;
    try {
      await appendQueue.current;
      if (generation.current === epoch && id) await api.voiceCommit(id);
    } catch (e) {
      if (generation.current === epoch)
        fail(e instanceof Error ? e.message : "音频提交失败。");
    }
  };
  const interrupt = async () => {
    stopPlayback();
    try {
      await api.voiceCancel(session.current);
      setState(stream.current ? "listening" : "ready");
    } catch (e) {
      setError(e instanceof Error ? e.message : "无法打断回复。");
    }
  };
  return (
    <section className="surface realtime-panel">
      <div className="section-head">
        <h2>实时语音</h2>
        <span className={state === "idle" ? "badge" : "badge ok"}>
          {LABELS[state]}
        </span>
      </div>
      <p className="muted small">
        直接连接已配置的原生实时模型。点击开始后才申请麦克风权限；切换页面或关闭会话将停止录音。
      </p>
      {!available && (
        <div className="banner warn">
          请先在「设置与诊断」中配置实时语音服务。
        </div>
      )}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div className="voice-center">
        <div
          className={
            state === "listening" || state === "speaking"
              ? "voice-orb active"
              : "voice-orb"
          }
        >
          <Icon name="voice" size={36} />
        </div>
        <strong>{LABELS[state]}</strong>
        <span>
          {state === "listening"
            ? "说完后点击「发送这段录音」"
            : "音频采用 PCM16 · 单声道 · 24 kHz"}
        </span>
      </div>
      <div className="form-field">
        <label htmlFor="voice-instructions">本次会话说明（可选）</label>
        <input
          id="voice-instructions"
          value={instructions}
          disabled={state !== "idle"}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder="例如：用中文和我聊聊今天的计划"
        />
      </div>
      <div className="actions voice-actions">
        {state === "idle" ? (
          <button
            className="primary"
            disabled={!available}
            onClick={() => void start()}
          >
            开始语音
            <Icon name="voice" size={16} />
          </button>
        ) : (
          <>
            {state === "listening" && (
              <button className="primary" onClick={() => void commit()}>
                发送这段录音
              </button>
            )}
            {state === "ready" && (
              <button
                className="primary"
                onClick={() => {
                  setState("connecting");
                  void capture(session.current, generation.current).catch((e) =>
                    fail(e instanceof Error ? e.message : "无法录音"),
                  );
                }}
              >
                继续录音
              </button>
            )}
            {["speaking", "waiting"].includes(state) && (
              <button onClick={() => void interrupt()}>打断回复</button>
            )}
            <button className="danger" onClick={() => shutdown()}>
              关闭会话
            </button>
          </>
        )}
      </div>
      {transcript && (
        <div className="voice-transcript" role="log" aria-live="polite">
          {transcript}
        </div>
      )}
      <p className="muted small">
        麦克风音频会发送至你配置的实时服务；普通日志不保存音频字节。
      </p>
    </section>
  );
}
