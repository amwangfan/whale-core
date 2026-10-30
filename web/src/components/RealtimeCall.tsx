import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import Icon from "./Icon";

/** AudioWorklet 采集器：攒够 1024 个采样就抛回主线程（经 Blob URL 加载，
 *  不引入额外构建产物；与 PR#4 契约一致）。 */
const PROCESSOR = `class Capture extends AudioWorkletProcessor {
  constructor(){super();this.samples=[];}
  process(inputs){
    const input=inputs[0]?.[0];
    if(input)for(const value of input)this.samples.push(value);
    if(this.samples.length>=1024){
      this.port.postMessage(new Float32Array(this.samples));this.samples=[];
    }
    return true;
  }
}registerProcessor('whale-capture',Capture);`;

type CallState = "idle" | "connecting" | "connected";
const LABELS: Record<CallState, string> = {
  idle: "尚未连接",
  connecting: "正在连接…",
  connected: "通话中",
};

/** 浏览器 AudioWorklet + WebSocket 的低延迟 PCM 通话（PR#4 移植）。
 *  只跑 PCM 音频；provider 凭据永远不进入本组件——票据由 /voice/session 签发。 */
export default function RealtimeCall({ available }: { available: boolean }) {
  const [status, setStatus] = useState<CallState>("idle");
  const [error, setError] = useState("");
  const [hint, setHint] = useState("");
  const context = useRef<AudioContext | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const capture = useRef<AudioWorkletNode | null>(null);
  const nextPlayback = useRef(0);
  const active = useRef(false);
  const stop = () => {
    active.current = false;
    capture.current?.disconnect();
    capture.current = null;
    if (socket.current) {
      socket.current.onclose = null;
      socket.current.close();
      socket.current = null;
    }
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    void context.current?.close();
    context.current = null;
    nextPlayback.current = 0;
    setStatus("idle");
  };
  useEffect(
    () => () => {
      stop();
    },
    [],
  );
  const start = async () => {
    if (active.current) return;
    setError("");
    setHint("");
    setStatus("connecting");
    active.current = true;
    try {
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error("需要 HTTPS 或 localhost 才能使用麦克风。");
      const audio = new AudioContext({ sampleRate: 16000 });
      context.current = audio;
      await audio.resume();
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
        video: false,
      });
      if (!active.current) {
        mic.getTracks().forEach((t) => t.stop());
        return;
      }
      stream.current = mic;
      const session = await api.voiceSession();
      if (!active.current) return;
      if (session.protocol !== "whale-io-pcm" || !session.ticket)
        throw new Error(
          "当前是高级 HTTP 会话桥；网页实时通话需要启用 whale-io PCM 插件。",
        );
      if (location.protocol === "https:" && !session.url.startsWith("wss:"))
        throw new Error("HTTPS 控制台需要配置 WSS 反代地址 public_ws_url。");
      const ws = new WebSocket(session.url, ["whale", session.ticket]);
      socket.current = ws;
      ws.binaryType = "arraybuffer";
      ws.onerror = () => {
        if (active.current) {
          setError("音频连接失败，请检查 PCM 桥、认证和 WebSocket 反代。");
          stop();
        }
      };
      ws.onclose = () => {
        if (active.current) {
          setHint("通话已结束。");
          stop();
        }
      };
      ws.onmessage = (event) => {
        if (!active.current) return;
        if (typeof event.data === "string") {
          const data = JSON.parse(event.data) as { type?: string };
          if (data.type === "ready") setStatus("connected");
          return;
        }
        if (!(event.data instanceof ArrayBuffer) || !context.current) return;
        const pcm = new DataView(event.data);
        const ctx = context.current;
        const buffer = ctx.createBuffer(1, event.data.byteLength / 2, 16000);
        const channel = buffer.getChannelData(0);
        for (let i = 0; i < channel.length; i++)
          channel[i] = pcm.getInt16(i * 2, true) / 32768;
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        const when = Math.max(ctx.currentTime + 0.03, nextPlayback.current);
        if (when - ctx.currentTime > 3) {
          nextPlayback.current = ctx.currentTime;
          return;
        }
        source.start(when);
        nextPlayback.current = when + buffer.duration;
      };
      const url = URL.createObjectURL(
        new Blob([PROCESSOR], { type: "application/javascript" }),
      );
      try {
        await audio.audioWorklet.addModule(url);
      } finally {
        URL.revokeObjectURL(url);
      }
      if (!active.current) return;
      const node = new AudioWorkletNode(audio, "whale-capture");
      capture.current = node;
      node.port.onmessage = (event) => {
        if (
          !active.current ||
          ws.readyState !== WebSocket.OPEN ||
          ws.bufferedAmount > 65536
        )
          return;
        const input = event.data as Float32Array;
        const ratio = audio.sampleRate / 16000;
        const count = Math.floor(input.length / ratio);
        const out = new ArrayBuffer(count * 2);
        const view = new DataView(out);
        for (let i = 0; i < count; i++) {
          const sample = Math.max(
            -1,
            Math.min(1, input[Math.floor(i * ratio)]),
          );
          view.setInt16(
            i * 2,
            sample < 0 ? sample * 32768 : sample * 32767,
            true,
          );
        }
        ws.send(out);
      };
      const source = audio.createMediaStreamSource(mic);
      source.connect(node);
      // 保持 worklet 时钟运转，但绝不把麦克风声音送进扬声器
      const silent = audio.createGain();
      silent.gain.value = 0;
      node.connect(silent);
      silent.connect(audio.destination);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      stop();
    }
  };
  return (
    <section className="surface realtime-panel">
      <div className="section-head">
        <h2>低延迟通话 · PCM 桥</h2>
        <span className={status === "connected" ? "badge ok" : "badge"}>
          {LABELS[status]}
        </span>
      </div>
      <p className="muted small">
        通过现有 whale-io 音频桥对话，音频不经过文字模型。点击开始后才申请麦克风权限。
      </p>
      {!available && (
        <div className="banner warn">
          请先在「设置与诊断」中配置 WHALE_PCM_BRIDGE_* 并启用 voice_bridge 插件。
        </div>
      )}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div className="voice-center">
        <div className={status === "connected" ? "voice-orb active" : "voice-orb"}>
          <Icon name="voice" size={36} />
        </div>
        <strong>
          {status === "connected"
            ? "我在听。"
            : status === "connecting"
              ? "正在连接…"
              : "让陪伴，有声音"}
        </strong>
        <span>
          {status === "connected"
            ? "已连接 whale-io 音频桥，可以说话了"
            : "音频采用 PCM16 · 单声道 · 16 kHz"}
        </span>
      </div>
      <div className="actions voice-actions">
        {status === "idle" ? (
          <button
            className="primary"
            disabled={!available}
            onClick={() => void start()}
          >
            开始实时语音
            <Icon name="voice" size={16} />
          </button>
        ) : (
          <button
            className="danger"
            onClick={() => {
              setHint("通话已停止。");
              stop();
            }}
          >
            结束通话
          </button>
        )}
      </div>
      {hint && (
        <p role="status" className="muted small">
          {hint}
        </p>
      )}
      <p className="muted small">
        需要麦克风权限、已启用的 voice_bridge，以及可达的音频桥。设备回声和打断能力取决于桥的运行模式。
      </p>
    </section>
  );
}
