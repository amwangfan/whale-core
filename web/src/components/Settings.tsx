import { useCallback, useState, useEffect } from "react";
import { api, type RuntimeSettings } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";

const SERVICES = [
  {
    label: "搜索",
    fields: [
      ["WHALE_SEARCH_PROVIDER", "搜索供应商（tavily / brave / ddg）"],
      ["TAVILY_API_KEY", "Tavily API Key"],
      ["BRAVE_API_KEY", "Brave API Key"],
    ],
  },
  {
    label: "向量记忆召回（可选）",
    fields: [
      ["WHALE_EMBEDDING_BASE_URL", "Embedding API 端点"],
      ["WHALE_EMBEDDING_API_KEY", "Embedding API Key"],
      ["WHALE_EMBEDDING_MODEL", "Embedding 模型名称"],
    ],
  },
  {
    label: "图片生成",
    fields: [
      ["WHALE_IMAGE_BASE_URL", "API 端点"],
      ["WHALE_IMAGE_API_KEY", "API Key"],
      ["WHALE_IMAGE_MODEL", "模型名称"],
    ],
  },
  {
    label: "语音识别与合成",
    fields: [
      ["WHALE_SPEECH_BASE_URL", "API 端点"],
      ["WHALE_SPEECH_API_KEY", "API Key"],
      ["WHALE_ASR_MODEL", "识别模型"],
      ["WHALE_TTS_MODEL", "合成模型"],
      ["WHALE_TTS_VOICE", "音色"],
    ],
  },
  {
    label: "视频生成",
    fields: [
      ["WHALE_VIDEO_BASE_URL", "API 端点"],
      ["WHALE_VIDEO_API_KEY", "API Key"],
      ["WHALE_VIDEO_MODEL", "模型名称"],
      ["DASHSCOPE_API_KEY", "DashScope API Key"],
    ],
  },
  {
    label: "实时语音",
    fields: [
      ["WHALE_REALTIME_URL", "WebSocket 端点"],
      ["WHALE_REALTIME_API_KEY", "API Key"],
      ["WHALE_REALTIME_MODEL", "模型名称"],
      ["WHALE_REALTIME_VOICE", "音色"],
      ["WHALE_REALTIME_PROTOCOL", "会话协议（ga / legacy，默认 ga）"],
    ],
  },
];
export default function Settings({
  onDisconnect,
  onRunSetup,
}: {
  onDisconnect: () => void;
  onRunSetup?: () => void;
}) {
  const load = useCallback(() => api.settings(), []);
  const settings = useApi(load);
  const diagnosticLoad = useCallback(() => api.diagnostics(), []);
  const diagnostics = useApi(diagnosticLoad);
  const [draft, setDraft] = useState<RuntimeSettings | null>(null);
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [services, setServices] = useState<Record<string, string | null>>({});
  const [taboosText, setTaboosText] = useState("");
  const [budgetDraft, setBudgetDraft] = useState({
    model_calls: 200,
    media_jobs: 50,
  });
  const [roleModels, setRoleModels] = useState<
    Record<string, { base_url?: string; model?: string; api_key?: string }>
  >({});
  useEffect(() => {
    if (settings.data) {
      setDraft(settings.data);
      setServices(
        Object.fromEntries(
          Object.entries(settings.data.services ?? {}).map(([k, v]) => [
            k,
            typeof v === "string" ? v : "",
          ]),
        ),
      );
      setTaboosText((settings.data.persona_taboos ?? []).join("\n"));
      if (settings.data.budget) {
        setBudgetDraft({ ...settings.data.budget.limits });
      }
      const m: Record<string, { base_url?: string; model?: string; api_key?: string }> = {};
      for (const [role, cfg] of Object.entries(settings.data.models ?? {})) {
        m[role] = { base_url: cfg.base_url ?? "", model: cfg.model ?? "", api_key: "" };
      }
      setRoleModels(m);
    }
  }, [settings.data]);
  const save = async () => {
    if (!draft) return;
    setBusy("save");
    setNotice("");
    setError("");
    try {
      const validBudget = (n: number) =>
        Number.isInteger(n) && n >= 0 && n <= 100000;
      if (
        !validBudget(budgetDraft.model_calls) ||
        !validBudget(budgetDraft.media_jobs)
      ) {
        setError("请求预算必须是 0–100000 的整数（0 = 关闭该能力）。");
        return;
      }
      await api.saveSettings({
        profile: draft.profile,
        model: {
          base_url: draft.model.base_url,
          model: draft.model.model,
          ...(clearKey
            ? { api_key: null }
            : key.trim()
              ? { api_key: key.trim() }
              : {}),
        },
        intent_model_enabled: draft.intent_model_enabled,
        persona_taboos: taboosText
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
        models: Object.fromEntries(
          Object.entries(roleModels).map(([role, cfg]) => [
            role,
            {
              base_url: cfg.base_url || "",
              model: cfg.model || "",
              ...(cfg.api_key ? { api_key: cfg.api_key } : {}),
            },
          ]),
        ),
        services,
        budget: {
          model_calls: budgetDraft.model_calls,
          media_jobs: budgetDraft.media_jobs,
        },
      });
      setKey("");
      setClearKey(false);
      settings.reload();
      diagnostics.reload();
      setNotice("设置已保存并生效。");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };
  const test = async () => {
    setBusy("test");
    setNotice("");
    setError("");
    try {
      const r = await api.testProvider();
      if (r.ok) setNotice(`模型连接成功 · ${r.latency_ms} ms`);
      else
        setError(
          `模型连接失败：${r.error_kind ?? "无有效回复"}。请检查模型端点、名称与密钥。`,
        );
      diagnostics.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };
  return (
    <div>
      {settings.error && <div className="banner error">{settings.error}</div>}
      {settings.loading && !draft && <div className="empty">正在读取设置…</div>}
      {notice && (
        <div className="banner ok" role="status">
          {notice}
        </div>
      )}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div className="settings-grid">
        <section className="surface">
          <div className="section-head">
            <h2>模型与运行配置</h2>
            <Icon name="settings" size={18} />
          </div>
          {onRunSetup && (
            <button type="button" className="text-button" onClick={onRunSetup}>
              重新打开首次配置向导
            </button>
          )}
          {draft && (
            <form
              className="settings-form"
              onSubmit={(e) => {
                e.preventDefault();
                void save();
              }}
            >
              <div className="form-field">
                <label htmlFor="profile">运行方式</label>
                <select
                  id="profile"
                  value={draft.profile}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      profile: e.target.value as "local" | "cloud",
                    })
                  }
                >
                  <option value="local">本地模型 / OpenAI 兼容服务</option>
                  <option value="cloud">云端模型 / OpenAI 兼容服务</option>
                </select>
                <small>
                  切换运行方式后，请核对下面的端点与模型名称。保存后立即生效。
                </small>
              </div>
              <div className="form-field">
                <label htmlFor="model-url">API 端点</label>
                <input
                  id="model-url"
                  type="url"
                  required
                  value={draft.model.base_url}
                  placeholder="https://api.deepseek.com"
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      model: { ...draft.model, base_url: e.target.value },
                    })
                  }
                />
                <small>
                  填写 OpenAI 兼容服务的基础 URL，本地服务可使用回环 HTTP 地址。
                </small>
              </div>
              <div className="form-field">
                <label htmlFor="model-name">模型名称</label>
                <input
                  id="model-name"
                  required
                  value={draft.model.model}
                  placeholder="deepseek-chat"
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      model: { ...draft.model, model: e.target.value },
                    })
                  }
                />
              </div>
              <div className="form-field">
                <label htmlFor="model-key">
                  API 密钥{" "}
                  <span className="badge">
                    {draft.model.api_key_configured ? "已配置" : "未配置"}
                  </span>
                </label>
                <input
                  id="model-key"
                  type="password"
                  autoComplete="new-password"
                  value={key}
                  disabled={clearKey}
                  placeholder={
                    draft.model.api_key_configured
                      ? "留空保留已配置的密钥"
                      : "输入 API Key，本地端点可留空"
                  }
                  onChange={(e) => setKey(e.target.value)}
                />
                <small>
                  密钥保存后不回显。输入新密钥将替换原值；同一配置档内留空保持原值。
                </small>
              </div>
              {draft.profile !== settings.data?.profile && (
                <div className="banner warn">
                  切换运行方式后，另一配置档的旧密钥不会沿用。请核对端点、模型，并按需填写新的
                  API 密钥。
                </div>
              )}
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={clearKey}
                  onChange={(e) => {
                    setClearKey(e.target.checked);
                    if (e.target.checked) setKey("");
                  }}
                />
                保存时清除当前模型密钥
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={draft.intent_model_enabled}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      intent_model_enabled: e.target.checked,
                    })
                  }
                />
                启用模型意图规划
              </label>
              <div className="muted small">
                让模型辅助理解工具与复杂任务；会产生额外模型调用。
              </div>
              <div className="form-field">
                <label>请求预算（每日限额，0 = 关闭该能力）</label>
                <div className="form-field">
                  <label htmlFor="budget-model-calls">模型调用（次/天）</label>
                  <input
                    id="budget-model-calls"
                    type="number"
                    min={0}
                    max={100000}
                    step={1}
                    value={
                      Number.isFinite(budgetDraft.model_calls)
                        ? budgetDraft.model_calls
                        : ""
                    }
                    onChange={(e) =>
                      setBudgetDraft({
                        ...budgetDraft,
                        model_calls: e.target.valueAsNumber,
                      })
                    }
                  />
                </div>
                <div className="form-field">
                  <label htmlFor="budget-media-jobs">媒体生成（次/天）</label>
                  <input
                    id="budget-media-jobs"
                    type="number"
                    min={0}
                    max={100000}
                    step={1}
                    value={
                      Number.isFinite(budgetDraft.media_jobs)
                        ? budgetDraft.media_jobs
                        : ""
                    }
                    onChange={(e) =>
                      setBudgetDraft({
                        ...budgetDraft,
                        media_jobs: e.target.valueAsNumber,
                      })
                    }
                  />
                </div>
                <small>
                  今日已用：模型 {settings.data?.budget?.used.model_calls ?? 0}{" "}
                  次、媒体 {settings.data?.budget?.used.media_jobs ?? 0}{" "}
                  次（预算日 {settings.data?.budget?.day ?? "—"}
                  ，次日自动重置）。超限的请求会被拒绝；失败的请求也会占用额度。
                </small>
              </div>
              <div className="form-field">
                <label>分角色模型（可选覆盖）</label>
                {(["intent", "executor"] as const).map((role) => (
                  <div key={role} className="role-model-row">
                    <div className="muted small">
                      {role === "intent"
                        ? "意图模型（规划+发送前审核，同一角色）"
                        : "任务执行模型（多轮工具循环，需 WHALE_EXECUTOR_ENABLED=1）"}
                    </div>
                    <input
                      placeholder="模型名（留空=跟随主回复模型）"
                      value={roleModels[role]?.model ?? ""}
                      onChange={(e) =>
                        setRoleModels({
                          ...roleModels,
                          [role]: { ...roleModels[role], model: e.target.value },
                        })
                      }
                    />
                    <input
                      placeholder="API 端点（留空=跟随主端点）"
                      value={roleModels[role]?.base_url ?? ""}
                      onChange={(e) =>
                        setRoleModels({
                          ...roleModels,
                          [role]: { ...roleModels[role], base_url: e.target.value },
                        })
                      }
                    />
                    <input
                      type="password"
                      placeholder={settings.data?.models?.[role]?.api_key_configured ? "已配置（留空保持）" : "API key（可选）"}
                      value={roleModels[role]?.api_key ?? ""}
                      onChange={(e) =>
                        setRoleModels({
                          ...roleModels,
                          [role]: { ...roleModels[role], api_key: e.target.value },
                        })
                      }
                    />
                  </div>
                ))}
                <small>
                  意图/任务执行/安全审核模型可单独换端点与密钥；留空的项跟随主回复模型。
                </small>
              </div>
              <div className="form-field">
                <label htmlFor="persona-taboos">人格禁忌（用户自设）</label>
                <textarea
                  id="persona-taboos"
                  rows={4}
                  value={taboosText}
                  onChange={(e) => setTaboosText(e.target.value)}
                  placeholder={"每行一条，例如：\n不谈论政治\n23 点后提醒睡觉"}
                />
                <small>
                  核心不预置任何强制禁忌——这里写的每条都会并入当前角色卡的边界，
                  保存后立即生效。
                </small>
              </div>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={services.WHALE_SHELL_ENABLED === "1"}
                  onChange={(e) =>
                    setServices({
                      ...services,
                      WHALE_SHELL_ENABLED: e.target.checked ? "1" : "0",
                    })
                  }
                />
                启用受控 Shell（Linux）
              </label>
              <div className="form-field">
                <label htmlFor="shell-tier">Shell 权限档位</label>
                <select
                  id="shell-tier"
                  value={services.WHALE_SHELL_TIER || "managed"}
                  onChange={(e) =>
                    setServices({
                      ...services,
                      WHALE_SHELL_TIER: e.target.value,
                    })
                  }
                >
                  <option value="managed">managed · 受管命令</option>
                  <option value="safe">safe · 只读命令</option>
                  <option value="personal">personal · 个人自由命令</option>
                </select>
                <small>
                  默认 managed。只有明确选择 personal
                  才允许自由命令；仍须逐次审批并经过系统沙箱，核心硬性拒绝规则继续生效。
                </small>
              </div>
              <div className="muted small">
                需要 Linux、bubblewrap 与
                prlimit；命令逐次审批，在系统沙箱中执行。其他平台不会降级为无隔离运行。
              </div>
              <div className="service-groups">
                <div className="section-label">扩展服务配置</div>
                {SERVICES.map((group) => (
                  <details key={group.label} className="service-group">
                    <summary>
                      {group.label}
                      <span>配置端点与凭据</span>
                    </summary>
                    <div className="service-fields">
                      {group.fields.map(([name, label]) => (
                        <div className="form-field" key={name}>
                          <label htmlFor={name}>
                            {label}
                            {typeof settings.data?.services?.[name] ===
                              "boolean" &&
                              settings.data.services[name] && (
                                <span className="badge">已配置</span>
                              )}
                          </label>
                          <input
                            id={name}
                            type={name.endsWith("_KEY") ? "password" : "text"}
                            autoComplete="off"
                            value={services[name] ?? ""}
                            disabled={services[name] === null}
                            placeholder={
                              name.endsWith("_KEY") ? "留空保留原值" : name
                            }
                            onChange={(e) =>
                              setServices({
                                ...services,
                                [name]: e.target.value,
                              })
                            }
                          />
                          {name.endsWith("_KEY") && (
                            <label className="clear-secret">
                              <input
                                type="checkbox"
                                checked={services[name] === null}
                                onChange={(e) =>
                                  setServices({
                                    ...services,
                                    [name]: e.target.checked ? null : "",
                                  })
                                }
                              />
                              保存时清除此密钥
                            </label>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                ))}
              </div>
              <div className="actions">
                <button
                  className="primary"
                  type="submit"
                  disabled={Boolean(busy)}
                >
                  {busy === "save" ? "正在保存…" : "保存设置"}
                </button>
                <button
                  type="button"
                  onClick={() => void test()}
                  disabled={Boolean(busy)}
                >
                  {busy === "test" ? "正在测试…" : "测试当前连接"}
                </button>
              </div>
              <small className="muted">
                连接测试使用已保存的配置，并发送一次简短模型请求。
              </small>
            </form>
          )}
          <div className="settings-disconnect">
            <button className="text-button" onClick={onDisconnect}>
              <Icon name="logout" size={14} />
              断开当前控制台连接
            </button>
          </div>
        </section>
        <div>
          <section className="surface">
            <div className="section-head">
              <h2>运行诊断</h2>
              <button
                className="text-button"
                disabled={diagnostics.loading}
                onClick={diagnostics.reload}
              >
                <Icon name="refresh" size={14} />
                刷新
              </button>
            </div>
            {diagnostics.error && (
              <div className="banner error">{diagnostics.error}</div>
            )}
            {diagnostics.loading && !diagnostics.data && (
              <div className="empty">正在诊断…</div>
            )}
            {diagnostics.data?.checks.map((check, i) => (
              <div className="diagnostic-item" key={`${check.name}-${i}`}>
                <span
                  className={
                    check.status === "ok"
                      ? "badge ok"
                      : check.status === "fail"
                        ? "badge bad"
                        : "badge warn"
                  }
                >
                  {check.status === "ok"
                    ? "正常"
                    : check.status === "fail"
                      ? "异常"
                      : "注意"}
                </span>
                <div>
                  <strong>{check.name}</strong>
                  <p>{check.detail}</p>
                </div>
              </div>
            ))}
          </section>
          <section className="surface" style={{ marginTop: 20 }}>
            <div className="section-head">
              <h2>当前能力</h2>
            </div>
            <p className="muted small">由运行时已挂载的工具提供。</p>
            <div className="capability-tags">
              {settings.data?.capabilities.map((c) => (
                <span className="badge mono" key={c}>
                  {c}
                </span>
              ))}
              {settings.data?.capabilities.length === 0 && (
                <span className="muted small">
                  尚未挂载工具，可前往插件页查看配置。
                </span>
              )}
            </div>
            {settings.data?.notes.map((n, i) => (
              <p
                className="muted small"
                key={i}
                style={{ lineHeight: 1.8, marginTop: 14 }}
              >
                {n}
              </p>
            ))}
          </section>
        </div>
      </div>
    </div>
  );
}
