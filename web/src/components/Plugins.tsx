import { useCallback, useState } from 'react';
import { api, PluginHealth } from '../api';
import { useApi } from '../hooks';

type HealthState =
  | { kind: 'loading' }
  | { kind: 'ok'; health: PluginHealth }
  | { kind: 'error'; message: string };

export default function Plugins() {
  const listFn = useCallback(() => api.plugins(), []);
  const { data: plugins, error, loading, reload } = useApi(listFn);
  const [busy, setBusy] = useState<string | null>(null);
  const [health, setHealth] = useState<Record<string, HealthState>>({});

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
              return (
                <tr key={p.plugin_id}>
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
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
