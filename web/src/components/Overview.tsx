import { useCallback } from 'react';
import { api } from '../api';
import { useApi } from '../hooks';

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${seconds % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export default function Overview() {
  const statusFn = useCallback(() => api.status(), []);
  const healthFn = useCallback(() => api.health(), []);
  const status = useApi(statusFn);
  const health = useApi(healthFn);

  const reloadAll = () => {
    status.reload();
    health.reload();
  };

  return (
    <div>
      <div className="page-head">
        <h2>总览</h2>
        <button onClick={reloadAll}>刷新</button>
      </div>

      <div className="cards">
        <div className="card">
          <div className="card-title">健康</div>
          {health.loading ? (
            <div className="muted">加载中…</div>
          ) : health.error ? (
            <div className="badge bad">异常：{health.error}</div>
          ) : (
            <div className={health.data?.ok ? 'badge ok' : 'badge bad'}>
              {health.data?.ok ? 'ok' : 'not ok'}
            </div>
          )}
        </div>

        {status.error && (
          <div className="card">
            <div className="card-title">状态</div>
            <div className="badge bad">{status.error}</div>
          </div>
        )}

        {status.data && (
          <>
            <div className="card">
              <div className="card-title">版本</div>
              <div className="card-value">{status.data.version}</div>
            </div>
            <div className="card">
              <div className="card-title">Profile</div>
              <div className="card-value">{status.data.profile}</div>
            </div>
            <div className="card">
              <div className="card-title">运行时长</div>
              <div className="card-value">{formatUptime(status.data.uptime_s)}</div>
            </div>
            <div className="card">
              <div className="card-title">插件</div>
              <div className="card-value">
                {status.data.plugins.enabled} / {status.data.plugins.loaded}
                <span className="muted">（启用/加载）</span>
              </div>
            </div>
            <div className="card">
              <div className="card-title">记忆</div>
              <div className="card-value">
                {status.data.memory.episodes} 幕 / {status.data.memory.preferences} 偏好
              </div>
            </div>
            <div className="card">
              <div className="card-title">待审批草稿</div>
              <div className="card-value">{status.data.drafts_pending}</div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
