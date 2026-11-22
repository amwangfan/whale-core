import { useCallback, useEffect, useState } from 'react';
import { Compass, Cpu, Eye, ListTodo, Plus, Zap } from 'lucide-react';
import {
  bindRole,
  fetchModelsRegistry,
  fetchSettings,
  saveSettings,
  testRole,
  upsertRegistryEntry,
  type ModelsRegistry,
} from './api';
import type { Connection } from './api';
import {
  Banner,
  Button,
  Field,
  FocusCardTitle,
  FocusGrid,
  Input,
  Select,
  SegmentedControl,
  Switch,
  Tag,
} from './ds';
import { Modal, PageHeading } from './ui';
import { useWorkspace } from './workspace';

type RoleKey = 'reply_fast' | 'reply_slow' | 'vision' | 'intent' | 'executor';
const ROLES: { key: RoleKey; name: string; desc: string; icon: typeof Zap }[] = [
  { key: 'reply_fast', name: '回复模型（快）', desc: '日常对话、即时响应', icon: Zap },
  { key: 'reply_slow', name: '回复模型（慢）', desc: '复杂推理、深度思考', icon: Compass },
  { key: 'vision', name: '视觉模型', desc: '图像理解、识别与分析', icon: Eye },
  { key: 'intent', name: '意图模型', desc: '意图识别、路由与发送前审核', icon: Cpu },
  { key: 'executor', name: '任务执行模型', desc: '多轮工具循环的任务执行', icon: ListTodo },
];
const EFFORTS = ['', 'low', 'medium', 'high'] as const;
const effortLabel = (e: string) => (e === '' ? '关闭' : e);

function RoleCard({
  role,
  registry,
  connection,
  onChanged,
  notify,
  focused,
  intentEnabled,
  onToggleIntent,
}: {
  role: (typeof ROLES)[number];
  registry: ModelsRegistry;
  connection: Connection;
  onChanged: () => Promise<void>;
  notify: (t: string, e?: boolean) => void;
  focused: boolean;
  intentEnabled?: boolean;
  onToggleIntent?: (v: boolean) => Promise<void>;
}) {
  const boundName = registry.roles[role.key] || '';
  const entry = registry.entries.find((e) => e.name === boundName) || null;
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [testMsg, setTestMsg] = useState('');
  const [effort, setEffort] = useState(entry?.effort ?? '');
  useEffect(() => {
    setEffort(entry?.effort ?? '');
  }, [entry?.name, entry?.effort]);

  if (!focused) {
    return (
      <div className="model-compact">
        <span className="model-compact-row">
          <em>条目</em>
          {entry ? entry.name : '未绑定'}
        </span>
        <span className="model-compact-row">
          <em>模型</em>
          {entry ? entry.model : '—'}
        </span>
        {entry?.effort ? (
          <span className="model-compact-row">
            <em>思考强度</em>
            {effortLabel(entry.effort)}
          </span>
        ) : null}
      </div>
    );
  }

  const bind = async (name: string) => {
    setBusy('bind');
    setError('');
    try {
      await bindRole(connection, role.key, name || null);
      await onChanged();
      notify(name ? `已绑定「${name}」。` : '已解绑。');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };
  const saveEffort = async (next: string) => {
    if (!entry) return;
    setBusy('effort');
    setError('');
    try {
      await upsertRegistryEntry(connection, {
        name: entry.name,
        provider: entry.provider,
        model: entry.model,
        effort: next,
        ...(entry.base_url ? { base_url: entry.base_url } : {}),
      });
      await onChanged();
      notify('思考强度已保存。');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };
  const test = async () => {
    setBusy('test');
    setTestMsg('');
    try {
      const r = await testRole(connection, role.key);
      setTestMsg(
        r.ok
          ? `连接成功 · ${r.latency_ms} ms`
          : `连接失败：${r.error_kind || '无有效回复'}${r.error_kind === 'unbound' ? '（未绑定条目）' : r.error_kind === 'config' ? '（凭据未配置）' : ''}`,
      );
    } catch (e) {
      setTestMsg(`连接失败：${(e as Error).message}`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="model-form">
      <Field label="绑定注册表条目" hint="该角色生效的模型来自注册表条目。">
        <div className="model-bind-row">
          <Select
            aria-label={`${role.name}绑定条目`}
            value={boundName}
            disabled={busy !== ''}
            onChange={(e) => void bind(e.target.value)}
          >
            <option value="">未绑定</option>
            {registry.entries.map((en) => (
              <option key={en.name} value={en.name}>
                {en.name}（{en.model} · {en.provider}）
              </option>
            ))}
          </Select>
        </div>
      </Field>
      {entry && (
        <Field label="思考强度" hint="按模型能力配置推理档位；不支持的模型请留「关闭」。">
          <SegmentedControl
            label="思考强度"
            value={effort}
            onChange={(v) => {
              setEffort(v);
              void saveEffort(v);
            }}
            options={EFFORTS.map((e) => ({ value: e, label: effortLabel(e) }))}
          />
        </Field>
      )}
      {role.key === 'intent' && onToggleIntent && (
        <div className="model-intent-toggle">
          <div>
            <strong>启用模型意图规划</strong>
            <small>让意图模型辅助理解工具与复杂任务；会产生额外模型调用。</small>
          </div>
          <Switch
            label="启用模型意图规划"
            checked={intentEnabled ?? false}
            onChange={(v) => void onToggleIntent(v)}
          />
        </div>
      )}
      {error && <Banner tone="error">{error}</Banner>}
      {testMsg && (
        <Banner tone={testMsg.startsWith('连接成功') ? 'ok' : 'error'}>{testMsg}</Banner>
      )}
      <div className="model-form-actions">
        <Button variant="secondary" size="sm" busy={busy === 'test'} onClick={test}>
          测试该角色
        </Button>
        {boundName && (
          <Button variant="ghost" size="sm" disabled={busy !== ''} onClick={() => void bind('')}>
            解绑
          </Button>
        )}
      </div>
    </div>
  );
}

export default function ModelsPage() {
  const { connection, notify } = useWorkspace();
  const [registry, setRegistry] = useState<ModelsRegistry | null>(null);
  const [intentEnabled, setIntentEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [focused, setFocused] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: '', provider: 'agentscope', model: '', effort: '', base_url: '' });
  const [createBusy, setCreateBusy] = useState(false);

  const load = useCallback(async () => {
    if (!connection) return;
    setLoading(true);
    setError('');
    try {
      const [reg, st] = await Promise.all([
        fetchModelsRegistry(connection),
        fetchSettings(connection).catch(() => null),
      ]);
      setRegistry(reg);
      if (st) setIntentEnabled(st.intent_model_enabled);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [connection]);
  useEffect(() => {
    void load();
  }, [load]);

  if (!connection) return null;
  const c = connection;
  return (
    <div className="page-enter">
      <PageHeading
        page="models"
        action={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setCreating(true)}>
            添加模型条目
          </Button>
        }
      />
      <Banner tone="info">
        模型注册表是唯一事实源：五个角色经它装配。点卡聚焦配置绑定与思考强度，逐角色测试连通。
      </Banner>
      {error && <Banner tone="error">{error}</Banner>}
      {loading && !registry && <p className="muted padded">正在读取模型注册表…</p>}
      {registry && (
        <FocusGrid
          focused={focused}
          onFocus={setFocused}
          items={ROLES.map((role) => ({
            key: role.key,
            ariaLabel: role.name,
            head: () => (
              <FocusCardTitle
                icon={<role.icon size={19} />}
                name={role.name}
                desc={role.desc}
                aside={
                  registry.roles[role.key] ? (
                    <Tag tone="mint" dot>
                      {registry.roles[role.key]}
                    </Tag>
                  ) : (
                    <Tag tone="slate">未绑定</Tag>
                  )
                }
              />
            ),
            body: (f) => (
              <RoleCard
                role={role}
                registry={registry}
                connection={c}
                onChanged={load}
                notify={notify}
                focused={f}
                intentEnabled={intentEnabled}
                onToggleIntent={async (v) => {
                  await saveSettings(c, { intent_model_enabled: v });
                  setIntentEnabled(v);
                  notify(v ? '模型意图规划已启用。' : '模型意图规划已停用。');
                }}
              />
            ),
          }))}
        />
      )}

      {creating && (
        <Modal title="添加模型条目" onClose={() => !createBusy && setCreating(false)}>
          <div className="model-form">
            <Field label="条目名" hint="唯一标识，角色按它绑定。">
              <Input
                aria-label="条目名"
                value={form.name}
                placeholder="如 v4-pro-0813"
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </Field>
            <div className="model-form-grid">
              <Field label="供应商">
                <Input
                  aria-label="供应商"
                  value={form.provider}
                  placeholder="agentscope / deepseek / …"
                  onChange={(e) => setForm({ ...form, provider: e.target.value })}
                />
              </Field>
              <Field label="模型 id">
                <Input
                  aria-label="模型 id"
                  value={form.model}
                  placeholder="deepseek-v4-pro-0813"
                  onChange={(e) => setForm({ ...form, model: e.target.value })}
                />
              </Field>
            </div>
            <div className="model-form-grid">
              <Field label="思考强度">
                <Select
                  aria-label="思考强度"
                  value={form.effort}
                  onChange={(e) => setForm({ ...form, effort: e.target.value })}
                >
                  <option value="">关闭</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </Select>
              </Field>
              <Field label="自定义端点（可选）">
                <Input
                  aria-label="自定义端点"
                  type="url"
                  value={form.base_url}
                  placeholder="留空用供应商默认"
                  onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                />
              </Field>
            </div>
            <div className="model-form-actions">
              <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>
                取消
              </Button>
              <span className="model-form-gap" />
              <Button
                variant="primary"
                size="sm"
                busy={createBusy}
                disabled={!form.name.trim() || !form.model.trim()}
                onClick={async () => {
                  setCreateBusy(true);
                  try {
                    await upsertRegistryEntry(c, {
                      name: form.name.trim(),
                      provider: form.provider.trim() || 'agentscope',
                      model: form.model.trim(),
                      effort: form.effort,
                      ...(form.base_url.trim() ? { base_url: form.base_url.trim() } : {}),
                    });
                    setCreating(false);
                    setForm({ name: '', provider: 'agentscope', model: '', effort: '', base_url: '' });
                    await load();
                    notify('模型条目已保存。');
                  } catch (e) {
                    notify((e as Error).message, true);
                  } finally {
                    setCreateBusy(false);
                  }
                }}
              >
                创建条目
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
