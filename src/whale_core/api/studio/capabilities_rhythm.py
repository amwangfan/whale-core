"""S5 能力配置 + G4 会话节奏（ConversationRhythm）端点。

G4 纪律：节奏卡**只影响回复节奏**（活跃模式/分段上限/免打扰），不得携带
任何工具权限或记忆授权语义——严格字段表直接 422 掉未知字段（含
capabilities/scopes/memory 之类越界字段）；写路径 ctx.lock + 版本乐观锁 +
全量审计（只元数据）。
"""

from __future__ import annotations

import re

from whale_core.api.studio.shared import (
    _CAPS_PREFIX,
    StudioError,
    _audit,
    _ceiling_for,
    _check_expected_version,
    _now_iso,
    _ok,
    _parse_conversation_key,
    _reject_unknown_fields,
    _require_json_body,
)
from whale_core.api.studio.services import StudioServices
from whale_core.life.conversation_context import (
    SCOPE_CHAT_BASIC,
    SCOPE_DEVICE_NOTIFY,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TASK_READ_SELF,
    SCOPE_TOOL_SHELL_SAFE,
    SCOPE_TOOL_WEB_SEARCH,
)
from whale_core.life.rhythm import (
    ACTIVITY_MODES,
    MAX_SEGMENTS_LIMIT,
    ConversationRhythm,
    RhythmStore,
)

#: 能力白名单：本端点只管理能力 scope；memory.* 授权走 memories 绑定端点。
KNOWN_CAPABILITY_SCOPES = frozenset({
    SCOPE_CHAT_BASIC,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TOOL_WEB_SEARCH,
    SCOPE_TOOL_SHELL_SAFE,
    SCOPE_TASK_READ_SELF,
    SCOPE_DEVICE_NOTIFY,
})

_SCOPE_RE = re.compile(r"^[a-z0-9_.:\-]+$")


def _put_capabilities(handler, ctx, svc: StudioServices, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    raw_scopes = body.get("capabilities")
    if not isinstance(raw_scopes, list) or len(raw_scopes) > 32:
        raise StudioError(422, "invalid_request",
                          "capabilities 必须是最多 32 项的字符串数组")
    scopes: list[str] = []
    for item in raw_scopes:
        if not isinstance(item, str) or not _SCOPE_RE.match(item) \
                or len(item) > 80:
            raise StudioError(422, "invalid_request", f"非法 scope: {item!r}")
        if item not in KNOWN_CAPABILITY_SCOPES:
            raise StudioError(422, "invalid_request",
                              f"未知能力 scope {item!r}；memory.* 授权请使用 "
                              "conversations/{{key}}/memories 绑定端点")
        if item not in scopes:
            scopes.append(item)
    ceiling = _ceiling_for(ref.kind)
    over = [s for s in scopes if s not in ceiling]
    key = ref.as_key()
    if over:
        _audit(ctx, "capabilities.set", outcome="denied", reason="scope_ceiling",
               conversation_key=key, scopes=over)
        raise StudioError(403, "scope_ceiling",
                          "配置超过该会话类型的能力上限（群内不提权，INV-03）",
                          ceiling=list(ceiling), rejected=over)
    caps_key = f"{_CAPS_PREFIX}{key}"
    with ctx.lock:
        old = ctx.store.get(caps_key)
        old = old if isinstance(old, dict) else None
        current_version = int(old.get("version") or 0) if old else 0
        current_scopes = sorted(str(s) for s in old.get("scopes") or ()) if old else []
        # 幂等重放优先于版本检查（N13）
        if sorted(scopes) == current_scopes:
            handler._send(200, _ok({"key": key, "capabilities": current_scopes,
                                    "version": current_version,
                                    "changed": False}))
            return
        try:
            _check_expected_version(body, current_version)
        except StudioError as exc:
            _audit(ctx, "capabilities.set", outcome="denied", reason=exc.code,
                   conversation_key=key)
            raise
        version = current_version + 1
        ctx.store.put(caps_key, {
            "scopes": sorted(scopes), "version": version,
            "updated_by": "studio-api", "updated_at": _now_iso(),
        })
        _audit(ctx, "capabilities.set", conversation_key=key,
               scopes=sorted(scopes), version=version)
    handler._send(200, _ok({"key": key, "capabilities": sorted(scopes),
                            "version": version, "changed": True,
                            "ceiling": list(ceiling)}))


# ---------------------------------------------------------------------------
# G4：会话节奏（ConversationRhythm）
# ---------------------------------------------------------------------------

_RHYTHM_FIELDS = frozenset({"activity", "max_segments", "dnd",
                            "expected_version"})


def _rhythm_view(key: str, rhythm: ConversationRhythm, *, configured: bool) -> dict:
    return {
        "key": key,
        "rhythm": {
            "activity": rhythm.activity,
            "max_segments": rhythm.max_segments,
            "dnd": rhythm.dnd,
        },
        "version": rhythm.version,
        "configured": configured,
    }


def _get_rhythm(handler, ctx, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    key = ref.as_key()
    store = RhythmStore(ctx.store)
    handler._send(200, _ok(_rhythm_view(
        key, store.get(key), configured=store.configured(key))))


def _parse_rhythm_body(body: dict) -> ConversationRhythm:
    _reject_unknown_fields(body, set(_RHYTHM_FIELDS))
    activity = body.get("activity", "balanced")
    if not isinstance(activity, str) or activity not in ACTIVITY_MODES:
        raise StudioError(422, "invalid_request",
                          f"activity 必须是 {list(ACTIVITY_MODES)!r} 之一")
    max_segments = body.get("max_segments", 0)
    if not isinstance(max_segments, int) or isinstance(max_segments, bool) \
            or not 0 <= max_segments <= MAX_SEGMENTS_LIMIT:
        raise StudioError(422, "invalid_request",
                          f"max_segments 必须是 0..{MAX_SEGMENTS_LIMIT} 的整数"
                          "（0 = 跟随默认：群2/私3）")
    dnd = body.get("dnd", False)
    if not isinstance(dnd, bool):
        raise StudioError(422, "invalid_request", "dnd 必须是布尔值")
    return ConversationRhythm(activity=activity, max_segments=max_segments,
                              dnd=dnd)


def _put_rhythm(handler, ctx, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    rhythm = _parse_rhythm_body(body)
    key = ref.as_key()
    store = RhythmStore(ctx.store)
    with ctx.lock:
        current = store.get(key)
        # 幂等重放优先于版本检查：同值重试是 200 no-op（N13）。
        if (rhythm.activity, rhythm.max_segments, rhythm.dnd) == (
                current.activity, current.max_segments, current.dnd):
            handler._send(200, _ok({
                **_rhythm_view(key, current, configured=store.configured(key)),
                "changed": False,
            }))
            return
        try:
            _check_expected_version(body, current.version)
        except StudioError as exc:
            _audit(ctx, "rhythm.set", outcome="denied", reason=exc.code,
                   conversation_key=key)
            raise
        version = store.put(key, rhythm)
        _audit(ctx, "rhythm.set", conversation_key=key,
               activity=rhythm.activity, max_segments=rhythm.max_segments,
               dnd=rhythm.dnd, version=version)
    handler._send(200, _ok({
        **_rhythm_view(key, store.get(key), configured=True),
        "changed": True,
    }))
