import { useCallback, useEffect, useState } from "react";
import { api, type CapabilityResult, type VideoTask } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";
import RealtimeVoice from "./RealtimeVoice";
import RealtimeCall from "./RealtimeCall";
import ShellTerminal from "./ShellTerminal";

const MODES = [
  {
    id: "media.image.generate",
    label: "生成图片",
    icon: "image",
    description: "把一个想法，变成可以看见的画面。",
  },
  {
    id: "tts.synthesize",
    label: "文字转语音",
    icon: "voice",
    description: "选择音色，让文字拥有声音。",
  },
  {
    id: "asr.transcribe",
    label: "语音转文字",
    icon: "voice",
    description: "上传一段录音，留下清晰的文字。",
  },
  {
    id: "media.video.generate",
    label: "生成视频",
    icon: "image",
    description: "从一句描述开始，创造一段动态画面。",
  },
  {
    id: "shell.execute",
    label: "受控终端",
    icon: "events",
    description: "通过审批与系统沙箱执行命令。",
  },
  {
    id: "voice.realtime",
    label: "实时语音",
    icon: "voice",
    description: "和鲸鱼直接说说话。",
  },
  {
    id: "voice.pcm_bridge",
    label: "低延迟通话",
    icon: "voice",
    description: "经 whale-io 音频桥的原生 PCM 通话（AudioWorklet + WebSocket）。",
  },
];

export function artifactPaths(text: string): string[] {
  return [
    ...new Set(
      text.match(/\/api\/v1\/artifacts\/[a-f0-9]{32}(?![a-f0-9])/g) ?? [],
    ),
  ];
}

export function ArtifactPreview({ path }: { path: string }) {
  const [media, setMedia] = useState<{ url: string; type: string } | null>(
    null,
  );
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    void api
      .artifact(path, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setMedia({ url: objectUrl, type: blob.type });
      })
      .catch((e) => {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);
  if (error) return <div className="banner error">产物读取失败：{error}</div>;
  if (!media) return <div className="empty">正在读取产物…</div>;
  const extension = media.type.startsWith("image/")
    ? media.type.split("/")[1]
    : media.type.startsWith("video/")
      ? "mp4"
      : "mp3";
  return (
    <div className="artifact-preview">
      {media.type.startsWith("image/") && (
        <img src={media.url} alt="鲸鱼生成的图片" />
      )}
      {media.type.startsWith("audio/") && <audio controls src={media.url} />}
      {media.type.startsWith("video/") && <video controls src={media.url} />}
      <a
        className="download-link"
        href={media.url}
        download={`whale-${path.split("/").pop()}.${extension}`}
      >
        下载产物
        <Icon name="arrow" size={14} />
      </a>
    </div>
  );
}

export default function Media() {
  const settingsLoad = useCallback(() => api.settings(), []);
  const settings = useApi(settingsLoad);
  const [mode, setMode] = useState(MODES[0].id);
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState("1024x1024");
  const [voice, setVoice] = useState("");
  const [duration, setDuration] = useState(5);
  const [file, setFile] = useState<File | null>(null);
  const [language, setLanguage] = useState("zh");
  const [result, setResult] = useState<CapabilityResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [videos, setVideos] = useState<VideoTask[]>([]);
  const selected = MODES.find((m) => m.id === mode)!;
  const available = settings.data?.capabilities.includes(mode) ?? false;
  const loadVideos = useCallback(async () => {
    try {
      setVideos(await api.videoTasks());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    if (mode === "media.video.generate" && available) void loadVideos();
  }, [mode, available, loadVideos]);
  const switchMode = (id: string) => {
    setMode(id);
    setResult(null);
    setError("");
    setSize(id === "media.video.generate" ? "1280x720" : "1024x1024");
  };
  const run = async () => {
    setBusy("run");
    setError("");
    setResult(null);
    try {
      let params: Record<string, unknown>;
      if (mode === "asr.transcribe") {
        if (!file) throw new Error("请先选择音频文件。");
        if (file.size > 20 * 1024 * 1024)
          throw new Error("音频文件不能超过 20 MiB。");
        const format = file.name.split(".").pop()?.toLowerCase();
        if (
          !format ||
          !["wav", "webm", "ogg", "mp3", "m4a", "flac", "mp4"].includes(format)
        )
          throw new Error("请选择受支持的音频格式。");
        const audio = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(",")[1]);
          reader.onerror = () => reject(new Error("无法读取音频文件。"));
          reader.readAsDataURL(file);
        });
        params = { audio_base64: audio, format, lang: language };
      } else if (mode === "tts.synthesize")
        params = {
          text: prompt,
          ...(voice.trim() ? { voice: voice.trim() } : {}),
        };
      else if (mode === "media.video.generate")
        params = { prompt, size, duration_s: duration };
      else params = { prompt, size };
      setResult(await api.runCapability(mode, params));
      if (mode === "media.video.generate") void loadVideos();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };
  const videoAction = async (id: string, action: "poll" | "cancel") => {
    setBusy(id);
    setError("");
    try {
      await api.videoAction(id, action);
      await loadVideos();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };
  return (
    <div>
      <div className="mode-tabs" role="tablist" aria-label="媒体能力">
        {MODES.filter(
          (m) =>
            m.id !== "shell.execute" ||
            settings.data?.capabilities.includes("shell.execute"),
        ).map((m) => (
          <button
            key={m.id}
            disabled={Boolean(busy)}
            role="tab"
            aria-selected={mode === m.id}
            className={mode === m.id ? "active" : ""}
            onClick={() => switchMode(m.id)}
          >
            <Icon name={m.icon} size={17} />
            {m.label}
          </button>
        ))}
      </div>
      {settings.error && <div className="banner error">{settings.error}</div>}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {mode === "voice.realtime" ? (
        <RealtimeVoice available={available} />
      ) : mode === "voice.pcm_bridge" ? (
        <RealtimeCall available={available} />
      ) : mode === "shell.execute" ? (
        <ShellTerminal available={available} />
      ) : (
        <div className="settings-grid">
          <section className="surface">
            <div className="section-head">
              <h2>{selected.label}</h2>
              <span className={available ? "badge ok" : "badge warn"}>
                {settings.loading
                  ? "读取配置…"
                  : available
                    ? "已就绪"
                    : "未配置"}
              </span>
            </div>
            <p className="muted small">{selected.description}</p>
            {!settings.loading && !available && (
              <div className="banner warn">
                请先在「设置与诊断」中配置对应服务，保存后刷新本页。
              </div>
            )}
            <form
              className="settings-form"
              onSubmit={(e) => {
                e.preventDefault();
                void run();
              }}
            >
              {mode === "asr.transcribe" ? (
                <>
                  <div className="form-field">
                    <label htmlFor="asr-file">音频文件</label>
                    <input
                      id="asr-file"
                      type="file"
                      accept=".wav,.webm,.ogg,.mp3,.m4a,.flac,.mp4"
                      onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                    />
                    <small>
                      支持 WAV、WebM、OGG、MP3、M4A、FLAC、MP4，最大 20
                      MiB。文件将发送至你配置的识别服务。
                    </small>
                  </div>
                  <div className="form-field">
                    <label htmlFor="asr-language">语言</label>
                    <input
                      id="asr-language"
                      value={language}
                      onChange={(e) => setLanguage(e.target.value)}
                      placeholder="zh"
                    />
                  </div>
                </>
              ) : (
                <div className="form-field">
                  <label htmlFor="media-prompt">
                    {mode === "tts.synthesize" ? "要朗读的文字" : "画面描述"}
                  </label>
                  <textarea
                    id="media-prompt"
                    rows={6}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    placeholder={
                      mode === "tts.synthesize"
                        ? "写下你想听见的那句话…"
                        : "描述主体、环境、光线和你想要的感觉…"
                    }
                    required
                    maxLength={4000}
                  />
                </div>
              )}
              {mode === "tts.synthesize" && (
                <div className="form-field">
                  <label htmlFor="tts-voice">音色（可选）</label>
                  <input
                    id="tts-voice"
                    value={voice}
                    onChange={(e) => setVoice(e.target.value)}
                    placeholder="留空使用已配置音色"
                  />
                </div>
              )}
              {(mode === "media.image.generate" ||
                mode === "media.video.generate") && (
                <div className="form-field">
                  <label htmlFor="media-size">画面尺寸</label>
                  <select
                    id="media-size"
                    value={size}
                    onChange={(e) => setSize(e.target.value)}
                  >
                    {(mode === "media.video.generate"
                      ? ["1280x720", "720x1280", "1024x1024"]
                      : ["1024x1024", "1792x1024", "1024x1792"]
                    ).map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                  <small>实际支持的尺寸由供应商和模型决定。</small>
                </div>
              )}
              {mode === "media.video.generate" && (
                <div className="form-field">
                  <label htmlFor="video-duration">时长（秒）</label>
                  <select
                    id="video-duration"
                    value={duration}
                    onChange={(e) => setDuration(Number(e.target.value))}
                  >
                    {[5, 10, 15].map((seconds) => (
                      <option key={seconds} value={seconds}>
                        {seconds} 秒
                      </option>
                    ))}
                  </select>
                  <small>
                    可用时长取决于模型，当前视频供应商接受 5 / 10 / 15 秒。
                  </small>
                </div>
              )}
              <button
                className="primary"
                disabled={
                  Boolean(busy) ||
                  !available ||
                  (mode === "asr.transcribe" ? !file : !prompt.trim())
                }
                type="submit"
              >
                {busy === "run" ? "正在处理…" : selected.label}
                <Icon name="arrow" size={16} />
              </button>
              <small className="muted">
                此操作调用你配置的真实服务，按供应商规则计费。
              </small>
            </form>
          </section>
          <section className="surface">
            <div className="section-head">
              <h2>处理结果</h2>
            </div>
            {!result && !busy && (
              <div className="empty illustrated">
                <Icon name={selected.icon} size={35} />
                <strong>给灵感一点形状</strong>
                <span>生成或识别结果将显示在这里。</span>
              </div>
            )}
            {busy === "run" && (
              <div className="empty">正在等待服务返回，请稍候…</div>
            )}
            {result && (
              <>
                <span
                  className={
                    ["answered", "succeeded"].includes(result.status)
                      ? "badge ok"
                      : "badge warn"
                  }
                >
                  {result.status}
                </span>
                <div className="media-result-text">
                  {result.output || result.reply}
                </div>
                {artifactPaths(
                  `${result.output ?? ""} ${result.reply ?? ""}`,
                ).map((path) => (
                  <ArtifactPreview key={path} path={path} />
                ))}
                {result.task_id && (
                  <p className="muted small">
                    任务 {result.task_id} · 可在任务中心查看回执
                  </p>
                )}
              </>
            )}
          </section>
        </div>
      )}
      {mode === "media.video.generate" && (
        <section className="surface" style={{ marginTop: 22 }}>
          <div className="section-head">
            <h2>视频任务</h2>
            <button
              className="text-button"
              disabled={Boolean(busy) || !available}
              onClick={() => void loadVideos()}
            >
              <Icon name="refresh" size={14} />
              刷新列表
            </button>
          </div>
          {videos.length === 0 && (
            <div className="empty">还没有视频生成任务。</div>
          )}
          <div className="video-tasks">
            {videos.map((task) => (
              <div className="video-task" key={task.task_id}>
                <div>
                  <span
                    className={
                      task.status === "succeeded"
                        ? "badge ok"
                        : task.status === "failed"
                          ? "badge bad"
                          : "badge"
                    }
                  >
                    {task.status}
                  </span>
                  <span className="muted small">{task.task_id}</span>
                </div>
                <p>{task.prompt}</p>
                {task.error && <div className="banner error">{task.error}</div>}
                {task.url &&
                  artifactPaths(task.url).map((path) => (
                    <ArtifactPreview key={path} path={path} />
                  ))}
                {["pending", "running"].includes(task.status) && (
                  <div className="actions">
                    <button
                      disabled={Boolean(busy)}
                      onClick={() => void videoAction(task.task_id, "poll")}
                    >
                      检查进度
                    </button>
                    <button
                      disabled={Boolean(busy)}
                      onClick={() => void videoAction(task.task_id, "cancel")}
                    >
                      取消任务
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
