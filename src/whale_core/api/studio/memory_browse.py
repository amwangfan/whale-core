"""UX-03（issue #38）：按人物 / 群聊 / 空间的记忆浏览与治理 API。

纪律：

- **服务端强制 ACL**（不靠前端过滤）：
  - protected_original → 整体拒绝（403 vault_restricted），原始库浏览只走
    ``/studio/vault/records``（脱敏、审计、owner 管理通道）；
  - member_private → 管理通道只见**元数据行**（无 summary/正文），全文管理
    只走成员本人路径（``/studio/members/memory/*``，MEM-02/03）；
  - 其余空间（owner_working_copy / group_space / shareable_fork / 遗留未分区）
    → owner 管理通道全量可读可治理。
- 分页在服务端（offset/limit，limit ≤ 200）；存储层按 space_id 索引查询，
  不扫全库后在前端过滤。
- 所有治理写操作审计（只元数据，不存记忆正文）。
- 「停止继续记录」：space 级暂停开关（kv ``space:paused:{space_id}``），
  自动写入路径（聊天情节沉淀 / 成员提炼）尊重该开关；成员级退出仍是
  consent opt-out（既有，不在此重复）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from whale_core.api.studio.services import StudioServices
from whale_core.api.studio.shared import (
    StudioError,
    _audit,
    _now_iso,
    _ok,
    _reject_unknown_fields,
    _require_json_body,
)
from whale_core.memory.governance import forget_gone_everywhere
from whale_core.memory.pause import PAUSED_PREFIX, is_recording_paused
from whale_core.memory.spaces import MemorySpaceType
from whale_core.memory.store import MemoryKind

_PAUSED_PREFIX = PAUSED_PREFIX
_BROWSE_MAX_LIMIT = 200
_MEMBER_BIND_PREFIX = "member:bind:"

_SCOPE_NOTES = {
    MemorySpaceType.PROTECTED_ORIGINAL.value:
        "主人原始库：仅主人本人已认证私聊可直连；管理界面只见过脱敏摘要",
    MemorySpaceType.OWNER_WORKING_COPY.value:
        "主人工作副本：主人会话（私聊/群聊）可用",
    MemorySpaceType.GROUP_SPACE.value:
        "群公共记忆：该群会话内可用",
    MemorySpaceType.SHAREABLE_FORK.value:
        "受控副本：仅绑定了它的会话可用",
    MemorySpaceType.MEMBER_PRIVATE.value:
        "群友私有：仅本人已认证私聊可见；管理通道只展示元数据",
    "": "遗留未分区记录（迁移前数据）",
}


# ---------------------------------------------------------------- 工具


def _parse_time(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise StudioError(422, "invalid_request",
                          f"时间格式非法（需 ISO 8601）: {raw[:40]!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _space_meta(svc: StudioServices, space_id: str) -> dict:
    return svc.spaces.get(space_id) or {}


def _scope_note(space_type: str) -> str:
    return _SCOPE_NOTES.get(space_type, _SCOPE_NOTES[""])


def _count_records(store: Any, space_id: str) -> int:
    return sum(len(store.list_by_kind(kind, space_ids=(space_id,)))
               for kind in MemoryKind)


# ---------------------------------------------------------------- 主体视图


def _subjects(handler, ctx, svc: StudioServices) -> None:
    """GET /studio/memory/subjects：主体优先视图（主人 / 群友 / 群聊 / 未分区）。"""
    owner_spaces: list[dict] = []
    group_spaces: list[dict] = []
    fork_spaces: list[dict] = []
    vault_present = False
    for ref in svc.spaces.list_spaces(include_protected=True):
        meta = svc.spaces.get(ref.space_id) or {}
        entry = {
            "space_id": ref.space_id,
            "space_type": ref.type.value,
            "name": str(meta.get("name") or ""),
            "paused": is_recording_paused(ctx.store, ref.space_id),
            "scope_note": _scope_note(ref.type.value),
        }
        if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
            # INV-08：原始库只暴露存在，不给计数
            vault_present = True
            continue
        entry["record_count"] = _count_records(ctx.store, ref.space_id)
        if ref.type is MemorySpaceType.OWNER_WORKING_COPY:
            owner_spaces.append(entry)
        elif ref.type is MemorySpaceType.GROUP_SPACE:
            group_spaces.append(entry)
        else:
            fork_spaces.append(entry)

    members: list[dict] = []
    for key in sorted(ctx.store.keys()):
        if not key.startswith(_MEMBER_BIND_PREFIX):
            continue
        data = ctx.store.get(key)
        if not isinstance(data, dict):
            continue
        principal_id = str(data.get("principal_id") or "")
        space_id = str(data.get("space_id") or "")
        ref = svc.spaces.ref(space_id)
        if ref is None:
            continue
        members.append({
            "principal_id": principal_id,
            "space_id": space_id,
            "record_count": _count_records(ctx.store, space_id),
            "opted_out": svc.consents.is_opted_out(principal_id),
            "consent_active": svc.consents.has_valid(principal_id),
            "paused": is_recording_paused(ctx.store, space_id),
            "scope_note": _scope_note(MemorySpaceType.MEMBER_PRIVATE.value),
        })

    legacy_count = _count_records(ctx.store, "")
    _audit(ctx, "memory.subjects", members=len(members),
           owner_spaces=len(owner_spaces), groups=len(group_spaces))
    handler._send(200, _ok({
        "owner": {"spaces": owner_spaces, "vault_present": vault_present},
        "members": members,
        "groups": group_spaces,
        "forks": fork_spaces,
        "legacy_unscoped_records": legacy_count,
    }))


# ---------------------------------------------------------------- 浏览


def _resolve_space_filter(handler, ctx, svc: StudioServices) -> "tuple[str, ...]":
    """space_id / principal_id / conversation_key → 空间集合（显式元组）。"""
    q = handler.query
    space_id = (q.get("space_id", [""])[0] or "").strip()
    principal_id = (q.get("principal_id", [""])[0] or "").strip()
    conv_key = (q.get("conversation_key", [""])[0] or "").strip()
    chosen = [bool(space_id), bool(principal_id), bool(conv_key)]
    if sum(chosen) != 1:
        raise StudioError(422, "invalid_request",
                          "space_id / principal_id / conversation_key 三选一")
    if space_id:
        if svc.spaces.ref(space_id) is None:
            raise StudioError(404, "not_found", "空间不存在或已注销")
        return (space_id,)
    if principal_id:
        binding = svc.bindings.member_binding_for(principal_id)
        if not binding:
            raise StudioError(404, "not_found", "该成员没有个人记忆空间")
        return (str(binding.get("space_id") or ""),)
    rows = ctx.store.get(f"bind:mem:{conv_key}")
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    space_ids = tuple(str(r.get("space_id") or "")
                      for r in rows if r.get("can_read") and r.get("space_id"))
    if not space_ids:
        raise StudioError(404, "not_found", "该会话没有可读的记忆空间绑定")
    return space_ids


def _record_row(ctx, svc: StudioServices, rec, *, metadata_only: bool) -> dict:
    content = dict(rec.content or {})
    space_id = rec.space_id or ""
    stype = ""
    if space_id:
        meta = _space_meta(svc, space_id)
        stype = str(meta.get("type") or "")
    row = {
        "key": rec.key,
        "kind": rec.kind.value,
        "space_id": space_id,
        "space_type": stype,
        "scope_note": _scope_note(stype),
        "created_at": rec.created_at.isoformat(),
        "superseded": "superseded" in rec.tags,
        "confidence": float(content.get("confidence") or 0.0),
        "source": str(content.get("source") or content.get("layer") or ""),
        "source_group": str(content.get("source_group") or ""),
        "expires_at": content.get("expires_at"),
        "last_confirmed": str(content.get("last_confirmed") or ""),
    }
    if metadata_only:
        # member_private：管理通道只见元数据（MEM-02/03，内容仅本人路径）
        row_meta = dict(row)
        row_meta["restricted"] = True
        return row_meta
    row.update({
        "summary": str(content.get("summary") or content.get("text") or "")[:1000],
        "source_message_ids": [
            str(m) for m in (content.get("source_message_ids") or [])][:8],
        "tags": [t for t in rec.tags if t not in ("member",)],
    })
    return row


def _browse(handler, ctx, svc: StudioServices) -> None:
    """GET /studio/memory/browse：筛选 + 服务端分页。"""
    space_ids = _resolve_space_filter(handler, ctx, svc)
    refs = [svc.spaces.ref(s) for s in space_ids if s]
    if any(r is not None and r.type is MemorySpaceType.PROTECTED_ORIGINAL
           for r in refs):
        _audit(ctx, "memory.browse", outcome="denied", reason="vault_restricted")
        raise StudioError(403, "vault_restricted",
                          "原始库记录请走 /studio/vault/records（脱敏视图）")
    metadata_only = any(
        r is not None and r.type is MemorySpaceType.MEMBER_PRIVATE for r in refs)

    q = handler.query
    raw_kind = (q.get("kind", [""])[0] or "").strip()
    kind_filter: "tuple[MemoryKind, ...] | None" = None
    if raw_kind:
        try:
            kind_filter = tuple(MemoryKind(k.strip())
                                for k in raw_kind.split(",") if k.strip())
        except ValueError:
            raise StudioError(422, "invalid_request", f"未知 kind: {raw_kind!r}")
        kind_filter = kind_filter or None
    text_q = (q.get("q", [""])[0] or "").strip().lower()
    source = (q.get("source", [""])[0] or "").strip()
    since = _parse_time(q.get("since", [""])[0] or "")
    until = _parse_time(q.get("until", [""])[0] or "")
    include_superseded = (q.get("include_superseded", [""])[0]
                          or "").strip() in ("1", "true", "yes")
    try:
        limit = int(q.get("limit", ["50"])[0] or "50")
        offset = int(q.get("offset", ["0"])[0] or "0")
    except ValueError:
        raise StudioError(422, "invalid_request", "limit/offset 必须是整数")
    if not 1 <= limit <= _BROWSE_MAX_LIMIT:
        raise StudioError(422, "invalid_request",
                          f"limit 须在 1..{_BROWSE_MAX_LIMIT}")
    if offset < 0:
        raise StudioError(422, "invalid_request", "offset 不能为负")

    matched: list = []
    scan_kinds = kind_filter or tuple(MemoryKind)
    for kind in scan_kinds:
        for rec in ctx.store.list_by_kind(kind, space_ids=space_ids):
            if not include_superseded and "superseded" in rec.tags:
                continue
            if since and rec.created_at < since:
                continue
            if until and rec.created_at > until:
                continue
            content = dict(rec.content or {})
            if source:
                rec_source = str(content.get("source")
                                 or content.get("layer") or "")
                if rec_source != source:
                    continue
            if text_q:
                summary = str(content.get("summary")
                              or content.get("text") or "").lower()
                if text_q not in summary and text_q not in rec.key.lower():
                    continue
            matched.append(rec)
    matched.sort(key=lambda r: r.created_at, reverse=True)
    total = len(matched)
    page = matched[offset:offset + limit]
    _audit(ctx, "memory.browse", spaces=len(space_ids), total=total,
           metadata_only=metadata_only)
    handler._send(200, _ok({
        "space_ids": list(space_ids),
        "metadata_only": metadata_only,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_record_row(ctx, svc, r, metadata_only=metadata_only)
                  for r in page],
    }))


# ---------------------------------------------------------------- 治理操作


def _record_for_owner_op(ctx, svc: StudioServices, key: str):
    """取出待治理记录并强制空间 ACL；越权一律 403/404（不泄露存在性）。"""
    rec = ctx.store.get_record(key)
    if rec is None:
        raise StudioError(404, "not_found", "记录不存在")
    ref = svc.spaces.ref(rec.space_id) if rec.space_id else None
    if ref is not None and ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        raise StudioError(403, "vault_restricted",
                          "原始库记录不可在管理面改写；纠错请新建工作副本记录")
    if ref is not None and ref.type is MemorySpaceType.MEMBER_PRIVATE:
        raise StudioError(403, "member_private_restricted",
                          "群友私有记忆只能由本人经已认证私聊管理"
                          "（/studio/members/memory/*）")
    return rec


def _correct(handler, ctx, svc: StudioServices) -> None:
    """POST /studio/memory/correct：纠错——旧记录 superseded，新记录继任。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"key", "new_summary"})
    key = str(body.get("key") or "").strip()
    new_summary = body.get("new_summary")
    if not isinstance(new_summary, str) or not (1 <= len(new_summary.strip())
                                                <= 1500):
        raise StudioError(422, "invalid_request",
                          "new_summary 必须是 1..1500 字符的字符串")
    with ctx.lock:
        old = _record_for_owner_op(ctx, svc, key)
        old_content = dict(old.content)
        old_content["layer"] = "superseded"
        old_content["superseded_at"] = _now_iso()
        ctx.store.put_record(type(old)(
            kind=old.kind, key=old.key, content=old_content,
            record_id=old.record_id, created_at=old.created_at,
            tags=tuple(set(old.tags) | {"superseded"}), space_id=old.space_id))
        new_key = f"mem:corr-{uuid4().hex[:16]}"
        new_content = dict(old.content)
        new_content.update({
            "summary": new_summary.strip(),
            "source": "owner-correct",
            "corrected_from": old.key,
            "last_confirmed": _now_iso(),
        })
        new_content.pop("superseded_at", None)
        new_content["layer"] = str(old.content.get("layer") or "")
        ctx.store.put_record(type(old)(
            kind=old.kind, key=new_key, content=new_content,
            tags=tuple(t for t in old.tags if t != "superseded"),
            space_id=old.space_id))
        _audit(ctx, "memory.correct", old_key_tail=key[-8:],
               new_key_tail=new_key[-8:], space_id=old.space_id)
    handler._send(200, _ok({"corrected": True, "old_key": key,
                            "new_key": new_key}))


def _expire(handler, ctx, svc: StudioServices) -> None:
    """POST /studio/memory/expire：标记过期——记录保留但即刻退出召回。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"key"})
    key = str(body.get("key") or "").strip()
    with ctx.lock:
        rec = _record_for_owner_op(ctx, svc, key)
        content = dict(rec.content)
        content["expires_at"] = _now_iso()
        ctx.store.put_record(type(rec)(
            kind=rec.kind, key=rec.key, content=content,
            record_id=rec.record_id, created_at=rec.created_at,
            tags=rec.tags, space_id=rec.space_id))
        _audit(ctx, "memory.expire", key_tail=key[-8:], space_id=rec.space_id)
    handler._send(200, _ok({"expired": True, "key": key}))


def _delete(handler, ctx, svc: StudioServices) -> None:
    """POST /studio/memory/delete：真删（含 embedding 缓存传播）。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"key"})
    key = str(body.get("key") or "").strip()
    with ctx.lock:
        rec = _record_for_owner_op(ctx, svc, key)
        registry = getattr(getattr(ctx, "manager", None), "registry", None)
        deleted = forget_gone_everywhere(ctx.store, key, registry=registry)
        _audit(ctx, "memory.delete", key_tail=key[-8:], space_id=rec.space_id,
               deleted=bool(deleted))
    handler._send(200, _ok({"deleted": bool(deleted), "key": key}))


def _pause(handler, ctx, svc: StudioServices) -> None:
    """POST /studio/memory/pause：空间级「停止/恢复继续记录」开关。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"space_id", "paused"})
    space_id = str(body.get("space_id") or "").strip()
    paused = body.get("paused") is True
    ref = svc.spaces.ref(space_id)
    if ref is None:
        raise StudioError(404, "not_found", "空间不存在或已注销")
    if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        raise StudioError(403, "vault_restricted", "原始库不支持暂停开关")
    with ctx.lock:
        ctx.store.put(f"{_PAUSED_PREFIX}{space_id}", {
            "paused": paused, "at": _now_iso(), "by": "studio-api"})
        _audit(ctx, "memory.pause" if paused else "memory.resume",
               space_id=space_id)
    handler._send(200, _ok({"space_id": space_id, "paused": paused}))
