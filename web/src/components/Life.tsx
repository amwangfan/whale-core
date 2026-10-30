import { useCallback, useState } from "react";
import { api, ConsoleSettings, Job } from "../api";
import { useApi } from "../hooks";
import { JobResult } from "./Media";
export default function Life() {
  const state = useApi(useCallback(() => api.life(), []));
  const settings = useApi(useCallback(() => api.settings(), []));
  const [topic, setTopic] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setError("");
    try {
      await fn();
      state.reload();
      settings.reload();
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <div>
      <div className="page-head">
        <div>
          <h2>主动生活</h2>
          <p className="muted">保持好奇，也尊重你的节奏。</p>
        </div>
        <button
          className={state.data?.settings.paused ? "primary" : ""}
          onClick={() =>
            void run(async () => {
              await api.saveSettings({
                life: {
                  ...state.data!.settings,
                  paused: !state.data?.settings.paused,
                },
              });
              setNotice(
                state.data?.settings.paused
                  ? "自主探索已开启，将按设置的窗口运行。"
                  : "主动行为已暂停。",
              );
            })
          }
          disabled={!state.data}
        >
          {state.data?.settings.paused ? "开启主动生活" : "暂停主动生活"}
        </button>
      </div>
      {(error || state.error) && (
        <div className="banner error">{error || state.error}</div>
      )}
      {notice && <div className="banner ok">{notice}</div>}
      <div className="overview-columns">
        <section className="panel settings-panel">
          <div className="panel-head">
            <h3>最近感兴趣的事</h3>
            <span className="badge">
              {state.data?.interests.length || 0} 个话题
            </span>
          </div>
          <form
            className="searchbar"
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                await api.interest(topic);
                setTopic("");
              });
            }}
          >
            <input
              aria-label="兴趣话题"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="比如：AR 眼镜、电子音乐…"
            />
            <button className="primary" disabled={!topic.trim()}>
              添加
            </button>
          </form>
          <ul className="list">
            {state.data?.interests.map((i) => (
              <li className="interest-row" key={i.topic}>
                <strong>{i.topic}</strong>
                <span className="muted small">兴趣 {i.weight.toFixed(1)}</span>
                <button
                  className="text-button"
                  onClick={() =>
                    void run(async () => {
                      await api.lifeFeedback(i.topic, "liked");
                    })
                  }
                >
                  喜欢
                </button>
                <button
                  className="text-button"
                  onClick={() =>
                    void run(async () => {
                      await api.lifeFeedback(i.topic, "ignored");
                      setNotice("已延长下次探索间隔。");
                    })
                  }
                >
                  晚点
                </button>
                <button
                  className="text-button"
                  onClick={() =>
                    void run(async () => {
                      await api.interest(i.topic, true);
                    })
                  }
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
          <p className="muted small">
            探索只从你允许的来源中生成候选，发送仍经过渠道策略与审批。
          </p>
          <button
            disabled={state.data?.settings.paused}
            onClick={() =>
              void run(async () => {
                setJob(await api.submitJob("explore", {}));
                setNotice("探索任务已创建，请到任务记录查看进度。");
              })
            }
          >
            探索一次
          </button>
          {job && <JobResult job={job} />}
        </section>
        <section className="panel settings-panel">
          <div className="panel-head">
            <h3>探索日记</h3>
          </div>
          {!state.data?.diary.length ? (
            <div className="empty">
              还没有日记。添加兴趣、设置允许的来源后，开启第一次探索。
            </div>
          ) : (
            state.data.diary
              .slice(-10)
              .reverse()
              .map((d, i) => (
                <div key={i} className="diary-entry">
                  <span className="muted small">{d.day}</span>
                  <p>{d.note}</p>
                  {d.sources.map((url) => (
                    <a key={url} href={url} target="_blank" rel="noreferrer">
                      阅读来源 ↗
                    </a>
                  ))}
                </div>
              ))
          )}
        </section>
      </div>
      {settings.data && (
        <LifeSettings
          initial={settings.data}
          save={(value) =>
            run(async () => {
              await api.saveSettings(value);
              setNotice("策略已保存。");
            })
          }
        />
      )}
    </div>
  );
}
function LifeSettings({
  initial,
  save,
}: {
  initial: ConsoleSettings;
  save: (value: Partial<ConsoleSettings>) => Promise<void>;
}) {
  const [life, setLife] = useState(initial.life);
  const [hosts, setHosts] = useState(initial.life.allowed_hosts.join("\n"));
  const [shell, setShell] = useState(initial.shell);
  const [busy, setBusy] = useState(false);
  return (
    <section className="panel settings-panel">
      <div className="panel-head">
        <h3>打扰与执行边界</h3>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setBusy(true);
          void save({
            life: {
              ...life,
              paused: initial.life.paused,
              allowed_hosts: hosts
                .split("\n")
                .map((h) => h.trim())
                .filter(Boolean),
            },
            shell,
          }).finally(() => setBusy(false));
        }}
      >
        <div className="form-grid">
          <label className="field">
            每天主动消息上限
            <input
              type="number"
              min="0"
              max="100"
              value={life.daily_limit}
              onChange={(e) =>
                setLife((v) => ({ ...v, daily_limit: Number(e.target.value) }))
              }
            />
          </label>
          <label className="field">
            探索间隔（秒）
            <input
              type="number"
              min="300"
              value={life.interval_s}
              onChange={(e) =>
                setLife((v) => ({ ...v, interval_s: Number(e.target.value) }))
              }
            />
          </label>
          <label className="field">
            静默开始（上海时间）
            <input
              type="time"
              value={life.quiet_start}
              onChange={(e) =>
                setLife((v) => ({ ...v, quiet_start: e.target.value }))
              }
            />
          </label>
          <label className="field">
            静默结束
            <input
              type="time"
              value={life.quiet_end}
              onChange={(e) =>
                setLife((v) => ({ ...v, quiet_end: e.target.value }))
              }
            />
          </label>
          <label className="field">
            允许探索的来源（每行一个域名）
            <textarea
              rows={4}
              value={hosts}
              placeholder="例如：arxiv.org"
              onChange={(e) => setHosts(e.target.value)}
            />
          </label>
          <label className="field">
            Shell 执行档位
            <select
              value={shell.tier}
              onChange={(e) =>
                setShell((v) => ({ ...v, tier: e.target.value }))
              }
            >
              <option value="managed">managed · 无自由命令</option>
              <option value="safe">safe · 无自由命令</option>
              <option value="personal">personal · 审批后隔离执行</option>
            </select>
            <small>Linux 命名空间和资源限制不可用时拒绝运行。</small>
          </label>
        </div>
        <fieldset>
          <legend>允许的主动渠道</legend>
          {["hud", "phone_notice", "direct_message", "call"].map((c) => (
            <label className="check-label" key={c}>
              <input
                type="checkbox"
                checked={life.allowed_channels.includes(c)}
                onChange={(e) =>
                  setLife((v) => ({
                    ...v,
                    allowed_channels: e.target.checked
                      ? [...v.allowed_channels, c]
                      : v.allowed_channels.filter((x) => x !== c),
                  }))
                }
              />
              {c}
            </label>
          ))}
          <label className="check-label">
            <input
              type="checkbox"
              checked={life.allow_calls}
              onChange={(e) =>
                setLife((v) => ({ ...v, allow_calls: e.target.checked }))
              }
            />
            允许主动电话（仍需单次审批）
          </label>
        </fieldset>
        <button className="primary" disabled={busy}>
          {busy ? "保存中…" : "保存策略"}
        </button>
      </form>
    </section>
  );
}
