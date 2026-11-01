/**
 * 主动行为控制面板（issue #42）：
 * - 全局四字段（主动消息每日上限 / 被动回复保险丝 / 静默时段起止），
 *   每字段显示当前值 + 来源徽标（自定义/默认）+ 影响说明；保存只提交改动字段，
 *   「恢复默认」走 reset；
 * - 静默规则固定文案；部署侧锁定字段只读展示；
 * - 渠道默认档位（安静/自然/积极 + 免打扰 + 分段上限），activity="" 清除覆盖；
 * - 会话级节奏只读（编辑在 Studio 会话卡）；「最近决策」联动事件页。
 */

import { useCallback, useEffect, useState } from "react";
import {
  studioApi,
  type InitiativeRhythmView,
  type InitiativeView,
} from "../api";

const FIELD_DEFS: {
  key: "daily_limit" | "passive_daily_limit" | "quiet_start" | "quiet_end";
  label: string;
  kind: "int" | "time";
}[] = [
  { key: "daily_limit", label: "主动消息每日上限", kind: "int" },
  { key: "passive_daily_limit", label: "被动回复每日保险丝", kind: "int" },
  { key: "quiet_start", label: "静默开始时间", kind: "time" },
  { key: "quiet_end", label: "静默结束时间", kind: "time" },
];

const LOCKED_LABELS: Record<string, string> = {
  enabled: "主动行为总开关",
  allowed_channels: "允许的渠道",
  auto_send: "自动发送",
  allow_calls: "允许主动通话",
};

const ACTIVITY_LABELS: Record<string, string> = {
  quiet: "安静",
  balanced: "自然",
  lively: "积极",
};

const CHANNEL_LABELS: Record<string, string> = {
  qq: "QQ",
  call: "语音通话",
  hud: "HUD",
  notify: "通知",
};

const DECISION_STATUS: Record<string, string> = {
  open: "进行中",
  replied: "已回复",
  silent: "静默",
  failed: "失败",
  partial: "部分完成",
};

function decisionBadge(status: string): string {
  switch (status) {
    case "replied":
      return "badge ok";
    case "failed":
      return "badge bad";
    case "partial":
      return "badge warn";
    default:
      return "badge";
  }
}

function fmtClock(epochS: number): string {
  const d = new Date(epochS * 1000);
  return Number.isNaN(d.getTime())
    ? String(epochS)
    : d.toLocaleTimeString("zh-CN", { hour12: false });
}

interface ChannelForm {
  activity: string;
  max_segments: number;
  dnd: boolean;
}

export default function Initiative({
  onNavigate,
}: {
  onNavigate?: (tab: string) => void;
}) {
  const [view, setView] = useState<InitiativeView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const [channelForms, setChannelForms] = useState<Record<string, ChannelForm>>({});
  const [channelBusy, setChannelBusy] = useState("");

  const applyView = useCallback((v: InitiativeView) => {
    setView(v);
    const f: Record<string, string> = {};
    for (const def of FIELD_DEFS) {
      const fv = v.global.fields[def.key];
      f[def.key] = fv ? String(fv.value) : "";
    }
    setForm(f);
    const ch: Record<string, ChannelForm> = {};
    for (const [name, c] of Object.entries(v.rhythm.channels)) {
      ch[name] = {
        activity: c.activity,
        max_segments: c.max_segments,
        dnd: c.dnd,
      };
    }
    setChannelForms(ch);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      applyView(await studioApi.initiative());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [applyView]);

  useEffect(() => {
    void load();
  }, [load]);

  const saveGlobal = async () => {
    if (!view) return;
    const patch: Record<string, number | string> = {};
    for (const def of FIELD_DEFS) {
      const current = view.global.fields[def.key];
      const raw = (form[def.key] ?? "").trim();
      const cur = current ? String(current.value) : "";
      if (raw === cur) continue; // 未改动不提交
      if (def.kind === "int") {
        const n = Number(raw);
        if (!Number.isInteger(n) || n < 0 || n > 100000) {
          setError(`${def.label}须是 0..100000 的整数。`);
          return;
        }
        patch[def.key] = n;
      } else {
        if (!/^\d{1,2}:\d{2}$/.test(raw)) {
          setError(`${def.label}须为 HH:MM 格式。`);
          return;
        }
        patch[def.key] = raw;
      }
    }
    if (Object.keys(patch).length === 0) {
      setNotice("没有改动需要保存。");
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const global = await studioApi.putInitiativeGlobal(patch);
      setView((v) => (v ? { ...v, global } : v));
      const f = { ...form };
      for (const def of FIELD_DEFS) {
        const fv = global.fields[def.key];
        if (fv) f[def.key] = String(fv.value);
      }
      setForm(f);
      setNotice("已保存全局主动行为设置。");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const resetGlobal = async () => {
    if (!confirm("恢复所有主动行为设置为默认值？（将清除控制台的自定义覆盖）"))
      return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const global = await studioApi.putInitiativeGlobal({ reset: true });
      setView((v) => (v ? { ...v, global } : v));
      const f = { ...form };
      for (const def of FIELD_DEFS) {
        const fv = global.fields[def.key];
        if (fv) f[def.key] = String(fv.value);
      }
      setForm(f);
      setNotice("已恢复默认。");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const saveChannel = async (channel: string) => {
    const cf = channelForms[channel];
    if (!cf) return;
    setChannelBusy(channel);
    setError("");
    setNotice("");
    try {
      const rhythm: InitiativeRhythmView = await studioApi.putInitiativeChannel(
        channel,
        cf,
      );
      setView((v) => (v ? { ...v, rhythm } : v));
      setNotice(
        cf.activity
          ? `已保存 ${CHANNEL_LABELS[channel] ?? channel} 渠道档位。`
          : `已清除 ${CHANNEL_LABELS[channel] ?? channel} 渠道覆盖（跟随内置默认）。`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChannelBusy("");
    }
  };

  return (
    <div>
      <div className="page-head">
        <h2>主动行为</h2>
        <button onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
      <p className="muted">
        这里解释并调整鲸鱼「主动说话」的频率、时段与各渠道节奏；你主动发消息时鲸鱼照常回应。
      </p>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}
      {loading && !view && <p className="muted">加载中…</p>}

      {view && (
        <>
          <section aria-label="全局主动行为">
            <div className="section-head">
              <h2>全局设置</h2>
              <span className="muted small">{view.global.quiet_hours_rule}</span>
            </div>
            <div className="initiative-fields">
              {FIELD_DEFS.map((def) => {
                const fv = view.global.fields[def.key];
                if (!fv) return null;
                return (
                  <div className="initiative-field" key={def.key}>
                    <label>
                      {def.label}
                      <input
                        aria-label={def.label}
                        value={form[def.key] ?? ""}
                        inputMode={def.kind === "int" ? "numeric" : undefined}
                        placeholder={def.kind === "time" ? "HH:MM" : undefined}
                        onChange={(e) =>
                          setForm((f) => ({ ...f, [def.key]: e.target.value }))
                        }
                      />
                    </label>
                    <span
                      className={
                        fv.source === "console" ? "badge info" : "badge soft"
                      }
                    >
                      {fv.source === "console" ? "自定义" : "默认"}
                    </span>
                    <span className="muted small">{fv.note}</span>
                  </div>
                );
              })}
            </div>
            <div className="actions">
              <button
                className="primary"
                disabled={saving}
                onClick={() => void saveGlobal()}
              >
                {saving ? "保存中…" : "保存全局设置"}
              </button>
              <button disabled={saving} onClick={() => void resetGlobal()}>
                恢复默认
              </button>
            </div>
            {view.global.locked_by_deploy.length > 0 && (
              <p className="muted small">
                部署侧锁定（控制台只读，需修改部署配置）：
                {view.global.locked_by_deploy
                  .map((k) => `${LOCKED_LABELS[k] ?? k}（${k}）`)
                  .join("、")}
              </p>
            )}
          </section>

          <section aria-label="渠道默认档位">
            <div className="section-head">
              <h2>渠道默认档位</h2>
              <span className="muted small">
                控制各渠道回复节奏：安静只回必要的、积极会更主动地多说几句。
              </span>
            </div>
            {Object.keys(channelForms).length === 0 && (
              <div className="empty">暂无渠道覆盖，全部跟随内置默认。</div>
            )}
            {Object.entries(channelForms).map(([channel, cf]) => (
              <div className="initiative-channel" key={channel}>
                <span className="subject-name">
                  {CHANNEL_LABELS[channel] ?? channel}
                </span>
                <select
                  aria-label={`${channel} 档位`}
                  value={cf.activity}
                  onChange={(e) =>
                    setChannelForms((m) => ({
                      ...m,
                      [channel]: { ...cf, activity: e.target.value },
                    }))
                  }
                >
                  <option value="">跟随内置默认</option>
                  {(view.rhythm.activity_modes.length > 0
                    ? view.rhythm.activity_modes
                    : ["quiet", "balanced", "lively"]
                  ).map((m) => (
                    <option key={m} value={m}>
                      {ACTIVITY_LABELS[m] ?? m}
                    </option>
                  ))}
                </select>
                <select
                  aria-label={`${channel} 分段上限`}
                  value={cf.max_segments}
                  onChange={(e) =>
                    setChannelForms((m) => ({
                      ...m,
                      [channel]: { ...cf, max_segments: Number(e.target.value) },
                    }))
                  }
                >
                  {[0, 1, 2, 3, 4, 5].map((n) => (
                    <option key={n} value={n}>
                      {n === 0 ? "分段跟随默认" : `最多 ${n} 段`}
                    </option>
                  ))}
                </select>
                <label className="check-row">
                  <input
                    type="checkbox"
                    aria-label={`${channel} 免打扰`}
                    checked={cf.dnd}
                    onChange={(e) =>
                      setChannelForms((m) => ({
                        ...m,
                        [channel]: { ...cf, dnd: e.target.checked },
                      }))
                    }
                  />
                  免打扰
                </label>
                <button
                  disabled={channelBusy === channel}
                  onClick={() => void saveChannel(channel)}
                >
                  {channelBusy === channel ? "保存中…" : "保存档位"}
                </button>
              </div>
            ))}
          </section>

          <section aria-label="会话级节奏">
            <div className="section-head">
              <h2>会话级节奏（只读）</h2>
              <span className="muted small">
                会话级节奏请到「工作台」对应会话卡上编辑。
              </span>
            </div>
            {view.rhythm.conversations.length === 0 ? (
              <div className="empty">暂无会话级节奏覆盖。</div>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>会话</th>
                      <th>档位</th>
                      <th>分段上限</th>
                      <th>免打扰</th>
                      <th>版本</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.rhythm.conversations.map((c) => (
                      <tr key={c.conversation_key}>
                        <td className="mono">{c.conversation_key}</td>
                        <td>{ACTIVITY_LABELS[c.activity] ?? c.activity}</td>
                        <td>{c.max_segments === 0 ? "跟随默认" : c.max_segments}</td>
                        <td>{c.dnd ? "是" : "否"}</td>
                        <td className="mono">{c.version}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section aria-label="最近决策">
            <div className="section-head">
              <h2>最近决策</h2>
              <span className="muted small">
                鲸鱼最近为什么回复/没回复（来自 Trace 账本）。
              </span>
            </div>
            {view.recent_decisions.length === 0 ? (
              <div className="empty">暂无决策记录。</div>
            ) : (
              <ul className="list">
                {view.recent_decisions.map((d) => (
                  <li className="list-item" key={d.trace_id}>
                    <div className="list-main">
                      <div className="record-tags">
                        <span className="muted mono">{fmtClock(d.at)}</span>
                        <span className={decisionBadge(d.status)}>
                          {DECISION_STATUS[d.status] ?? d.status}
                        </span>
                        <span className="mono muted">{d.conversation_id}</span>
                      </div>
                      <div>{d.reason_text || d.reason_code || "—"}</div>
                    </div>
                    <div className="actions">
                      <button
                        className="text-button mono"
                        title="在事件页查看该 trace"
                        onClick={() => onNavigate?.("events")}
                      >
                        {d.trace_id}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}
