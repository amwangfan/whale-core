"""Candidate reranking preserves evidence while changing task order."""

from whale_core.memory.activation import MemoryCandidate
from whale_core.memory.rerank import rerank


def test_rerank_prefers_matching_attribute_without_dropping_candidates():
    candidates = (
        MemoryCandidate("unrelated", "门口的猫粮在鞋柜", 0.7, "curated"),
        MemoryCandidate("access", "旧门码已经停用，不能进门", 0.6, "owner_fact"),
    )
    result = rerank("旧门码还能进门吗", candidates)
    assert {item.key for item in result} == {"unrelated", "access"}
    assert result[0].key == "access"
    assert result[0].rerank_reason == "matching_attribute"


def test_rerank_does_not_change_authorization_or_fact_state():
    candidate = MemoryCandidate("fact", "我养的猫叫年糕", 0.4, "owner_fact")
    result = rerank("我的猫叫什么", (candidate,))
    assert result[0].key == candidate.key
    assert result[0].source == candidate.source
    assert candidate.score == 0.4
