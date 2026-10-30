import { useCallback, useEffect, useState } from "react";
import { api, Artifact, Job, rawRequest } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";
import RealtimeCall from "./RealtimeCall";
export const TERMINAL = [
  "succeeded",
  "failed",
  "denied",
  "cancelled",
  "unknown",
];
export function ArtifactView({ artifact }: { artifact: Artifact }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    void rawRequest(`/artifacts/${encodeURIComponent(artifact.artifact_id)}`)
      .then((r) => r.blob())
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (!cancelled) setUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [artifact.artifact_id]);
  if (error) return <div className="banner error">{error}</div>;
  if (!url) return <div className="muted">读取产物…</div>;
  return (
    <div className="artifact">
      {artifact.media_type.startsWith("image/") ? (
        <img src={url} alt="鲸鱼生成的图片" />
      ) : artifact.media_type.startsWith("audio/") ? (
        <audio controls src={url} />
      ) : (
        <video controls src={url} />
      )}
      <a
        className="download-link"
        href={url}
        download={`whale-${artifact.artifact_id}.${artifact.media_type.split("/")[1]}`}
      >
        下载产物 ↗
      </a>
    </div>
  );
}
export function JobResult({ job }: { job: Job }) {
  return (
    <div className="job-result">
      <div className="actions">
        <span
          className={`badge ${job.status === "succeeded" ? "ok" : job.status === "failed" ? "bad" : "warn"}`}
        >
          {job.status}
        </span>
        <span className="muted small mono">{job.task_id}</span>
      </div>
      {job.error && <div className="banner error">{job.error}</div>}
      {job.result?.text && <pre className="result-text">{job.result.text}</pre>}
      {job.result?.reply && (
        <pre className="result-text">{job.result.reply}</pre>
      )}
      {job.result?.artifacts?.map((a) => (
        <ArtifactView artifact={a} key={a.artifact_id} />
      ))}
      {job.result &&
        Object.keys(job.result).some(
          (k) => !["text", "reply", "artifacts"].includes(k),
        ) && (
          <details>
            <summary>执行详情</summary>
            <pre className="result-text">
              {JSON.stringify(job.result, null, 2)}
            </pre>
          </details>
        )}
    </div>
  );
}
const MODES = [
  {
    key: "channel",
    name: "设备投递",
    icon: "approvals",
    hint: '填写 {"channel":"hud","text":"想传达的话"}；接收方取插件固定配置，投递前需要审批。',
  },
  {
    key: "plugin",
    name: "外置插件",
    icon: "plugins",
    hint: "填写插件动作 JSON，审批后通过网关执行。",
  },
  {
    key: "image",
    name: "图片",
    icon: "media",
    hint: "把脑海中的画面变成一张图片。",
  },
  { key: "tts", name: "声音", icon: "chat", hint: "让文字拥有声音。" },
  { key: "asr", name: "听写", icon: "events", hint: "上传音频，转成文字。" },
  {
    key: "video",
    name: "视频",
    icon: "media",
    hint: "通过已配置的视频服务桥创建异步任务。",
  },
  {
    key: "shell",
    name: "受控命令",
    icon: "tasks",
    hint: "命令需要审批，并在 Linux 隔离环境中执行。",
  },
  {
    key: "skill",
    name: "工作流",
    icon: "plugins",
    hint: "将已有工具组合成可复用的工作流。",
  },
  {
    key: "voice",
    name: "实时语音",
    icon: "chat",
    hint: "连接独立音频桥，实时链路直接传输音频。",
  },
];
export default function Media() {
  const [mode, setMode] = useState("image");
  const [text, setText] = useState("");
  const [size, setSize] = useState("1024x1024");
  const [voice, setVoice] = useState("");
  const [rate, setRate] = useState("1");
  const [file, setFile] = useState<File | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [session, setSession] = useState<{
    url: string;
    session_id: string;
  } | null>(null);
  const plugins = useApi(useCallback(() => api.plugins(), []));
  useEffect(() => {
    if (
      !job ||
      TERMINAL.includes(job.status) ||
      job.status === "pending_approval"
    )
      return;
    let active = true;
    const timer = setInterval(() => {
      void api
        .job(job.task_id)
        .then((j) => {
          if (active) setJob(j);
        })
        .catch((e) => {
          if (active) setError(String(e));
        });
    }, 1200);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [job?.task_id, job?.status]);
  const submit = async () => {
    setBusy(true);
    setError("");
    setSession(null);
    try {
      if (mode === "voice") {
        setSession(await api.voiceSession());
        return;
      }
      let args: Record<string, unknown> = { prompt: text, text, size };
      if (mode === "tts") args = { text, voice, rate: Number(rate) };
      if (mode === "shell") args = { command: text };
      if (mode === "plugin" || mode === "channel") args = JSON.parse(text);
      if (mode === "skill") args = { definition: text, input: "" };
      if (mode === "asr") {
        if (!file) throw new Error("请先选择音频文件");
        if (file.size > 25 * 1024 * 1024) throw new Error("音频不能超过 25 MB");
        const audio = await new Promise<string>((resolve, reject) => {
          const r = new FileReader();
          r.onload = () => resolve(String(r.result).split(",")[1]);
          r.onerror = () => reject(new Error("读取音频失败"));
          r.readAsDataURL(file);
        });
        args = { audio, lang: "zh" };
      }
      if (mode === "video")
        args = { prompt: text, duration_s: 5, size: "1280x720" };
      setJob(await api.submitJob(mode, args));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const working =
    job && !TERMINAL.includes(job.status) && job.status !== "pending_approval";
  const selected = MODES.find((m) => m.key === mode)!;
  return (
    <div>
      <div className="page-head">
        <div>
          <h2>声音与创作</h2>
          <p className="muted">一个想法，可以有很多种表达。</p>
        </div>
      </div>
      <div className="mode-tabs">
        {MODES.map((m) => (
          <button
            disabled={Boolean(working)}
            key={m.key}
            className={mode === m.key ? "active" : ""}
            onClick={() => {
              setMode(m.key);
              setJob(null);
              setError("");
              setText("");
            }}
          >
            <Icon name={m.icon} size={17} />
            {m.name}
          </button>
        ))}
      </div>
      {mode === "voice" ? (
        <RealtimeCall />
      ) : (
        <div className="studio-columns">
          <section className="panel studio-form">
            <h3>{selected.name}工作台</h3>
            <p className="muted">{selected.hint}</p>
            {mode === "asr" ? (
              <label className="file-drop">
                <Icon name="events" size={32} />
                <strong>选择一段音频</strong>
                <span>WAV / MP3 / WebM · 最多 25 MB</span>
                <input
                  aria-label="上传音频"
                  type="file"
                  accept="audio/*"
                  onChange={(e) => setFile(e.target.files?.[0] || null)}
                />
                {file && <span>{file.name}</span>}
              </label>
            ) : (
              mode !== "voice" && (
                <label className="field">
                  <span>
                    {mode === "shell"
                      ? "命令"
                      : mode === "skill"
                        ? "工作流定义"
                        : "内容描述"}
                  </span>
                  <textarea
                    rows={7}
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    placeholder={
                      mode === "image"
                        ? "例如：清晨的海边，一只跃出水面的鲸鱼，温柔的蓝绿色调…"
                        : mode === "tts"
                          ? "输入你希望鲸鱼读出的文字…"
                          : mode === "shell"
                            ? "例如：ls -la"
                            : mode === "skill"
                              ? "# skill: research\n## step: search\ntool: search_web\nargs:\n  query: 鲸鱼\nsave_as: results"
                              : "描述画面、动作和氛围…"
                    }
                  />
                </label>
              )
            )}
            {mode === "image" && (
              <label className="field">
                <span>画面比例</span>
                <select value={size} onChange={(e) => setSize(e.target.value)}>
                  <option value="1024x1024">正方形 · 1024 × 1024</option>
                  <option value="1536x1024">横向 · 1536 × 1024</option>
                  <option value="1024x1536">纵向 · 1024 × 1536</option>
                </select>
              </label>
            )}
            {mode === "tts" && (
              <div className="form-grid">
                <label className="field">
                  音色
                  <input
                    value={voice}
                    onChange={(e) => setVoice(e.target.value)}
                    placeholder="默认音色"
                  />
                </label>
                <label className="field">
                  语速
                  <input
                    type="number"
                    min="0.25"
                    max="4"
                    step="0.1"
                    value={rate}
                    onChange={(e) => setRate(e.target.value)}
                  />
                </label>
              </div>
            )}
            {mode === "shell" && (
              <p className="banner warn">
                默认 managed 档禁止自由命令。先在主动生活页设置执行档位；依赖
                Linux bubblewrap，隔离不可用时拒绝执行。
              </p>
            )}
            <button
              className="primary"
              disabled={
                busy ||
                Boolean(working) ||
                (mode !== "voice" && mode !== "asr" && !text.trim())
              }
              onClick={() => void submit()}
            >
              {busy
                ? "提交中…"
                : mode === "voice"
                  ? "创建实时会话"
                  : "创建任务"}
              <Icon name="arrow" size={16} />
            </button>
            {error && (
              <div className="banner error" role="alert">
                {error}
              </div>
            )}
            {session && (
              <div className="banner ok">
                <p>会话已创建。请使用桥接端兼容的设备或客户端连接：</p>
                <code className="break-all">{session.url}</code>
              </div>
            )}
          </section>
          <section className="panel studio-output">
            <div className="panel-head">
              <h3>创作结果</h3>
              {job && working && (
                <button
                  onClick={() =>
                    void api
                      .cancelJob(job.task_id)
                      .then(setJob)
                      .catch((e) => setError(String(e)))
                  }
                >
                  取消任务
                </button>
              )}
            </div>
            {job ? (
              <>
                <JobResult job={job} />
                {job.status === "pending_approval" && (
                  <p className="banner warn">
                    任务等待审批。前往「待办审批」查看完整参数后决定是否执行。
                  </p>
                )}
                {working && (
                  <div className="working-indicator">
                    <span className="status-dot" />
                    正在执行，可在任务记录中继续查看。
                  </div>
                )}
              </>
            ) : (
              <div className="illustrated-empty">
                <Icon name="media" size={40} />
                <strong>留一点空间，给新想法</strong>
                <p>任务完成后，图片、音频和执行结果会出现在这里。</p>
              </div>
            )}
          </section>
        </div>
      )}
      <p className="muted small">
        当前启用：
        {plugins.data
          ?.filter((p) => p.enabled)
          .map((p) => p.name)
          .join(" · ") || "读取中"}
        。服务未配置时会报告失败，不生成模拟结果。
      </p>
    </div>
  );
}
