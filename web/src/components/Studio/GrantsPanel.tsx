/**
 * S6 临时授权区：一次性口令的创建（明文仅展示一次）与租约管理。
 */

import { useState } from "react";
import {
  ApiError,
  studioApi,
  type TemporaryCode,
  type TemporaryCodeCreated,
  type TemporaryGrant,
} from "../../api";
import { TemporaryGrantCard } from "./cards";

/** 与后端 GRANTABLE_SCOPES 对齐（tool.shell.safe 不在口令可授范围内）。 */
const GRANTABLE_SCOPES = [
  "chat.basic",
  "task.read:self",
  "tool.web.search",
  "outbox.submit",
  "device.notify",
];

export default function GrantsPanel({
  codes,
  grants,
  now,
  onChanged,
  onError,
}: {
  codes: TemporaryCode[];
  grants: TemporaryGrant[];
  now: number;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [mode, setMode] = useState<"random" | "custom">("random");
  const [phrase, setPhrase] = useState("");
  const [scopes, setScopes] = useState<Set<string>>(new Set(["task.read:self"]));
  const [ttlS, setTtlS] = useState(86400);
  const [grantTtlS, setGrantTtlS] = useState(600);
  const [grantMaxUses, setGrantMaxUses] = useState(1);
  const [busy, setBusy] = useState(false);
  const [revoking, setRevoking] = useState("");
  const [created, setCreated] = useState<TemporaryCodeCreated | null>(null);
  const [copied, setCopied] = useState(false);

  const toggleScope = (scope: string) => {
    setScopes((cur) => {
      const next = new Set(cur);
      if (next.has(scope)) next.delete(scope);
      else next.add(scope);
      return next;
    });
  };

  const create = async () => {
    setBusy(true);
    setCopied(false);
    try {
      const res = await studioApi.createCode({
        scopes: [...scopes],
        ...(mode === "custom" ? { custom_phrase: phrase } : {}),
        ttl_s: ttlS,
        grant_ttl_s: grantTtlS,
        grant_max_uses: grantMaxUses,
      });
      // 明文仅存在于这一份 state；关闭后任何地方都不可再见（服务端只存校验值）。
      setCreated(res);
      setPhrase("");
      onChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : "创建口令失败。");
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.code);
      setCopied(true);
    } catch {
      onError("复制失败，请手动选择文本复制。");
    }
  };

  const revokeCode = async (codeId: string) => {
    setRevoking(codeId);
    try {
      await studioApi.revokeCode(codeId);
      onChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : "撤销失败。");
    } finally {
      setRevoking("");
    }
  };

  const revokeGrant = async (grantId: string) => {
    setRevoking(grantId);
    try {
      await studioApi.revokeGrant(grantId);
      onChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : "撤销失败。");
    } finally {
      setRevoking("");
    }
  };

  return (
    <section className="studio-grants" aria-label="临时授权">
      <h2>临时授权</h2>
      <p className="muted">
        一次性口令验证一次即核销；租约绑定真实发送者与私聊，到期或用尽自动降权。
      </p>

      <form
        className="studio-code-form"
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
      >
        <fieldset>
          <legend>创建一次性口令</legend>
          <div className="studio-code-mode" role="radiogroup" aria-label="口令形式">
            <label>
              <input
                type="radio"
                name="code-mode"
                checked={mode === "random"}
                onChange={() => setMode("random")}
              />
              随机高熵口令（推荐）
            </label>
            <label>
              <input
                type="radio"
                name="code-mode"
                checked={mode === "custom"}
                onChange={() => setMode("custom")}
              />
              自定义短语
            </label>
          </div>
          {mode === "custom" && (
            <input
              aria-label="自定义短语"
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              placeholder="长度与强度会被服务端校验"
            />
          )}
          <div className="studio-scope-picker">
            {GRANTABLE_SCOPES.map((s) => (
              <label key={s}>
                <input
                  type="checkbox"
                  checked={scopes.has(s)}
                  onChange={() => toggleScope(s)}
                />
                {s}
              </label>
            ))}
          </div>
          <div className="studio-code-nums">
            <label>
              口令有效期（秒）
              <input
                type="number"
                min={1}
                value={ttlS}
                onChange={(e) => setTtlS(Number(e.target.value))}
              />
            </label>
            <label>
              租约有效期（秒）
              <input
                type="number"
                min={1}
                value={grantTtlS}
                onChange={(e) => setGrantTtlS(Number(e.target.value))}
              />
            </label>
            <label>
              租约可用次数
              <input
                type="number"
                min={1}
                max={100}
                value={grantMaxUses}
                onChange={(e) => setGrantMaxUses(Number(e.target.value))}
              />
            </label>
          </div>
          <button
            type="submit"
            className="primary"
            disabled={busy || scopes.size === 0 || (mode === "custom" && !phrase.trim())}
          >
            {busy ? "正在创建…" : "创建口令"}
          </button>
        </fieldset>
      </form>

      {created && (
        <div className="studio-code-reveal" role="status">
          <p>
            <strong>口令明文仅显示这一次</strong>，服务端只保存校验值，无法找回：
          </p>
          <code data-testid="code-plaintext">{created.code}</code>
          <div className="studio-modal-actions">
            <button type="button" onClick={() => void copy()}>
              {copied ? "已复制" : "复制"}
            </button>
            <button type="button" onClick={() => setCreated(null)}>
              我已保存，关闭
            </button>
          </div>
        </div>
      )}

      <h3>口令清单（不含明文）</h3>
      {codes.length === 0 ? (
        <p className="muted">暂无口令。</p>
      ) : (
        <ul className="studio-code-list">
          {codes.map((c) => {
            const state = c.consumed_at
              ? "已核销"
              : c.revoked_at
                ? "已撤销"
                : c.expires_at <= now
                  ? "已过期"
                  : "待使用";
            return (
              <li key={c.code_id} data-testid={`code-${c.code_id}`}>
                <code>{c.code_id}</code>
                <span>{(c.allowed_scopes ?? []).join(", ")}</span>
                <span>{state}</span>
                {state === "待使用" && (
                  <button
                    type="button"
                    disabled={revoking === c.code_id}
                    onClick={() => void revokeCode(c.code_id)}
                  >
                    撤销
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <h3>活跃租约</h3>
      {grants.length === 0 ? (
        <p className="muted">暂无活跃租约。</p>
      ) : (
        <div className="studio-grant-list">
          {grants.map((g) => (
            <TemporaryGrantCard
              key={g.grant_id}
              grant={g}
              now={now}
              busy={revoking === g.grant_id}
              onRevoke={(id) => void revokeGrant(id)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
