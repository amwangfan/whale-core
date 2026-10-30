import { useCallback, useState } from "react";
import { api, rawRequest } from "../api";
import { useApi } from "../hooks";
import { PluginConfigPanel } from "./Plugins";
export default function Settings() {
  const settings = useApi(useCallback(() => api.settings(), []));
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <div className="page-head">
        <div>
          <h2>设置与诊断</h2>
          <p className="muted">从第一次连接，到每一次顺畅运行。</p>
        </div>
      </div>
      {(error || settings.error) && (
        <div className="banner error">{error || settings.error}</div>
      )}
      {notice && (
        <div className="banner ok" role="status">
          {notice}
        </div>
      )}
      <section className="panel settings-panel">
        <div className="panel-head">
          <h3>01 / 连接模型</h3>
          <span className="badge">{settings.data?.profile || "读取中"}</span>
        </div>
        <p className="muted">
          填写一个 OpenAI
          兼容服务地址与模型名称。云端密钥或本地服务均可在这里设置。
        </p>
        {settings.data && (
          <PluginConfigPanel
            pluginId={
              settings.data.profile === "cloud" ? "deepseek" : "local_model"
            }
            onSaved={settings.reload}
          />
        )}
      </section>
      <section className="panel settings-panel">
        <div className="panel-head">
          <h3>02 / 鲸鱼的人格</h3>
        </div>
        <div className="persona-grid">
          {settings.data?.personas.map((p) => (
            <button
              key={p.card_id}
              className={`persona-card ${settings.data?.persona === p.card_id ? "selected" : ""}`}
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await api.saveSettings({ persona: p.card_id });
                  settings.reload();
                  setNotice("人格已切换，将用于后续对话。");
                })
              }
            >
              <strong>{p.title}</strong>
              <small>{p.description}</small>
              <span>
                {settings.data?.persona === p.card_id ? "当前人格" : "选择人格"}
              </span>
            </button>
          ))}
        </div>
      </section>
      <section className="panel settings-panel">
        <div className="panel-head">
          <h3>03 / 意图识别</h3>
        </div>
        <p className="muted">
          明确的搜索请求始终先走规则。开启模型规划后，其他请求会多一次模型调用。
        </p>
        <button
          disabled={busy || !settings.data}
          onClick={() =>
            void run(async () => {
              await api.saveSettings({
                intent_model_enabled: !settings.data?.intent_model_enabled,
              });
              settings.reload();
              setNotice("意图规划设置已保存。");
            })
          }
        >
          {settings.data?.intent_model_enabled
            ? "关闭模型规划"
            : "开启模型规划"}
        </button>
      </section>
      <section className="panel settings-panel">
        <div className="panel-head">
          <h3>04 / 配对设备身份</h3>
        </div>
        <p className="muted">
          仅明确绑定的 channel/account 可通过接入 API 写入对话。每行填写{" "}
          <code>channel,account</code>。
        </p>
        {settings.data && (
          <Bindings
            initial={settings.data.bindings
              .map((b) => `${b.channel},${b.account}`)
              .join("\n")}
            onSave={(text) =>
              void run(async () => {
                const bindings = text
                  .split("\n")
                  .filter((x) => x.trim())
                  .map((x) => {
                    const [channel, account, ...rest] = x
                      .split(",")
                      .map((v) => v.trim());
                    if (!channel || !account || rest.length)
                      throw new Error("每行需要 channel,account");
                    return { channel, account };
                  });
                await api.saveSettings({ bindings });
                settings.reload();
                setNotice("设备身份已保存。");
              })
            }
          />
        )}
      </section>
      {settings.data && (
        <Budget
          initial={settings.data.budget}
          save={(budget) =>
            run(async () => {
              await api.saveSettings({ budget });
              settings.reload();
              setNotice("请求预算已保存。");
            })
          }
        />
      )}
      <section className="panel settings-panel">
        <div className="panel-head">
          <h3>05 / 数据与运行诊断</h3>
        </div>
        <p className="muted">
          完整备份包含记忆、会话、任务与设置；密钥和媒体文件需另外私密备份。恢复前停止全部写入进程，再使用{" "}
          <code>whale restore 文件 --offline</code>。
        </p>
        <div className="actions">
          <button
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const res = await rawRequest("/backup", { method: "POST" });
                const url = URL.createObjectURL(await res.blob());
                const a = document.createElement("a");
                a.href = url;
                a.download = `whale-backup-${new Date().toISOString().slice(0, 10)}.sqlite3`;
                a.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
                setNotice("完整备份已下载。");
              })
            }
          >
            下载完整备份
          </button>
          <button
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const report = await api.diagnostics();
                await navigator.clipboard.writeText(
                  JSON.stringify(report, null, 2),
                );
                setNotice("诊断摘要已复制，不包含 API 密钥和对话原文。");
              })
            }
          >
            复制诊断摘要
          </button>
        </div>
      </section>
    </div>
  );
}
function Bindings({
  initial,
  onSave,
}: {
  initial: string;
  onSave: (s: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <div className="field">
      <textarea
        aria-label="配对身份"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="qq,你的固定账号 ID"
        rows={3}
      />
      <button onClick={() => onSave(value)}>保存配对</button>
    </div>
  );
}

function Budget({
  initial,
  save,
}: {
  initial: { model_calls: number; media_jobs: number };
  save: (budget: { model_calls: number; media_jobs: number }) => Promise<void>;
}) {
  const [value, setValue] = useState(initial);
  const [busy, setBusy] = useState(false);
  return (
    <section className="panel settings-panel">
      <div className="panel-head">
        <h3>每日调用预算</h3>
      </div>
      <p className="muted">
        按上海时间自然日计数。失败请求也占用预算；这是请求次数上限，实际费用以服务商账单为准。
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setBusy(true);
          void save(value).finally(() => setBusy(false));
        }}
      >
        <div className="form-grid">
          <label className="field">
            模型调用
            <input
              type="number"
              min="0"
              max="100000"
              value={value.model_calls}
              onChange={(e) =>
                setValue((v) => ({ ...v, model_calls: Number(e.target.value) }))
              }
            />
          </label>
          <label className="field">
            媒体任务
            <input
              type="number"
              min="0"
              max="100000"
              value={value.media_jobs}
              onChange={(e) =>
                setValue((v) => ({ ...v, media_jobs: Number(e.target.value) }))
              }
            />
          </label>
        </div>
        <button disabled={busy}>保存预算</button>
      </form>
    </section>
  );
}
