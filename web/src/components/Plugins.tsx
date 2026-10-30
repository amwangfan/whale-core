import { useCallback, useEffect, useState } from "react";
import { api, PluginDetail } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";
export function PluginConfigPanel({
  pluginId,
  onSaved,
}: {
  pluginId: string;
  onSaved: () => void;
}) {
  const [detail, setDetail] = useState<PluginDetail | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    try {
      const d = await api.plugin(pluginId);
      setDetail(d);
      setValues(
        Object.fromEntries(
          Object.entries({
            ...Object.fromEntries(
              Object.entries(d.config_schema?.properties || {}).map(
                ([k, v]) => [k, v.default ?? ""],
              ),
            ),
            ...d.config,
          }).map(([k, v]) => [
            k,
            v === "***" ? "" : typeof v === "string" ? v : JSON.stringify(v),
          ]),
        ),
      );
    } catch (e) {
      setError(String(e));
    }
  }, [pluginId]);
  useEffect(() => {
    void load();
  }, [load]);
  const save = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const config: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(values)) {
        const spec = detail?.config_schema?.properties?.[key];
        if (detail?.config?.[key] === "***" && !value) continue;
        if (spec?.type === "number" || spec?.type === "integer") {
          if (!value.trim() || !Number.isFinite(Number(value)))
            throw new Error(`${key} 需要有效数字`);
          config[key] = Number(value);
        } else if (spec?.type === "boolean") config[key] = value === "true";
        else config[key] = value;
      }
      await api.setPluginConfig(pluginId, config);
      await load();
      onSaved();
      setNotice("配置已保存，并已应用到后续调用。");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const r = await api.pluginTest(pluginId);
      setNotice(
        r.ok
          ? `检查通过${r.latency_ms !== null ? ` · ${r.latency_ms} ms` : ""}（${r.via === "ping" ? "实际模型请求" : "本地配置检查"}）`
          : `检查失败：${r.error_kind || "请确认插件已启用且凭据完整"}`,
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="config-panel">
      {!detail ? (
        <p className="muted">读取配置…</p>
      ) : (
        <>
          <div className="form-grid">
            {Object.entries(values).map(([key, value]) => {
              const spec = detail.config_schema?.properties?.[key];
              const secret =
                spec?.format === "secret" ||
                /key|token|secret|password/i.test(key);
              return (
                <label className="field" key={key}>
                  <span>
                    {(
                      {
                        base_url: "服务地址",
                        model: "模型名称",
                        api_key: "API 密钥",
                        timeout: "超时（秒）",
                        provider: "搜索服务",
                        target: "固定接收方",
                        voice: "音色",
                      } as Record<string, string>
                    )[key] || key}
                  </span>
                  {spec?.type === "boolean" ? (
                    <select
                      value={value}
                      onChange={(e) =>
                        setValues((v) => ({ ...v, [key]: e.target.value }))
                      }
                    >
                      <option value="true">开启</option>
                      <option value="false">关闭</option>
                    </select>
                  ) : (
                    <input
                      type={
                        secret
                          ? "password"
                          : spec?.type === "number"
                            ? "number"
                            : "text"
                      }
                      autoComplete="off"
                      value={value}
                      placeholder={
                        detail.config?.[key] === "***"
                          ? "已配置 · 留空保留原值"
                          : key
                      }
                      onChange={(e) =>
                        setValues((v) => ({ ...v, [key]: e.target.value }))
                      }
                    />
                  )}{" "}
                  {secret && (
                    <small>
                      密钥不会回显。留空保留；清除请在 API 提交 null。
                    </small>
                  )}
                </label>
              );
            })}
          </div>
          <div className="actions">
            <button
              className="primary"
              disabled={busy}
              onClick={() => void save()}
            >
              {busy ? "处理中…" : "保存配置"}
            </button>
            <button disabled={busy} onClick={() => void test()}>
              测试连接 / 配置
            </button>
          </div>
          <p className="muted small">
            模型连接测试会发起一次简短请求；其他能力执行本地配置检查。保存配置后再启用插件。
          </p>
        </>
      )}
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
    </div>
  );
}
export default function Plugins() {
  const { data, loading, error, reload } = useApi(
    useCallback(() => api.plugins(), []),
  );
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const toggle = async (id: string, enabled: boolean) => {
    setBusy(id);
    setActionError("");
    try {
      await api.setPluginEnabled(id, enabled);
      setNotice(enabled ? "插件已启用。" : "插件已禁用，后续调用已停止。");
      reload();
    } catch (e) {
      setActionError(String(e));
      reload();
    } finally {
      setBusy("");
    }
  };
  return (
    <div>
      <div className="page-head">
        <div>
          <h2>插件与能力</h2>
          <p className="muted">按需开启，让鲸鱼拥有适合你的能力。</p>
        </div>
        <button onClick={reload}>刷新列表</button>
      </div>
      {(error || actionError) && (
        <div className="banner error">{error || actionError}</div>
      )}
      {notice && <div className="banner ok">{notice}</div>}
      {loading && !data && <div className="empty">读取能力…</div>}
      <div className="plugin-grid">
        {data?.map((p) => (
          <article
            key={p.plugin_id}
            className={`plugin-card ${p.enabled ? "is-enabled" : ""}`}
          >
            <div className="plugin-top">
              <span className="plugin-icon">
                <Icon
                  name={
                    p.plugin_id.includes("speech")
                      ? "chat"
                      : p.plugin_id.includes("media")
                        ? "media"
                        : "plugins"
                  }
                  size={24}
                />
              </span>
              <span
                className={`badge ${p.enabled ? "ok" : p.status === "error" ? "bad" : ""}`}
              >
                {p.enabled
                  ? "已启用"
                  : p.status === "error"
                    ? "配置异常"
                    : "未启用"}
              </span>
            </div>
            <h3>{p.name}</h3>
            <p className="plugin-id mono">
              {p.plugin_id} · v{p.version}
            </p>
            <div className="cap-tags">
              {(Array.isArray(p.provides) ? (p.provides as string[]) : []).map(
                (c) => (
                  <span className="badge" key={c}>
                    {c}
                  </span>
                ),
              )}
              {(Array.isArray(p.actions) ? (p.actions as string[]) : []).map(
                (c) => (
                  <span className="badge" key={c}>
                    {c}
                  </span>
                ),
              )}
            </div>
            <div className="plugin-actions">
              <button
                onClick={() =>
                  setExpanded(expanded === p.plugin_id ? null : p.plugin_id)
                }
              >
                {expanded === p.plugin_id ? "收起配置" : "配置与测试"}
              </button>
              <button
                className={p.enabled ? "" : "primary"}
                disabled={busy === p.plugin_id}
                onClick={() => void toggle(p.plugin_id, !p.enabled)}
              >
                {busy === p.plugin_id ? "处理中…" : p.enabled ? "禁用" : "启用"}
              </button>
            </div>
            {expanded === p.plugin_id && (
              <PluginConfigPanel pluginId={p.plugin_id} onSaved={reload} />
            )}
          </article>
        ))}
      </div>
      {!loading && !data?.length && (
        <div className="empty">没有加载的插件。</div>
      )}
    </div>
  );
}
