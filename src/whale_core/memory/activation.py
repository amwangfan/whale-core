"""Choose how to use already authorized, current retrieval candidates.

This module never reads storage, checks permissions, or changes fact state.
Callers must apply their existing ACL, validity and deletion checks first.
"""
from __future__ import annotations

from dataclasses import dataclass

from whale_core.memory.relations import query_attributes, relation_hints
from whale_core.memory.roles import (
    access_relevance, boundary_relevance, constraint_relevance, lexical_overlap,
    memory_role,
)
from whale_core.memory.task_plan import evidence_needs, plan_query


@dataclass(frozen=True)
class MemoryCandidate:
    key: str
    summary: str
    score: float
    source: str
    subject_id: str = "owner"
    rerank_reason: str = ""


@dataclass(frozen=True)
class ActivationDecision:
    candidate: MemoryCandidate
    decision: str  # USE or CONSTRAIN
    reason: str
    score: float


def activate(query: str, candidates, *, limit: int = 8,
             is_group: bool = False) -> tuple[ActivationDecision, ...]:
    """Select applicable rules and cover the query's distinct evidence needs."""
    if limit <= 0:
        return ()
    plan = plan_query(query, group=is_group)
    requested = query_attributes(query)
    ranked: list[tuple[float, MemoryCandidate, str, str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.summary.strip() or candidate.key in seen:
            continue
        seen.add(candidate.key)
        role = memory_role(candidate.summary)
        if role == "one_off":
            retention_question = any(word in query for word in
                                     ("凭证", "存档", "该保留什么", "应该保留什么"))
            if retention_question and lexical_overlap(candidate.summary, query):
                ranked.append((candidate.score + 1, candidate, "CONSTRAIN",
                               "retention_constraint", ()))
            continue
        if role == "boundary":
            relevance = boundary_relevance(
                candidate.summary, query, subject_id=candidate.subject_id,
                is_group=is_group)
            if relevance:
                ranked.append((candidate.score + relevance, candidate,
                               "CONSTRAIN", "disclosure_boundary", ()))
            continue
        if role == "access_permission":
            relevance = access_relevance(candidate.summary, query)
            if relevance:
                ranked.append((candidate.score + relevance, candidate,
                               "CONSTRAIN", "access_permission", ("access",)))
            continue
        if role == "constraint":
            relevance = constraint_relevance(candidate.summary, query)
            if relevance:
                ranked.append((candidate.score + relevance, candidate,
                               "CONSTRAIN", "behavior_constraint", ()))
            continue

        coverage = evidence_needs(candidate.summary, plan)
        content_needs = set(plan.needs) - {"disclosure", "proactive"}
        if content_needs and not (content_needs & set(coverage)):
            continue
        hints = relation_hints(candidate.summary)
        attribute_match = any(
            requested.get(str(hint.get("entity")), set()) &
            set(str(hint.get("attribute") or "").split(","))
            for hint in hints)
        minimum_overlap = (1 if any(word in query for word in
                                    ("记得", "之前说", "曾经说")) else 2)
        if (not coverage and not attribute_match
                and lexical_overlap(candidate.summary, query) < minimum_overlap):
            continue
        score = candidate.score + 0.35 * len(coverage) + 0.25 * attribute_match
        ranked.append((score, candidate, "USE",
                       "task_evidence" if coverage else "retrieval_evidence", coverage))

    # Applicable constraints are independent of ordinary Top-K competition.
    rules = sorted((row for row in ranked if row[2] == "CONSTRAIN"),
                   key=lambda row: row[0], reverse=True)
    facts = sorted((row for row in ranked if row[2] == "USE"),
                   key=lambda row: row[0], reverse=True)
    selected = rules[:min(3, limit)]
    covered: set[str] = set()
    remaining = limit - len(selected)
    while facts and remaining:
        best = max(facts, key=lambda row: (len(set(row[4]) - covered), row[0]))
        facts.remove(best)
        selected.append(best)
        covered.update(best[4])
        remaining -= 1
    return tuple(ActivationDecision(row[1], row[2], row[3], row[0])
                 for row in selected)
