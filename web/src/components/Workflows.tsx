import { useCallback, useEffect, useState } from "react";
import { api, type WorkflowSettings } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";
import Life from "./Life";

export default function Workflows() {
  const loadSkills = useCallback(() => api.skills(), []);
  const skills = useApi(loadSkills);
  const loadSettings = useCallback(() => api.workflowSettings(), []);
  const settings = useApi(loadSettings);
  const [draft, setDraft] = useState<WorkflowSettings | null>(null);
  const [skill, setSkill] = useState("");
  const [input, setInput] = useState("");
  const [topics, setTopics] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<unknown>(null);
  useEffect(() => {
    if (settings.data) setDraft(settings.data);
  }, [settings.data]);
  useEffect(() => {
    if (skills.data?.length && !skill) setSkill(skills.data[0].name);
  }, [skills.data, skill]);
  const run = async (kind: "skill" | "explore" | "settings") => {
    setBusy(kind);
    setError("");
    setNotice("");
    try {
      if (kind === "settings" && draft) {
        await api.saveWorkflowSettings(draft);
        settings.reload();
        setNotice("主动行为设置已保存。");
      } else if (kind === "skill") setResult(await api.runSkill(skill, input));
      else if (kind === "explore")
        setResult(
          await api.explore(
            topics
              .split(/[,，\n]/)
              .map((s) => s.trim())
              .filter(Boolean),
          ),
        );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };
  return (
    <div>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="banner ok" role="status">
          {notice}
        </div>
      )}
      <div className="settings-grid">
        <div>
          <section className="surface">
            <div className="section-head">
              <h2>Skill 工作流</h2>
              <Icon name="workflow" size={20} />
            </div>
            <p className="muted small">
              组合已有工具完成一个目标。每一步仍遵守核心的权限策略。
            </p>
            {skills.error && <div className="banner error">{skills.error}</div>}
            {skills.data?.length === 0 && (
              <div className="empty">暂无可用工作流。</div>
            )}
            {Boolean(skills.data?.length) && (
              <form
                className="settings-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void run("skill");
                }}
              >
                <div className="form-field">
                  <label htmlFor="workflow-skill">选择工作流</label>
                  <select
                    id="workflow-skill"
                    value={skill}
                    onChange={(e) => setSkill(e.target.value)}
                  >
                    {skills.data?.map((s) => (
                      <option key={s.name} value={s.name}>
                        {s.description || s.name}
                      </option>
                    ))}
                  </select>
                  <small>
                    {skills.data?.find((s) => s.name === skill)?.name}
                  </small>
                </div>
                <div className="form-field">
                  <label htmlFor="workflow-input">这次的目标</label>
                  <textarea
                    id="workflow-input"
                    rows={3}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    required
                    placeholder="例如：查找一种新技术的原始来源…"
                  />
                </div>
                <button
                  className="primary"
                  disabled={Boolean(busy) || !skill || !input.trim()}
                  type="submit"
                >
                  {busy === "skill" ? "运行中…" : "运行工作流"}
                  <Icon name="arrow" size={15} />
                </button>
              </form>
            )}
          </section>
          <section className="surface" style={{ marginTop: 22 }}>
            <div className="section-head">
              <h2>一起探索</h2>
              <Icon name="explore" size={20} />
            </div>
            <p className="muted small">
              围绕你感兴趣的话题搜索资料，形成候选分享与探索记录。
            </p>
            <form
              className="settings-form"
              onSubmit={(e) => {
                e.preventDefault();
                void run("explore");
              }}
            >
              <div className="form-field">
                <label htmlFor="explore-topics">探索话题</label>
                <textarea
                  id="explore-topics"
                  rows={3}
                  value={topics}
                  onChange={(e) => setTopics(e.target.value)}
                  placeholder="AI 可穿戴设备，机器人，独立创作"
                  required
                />
                <small>
                  用逗号或换行分隔多个话题。候选内容先进入待办，不会立即对外发送。
                </small>
              </div>
              <button
                className="primary"
                disabled={Boolean(busy) || !topics.trim()}
                type="submit"
              >
                {busy === "explore" ? "正在探索…" : "探索一次"}
                <Icon name="arrow" size={15} />
              </button>
            </form>
          </section>
          <Life />
        </div>
        <div>
          <section className="surface">
            <div className="section-head">
              <h2>主动行为</h2>
              <span className={draft?.paused ? "badge" : "badge ok"}>
                {draft?.paused ? "已暂停" : "已开启"}
              </span>
            </div>
            {settings.error && (
              <div className="banner error">{settings.error}</div>
            )}
            {draft && (
              <form
                className="settings-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void run("settings");
                }}
              >
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={draft.paused}
                    onChange={(e) =>
                      setDraft({ ...draft, paused: e.target.checked })
                    }
                  />
                  暂停主动行为
                </label>
                <div className="form-field">
                  <label htmlFor="daily-limit">每日行动上限</label>
                  <input
                    id="daily-limit"
                    type="number"
                    min={0}
                    max={1000}
                    value={draft.daily_limit}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        daily_limit: Number(e.target.value),
                      })
                    }
                  />
                </div>
                <div className="form-columns">
                  <div className="form-field">
                    <label htmlFor="quiet-start">静默开始</label>
                    <input
                      id="quiet-start"
                      type="time"
                      value={draft.quiet_start}
                      onChange={(e) =>
                        setDraft({ ...draft, quiet_start: e.target.value })
                      }
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="quiet-end">静默结束</label>
                    <input
                      id="quiet-end"
                      type="time"
                      value={draft.quiet_end}
                      onChange={(e) =>
                        setDraft({ ...draft, quiet_end: e.target.value })
                      }
                    />
                  </div>
                </div>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={draft.schedule_enabled ?? false}
                    onChange={(e) =>
                      setDraft({ ...draft, schedule_enabled: e.target.checked })
                    }
                  />
                  定期自动探索
                </label>
                {draft.schedule_enabled && (
                  <div className="form-field">
                    <label htmlFor="explore-interval">探索间隔（分钟）</label>
                    <input
                      id="explore-interval"
                      type="number"
                      min={1}
                      max={10080}
                      value={Math.round((draft.interval_s ?? 3600) / 60)}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          interval_s: Number(e.target.value) * 60,
                        })
                      }
                    />
                    <small>
                      后台定期运行，可能调用已配置搜索服务。发现内容仍先进入候选草稿箱。
                    </small>
                  </div>
                )}
                <div className="form-field">
                  <label htmlFor="workflow-timezone">时区</label>
                  <input
                    id="workflow-timezone"
                    value={draft.timezone}
                    onChange={(e) =>
                      setDraft({ ...draft, timezone: e.target.value })
                    }
                    required
                    placeholder="Asia/Shanghai"
                  />
                </div>
                <button type="submit" disabled={Boolean(busy)}>
                  {busy === "settings" ? "保存中…" : "保存主动行为设置"}
                </button>
              </form>
            )}
          </section>
          {result !== null && (
            <section className="surface" style={{ marginTop: 22 }}>
              <div className="section-head">
                <h2>本次执行记录</h2>
              </div>
              <pre className="memory-json">
                {JSON.stringify(result, null, 2)}
              </pre>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
