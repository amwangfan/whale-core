import { useCallback, useEffect, useState } from "react";
import { api, ApiError, PluginDetail, PluginHealth, PluginInfo } from "../api";
import { useApi } from "../hooks";

const KIND_LABELS: Record<string, string> = {
  input: "输入型",
  output: "输出型",
  channel: "渠道（双向收发）",
  processor: "内部处理",
  model: "模型供给",
  memory: "记忆存储",
};

const PLUGIN_ROLE: Record<string, string> = {
  deepseek: "chat",
  local_model: "chat",
  llm_intent: "intent",
  llm_executor: "executor",
  llm_safety: "safety",
};

const ROLE_LABELS: Record<string, string> = {
  chat: "回复模型（互动对话）",
  intent: "意图模型",
  executor: "任务执行模型",
  safety: "安全审核模型",
};

/** 模型插件专用配置界面：供应商/端点/模型/密钥由用户自行决定。 */
function ModelConfigPanel({
  pluginId,
  onSaved,
}: {
  pluginId: string;
  onSaved: () => void;
}) {
  const role = PLUGIN_ROLE[pluginId] ?? "chat";
  const [endpoint, setEndpoint] = useState("");
  const [modelName, setModelName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    api
      .settings()
      .then((s) => {
        if (!alive) return;
        const cfg = (s.models ?? {})[role];
        // chat 角色的配置在主 model 键里；role 矩阵里的值优先，主键兜底。
        // 端点/模型名正常回显；api_key 永不回显（只报「已配置」占位）。
        const fallback = role === "chat" ? s.model : undefined;
        setEndpoint((cfg?.base_url ?? "") || (fallback?.base_url ?? ""));
        setModelName((cfg?.model ?? "") || (fallback?.model ?? ""));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [role]);

  const save = async () => {
    setBusy("save");
    setNotice("");
    setError("");
    try {
      await api.saveSettings({
        models: {
          [role]: {
            base_url: endpoint,
            model: modelName,
            ...(apiKey ? { api_key: apiKey } : {}),
          },
        },
      });
      setApiKey("");
      setNotice("已保存并生效（装配已重建）。");
      onSaved();
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
      const r = await api.testPlugin(pluginId);
      if (r.ok)
        setNotice(`连接正常${r.latency_ms != null ? ` · ${r.latency_ms} ms` : ""}`);
      else setError(`测试失败：${r.error_kind ?? "未就绪"}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="model-config">
      <div className="section-label">
        {ROLE_LABELS[role] ?? "模型"} · 供应商与模型由你决定
      </div>
      <div className="form-field">
        <label>API 端点（OpenAI 兼容）</label>
        <input
          value={endpoint}
          onChange={(e) => setEndpoint(e.target.value)}
          placeholder="如 https://api.deepseek.com 或 http://127.0.0.1:11434/v1"
        />
      </div>
      <div className="form-field">
        <label>模型名</label>
        <input
          value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          placeholder="如 deepseek-v4.1-flash / qwen3.8-flash / kimi-k3"
        />
      </div>
      <div className="form-field">
        <label>API 密钥</label>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="留空=保持现状；本地回环端点可不需要"
        />
      </div>
      <div className="plugin-card-actions">
        <button disabled={Boolean(busy)} onClick={() => void save()}>
          {busy === "save" ? "保存中…" : "保存并生效"}
        </button>
        <button disabled={Boolean(busy)} onClick={() => void test()}>
          {busy === "test" ? "测试中…" : "测试连接"}
        </button>
      </div>
      {notice && <div className="banner ok">{notice}</div>}
      {error && <div className="banner error">{error}</div>}
    </div>
  );
}

type HealthState =
  | { kind: "loading" }
  | { kind: "ok"; health: PluginHealth }
  | { kind: "error"; message: string };

// ---------- 插件配置面板（脱敏视图 + key=value 编辑） ----------

interface KvRow {
  key: string;
  value: string;
  /** 脱敏视图中为 "***"（已配置的 secret）：不回显，等待用户输入新值。 */
  masked: boolean;
}

function configToRows(config: Record<string, unknown>): KvRow[] {
  return Object.entries(config).map(([key, value]) => ({
    key,
    value:
      value === "***"
        ? ""
        : typeof value === "string"
          ? value
          : (JSON.stringify(value) ?? ""),
    masked: value === "***",
  }));
}

/** 值解析：优先按 JSON（支持 123 / true / false / null / "文本"），失败按字符串。 */
function parseValue(raw: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === "") return "";
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return raw;
  }
}

function formatConfigValue(value: unknown): string {
  if (value === "***") return "***（已配置，不回显）";
  if (value === "") return "（未配置）";
  return typeof value === "string"
    ? value
    : (JSON.stringify(value) ?? String(value));
}

type ConfigLoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; detail: PluginDetail };

function PluginConfigPanel({
  pluginId,
  onSaved,
}: {
  pluginId: string;
  onSaved: () => void;
}) {
  const [state, setState] = useState<ConfigLoadState>({ kind: "loading" });
  const [rows, setRows] = useState<KvRow[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const detail = await api.plugin(pluginId);
      setState({ kind: "ok", detail });
      setRows(configToRows(detail.config ?? {}));
    } catch (e) {
      setState({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, [pluginId]);

  useEffect(() => {
    void load();
  }, [load]);

  const setRow = (i: number, patch: Partial<KvRow>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const submit = async () => {
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      const config: Record<string, unknown> = {};
      const original = state.kind === "ok" ? (state.detail.config ?? {}) : {};
      const currentKeys = new Set<string>();
      for (const row of rows) {
        const key = row.key.trim();
        if (!key) continue;
        currentKeys.add(key);
        // A masked, untouched secret must NOT be replaced with an empty string.
        if (row.masked && row.value === "") continue;
        config[key] = parseValue(row.value);
      }
      // The API accepts a patch; null explicitly deletes a removed key.
      for (const key of Object.keys(original)) {
        if (!currentKeys.has(key)) config[key] = null;
      }
      await api.setPluginConfig(pluginId, config);
      setNotice("配置已保存；未修改的敏感字段保持原值。");
      await load(); // 重新拉取脱敏视图
      onSaved(); // 刷新外层插件列表
    } catch (e) {
      const prefix =
        e instanceof ApiError && e.status === 422 ? "校验失败（422）：" : "";
      setSaveError(prefix + (e instanceof Error ? e.message : String(e)));
    } finally {
      setSaving(false);
    }
  };

  if (state.kind === "loading") return <div className="muted">加载配置…</div>;
  if (state.kind === "error")
    return <div className="banner error">{state.message}</div>;

  const config = state.detail.config ?? {};

  return (
    <div className="config-panel">
      <div>
        <div className="section-label">当前配置（脱敏视图）</div>
        {Object.keys(config).length === 0 ? (
          <div className="muted">暂无已保存的配置。</div>
        ) : (
          <div className="config-view">
            {Object.entries(config).map(([k, v]) => (
              <div key={k} className="config-row">
                <span className="mono">{k}</span>
                <span
                  className={v === "***" || v === "" ? "mono muted" : "mono"}
                >
                  {formatConfigValue(v)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="section-label">编辑配置（仅保存改动字段）</div>
        {rows.map((row, i) => (
          <div className="kv-row" key={i}>
            <input
              className="key mono"
              aria-label={`配置字段 ${i + 1}`}
              placeholder="key"
              value={row.key}
              onChange={(e) => setRow(i, { key: e.target.value })}
            />
            <input
              className="value mono"
              type={
                row.masked || /secret|token|key|password|cookie/i.test(row.key)
                  ? "password"
                  : "text"
              }
              aria-label={`配置值 ${row.key || i + 1}`}
              autoComplete="off"
              placeholder={
                row.masked
                  ? "已配置（***）：留空保持原值；修改后输入新值"
                  : '值（支持 JSON：123 / true / "文本"）；secret 字段不会写入普通 SQLite 配置'
              }
              value={row.value}
              onChange={(e) =>
                setRow(i, { value: e.target.value, masked: false })
              }
            />
            <button
              aria-label={`删除配置字段 ${row.key || i + 1}`}
              onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}
            >
              ✕
            </button>
          </div>
        ))}
        <div className="actions">
          <button
            onClick={() =>
              setRows((rs) => [...rs, { key: "", value: "", masked: false }])
            }
          >
            添加一行
          </button>
          <button
            className="primary"
            disabled={saving}
            onClick={() => void submit()}
          >
            {saving ? "保存中…" : "保存配置"}
          </button>
        </div>
        <div className="muted small">
          提示：仅提交改动字段；删除整行才会删除配置。敏感字段由服务端单独保存，
          控制台只显示 ***，未修改时不覆盖旧值。
        </div>
      </div>

      {saveError && <div className="banner error">{saveError}</div>}
      {notice && <div className="banner ok">{notice}</div>}
    </div>
  );
}

// ---------- 插件列表 ----------

export default function Plugins() {
  const listFn = useCallback(() => api.plugins(), []);
  const { data: plugins, error, loading, reload } = useApi(listFn);
  const [busy, setBusy] = useState<string | null>(null);
  const [health, setHealth] = useState<Record<string, HealthState>>({});
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [detailFor, setDetailFor] = useState<string | null>(null);

  const toggle = async (id: string, enabled: boolean) => {
    setBusy(id);
    setActionError("");
    try {
      await api.setPluginEnabled(id, enabled);
      reload();
    } catch (e) {
      setActionError(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const checkHealth = async (id: string) => {
    setHealth((h) => ({ ...h, [id]: { kind: "loading" } }));
    try {
      const h = await api.pluginHealth(id);
      setHealth((prev) => ({ ...prev, [id]: { kind: "ok", health: h } }));
    } catch (e) {
      setHealth((prev) => ({
        ...prev,
        [id]: {
          kind: "error",
          message: e instanceof Error ? e.message : String(e),
        },
      }));
    }
  };

  const testConnection = async (id: string) => {
    setBusy(id);
    setNotice("");
    setActionError("");
    try {
      const r = await api.testPlugin(id);
      if (r.ok)
        setNotice(
          `连接正常${r.latency_ms != null ? ` · ${r.latency_ms} ms` : ""}${r.via === "healthcheck" ? "（插件健康检查，未发起外部请求）" : ""}`,
        );
      else setActionError(`测试失败：${r.error_kind ?? "未就绪"}`);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  // 分组：回复模型独立；媒体生成聚合；其余按 kind 归组
  const groups: [string, (p: PluginInfo) => boolean][] = [
    ["回复模型", (p) => p.kind === "model" && (p.plugin_id === "deepseek" || p.plugin_id === "local_model")],
    ["意图模型", (p) => p.plugin_id === "llm_intent"],
    ["媒体生成", (p) => p.kind === "model" && !["deepseek", "local_model", "llm_intent"].includes(p.plugin_id)],
    ["工具与输出", (p) => p.kind === "output"],
    ["渠道", (p) => p.kind === "channel"],
  ];
  const grouped = (list: PluginInfo[]): [string, PluginInfo[]][] => {
    const out: [string, PluginInfo[]][] = [];
    const assigned = new Set<string>();
    for (const [label, match] of groups) {
      const items = list.filter((p) => !assigned.has(p.plugin_id) && match(p));
      items.forEach((p) => assigned.add(p.plugin_id));
      if (items.length) out.push([label, items]);
    }
    const rest = list.filter((p) => !assigned.has(p.plugin_id));
    if (rest.length) out.push(["其他", rest]);
    return out;
  };

  // 子页面：插件详情
  if (detailFor) {
    const p = plugins?.find((x) => x.plugin_id === detailFor);
    if (p) {
      const h = health[p.plugin_id];
      return (
        <div>
          <div className="page-head">
            <button className="text-button" onClick={() => setDetailFor(null)}>
              ← 返回插件列表
            </button>
          </div>
          <section className="surface plugin-detail">
            <div className="plugin-card-head">
              <h2 style={{ margin: 0 }}>{p.name}</h2>
              <span className={p.enabled ? "badge ok" : "badge"}>{p.status}</span>
            </div>
            <div className="muted small mono" style={{ margin: "4px 0 10px" }}>
              {p.plugin_id} · v{p.version} · {KIND_LABELS[p.kind] ?? p.kind}
            </div>
            {p.description && <p className="muted">{p.description}</p>}
            <div className="muted small plugin-card-io" style={{ marginBottom: 10 }}>
              {Array.isArray(p.inputs) && p.inputs.length > 0 && <div>输入：{p.inputs.join("、")}</div>}
              {Array.isArray(p.actions) && p.actions.length > 0 && <div>输出：{p.actions.join("、")}</div>}
              {Array.isArray(p.provides) && p.provides.length > 0 && <div>能力：{p.provides.join("、")}</div>}
            </div>
            <div className="plugin-card-health" style={{ marginBottom: 10 }}>
              {h?.kind === "loading" && <span className="muted small">检查中…</span>}
              {h?.kind === "ok" && (
                <span className={h.health.status === "healthy" ? "badge ok" : "badge warn"}
                      title={h.health.detail ?? ""}>
                  {h.health.status}
                </span>
              )}
              {h?.kind === "error" && <span className="badge bad">{h.message}</span>}
            </div>
            <div className="plugin-card-actions">
              <button disabled={busy === p.plugin_id} onClick={() => void toggle(p.plugin_id, !p.enabled)}>
                {p.enabled ? "禁用" : "启用"}
              </button>
              <button disabled={busy === p.plugin_id} onClick={() => void checkHealth(p.plugin_id)}>检查</button>
              <button disabled={busy === p.plugin_id} onClick={() => void testConnection(p.plugin_id)}>测试连接</button>
            </div>
            <div className="plugin-card-config" style={{ marginTop: 14 }}>
              <div className="section-label">配置</div>
              {p.kind === "model" ? (
                <ModelConfigPanel pluginId={p.plugin_id} onSaved={reload} />
              ) : (
                <PluginConfigPanel pluginId={p.plugin_id} onSaved={reload} />
              )}
            </div>
          </section>
        </div>
      );
    }
  }

  return (
    <div>
      <div className="page-head">
        <h2>插件</h2>
        <button onClick={reload}>刷新</button>
      </div>

      {notice && (
        <div className="banner ok" role="status">
          {notice}
        </div>
      )}
      {actionError && (
        <div className="banner error" role="alert">
          {actionError}
        </div>
      )}
      {loading && <div className="muted">加载中…</div>}
      {error && <div className="banner error">{error}</div>}
      {plugins && plugins.length === 0 && (
        <div className="empty">没有已加载的插件。</div>
      )}

      {plugins && plugins.length > 0 && grouped(plugins).map(([label, items]) => (
        <section key={label} className="plugin-group">
          <div className="section-label">{label}</div>
          <div className="plugin-grid">
            {items.map((p) => {
              const h = health[p.plugin_id];
              return (
                <div
                  key={p.plugin_id}
                  className="plugin-card clickable"
                  role="button"
                  tabIndex={0}
                  onClick={() => setDetailFor(p.plugin_id)}
                  onKeyDown={(e) => { if (e.key === "Enter") setDetailFor(p.plugin_id); }}
                >
                  <div className="plugin-card-head">
                    <strong>{p.name}</strong>
                    <span className={p.enabled ? "badge ok" : "badge"}>{p.status}</span>
                  </div>
                  <div className="plugin-card-kind">{KIND_LABELS[p.kind] ?? p.kind}</div>
                  {p.description && (
                    <div className="muted small plugin-card-desc">{p.description}</div>
                  )}
                  <div className="plugin-card-health">
                    {h?.kind === "ok" && (
                      <span className={h.health.status === "healthy" ? "badge ok" : "badge warn"}>
                        {h.health.status}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}