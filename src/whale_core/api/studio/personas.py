"""S5 人物卡域：list/detail/create/update + 会话 persona 绑定 + 导入/试玩。"""

from __future__ import annotations

import json
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
    "example_dialogue",
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
        "example_dialogue": card.example_dialogue,
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


# ---------------------------------------------------------------------------
# UX-02（issue #37）：导入预览（确认前不写入）与即时试玩
# ---------------------------------------------------------------------------

#: 试玩固定场景输入（相同输入对比默认人格与当前角色）。
_PLAYTEST_DEFAULT_INPUT = "我今天有点累，项目快到截止时间了。"

_MAX_IMPORT_CHARS = 40000


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9\-]+", "-", (name or "").lower()).strip("-")
    return slug[:40] or ""


def _import_mapping(card_data: dict) -> tuple[dict, dict, list]:
    """SillyTavern v1/v2 字段 → PersonaCard 字段。返回 (parsed, mapping,
    unmapped)。未识别字段进 unmapped（不静默丢字段）。"""
    parsed: dict = {}
    mapping: dict[str, str] = {}
    unmapped: list[dict] = []
    data = card_data
    if isinstance(card_data.get("data"), dict) and \
            str(card_data.get("spec") or "").startswith("chara_card"):
        data = card_data["data"]  # v2 包装
    known = {
        "name": "name",
        "personality": "temperament",
        "mes_example": "example_dialogue",
    }
    for src, dst in known.items():
        value = str(data.get(src) or "").strip()
        if value:
            parsed[dst] = value[:2000]
            mapping[src] = dst
    description = str(data.get("description") or "").strip()
    scenario = str(data.get("scenario") or "").strip()
    if description:
        parsed["summary"] = description[:300]
        mapping["description"] = "summary(≤300) + external_prompt(原文)"
    if scenario:
        mapping["scenario"] = "external_prompt(并入原文)"
    prompt_parts = [p for p in (description, f"场景：{scenario}" if scenario else "")
                    if p]
    if prompt_parts:
        parsed["external_prompt"] = "\n".join(prompt_parts)[:MAX_EXTERNAL_PROMPT_CHARS]
    for src in ("first_mes", "greetings"):
        value = data.get(src)
        text = value[0] if isinstance(value, list) and value else value
        if isinstance(text, str) and text.strip():
            unmapped.append({
                "field": src,
                "note": "首条问候暂不参与运行（仅展示），未写入卡",
                "preview": text.strip()[:120],
            })
    handled = set(known) | {"description", "scenario", "spec", "data",
                            "first_mes", "greetings"}
    for key in sorted(data):
        if key not in handled:
            value = data.get(key)
            preview = str(value)[:80] if not isinstance(value, (dict, list)) \
                else f"({type(value).__name__})"
            unmapped.append({"field": key, "note": "未识别字段，未写入",
                             "preview": preview})
    return parsed, mapping, unmapped


def _import_preview(handler, ctx, svc: StudioServices) -> None:
    """POST /studio/personas/import-preview：解析预览；不写任何数据。"""
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"text", "card_id"})
    raw = str(body.get("text") or "")
    if not raw.strip():
        raise StudioError(422, "invalid_request", "text 必填（JSON 或纯文本卡）")
    if len(raw) > _MAX_IMPORT_CHARS:
        raise StudioError(422, "invalid_request",
                          f"卡文本超过 {_MAX_IMPORT_CHARS} 字符上限")
    warnings: list[str] = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        parsed, mapping, unmapped = _import_mapping(data)
    else:
        # 纯文本：启发式提炼（与 to_simple_card 同族），原文进 external_prompt
        from whale_core.ui.persona_resolver import PersonaResolver
        preview = PersonaResolver.to_simple_card(raw)["suggested"]
        parsed = {k: v for k, v in preview.items() if v}
        parsed["external_prompt"] = raw[:MAX_EXTERNAL_PROMPT_CHARS]
        mapping = {"首行": "summary", "性格/气质行": "temperament",
                   "相处/关系行": "relationship_style", "全文": "external_prompt"}
        unmapped = []
        warnings.append("纯文本导入为启发式提炼，请逐项确认后再写入")
    if not parsed.get("name"):
        parsed.pop("name", None)
    suggested_id = _slugify(str(body.get("card_id") or parsed.get("name") or ""))
    conflicts: list[str] = []
    if suggested_id and (svc.personas.get(suggested_id) is not None
                         or ctx.store.get(f"{CARD_KEY_PREFIX}{suggested_id}")):
        conflicts.append(f"card_id 已存在：{suggested_id}（保存时将生成新 id）")
    missing = [f for f in ("summary", "temperament")
               if not (parsed.get(f) or "").strip()]
    handler._send(200, _ok({
        "parsed": parsed,
        "suggested_card_id": suggested_id,
        "mapping": mapping,
        "unmapped": unmapped,
        "missing": missing,
        "conflicts": conflicts,
        "warnings": warnings,
        "requires_confirmation": True,  # 预览永不直接写入
    }))


def _playtest(handler, ctx, svc: StudioServices) -> None:
    """POST /studio/personas/playtest：真实有效快照 + 真实模型的对比试玩。

    相同固定输入，两侧都不注入记忆/历史（隔离人格变量）；模型失败、
    降级与未生效字段如实显示。试玩不写记忆、不进会话历史。
    """
    from whale_core.ui.effective_persona import build_effective_persona

    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"persona_id", "card", "input"})
    orchestrator = getattr(ctx, "orchestrator", None)
    model = getattr(getattr(orchestrator, "companion", None), "model", None) \
        if orchestrator is not None else None
    if model is None:
        raise StudioError(503, "unavailable",
                          "模型未装配（检查设置页模型配置后再试玩）")
    text = str(body.get("input") or "").strip()[:500] or _PLAYTEST_DEFAULT_INPUT

    def _side(persona_id: str, inline: dict | None) -> dict:
        try:
            if inline is not None:
                fields = dict(inline)
                fields.setdefault("card_id", "playtest")
                fields.setdefault("title", "试玩预览")
                card = PersonaCard.from_dict(fields)
                snap = build_effective_persona(ctx.store, None, card=card)
                source = "unsaved_draft"
            else:
                snap = build_effective_persona(ctx.store, persona_id or None)
                source = snap.source
            reply = model.reply(text, persona=snap.model_context,
                                memories=(), history=())
            return {
                "persona_id": snap.persona_id,
                "source": source,
                "content_sha": snap.content_sha,
                "reply": reply,
                "fields_used": sorted({
                    d.field for d in snap.dispositions
                    if d.destination == "model_context" and d.used_value}),
            }
        except Exception as exc:
            from whale_core.core.deepseek import ModelError
            kind = exc.kind if isinstance(exc, ModelError) \
                else type(exc).__name__
            return {"persona_id": persona_id or "playtest",
                    "source": inline is not None and "unsaved_draft" or "",
                    "error": f"model_{kind}"}

    persona_id = str(body.get("persona_id") or "").strip()
    inline = body.get("card") if isinstance(body.get("card"), dict) else None
    if not persona_id and inline is None:
        raise StudioError(422, "invalid_request",
                          "persona_id 或 card（草稿字段）二选一")
    result = {
        "input": text,
        "baseline": _side("", None),  # 全局当前生效人格
        "trial": _side(persona_id, inline),
        "note": "两侧均无记忆/历史注入；试玩不写记忆、不进会话历史",
    }
    _audit(ctx, "persona.playtest",
           persona_id=persona_id or "(draft)",
           baseline_ok="reply" in result["baseline"],
           trial_ok="reply" in result["trial"])
    handler._send(200, _ok(result))


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
