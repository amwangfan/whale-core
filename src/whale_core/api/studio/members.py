"""G2：成员本人授权与个人记忆管理（consent.* / memory.*-own）。

纪律（下一阶段计划 §4 / §11 MEM-02/03/04）：

- consent.grant 必须携带「本人已认证私聊」的结构性证据
  （actor_conversation_key），其归属 principal 必须等于 consent 主体——
  群主/管理通道替成员授权一律 403（member_actor_mismatch）。
- 本人记忆管理（list/correct/delete）同样要求本人私聊证据；撤销 consent
  后仍可管理存量记忆（隐私权不随授权消失）。
- 管理通道（Bearer=主人）可以看到 consent 状态元数据，但看不到成员私人
  记忆全文（list-own 对群主同样要求成员本人证据）。
- 所有写操作严格字段表 + ctx.lock + 审计（只元数据，不存记忆正文）。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from whale_core.api.studio.services import StudioServices
from whale_core.api.studio.shared import (
    StudioError,
    _audit,
    _now_iso,
    _ok,
    _parse_conversation_key,
    _reject_unknown_fields,
    _require_json_body,
)
from whale_core.life.conversation_context import (
    principal_matches_private_conversation,
)
from whale_core.memory.bindings import BindingError
from whale_core.memory.consent import (
    DEFAULT_CONSENT_TTL_S,
    KNOWN_CONSENT_SCOPES,
    ConsentError,
)
from whale_core.memory.extraction import contains_sensitive
from whale_core.memory.governance import _purge_embedding, forget_gone_everywhere
from whale_core.memory.spaces import MemorySpaceError
from whale_core.memory.store import MemoryKind

#: principal_id 形如 anon:qq:10001 / paired:qq:10001。
_PRINCIPAL_RE = re.compile(
    r"^(anon|paired):[A-Za-z0-9_.\-]{1,32}:[A-Za-z0-9_.:\-]{1,64}$")
_GROUP_ID_RE = re.compile(r"^group:[A-Za-z0-9_.\-]{1,64}$")
_MAX_CONSENT_TTL_S = 10 * 365 * 86400
_MEMBER_LIST_LIMIT = 200


def _require_member_actor(store: Any, principal_raw: Any, raw_key: Any) -> tuple[str, str]:
    """校验「成员本人私聊」证据；返回 (principal_id, conversation_key)。

    这是 MEM-02 的 API 关卡：consent 主体 == 会话归属主体。群主用自己的
    私聊、或任何群会话 key，都无法通过（结构性校验，不看昵称/正文）。

    G6 红队中-2 加固：形状自洽只是必要条件——另要求该私聊**真实发生过
    入站对话**（chat:history 存在成员发言记录），否则持管理 token 者可为
    从未说过话的任意 uin 代授 consent。
    """
    if not isinstance(principal_raw, str) or not _PRINCIPAL_RE.match(principal_raw):
        raise StudioError(422, "invalid_request",
                          "principal_id 形如 anon:qq:123 / paired:qq:123")
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise StudioError(422, "invalid_request",
                          "actor_conversation_key 必填（成员本人已认证私聊）")
    ref = _parse_conversation_key(raw_key)
    if not principal_matches_private_conversation(
            ref.channel, ref.kind, ref.conversation_id, principal_raw):
        raise StudioError(403, "member_actor_mismatch",
                          "只有成员本人的已认证私聊可以发起该操作；"
                          "群主/他人代操作、群会话一律拒绝（MEM-02/03）")
    history = store.get(f"chat:history:{ref.conversation_id}")
    has_member_turn = isinstance(history, list) and any(
        isinstance(m, dict) and m.get("role") == "user" for m in history)
    if not has_member_turn:
        raise StudioError(403, "member_actor_no_evidence",
                          "该私聊尚无成员真实发言记录，不能代授 consent；"
                          "请让成员本人先私聊鲸鱼后再授权（MEM-02）")
    return principal_raw, ref.as_key()


def _consent_view(consent) -> dict:
    return {
        "principal_id": consent.principal_id,
        "source_group": consent.source_group,
        "scope": consent.scope,
        "version": consent.version,
        "granted_at": consent.granted_at,
        "revoked_at": consent.revoked_at,
        "ttl_s": consent.ttl_s,
        "granted_via": consent.granted_via,
    }


def _member_consent_grant(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "source_group", "scope",
                                  "ttl_s", "actor_conversation_key"})
    try:
        principal_id, actor_key = _require_member_actor(
            ctx.store, body.get("principal_id"), body.get("actor_conversation_key"))
    except StudioError as exc:
        _audit(ctx, "member.consent.grant", outcome="denied", reason=exc.code)
        raise
    source_group = body.get("source_group")
    if not isinstance(source_group, str) or not _GROUP_ID_RE.match(source_group):
        raise StudioError(422, "invalid_request",
                          "source_group 必须是 group:<群号> 形式的会话 id")
    scope = body.get("scope", "self_memory")
    if scope not in KNOWN_CONSENT_SCOPES:
        raise StudioError(422, "invalid_request",
                          f"未知授权 scope: {scope!r}")
    ttl_raw = body.get("ttl_s")
    if ttl_raw is None:
        ttl_s = float(DEFAULT_CONSENT_TTL_S)
    elif isinstance(ttl_raw, (int, float)) and not isinstance(ttl_raw, bool) \
            and 0 <= float(ttl_raw) <= _MAX_CONSENT_TTL_S:
        ttl_s = float(ttl_raw)
    else:
        raise StudioError(422, "invalid_request",
                          f"ttl_s 必须在 0..{_MAX_CONSENT_TTL_S} 之间")
    with ctx.lock:
        try:
            consent = svc.consents.grant(
                principal_id, source_group, scope=scope, ttl_s=ttl_s,
                granted_by=principal_id, granted_via=actor_key)
            ref = svc.bindings.ensure_member_space(
                principal_id, conversation_key=actor_key,
                granted_by=principal_id)
        except (ConsentError, BindingError, MemorySpaceError) as exc:
            _audit(ctx, "member.consent.grant", outcome="denied",
                   reason="invalid_request", principal_id=principal_id)
            raise StudioError(422, "invalid_request", str(exc)[:200])
        _audit(ctx, "member.consent.grant", principal_id=principal_id,
               source_group=source_group, version=consent.version,
               space_id=ref.space_id)
    handler._send(200, _ok({"consent": _consent_view(consent),
                            "space_id": ref.space_id}))


def _member_consent_revoke(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "source_group"})
    principal_raw = body.get("principal_id")
    if not isinstance(principal_raw, str) or not _PRINCIPAL_RE.match(principal_raw):
        raise StudioError(422, "invalid_request", "principal_id 非法")
    source_group = body.get("source_group")
    if source_group is not None and (not isinstance(source_group, str)
                                     or not _GROUP_ID_RE.match(source_group)):
        raise StudioError(422, "invalid_request", "source_group 非法")
    with ctx.lock:
        # owner 决策 2026-11-01：撤销语义 = opt-out（停止召回与写入）。
        # 管理通道撤销是安全正向操作，无需成员证据；grant 才要。
        if source_group:
            revoked = 1 if svc.consents.revoke(principal_raw, source_group) else 0
        else:
            revoked = svc.consents.revoke_all(principal_raw)
        svc.consents.opt_out(principal_raw)
        _audit(ctx, "member.consent.revoke", principal_id=principal_raw,
               source_group=source_group or "*", revoked=revoked)
    handler._send(200, _ok({"principal_id": principal_raw,
                            "revoked": revoked}))


def _member_consent_list(handler, ctx, svc: StudioServices) -> None:
    """consent 状态元数据视图（管理通道可见；不含任何记忆内容）。"""
    principal_id = str(handler.query.get("principal_id", [""])[0] or "")
    if not _PRINCIPAL_RE.match(principal_id):
        raise StudioError(422, "invalid_request", "principal_id 非法")
    rows = svc.consents.list_for(principal_id)
    handler._send(200, _ok({
        "principal_id": principal_id,
        "consents": [_consent_view(c) for c in rows],
        "active": svc.consents.has_valid(principal_id),
    }))


def _member_space_or_404(svc: StudioServices, principal_id: str) -> str:
    ref = svc.directory.space_for_member_management(principal_id)
    if ref is None:
        raise StudioError(404, "not_found", "该成员没有个人记忆空间（未授权过）")
    return ref.space_id


def _member_record_view(rec) -> dict:
    content = dict(rec.content)
    return {
        "key": rec.key,
        "kind": rec.kind.value,
        "summary": str(content.get("summary") or ""),
        "confidence": float(content.get("confidence") or 0.0),
        "source_group": str(content.get("source_group") or ""),
        "source_message_ids": list(content.get("source_message_ids") or []),
        "consent_version": int(content.get("consent_version") or 0),
        "observed_at": str(content.get("observed_at") or ""),
        "expires_at": content.get("expires_at"),
        "created_at": rec.created_at.isoformat(),
        "superseded": "superseded" in rec.tags,
    }


def _member_memory_list(handler, ctx, svc: StudioServices) -> None:
    """memory.list-own：本人查看自己的成员记忆（须本人私聊证据）。"""
    try:
        principal_id, _ = _require_member_actor(
            ctx.store,
            handler.query.get("principal_id", [""])[0],
            handler.query.get("actor_conversation_key", [""])[0])
    except StudioError as exc:
        _audit(ctx, "member.memory.list", outcome="denied", reason=exc.code)
        raise
    space_id = _member_space_or_404(svc, principal_id)
    items: list[dict] = []
    for kind in (MemoryKind.EPISODE, MemoryKind.PREFERENCE):
        if len(items) >= _MEMBER_LIST_LIMIT:
            break
        for rec in ctx.store.list_by_kind(kind, space_ids=(space_id,)):
            if "member" not in rec.tags or "superseded" in rec.tags:
                continue
            items.append(_member_record_view(rec))
            if len(items) >= _MEMBER_LIST_LIMIT:
                break
    _audit(ctx, "member.memory.list", principal_id=principal_id,
           count=len(items))
    handler._send(200, _ok({"principal_id": principal_id,
                            "space_id": space_id, "items": items}))


def _member_record_for_write(ctx, svc: StudioServices, principal_id: str,
                             key: str):
    """取出待纠正/删除的成员记录；越空间/越标签一律 404（不泄露存在性）。"""
    space_id = _member_space_or_404(svc, principal_id)
    rec = ctx.store.get_record(key)
    if rec is None or rec.space_id != space_id or "member" not in rec.tags:
        raise StudioError(404, "not_found", "记录不存在")
    return space_id, rec


def _plugin_registry(ctx):
    return getattr(getattr(ctx, "manager", None), "registry", None)


def _member_memory_correct(handler, ctx, svc: StudioServices) -> None:
    """memory.correct-own：本人更正一条记忆（旧记录 superseded，退出召回）。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "actor_conversation_key",
                                  "key", "new_summary"})
    try:
        principal_id, _ = _require_member_actor(
            ctx.store, body.get("principal_id"), body.get("actor_conversation_key"))
    except StudioError as exc:
        _audit(ctx, "member.memory.correct", outcome="denied", reason=exc.code)
        raise
    key = str(body.get("key") or "").strip()
    new_summary = body.get("new_summary")
    if not isinstance(new_summary, str) or not (1 <= len(new_summary.strip())
                                                <= 1500):
        raise StudioError(422, "invalid_request",
                          "new_summary 必须是 1..1500 字符的字符串")
    if contains_sensitive(new_summary):
        _audit(ctx, "member.memory.correct", outcome="denied",
               reason="sensitive_content", principal_id=principal_id)
        raise StudioError(422, "invalid_request",
                          "更正内容含敏感信息（手机号/证件/地址/凭据类），已拒绝")
    with ctx.lock:
        space_id, old = _member_record_for_write(ctx, svc, principal_id, key)
        old_content = dict(old.content)
        old_summary = str(old_content.get("summary") or "")
        # 旧记录退出一切召回（superseded），新记录留在同一成员空间。
        old_content["layer"] = "superseded"
        old_content["superseded_at"] = _now_iso()
        ctx.store.put_record(type(old)(
            kind=old.kind, key=old.key, content=old_content,
            record_id=old.record_id, created_at=old.created_at,
            tags=tuple(set(old.tags) | {"superseded"}), space_id=old.space_id))
        _purge_embedding(_plugin_registry(ctx), old_summary)
        new_key = f"member-mem:corr-{uuid4().hex[:16]}"
        new_content = dict(old.content)
        new_content.update({
            "summary": new_summary.strip()[:1500],
            "source": "member-correct",
            "layer": "member",
            "corrected_from": old.key,
            "last_confirmed": _now_iso(),
        })
        ctx.store.put_record(type(old)(
            kind=old.kind, key=new_key, content=new_content,
            tags=tuple(t for t in old.tags if t != "superseded"),
            space_id=space_id))
        _audit(ctx, "member.memory.correct", principal_id=principal_id,
               old_key_tail=key[-8:], new_key_tail=new_key[-8:])
    handler._send(200, _ok({"corrected": True, "old_key": key,
                            "new_key": new_key}))


def _member_memory_delete(handler, ctx, svc: StudioServices) -> None:
    """memory.delete-own：真删 + embedding 缓存 forget_text 传播。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"principal_id", "actor_conversation_key",
                                  "key"})
    try:
        principal_id, _ = _require_member_actor(
            ctx.store, body.get("principal_id"), body.get("actor_conversation_key"))
    except StudioError as exc:
        _audit(ctx, "member.memory.delete", outcome="denied", reason=exc.code)
        raise
    key = str(body.get("key") or "").strip()
    with ctx.lock:
        _member_record_for_write(ctx, svc, principal_id, key)
        deleted = forget_gone_everywhere(
            ctx.store, key, registry=_plugin_registry(ctx))
        _audit(ctx, "member.memory.delete", principal_id=principal_id,
               key_tail=key[-8:], deleted=bool(deleted))
    handler._send(200, _ok({"deleted": bool(deleted), "key": key}))
