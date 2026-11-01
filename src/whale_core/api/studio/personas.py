"""S5 人物卡域：list/detail/create/update + 会话 persona 绑定。"""

from __future__ import annotations

import re
from typing import Optional
from uuid import uuid4

from whale_core.api.studio.shared import (
    _ID_RE,
    StudioError,
    _audit,
    _check_expected_version,
    _ok,
    _parse_conversation_key,
    _reject_unknown_fields,
    _require_json_body,
)
from whale_core.api.studio.services import StudioServices
from whale_core.ui.persona_cards import (
    ACTIVE_CARD_KEY,
    CARD_KEY_PREFIX,
    MAX_EXTERNAL_PROMPT_CHARS,
    PersonaCard,
    switch_card,
)


def _persona_view(card: PersonaCard, *, builtin: bool) -> dict:
    """极简字段视图；**绝不回显** external_prompt / style_rules 等内部文本。"""
    return {
        "id": card.card_id,
        "title": card.title,
        "name": card.name,
        "summary": card.summary or card.description,
        "temperament": card.temperament,
        "relationship_style": card.relationship_style,
        "voice_hint": card.voice_hint or card.timbre_hint,
        "category": card.category,
        "author": card.author,
        "license": card.license,
        "source_url": card.source_url,
        "builtin": builtin,
        "has_external_prompt": bool(card.external_prompt),
    }


def _find_persona(ctx, svc: StudioServices, persona_id: str) -> Optional[PersonaCard]:
    card = svc.personas.get(persona_id)
    if card is not None:
        return card
    raw = ctx.store.get(f"{CARD_KEY_PREFIX}{persona_id}")
    if not isinstance(raw, dict):
        return None
    try:
        return PersonaCard.from_dict(raw)
    except (ValueError, TypeError):
        return None


def _list_personas(handler, ctx, svc: StudioServices) -> None:
    out = [_persona_view(svc.personas.require(cid), builtin=True)
           for cid in svc.personas.ids()]
    builtin_ids = set(svc.personas.ids())
    for key in sorted(ctx.store.keys()):
        if not key.startswith(CARD_KEY_PREFIX):
            continue
        cid = key[len(CARD_KEY_PREFIX):]
        if cid in builtin_ids:
            continue
        card = _find_persona(ctx, svc, cid)
        if card is not None:
            out.append(_persona_view(card, builtin=False))
    handler._send(200, _ok({"personas": out}))


# ---- 人物卡管理（owner 决策 2026-11-01：工作台可直接添加/修改人物卡）----

#: 自定义卡 id 白名单（小写 slug；内置卡 id 不可占用）。
_CARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")

#: 可编辑字符串字段（严格字段表，未知字段 422；与 PersonaCard 字段同名）。
_PERSONA_STR_FIELDS = frozenset({
    "title", "name", "summary", "description", "temperament",
    "relationship_style", "voice_hint", "category", "tone",
    "address_user_as", "timbre_hint", "artwork_ref",
    "author", "source_url", "license", "external_prompt",
})
_PERSONA_LIST_FIELDS = frozenset({"style_rules", "extra_taboos"})
_PERSONA_EDITABLE = _PERSONA_STR_FIELDS | _PERSONA_LIST_FIELDS | {"speaking_rate"}

#: 单字段长度硬上限（external_prompt 另由 PersonaCard 截断到
#: MAX_EXTERNAL_PROMPT_CHARS）；列表条数/单条长度上限。
_MAX_PERSONA_FIELD_CHARS = MAX_EXTERNAL_PROMPT_CHARS
_MAX_PERSONA_LIST_ITEMS = 50
_MAX_PERSONA_LIST_ITEM_CHARS = 300


def _persona_detail_view(card: PersonaCard, *, builtin: bool) -> dict:
    """编辑用完整视图。管理面仅 owner Bearer 可达，故含 external_prompt
    （列表视图 _persona_view 仍不回显内部文本）。"""
    view = _persona_view(card, builtin=builtin)
    view.update({
        "description": card.description,
        "tone": card.tone,
        "address_user_as": card.address_user_as,
        "style_rules": list(card.style_rules),
        "extra_taboos": list(card.extra_taboos),
        "timbre_hint": card.timbre_hint,
        "speaking_rate": card.speaking_rate,
        "artwork_ref": card.artwork_ref,
        "external_prompt": card.external_prompt,
        "channels": list(card.channels),
    })
    return view


def _get_persona_detail(handler, ctx, svc: StudioServices, card_id: str) -> None:
    if not _ID_RE.match(card_id):
        raise StudioError(404, "not_found", "unknown persona")
    card = _find_persona(ctx, svc, card_id)
    if card is None:
        raise StudioError(404, "not_found", f"人物卡不存在: {card_id!r}")
    handler._send(200, _ok({
        "persona": _persona_detail_view(
            card, builtin=svc.personas.get(card_id) is not None),
    }))


def _get_persona_effective(handler, ctx, svc: StudioServices, card_id: str) -> None:
    """issue #36：有效人格快照预览——保存的设定实际进入模型请求的确切文本。

    ``card_id == "active"`` 表示全局当前生效卡（等价于未绑定会话的解析结果）。
    返回 model_context 全文 + 每字段去向（model_context / tts_only /
    metadata_only）+ content_sha（供日志/Trace 引用）。管理面仅 owner Bearer
    可达；external_prompt 等内部文本本就在详情端点回显，此处不再加码。
    """
    from whale_core.ui.effective_persona import build_effective_persona

    if card_id == "active":
        snap = build_effective_persona(ctx.store, None)
    else:
        if not _ID_RE.match(card_id):
            raise StudioError(404, "not_found", "unknown persona")
        card = _find_persona(ctx, svc, card_id)
        if card is None:
            raise StudioError(404, "not_found", f"人物卡不存在: {card_id!r}")
        snap = build_effective_persona(ctx.store, card_id, card=card)
    handler._send(200, _ok({"effective": snap.as_dict()}))


def _parse_persona_fields(body: dict) -> dict:
    """严格字段表 + 类型/长度校验；返回 PersonaCard 可接受的字段子集。"""
    _reject_unknown_fields(body, _PERSONA_EDITABLE | {"card_id"})
    fields: dict = {}
    for key in sorted(_PERSONA_STR_FIELDS):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str):
            raise StudioError(422, "invalid_request", f"{key} 必须是字符串")
        fields[key] = value[:_MAX_PERSONA_FIELD_CHARS]
    for key in sorted(_PERSONA_LIST_FIELDS):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, list) \
                or not all(isinstance(v, str) for v in value):
            raise StudioError(422, "invalid_request", f"{key} 必须是字符串数组")
        cleaned = tuple(
            v.strip()[:_MAX_PERSONA_LIST_ITEM_CHARS] for v in value if v.strip())
        if len(cleaned) > _MAX_PERSONA_LIST_ITEMS:
            raise StudioError(422, "invalid_request",
                              f"{key} 最多 {_MAX_PERSONA_LIST_ITEMS} 条")
        fields[key] = cleaned
    if "speaking_rate" in body:
        rate = body["speaking_rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise StudioError(422, "invalid_request", "speaking_rate 必须是数字")
        if not 0.5 <= float(rate) <= 2.0:
            raise StudioError(422, "invalid_request",
                              "speaking_rate 须在 [0.5, 2.0] 内")
        fields["speaking_rate"] = float(rate)
    return fields


def _create_persona(handler, ctx, svc: StudioServices) -> None:
    body = _require_json_body(handler)
    fields = _parse_persona_fields(body)
    title = (fields.get("title") or "").strip()
    if not title:
        raise StudioError(422, "invalid_request", "title 不能为空")
    fields["title"] = title
    cid = str(body.get("card_id") or "").strip().lower()
    if cid and not _CARD_ID_RE.match(cid):
        raise StudioError(422, "invalid_request",
                          "card_id 只能含小写字母/数字/连字符（≤64 字符）")
    if not cid:
        cid = f"custom-{uuid4().hex[:8]}"
    if svc.personas.get(cid) is not None:
        raise StudioError(409, "builtin_immutable",
                          f"card_id 与内置卡冲突: {cid!r}")
    with ctx.lock:
        if ctx.store.get(f"{CARD_KEY_PREFIX}{cid}"):
            raise StudioError(409, "duplicate_card", f"card_id 已存在: {cid!r}")
        card = PersonaCard(card_id=cid, **fields)
        ctx.store.put(f"{CARD_KEY_PREFIX}{cid}", card.as_dict())
        _audit(ctx, "persona.create", card_id=cid, title=title)
    handler._send(201, _ok({"persona": _persona_detail_view(card, builtin=False)}))


def _update_persona(handler, ctx, svc: StudioServices, card_id: str) -> None:
    if not _ID_RE.match(card_id):
        raise StudioError(404, "not_found", "unknown persona")
    body = _require_json_body(handler)
    fields = _parse_persona_fields(body)
    if not fields:
        raise StudioError(422, "invalid_request", "没有需要修改的字段")
    if "title" in fields and not str(fields["title"]).strip():
        raise StudioError(422, "invalid_request", "title 不能为空")
    if svc.personas.get(card_id) is not None:
        raise StudioError(403, "builtin_immutable",
                          "内置卡不可修改；请以其为底稿新建一张自定义卡")
    with ctx.lock:
        raw = ctx.store.get(f"{CARD_KEY_PREFIX}{card_id}")
        if not isinstance(raw, dict):
            raise StudioError(404, "not_found", f"人物卡不存在: {card_id!r}")
        data = dict(raw)
        data.update(fields)
        try:
            updated = PersonaCard.from_dict(data)
        except (ValueError, TypeError) as exc:
            raise StudioError(422, "invalid_request", f"字段值非法: {exc}")
        ctx.store.put(f"{CARD_KEY_PREFIX}{card_id}", updated.as_dict())
        _audit(ctx, "persona.update", card_id=card_id, fields=sorted(fields))
        # 若改的是当前活跃卡，立即重渲 PersonaContinuity 快照，
        # 让修改对下游（companion/voice 渲染）当轮生效。
        active = str((ctx.store.get(ACTIVE_CARD_KEY) or {}).get("card_id") or "")
        if active == card_id:
            switch_card(ctx.store, card_id, library=svc.personas)
    handler._send(200, _ok({
        "persona": _persona_detail_view(updated, builtin=False)}))


def _put_persona(handler, ctx, svc: StudioServices, raw_key: str) -> None:
    ref = _parse_conversation_key(raw_key)
    body = _require_json_body(handler)
    persona_raw = body.get("persona_id")
    if persona_raw is not None and not isinstance(persona_raw, str):
        raise StudioError(422, "invalid_request", "persona_id 必须是字符串或 null")
    persona_id = (persona_raw or "").strip()
    if persona_id and _find_persona(ctx, svc, persona_id) is None:
        raise StudioError(422, "invalid_request",
                          f"persona_id 不存在: {persona_id[:80]!r}")
    key = ref.as_key()
    store_key = f"bind:persona:{key}"
    with ctx.lock:
        old = ctx.store.get(store_key)
        old = old if isinstance(old, dict) else None
        current_version = int(old.get("version") or 0) if old else 0
        current_pid = str(old.get("persona_id") or "") if old else ""
        if persona_id == current_pid:
            # 幂等重放优先：同值重试（带着自己首次的版本号）是 200 no-op，
            # 不是 409——重试不算并发冲突（N13）。
            handler._send(200, _ok({
                "key": key, "persona_id": current_pid or None,
                "version": current_version, "changed": False,
            }))
            return
        try:
            _check_expected_version(body, current_version)
        except StudioError as exc:
            _audit(ctx, "persona.bind", outcome="denied", reason=exc.code,
                   conversation_key=key)
            raise
        if persona_id:
            data = svc.bindings.bind_persona(key, persona_id,
                                             updated_by="studio-api")
            version = int(data.get("version") or current_version + 1)
        else:
            ctx.store.delete(store_key)
            version = 0
        _audit(ctx, "persona.unbind" if not persona_id else "persona.bind",
               conversation_key=key, from_persona=current_pid,
               to_persona=persona_id, version=version)
    handler._send(200, _ok({
        "key": key, "persona_id": persona_id or None,
        "version": version, "changed": True,
    }))
