"""Context-aware ranking of already authorized, current memory candidates."""
from __future__ import annotations

from dataclasses import replace

from whale_core.memory.activation import MemoryCandidate
from whale_core.memory.relations import query_attributes, relation_hints
from whale_core.memory.task_plan import evidence_needs, plan_query


def rerank(query: str, candidates) -> tuple[MemoryCandidate, ...]:
    """Keep every candidate; task and attribute signals only change its order."""
    requested = query_attributes(query)
    plan = plan_query(query)
    ranked = []
    for candidate in candidates:
        hints = relation_hints(candidate.summary)
        topics = {str(hint.get("entity")) for hint in hints}
        requested_topics = set(requested)
        attribute_match = any(
            requested.get(str(hint.get("entity")), set()) &
            set(str(hint.get("attribute") or "").split(","))
            for hint in hints)
        content_needs = set(evidence_needs(candidate.summary, plan)) - {
            "disclosure", "proactive"}
        if attribute_match:
            boost, reason = 0.25, "matching_attribute"
        elif requested_topics and topics and not (requested_topics & topics):
            boost, reason = -0.25, "different_topic"
        else:
            boost, reason = 0.0, "retrieval_order"
        boost += 0.2 * len(content_needs)
        ranked.append(replace(candidate, score=candidate.score + boost,
                              rerank_reason=reason))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return tuple(ranked)
