"""S5 记忆域：memory-spaces / vault / forks / 会话 memories 绑定。"""

from __future__ import annotations

from typing import Optional

from whale_core.api.studio.shared import (
    _MEMVER_PREFIX,
    StudioError,
    _audit,
    _check_expected_version,
    _ok,
    _parse_conversation_key,
    _parse_redact_mode,
    _require_json_body,
    builtin_redactor,
)
from whale_core.api.studio.services import StudioServices
from whale_core.life.conversation_context import MemorySpaceType
from whale_core.memory.bindings import BindingError
from whale_core.memory.fork import ForkError
from whale_core.memory.spaces import FORKABLE_TARGET_TYPES, WRITABLE_TYPES
from whale_core.memory.store import MemoryKind, MemoryStore

#: Fork 选记录上限（服务端硬限制）。
_MAX_RECORD_KEYS = 500


def _record_count(store: MemoryStore, space_id: str) -> int:
    return sum(len(store.list_by_kind(kind, space_ids=(space_id,)))
               for kind in MemoryKind)


def _space_view(ctx, svc: StudioServices, space_id: str) -> Optional[dict]:
    meta = svc.spaces.get(space_id)
    if meta is None:
        return None
    stype = str(meta.get("type") or "")
    if stype == MemorySpaceType.PROTECTED_ORIGINAL.value:
        # INV-08：原始库只暴露「存在 + 名称」，不给记录数/摘要/来源。
        return {
            "space_id": space_id,
            "space_type": stype,
            "name": str(meta.get("name") or ""),
            "version": int(meta.get("version") or 1),
            "restricted": True,
            "bindable": False,
        }
    ref = svc.spaces.ref(space_id)
    return {
        "space_id": space_id,
        "space_type": stype,
        "name": str(meta.get("name") or ""),
        "version": int(meta.get("version") or 1),
        "owner": str(meta.get("owner") or ""),
        "created_at": str(meta.get("created_at") or ""),
        "record_count": _record_count(ctx.store, space_id),
        "bindable": True,
        "writable": bool(ref and ref.type in WRITABLE_TYPES),
    }


def _list_memory_spaces(handler, ctx, svc: StudioServices) -> None:
    spaces = []
    for ref in svc.spaces.list_spaces(include_protected=True):
        view = _space_view(ctx, svc, ref.space_id)
        if view is not None:
            spaces.append(view)
    handler._send(200, _ok({"memory_spaces": spaces}))


def _parse_record_keys(body: dict) -> tuple[str, ...]:
    raw = body.get("record_keys")
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_RECORD_KEYS:
        raise StudioError(422, "invalid_request",
                          f"record_keys 必须是 1..{_MAX_RECORD_KEYS} 个字符串")
    keys: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > 300:
            raise StudioError(422, "invalid_request",
                              "record_keys 元素必须是 1..300 字符的字符串")
        keys.append(item.strip())
    return tuple(dict.fromkeys(keys))


def _parse_target_type(body: dict) -> MemorySpaceType:
    raw = body.get("target_type")
    try:
        ttype = MemorySpaceType(str(raw))
    except ValueError:
        raise StudioError(422, "invalid_request",
                          "target_type 必须是 shareable_fork/group_space/"
                          "owner_working_copy")
    if ttype not in FORKABLE_TARGET_TYPES:
        # Fork 永不产生新的 protected_original（MEM-HARD-01 纵深防御）
        raise StudioError(422, "invalid_request",
                          f"不允许 Fork 出 {ttype.value} 类型空间")
    return ttype


def _vault_records(handler, ctx, svc: StudioServices) -> None:
    """GET /studio/vault/records?space_id=…：可信管理通道的原始库记录浏览。

    仅接受 protected_original 空间；摘要经内置脱敏；上限 500 条防大包。
    这是 Fork 向导的选记录数据源（PRD §5.2「在可信管理界面选记录」）。
    注意：list_by_kind 存储层硬排除 vault（MEM-HARD-01），管理读取只能
    走 keys()+get_record 直读——这是刻意的管道分离，不是绕过。
    """
    space_id = str(handler.query.get("space_id", [""])[0] or "").strip() \
        or "vault-default"
    meta = svc.spaces.get(space_id)
    if meta is None or meta.get("type") != "protected_original":
        raise StudioError(404, "not_found", "受保护原始库不存在")
    items: list[dict] = []
    truncated = False
    for key in sorted(ctx.store.keys()):
        rec = ctx.store.get_record(key)
        if rec is None or rec.space_id != space_id:
            continue
        redacted = builtin_redactor(dict(rec.content))
        items.append({
            "key": rec.key,
            "kind": rec.kind.value,
            "tags": list(rec.tags),
            "summary": str(redacted.get("summary", ""))[:300],
            "created_at": rec.created_at.isoformat(),
        })
        if len(items) >= 500:
            truncated = True
            break
    else:
        truncated = False
    _audit(ctx, "vault.records", space_id=space_id, count=len(items))
    handler._send(200, _ok({"space_id": space_id, "count": len(items),
                            "truncated": truncated, "items": items}))


def _fork_preview(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    source = str(body.get("source_space") or "").strip()
    if not source:
        raise StudioError(422, "invalid_request", "source_space 必填")
    if svc.spaces.get(source) is None:
        raise StudioError(404, "not_found", "源空间不存在或已注销")
    keys = _parse_record_keys(body)
    mode, redactor = _parse_redact_mode(body.get("redact"))
    try:
        items = svc.forks.preview(source, keys, redact=redactor)
    except ForkError as exc:
        raise StudioError(422, "invalid_request", str(exc)[:200])
    _audit(ctx, "fork.preview", source_space=source,
           record_count=len(items), redact=mode)
    handler._send(200, _ok({
        "source_space": source,
        "redact": mode,
        "count": len(items),
        "items": list(items),
    }))


def _fork_create(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    if body.get("confirmed") is not True:
        _audit(ctx, "fork.create", outcome="denied", reason="unconfirmed")
        raise StudioError(422, "invalid_request",
                          "Fork 需要显式确认（confirmed=true）")
    source = str(body.get("source_space") or "").strip()
    if not source:
        raise StudioError(422, "invalid_request", "source_space 必填")
    if svc.spaces.get(source) is None:
        raise StudioError(404, "not_found", "源空间不存在或已注销")
    keys = _parse_record_keys(body)
    ttype = _parse_target_type(body)
    target_name = str(body.get("target_name") or "")[:120]
    mode, redactor = _parse_redact_mode(body.get("redact"))
    with ctx.lock:
        try:
            new_space = svc.forks.fork(
                source, keys, target_type=ttype, target_name=target_name,
                created_by="studio-api", redact=redactor, confirmed=True)
        except ForkError as exc:
            _audit(ctx, "fork.create", outcome="denied",
                   reason="invalid_request", source_space=source)
            raise StudioError(422, "invalid_request", str(exc)[:200])
        _audit(ctx, "fork.create", source_space=source,
               target_space=new_space, target_type=ttype.value,
               record_count=len(keys), redact=mode)
    view = _space_view(ctx, svc, new_space) or {"space_id": new_space}
    view["auto_bound"] = False  # Fork 不自动连线：绑定是独立授权动作
    handler._send(200, _ok({"fork": view}))


# ---------------------------------------------------------------------------
# 记忆绑定
# ---------------------------------------------------------------------------


def _memory_binding_view(svc: StudioServices, row: dict) -> dict:
    """单条记忆绑定的脱敏视图（vault 永不带名称/计数等细节）。"""
    sid = str(row.get("space_id") or "")
    ref = svc.spaces.ref(sid)
    base = {
        "space_id": sid,
        "can_read": bool(row.get("can_read")),
        "can_write": bool(row.get("can_write")),
        "granted_by": str(row.get("granted_by") or ""),
        "created_at": str(row.get("created_at") or ""),
    }
    if ref is None:
        base["missing"] = True  # 空间已注销/不存在：展示残绑定以便清理
        return base
    if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        # 绑定层本已拒绝 vault；这里是污染数据的纵深防御：只暴露存在与类型。
        return {"space_id": sid, "space_type": "protected_original",
                "restricted": True}
    base.update({
        "space_type": ref.type.value,
        "name": str((svc.spaces.get(sid) or {}).get("name") or ""),
        "space_version": ref.version,
    })
    return base


def _put_memories(handler, ctx, svc: StudioServices, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    space_id = str(body.get("space_id") or "").strip()
    if not space_id:
        raise StudioError(422, "invalid_request", "space_id 必填")
    remove = body.get("remove") is True
    can_read = body.get("can_read") is not False  # 默认读
    can_write = body.get("can_write") is True     # 默认不写
    space_ref = svc.spaces.ref(space_id)
    if space_ref is None and not remove:
        raise StudioError(404, "not_found", "空间不存在或已注销")
    # remove=True 且空间已注销：放行——这是清理残绑定的管理路径。
    # MEM-HARD-01（owner 决策 2026-11-01 修订）：vault 仅可绑主人本人大号
    # 私聊/控制台会话（宿主配置的 owner_conversations）；其余一律 403。
    if space_ref is not None and space_ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        conv_key = ref.as_key()
        if conv_key not in svc.bindings.owner_conversations:
            _audit(ctx, "memory.unbind" if remove else "memory.bind",
                   outcome="denied", reason="vault_not_bindable",
                   conversation_key=conv_key, space_id=space_id)
            raise StudioError(403, "vault_not_bindable",
                              "个人原始库只能连接主人本人的私聊会话；"
                              "其余会话请先创建受控副本（Fork）")
    key = ref.as_key()
    ver_key = f"{_MEMVER_PREFIX}{key}"
    with ctx.lock:
        rows = ctx.store.get(f"bind:mem:{key}")
        rows = [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []
        current_row = next((r for r in rows if r.get("space_id") == space_id), None)
        current_version = int(ctx.store.get(ver_key) or 0)
        # 幂等重放优先于版本检查：同值重复 / 重复解绑都是 200 no-op（N13）。
        if remove and current_row is None:
            handler._send(200, _ok({"key": key, "space_id": space_id,
                                    "version": current_version,
                                    "changed": False, "removed": True}))
            return
        if (not remove and current_row is not None
                and bool(current_row.get("can_read")) == can_read
                and bool(current_row.get("can_write")) == can_write):
            handler._send(200, _ok({"key": key, "space_id": space_id,
                                    "version": current_version,
                                    "changed": False}))
            return
        try:
            _check_expected_version(body, current_version)
        except StudioError as exc:
            _audit(ctx, "memory.bind", outcome="denied", reason=exc.code,
                   conversation_key=key, space_id=space_id)
            raise
        try:
            if remove:
                svc.bindings.unbind_memory(key, space_id)
                row = None
            else:
                row = svc.bindings.bind_memory(
                    key, space_id, can_read=can_read, can_write=can_write,
                    granted_by="studio-api")
        except BindingError as exc:
            _audit(ctx, "memory.bind", outcome="denied",
                   reason="invalid_request", conversation_key=key,
                   space_id=space_id)
            raise StudioError(422, "invalid_request", str(exc)[:200])
        version = current_version + 1
        ctx.store.put(ver_key, version)
        _audit(ctx, "memory.unbind" if remove else "memory.bind",
               conversation_key=key, space_id=space_id,
               can_read=bool(can_read) if not remove else None,
               can_write=bool(can_write) if not remove else None,
               version=version)
    handler._send(200, _ok({
        "key": key, "space_id": space_id, "version": version, "changed": True,
        "removed": remove,
        "binding": _memory_binding_view(svc, row) if row else None,
    }))
