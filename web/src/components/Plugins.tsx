import { Fragment, useCallback, useEffect, useState } from 'react';
import { api, ApiError, PluginDetail, PluginHealth } from '../api';
import { useApi } from '../hooks';

type HealthState =
  | { kind: 'loading' }
  | { kind: 'ok'; health: PluginHealth }
  | { kind: 'error'; message: string };

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
    value: value === '***' ? '' : typeof value === 'string' ? value : JSON.stringify(value) ?? '',
    masked: value === '***',
  }));
}

/** 值解析：优先按 JSON（支持 123 / true / false / null / "文本"），失败按字符串。 */
function parseValue(raw: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === '') return '';
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return raw;
  }
}

function formatConfigValue(value: unknown): string {
  if (value === '***') return '***（已配置，不回显）';
  if (value === '') return '（未配置）';
  return typeof value === 'string' ? value : JSON.stringify(value) ?? String(value);
}

type ConfigLoadState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ok'; detail: PluginDetail };

function PluginConfigPanel({ pluginId, onSaved }: { pluginId: string; onSaved: () => void }) {
  const [state, setState] = useState<ConfigLoadState>({ kind: 'loading' });
  const [rows, setRows] = useState<KvRow[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: 'loading' });
    try {
      const detail = await api.plugin(pluginId);
      setState({ kind: 'ok', detail });
      setRows(configToRows(detail.config ?? {}));
    } catch (e) {
      setState({ kind: 'error', message: e instanceof Error ? e.message : String(e) });
    }
  }, [pluginId]);

  useEffect(() => {
    void load();
  }, [load]);

  const setRow = (i: number, patch: Partial<KvRow>) =>
    setRows(rs => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const submit = async () => {
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      const config: Record<string, unknown> = {};
      for (const row of rows) {
        const key = row.key.trim();
        if (!key) continue; // 忽略空 key 行
        config[key] = parseValue(row.value);
      }
      await api.setPluginConfig(pluginId, config);
      setNotice('配置已保存（secret 字段将脱敏存储，只显示 ***）。');
      await load(); // 重新拉取脱敏视图
      onSaved(); // 刷新外层插件列表
    } catch (e) {
      const prefix = e instanceof ApiError && e.status === 422 ? '校验失败（422）：' : '';
      setSaveError(prefix + (e instanceof Error ? e.message : String(e)));
    } finally {
      setSaving(false);
    }
  };

  if (state.kind === 'loading') return <div className="muted">加载配置…</div>;
  if (state.kind === 'error') return <div className="banner error">{state.message}</div>;

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
                <span className={v === '***' || v === '' ? 'mono muted' : 'mono'}>
                  {formatConfigValue(v)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="section-label">编辑配置（每行一个 key=value，提交整体替换）</div>
        {rows.map((row, i) => (
          <div className="kv-row" key={i}>
            <input
              className="key mono"
              placeholder="key"
              value={row.key}
              onChange={e => setRow(i, { key: e.target.value })}
            />
            <input
              className="value mono"
              placeholder={
                row.masked
                  ? '已配置（***）：输入新值覆盖，留空提交将清除'
                  : '值（支持 JSON：123 / true / "文本"）；secret 字段会脱敏存储'
              }
              value={row.value}
              onChange={e => setRow(i, { value: e.target.value, masked: false })}
            />
            <button onClick={() => setRows(rs => rs.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <div className="actions">
          <button onClick={() => setRows(rs => [...rs, { key: '', value: '', masked: false }])}>
            添加一行
          </button>
          <button className="primary" disabled={saving} onClick={() => void submit()}>
            {saving ? '保存中…' : '保存配置'}
          </button>
        </div>
        <div className="muted small">
          提示：提交会整体替换该插件配置；secret 字段（键名含 token/key/secret/password
          或 schema 标记 format:secret）脱敏存储，保存后只显示 ***，不会回显明文。
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
  const [configFor, setConfigFor] = useState<string | null>(null);

  const toggle = async (id: string, enabled: boolean) => {
    setBusy(id);
    try {
      await api.setPluginEnabled(id, enabled);
      reload();
    } catch (e) {
      alert(`操作失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const checkHealth = async (id: string) => {
    setHealth(h => ({ ...h, [id]: { kind: 'loading' } }));
    try {
      const h = await api.pluginHealth(id);
      setHealth(prev => ({ ...prev, [id]: { kind: 'ok', health: h } }));
    } catch (e) {
      setHealth(prev => ({
        ...prev,
        [id]: { kind: 'error', message: e instanceof Error ? e.message : String(e) },
      }));
    }
  };

  return (
    <div>
      <div className="page-head">
        <h2>插件</h2>
        <button onClick={reload}>刷新</button>
      </div>

      {loading && <div className="muted">加载中…</div>}
      {error && <div className="banner error">{error}</div>}
      {plugins && plugins.length === 0 && <div className="empty">没有已加载的插件。</div>}

      {plugins && plugins.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>ID</th>
              <th>版本</th>
              <th>类型</th>
              <th>状态</th>
              <th>启用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {plugins.map(p => {
              const h = health[p.plugin_id];
              const expanded = configFor === p.plugin_id;
              return (
                <Fragment key={p.plugin_id}>
                  <tr>
                    <td>{p.name}</td>
                    <td className="mono">{p.plugin_id}</td>
                    <td className="mono">{p.version}</td>
                    <td>{p.kind}</td>
                    <td>
                      <span className={p.enabled ? 'badge ok' : 'badge'}>{p.status}</span>
                      {h?.kind === 'loading' && <span className="muted"> 检查中…</span>}
                      {h?.kind === 'ok' && (
                        <span
                          className={h.health.status === 'healthy' ? 'badge ok' : 'badge warn'}
                          title={h.health.detail ?? ''}
                        >
                          {h.health.status}
                        </span>
                      )}
                      {h?.kind === 'error' && <span className="badge bad">{h.message}</span>}
                    </td>
                    <td>{p.enabled ? '是' : '否'}</td>
                    <td className="actions">
                      <button
                        disabled={busy === p.plugin_id}
                        onClick={() => void toggle(p.plugin_id, !p.enabled)}
                      >
                        {p.enabled ? '禁用' : '启用'}
                      </button>
                      <button onClick={() => void checkHealth(p.plugin_id)}>健康</button>
                      <button onClick={() => setConfigFor(expanded ? null : p.plugin_id)}>
                        {expanded ? '收起' : '配置'}
                      </button>
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="expand-row">
                      <td colSpan={7}>
                        <PluginConfigPanel pluginId={p.plugin_id} onSaved={reload} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
