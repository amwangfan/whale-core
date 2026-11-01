"""UX-07（issue #42）：统一主动行为设置与决策解释 API。

纪律：

- **单一配置来源**：UI 写入 kv ``life:proactive:prefs``（白名单字段），
  runner 分发时经 ``initiative.load_prefs`` 读同一 kv——不存在只改 UI
  不生效。安全相关开关（enabled/allowed_channels/auto_send/allow_calls）
  不开热改，仍由部署侧决定（响应里如实标 source）。
- 静默时段只压制主动打扰；被动回复（reactive_inbound/reactive_completion）
  一律豁免（issue #22 固定规则，UI 只展示不提供开关）。
- 渠道/会话节奏（activity/dnd/max_segments）走 RhythmStore，同样单一来源。
- 决策解释来自 Trace 账本（#39）：decide 阶段的稳定 reason_code + 中文说明；
  账本不可用时返回空列表而非编造。
- 所有写操作审计；字段严格校验；设置不含、也不授予任何权限/scope。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from whale_core.api.studio.services import StudioServices
from whale_core.api.studio.shared import (
    StudioError,
    _audit,
    _ok,
    _reject_unknown_fields,
    _require_json_body,
)
from whale_core.life.initiative import (
    PREFS_KV_FIELDS,
    PREFS_KEY,
    ProactivePrefs,
    load_prefs,
)
from whale_core.life.rhythm import ACTIVITY_MODES, RhythmStore
from whale_core.life.trace import REASON_TEXT

#: 管理面可热改的全局字段（与 PREFS_KV_FIELDS 一致，显式引用）。
_EDITABLE = PREFS_KV_FIELDS

#: 字段影响预览（静态说明，配合当前生效值展示）。
_FIELD_NOTES = {
    "daily_limit": "主动消息（提醒/主动搭话）每日上限；被动回复不受影响",
    "passive_daily_limit": "被动回复每日保险丝（防跑飞）；0 需谨慎",
    "quiet_start": "静默开始（本地时）；静默期内不主动打扰",
    "quiet_end": "静默结束（本地时）",
}


def _prefs_view(ctx) -> dict:
    env_default = ProactivePrefs()  # 部署侧 env 在 runner 层；管理面展示默认值
    effective = load_prefs(ctx.store, env_default)
    overrides = ctx.store.get(PREFS_KEY)
    overrides = dict(overrides) if isinstance(overrides, dict) else {}
    fields: dict = {}
    for name in sorted(_EDITABLE):
        fields[name] = {
            "value": getattr(effective, name) if not name.startswith("quiet_")
                     else getattr(effective, name).strftime("%H:%M"),
            "source": "console" if name in overrides else "default",
            "note": _FIELD_NOTES.get(name, ""),
        }
    return {
        "fields": fields,
        "quiet_hours_rule": "静默时段只压制主动打扰；你主动发消息时鲸鱼照常回应",
        "editable": sorted(_EDITABLE),
        "locked_by_deploy": ["enabled", "allowed_channels", "auto_send",
                             "allow_calls"],
    }


def _rhythm_view(ctx) -> dict:
    rs = RhythmStore(ctx.store)
    channels: dict = {}
    for ch in ("qq", "call", "hud", "notify"):
        cd = rs.channel_default(ch)
        if cd is not None:
            channels[ch] = {"activity": cd.activity,
                            "max_segments": cd.max_segments, "dnd": cd.dnd}
    conversations: list = []
    for key in sorted(ctx.store.keys()):
        if not key.startswith("bind:rhythm:"):
            continue
        conv_key = key[len("bind:rhythm:"):]
        r = rs.get(conv_key)
        conversations.append({
            "conversation_key": conv_key, "activity": r.activity,
            "max_segments": r.max_segments, "dnd": r.dnd, "version": r.version,
        })
    return {"channels": channels, "conversations": conversations,
            "activity_modes": list(ACTIVITY_MODES)}


def _recent_decisions(ctx, limit: int = 20) -> list:
    """最近主动/被动决策解释（来自 Trace 账本；账本缺失则空列表）。"""
    led = getattr(ctx, "trace_ledger", None)
    if led is None:
        try:
            from whale_core.api.routes.traces import _ledger as _lazy
            class _H:  # 最小 handler 适配
                pass
            h = _H()
            h.ctx = ctx
            led = _lazy(h)
        except Exception:
            return []
    try:
        rows, _ = led.query(limit=min(max(int(limit), 1), 50))
    except Exception:
        return []
    out: list = []
    for t in rows:
        full = led.get(t.trace_id)
        if full is None:
            continue
        decide = next((s for s in full.spans if s.phase == "decide"), None)
        out.append({
            "trace_id": full.trace_id,
            "at": full.started_at,
            "conversation_id": full.conversation_id,
            "status": full.status,
            "reason_code": decide.reason_code if decide else "",
            "reason_text": REASON_TEXT.get(
                decide.reason_code, "") if decide else "",
        })
    return out


def _get_initiative(handler, ctx, svc: StudioServices) -> None:
    handler._send(200, _ok({
        "global": _prefs_view(ctx),
        "rhythm": _rhythm_view(ctx),
        "recent_decisions": _recent_decisions(ctx),
    }))


def _parse_time_hhmm(raw: object, field: str) -> str:
    if not isinstance(raw, str) or ":" not in raw:
        raise StudioError(422, "invalid_request", f"{field} 需 HH:MM 格式")
    try:
        hh, mm = raw.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
    except ValueError:
        raise StudioError(422, "invalid_request", f"{field} 需 HH:MM 格式")
    return f"{int(hh):02d}:{int(mm):02d}"


def _put_global(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    _reject_unknown_fields(body, set(_EDITABLE) | {"reset"})
    if body.get("reset") is True:
        with ctx.lock:
            ctx.store.delete(PREFS_KEY)
            _audit(ctx, "initiative.prefs.reset")
        handler._send(200, _ok({"global": _prefs_view(ctx)}))
        return
    fields: dict = {}
    for name in ("daily_limit", "passive_daily_limit"):
        if name in body:
            value = body[name]
            if value is None:
                fields[name] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0 or value > 100000:
                raise StudioError(422, "invalid_request",
                                  f"{name} 须为 0..100000 的整数或 null")
            fields[name] = value
    for name in ("quiet_start", "quiet_end"):
        if name in body:
            value = body[name]
            fields[name] = None if value is None else _parse_time_hhmm(value, name)
    if not fields:
        raise StudioError(422, "invalid_request", "没有需要修改的字段")
    with ctx.lock:
        data = ctx.store.get(PREFS_KEY)
        data = dict(data) if isinstance(data, dict) else {}
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)  # null = 恢复默认（删除覆盖）
            else:
                data[key] = value
        ctx.store.put(PREFS_KEY, data)
        _audit(ctx, "initiative.prefs.update", fields=sorted(fields))
    handler._send(200, _ok({"global": _prefs_view(ctx)}))


def _put_channel(handler, ctx, svc: StudioServices) -> None:
    """渠道级默认档位；activity="" 清除覆盖（恢复内置默认）。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"channel", "activity", "max_segments", "dnd"})
    channel = str(body.get("channel") or "").strip()[:32]
    if not channel:
        raise StudioError(422, "invalid_request", "channel 必填")
    activity = str(body.get("activity") or "").strip()
    if activity and activity not in ACTIVITY_MODES:
        raise StudioError(422, "invalid_request",
                          f"activity 须为 {'/'.join(ACTIVITY_MODES)} 或空串")
    max_segments = body.get("max_segments") or 0
    if isinstance(max_segments, bool) or not isinstance(max_segments, int) \
            or not 0 <= max_segments <= 5:
        raise StudioError(422, "invalid_request", "max_segments 须为 0..5")
    dnd = body.get("dnd") is True
    with ctx.lock:
        RhythmStore(ctx.store).set_channel_default(
            channel, activity=activity, max_segments=max_segments, dnd=dnd)
        _audit(ctx, "initiative.channel.set" if activity else
               "initiative.channel.reset", channel=channel,
               activity=activity or "")
    handler._send(200, _ok({"rhythm": _rhythm_view(ctx)}))
