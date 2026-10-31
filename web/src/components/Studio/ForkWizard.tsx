/**
 * S6 Fork 向导：受保护原始库的唯一出路（PRD §5.2）。
 *
 * 流程：选择记录 → 脱敏预览 → 勾选要复制的记录 → 显式确认 → POST forks。
 * Fork 成功**不会自动连线**（后端 auto_bound=false），副本出现在手牌后再拖。
 */

import { useEffect, useState } from "react";
import {
  ApiError,
  studioApi,
  type StudioForkPreviewItem,
  type StudioVaultRecord,
} from "../../api";

type Step = "select" | "preview" | "done";

export default function ForkWizard({
  sourceSpace,
  onClose,
  onDone,
}: {
  sourceSpace: string;
  onClose: () => void;
  onDone: (newSpaceId: string) => void;
}) {
  const [step, setStep] = useState<Step>("select");
  const [vaultItems, setVaultItems] = useState<StudioVaultRecord[]>([]);
  const [loadError, setLoadError] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [targetType, setTargetType] = useState("shareable_fork");
  const [targetName, setTargetName] = useState("");
  const [items, setItems] = useState<StudioForkPreviewItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [newSpaceId, setNewSpaceId] = useState("");

  // S7+：从管理管道加载原始库记录（摘要已脱敏），勾选代替手输 key。
  useEffect(() => {
    let cancelled = false;
    studioApi
      .vaultRecords(sourceSpace)
      .then((items) => {
        if (!cancelled) setVaultItems(items ?? []);
      })
      .catch((e) => {
        if (!cancelled)
          setLoadError(
            e instanceof ApiError ? e.message : "记录加载失败，请稍后重试。",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [sourceSpace]);

  const recordKeys = [...picked];

  const togglePick = (key: string) => {
    setPicked((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const preview = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await studioApi.forkPreview({
        source_space: sourceSpace,
        record_keys: recordKeys,
        redact: "builtin",
      });
      setItems(res.items);
      setSelected(new Set(res.items.map((i) => i.key)));
      setStep("preview");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "预览失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await studioApi.fork({
        source_space: sourceSpace,
        record_keys: items
          .filter((i) => selected.has(i.key))
          .map((i) => i.key),
        target_type: targetType,
        target_name: targetName,
        redact: "builtin",
        confirmed: true,
      });
      setNewSpaceId(res.fork.space_id);
      setStep("done");
      onDone(res.fork.space_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Fork 失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  const toggle = (key: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <div className="studio-modal-backdrop" role="presentation">
      <div
        className="studio-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Fork 向导：创建受控副本"
      >
        <h2>创建受控副本（Fork）</h2>
        <p className="muted">
          个人原始库不能直接连接任何会话。选择要复制的记录，预览脱敏结果并确认后，
          系统会创建一个独立副本；副本不会自动连线，也不会随原库变化。
        </p>

        {step === "select" && (
          <>
            <p>选择要复制的记录（摘要已脱敏）：</p>
            {loadError && (
              <div className="banner error" role="alert">
                {loadError}
              </div>
            )}
            <ul className="studio-fork-list">
              {vaultItems.map((item) => (
                <li key={item.key}>
                  <label>
                    <input
                      type="checkbox"
                      checked={picked.has(item.key)}
                      onChange={() => togglePick(item.key)}
                    />
                    <code>{item.key}</code>
                    <span className="muted">{item.summary.slice(0, 80)}</span>
                  </label>
                </li>
              ))}
              {vaultItems.length === 0 && !loadError && (
                <li className="muted">正在加载记录…</li>
              )}
            </ul>
            <label htmlFor="fork-type">副本类型</label>
            <select
              id="fork-type"
              value={targetType}
              onChange={(e) => setTargetType(e.target.value)}
            >
              <option value="shareable_fork">共享副本（默认只读）</option>
              <option value="owner_working_copy">主人工作副本</option>
              <option value="group_space">群组记忆库</option>
            </select>
            <label htmlFor="fork-name">副本名称</label>
            <input
              id="fork-name"
              value={targetName}
              onChange={(e) => setTargetName(e.target.value)}
              placeholder="例如：课程资料副本"
            />
            <div className="studio-modal-actions">
              <button type="button" onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy || recordKeys.length === 0}
                onClick={() => void preview()}
              >
                {busy ? "正在预览…" : "预览脱敏结果"}
              </button>
            </div>
          </>
        )}

        {step === "preview" && (
          <>
            <p>预览（已脱敏，所见即所得）。取消勾选可排除记录：</p>
            <ul className="studio-fork-list">
              {items.map((item) => (
                <li key={item.key}>
                  <label>
                    <input
                      type="checkbox"
                      checked={selected.has(item.key)}
                      onChange={() => toggle(item.key)}
                    />
                    <code>{item.key}</code>
                    <span className="muted">
                      {JSON.stringify(item.content).slice(0, 120)}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <label className="studio-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              我已审核脱敏预览，确认复制所选 {selected.size} 条记录为独立副本。
            </label>
            <div className="studio-modal-actions">
              <button type="button" onClick={() => setStep("select")}>
                上一步
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy || !confirmed || selected.size === 0}
                onClick={() => void submit()}
              >
                {busy ? "正在复制…" : "确认 Fork"}
              </button>
            </div>
          </>
        )}

        {step === "done" && (
          <>
            <p role="status">
              副本已创建：<code>{newSpaceId}</code>。它尚未连接任何会话——
              关闭本窗口后，把新的副本卡拖到目标会话即可。
            </p>
            <div className="studio-modal-actions">
              <button type="button" className="primary" onClick={onClose}>
                完成
              </button>
            </div>
          </>
        )}

        {error && (
          <div className="banner error" role="alert">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
