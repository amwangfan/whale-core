import { useState } from "react";
import { api, type CapabilityResult } from "../api";
import Icon from "./Icon";

export default function ShellTerminal({ available }: { available: boolean }) {
  const [command, setCommand] = useState("");
  const [result, setResult] = useState<CapabilityResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!available || !command.trim() || busy) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(
        await api.runCapability("shell.execute", { command: command.trim() }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="surface">
      <div className="section-head">
        <h2>受控终端</h2>
        <span className={available ? "badge ok" : "badge warn"}>
          {available ? "已配置" : "未启用"}
        </span>
      </div>
      <p className="muted small">
        命令遵循设置中的权限档位，逐次审批后在 Linux
        系统沙箱内执行。提交请求不会直接执行命令。
      </p>
      {!available && (
        <div className="banner warn">
          请先在「设置与诊断」中启用受控 Shell，并确认 Linux 沙箱依赖可用。
        </div>
      )}
      <form
        className="settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="form-field">
          <label htmlFor="shell-command">待执行命令</label>
          <textarea
            id="shell-command"
            className="mono"
            rows={4}
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            placeholder="例如：pwd"
            maxLength={4000}
            required
            disabled={busy}
          />
          <small>
            工作目录与权限由服务端策略决定。敏感操作仍可能被策略拒绝。
          </small>
        </div>
        <div>
          <button
            className="primary"
            type="submit"
            disabled={!available || busy || !command.trim()}
          >
            {busy ? "正在提交…" : "提交执行请求"}
            <Icon name="arrow" size={15} />
          </button>
        </div>
      </form>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {result && (
        <div className="shell-result">
          {result.status === "pending_approval" ? (
            <div className="banner warn" role="status">
              请求已登记，命令尚未执行。请前往「任务中心」审批后继续执行。
            </div>
          ) : (
            <div
              className={
                ["answered", "succeeded"].includes(result.status)
                  ? "banner ok"
                  : "banner warn"
              }
              role="status"
            >
              任务状态：{result.status}
            </div>
          )}
          {(result.output || result.reply) && (
            <pre className="memory-json">{result.output || result.reply}</pre>
          )}
          {result.task_id && (
            <p className="muted small">任务编号：{result.task_id}</p>
          )}
        </div>
      )}
    </section>
  );
}
