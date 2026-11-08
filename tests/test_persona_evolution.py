from datetime import datetime, timezone

import pytest

from whale_core.memory.experience import Experience, ExperienceStore
from whale_core.memory.persona_evolution import PersonaEvolutionStore
from whale_core.memory.store import InMemoryStore
from whale_core.ui.effective_persona import build_effective_persona
from whale_core.ui.persona_cards import PersonaCard


def _store_with_card():
    store = InMemoryStore()
    card = PersonaCard(card_id="companion", title="Companion",
                       name="鲸", tone="冷静")
    store.put("persona:card:companion", card.as_dict())
    store.put("persona:active_card", {"card_id": "companion"})
    return store


def test_evolution_is_proposal_until_approved_and_can_rollback():
    store = _store_with_card()
    evolution = PersonaEvolutionStore(store)
    before = build_effective_persona(store)
    proposal = evolution.propose(
        "companion", {"tone": "温柔但克制", "style_rules": ["少用客服腔"]},
        evidence_ids=["memory:confirmed:1"], rationale="用户明确反馈希望更自然",
    )
    assert proposal["state"] == "pending"
    assert build_effective_persona(store).model_context == before.model_context

    approved = evolution.approve(proposal["proposal_id"])
    after = build_effective_persona(store)
    assert approved["state"] == "approved"
    assert after.evolution_version == 1
    assert "气质：温柔但克制" in after.model_context
    assert "少用客服腔" in after.model_context
    assert after.content_sha != before.content_sha

    rolled = evolution.rollback("companion")
    restored = build_effective_persona(store)
    assert rolled["version"] == 2
    assert restored.evolution_version == 2
    assert restored.model_context == before.model_context
    assert evolution.history("companion")[-1]["rollback_of"] == 0


def test_evolution_rejects_permission_and_identity_fields():
    store = _store_with_card()
    evolution = PersonaEvolutionStore(store)
    with pytest.raises(ValueError):
        evolution.propose("companion", {"tool_scopes": ["device.shell"]},
                          evidence_ids=["exp:1"], rationale="not allowed")
    with pytest.raises(ValueError):
        evolution.propose("companion", {"name": "different identity"},
                          evidence_ids=["exp:1"], rationale="not allowed")


def test_only_trusted_nonexpired_experiences_can_drive_proposals():
    store = _store_with_card()
    experiences = ExperienceStore(store)
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    experiences.record(Experience(
        experience_id="confirmed", principal_id="owner",
        source="user_feedback", evidence="chat:1", action="reply.concise",
        outcome="success", confidence=1, ttl_s=0, confirmed_by_user=True,
        recorded_at=now.isoformat()), now=now)
    experiences.record(Experience(
        experience_id="candidate", principal_id="owner",
        source="candidate:ocr", evidence="ocr:1", action="reply.concise",
        outcome="success", confidence=0.2, ttl_s=3600, confirmed_by_user=False,
        recorded_at=now.isoformat()), now=now)
    evolution = PersonaEvolutionStore(store)
    proposal = evolution.propose_from_experiences(
        "companion", {"tone": "自然"}, experience_store=experiences,
        principal_id="owner", experience_ids=["confirmed"],
        rationale="基于本人明确反馈", now=now)
    assert proposal["evidence_ids"] == ["exp:owner:confirmed"]
    with pytest.raises(PermissionError):
        evolution.propose_from_experiences(
            "companion", {"tone": "自然"}, experience_store=experiences,
            principal_id="owner", experience_ids=["candidate"],
            rationale="不应从候选演化", now=now)


def test_stale_proposal_cannot_overwrite_newer_persona_version():
    store = _store_with_card()
    evolution = PersonaEvolutionStore(store)
    first = evolution.propose("companion", {"tone": "温柔"},
                              evidence_ids=["exp:1"], rationale="first")
    second = evolution.propose("companion", {"tone": "活泼"},
                               evidence_ids=["exp:2"], rationale="second")
    evolution.approve(first["proposal_id"])
    with pytest.raises(ValueError, match="stale"):
        evolution.approve(second["proposal_id"])
