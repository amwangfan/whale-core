"""Owner-only Studio management for versioned persona evolution.

A proposal is inert until explicitly approved; the imported card remains
unchanged and the effective-persona renderer applies only the active overlay.
"""
from __future__ import annotations

from whale_core.api.studio.personas import _find_persona
from whale_core.api.studio.shared import (
    _ID_RE, StudioError, _audit, _ok, _reject_unknown_fields,
    _require_json_body,
)
from whale_core.memory.experience import ExperienceStore
from whale_core.memory.persona_evolution import PersonaEvolutionStore
from whale_core.ui.persona_cards import active_card


def _manager(ctx, svc, raw_persona_id: str):
    if raw_persona_id == "active":
        card = active_card(ctx.store, library=svc.personas)
    elif _ID_RE.fullmatch(raw_persona_id):
        card = _find_persona(ctx, svc, raw_persona_id)
    else:
        card = None
    if card is None:
        raise StudioError(404, "not_found", "人物卡不存在")
    return PersonaEvolutionStore(ctx.store), card.card_id


def _evolution_view(handler, ctx, svc, raw_persona_id: str) -> None:
    evolution, persona_id = _manager(ctx, svc, raw_persona_id)
    fields, version = evolution.active_overlay(persona_id)
    handler._send(200, _ok({
        "persona_id": persona_id, "version": version,
        "active_fields": fields,
        "proposals": list(evolution.list_proposals(persona_id)),
        "history": list(evolution.history(persona_id)),
    }))


def _evolution_propose(handler, ctx, svc, raw_persona_id: str) -> None:
    evolution, persona_id = _manager(ctx, svc, raw_persona_id)
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {
        "patch", "rationale", "evidence_ids",
        "experience_ids", "principal_id",
    })
    patch = body.get("patch")
    rationale = body.get("rationale")
    if not isinstance(patch, dict) or not isinstance(rationale, str):
        raise StudioError(422, "invalid_request", "patch 必须为对象，rationale 必须为文字")
    from_experiences = body.get("experience_ids") is not None
    raw_ids = body.get("experience_ids") if from_experiences else body.get("evidence_ids")
    if not isinstance(raw_ids, list) or not raw_ids or not all(
        isinstance(item, str) and item.strip() for item in raw_ids
    ):
        raise StudioError(422, "invalid_request",
                          "experience_ids 或 evidence_ids 必须为非空字符串数组")
    if from_experiences and body.get("evidence_ids") is not None:
        raise StudioError(422, "invalid_request", "experience_ids 和 evidence_ids 不能同时使用")
    try:
        with ctx.lock:
            if from_experiences:
                principal = str(body.get("principal_id") or "").strip()
                if not principal:
                    raise ValueError("principal_id required for experience-backed proposal")
                proposal = evolution.propose_from_experiences(
                    persona_id, patch,
                    experience_store=ExperienceStore(ctx.store),
                    principal_id=principal, experience_ids=raw_ids,
                    rationale=rationale,
                )
            else:
                proposal = evolution.propose(
                    persona_id, patch,
                    evidence_ids=raw_ids, rationale=rationale)
            _audit(ctx, "persona.evolution.propose",
                   persona_id=persona_id, proposal_id=proposal["proposal_id"])
    except (ValueError, TypeError, PermissionError, KeyError) as exc:
        raise StudioError(422, "invalid_request", str(exc)) from exc
    handler._send(201, _ok({"proposal": proposal}))


def _evolution_decide(handler, ctx, svc, raw_persona_id: str,
                      proposal_id: str, action: str) -> None:
    evolution, persona_id = _manager(ctx, svc, raw_persona_id)
    if not _ID_RE.fullmatch(proposal_id):
        raise StudioError(404, "not_found", "提案不存在")
    with ctx.lock:
        pending = evolution.get_proposal(proposal_id)
        if pending is None or pending.get("persona_id") != persona_id:
            raise StudioError(404, "not_found", "提案不存在")
        try:
            if action == "approve":
                result = evolution.approve(proposal_id)
            else:
                ok = evolution.reject(proposal_id)
                if not ok:
                    raise ValueError("proposal is no longer pending")
                result = evolution.get_proposal(proposal_id)
            _audit(ctx, "persona.evolution." + action,
                   persona_id=persona_id, proposal_id=proposal_id)
        except (ValueError, TypeError) as exc:
            raise StudioError(409, "conflict", str(exc)) from exc
    handler._send(200, _ok({"proposal": result}))


def _evolution_rollback(handler, ctx, svc, raw_persona_id: str) -> None:
    evolution, persona_id = _manager(ctx, svc, raw_persona_id)
    body = _require_json_body(handler)
    _reject_unknown_fields(body, {"to_version"})
    version = body.get("to_version")
    if version is not None and (isinstance(version, bool) or
                                not isinstance(version, int) or version < 0):
        raise StudioError(422, "invalid_request", "to_version 必须为非负整数")
    with ctx.lock:
        try:
            result = evolution.rollback(persona_id, to_version=version)
            _audit(ctx, "persona.evolution.rollback",
                   persona_id=persona_id, version=result["version"])
        except ValueError as exc:
            raise StudioError(409, "conflict", str(exc)) from exc
    handler._send(200, _ok({"active": result}))
