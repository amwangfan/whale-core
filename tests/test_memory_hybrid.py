"""PR-14 混合记忆检索（词项 + 嵌入融合）测试。

HashEmbeddingProvider 只是确定性词项散列占位；语义召回用例用可控的
FakeEmbedder 注入向量，验证「词项零交集但向量相近」的记录能被召回。
"""
from __future__ import annotations

import math

import pytest

from whale_core.memory.curation import MemoryCurator
from whale_core.memory.hybrid import (
    HashEmbeddingProvider,
    HybridCurator,
    cosine,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore

QUERY = "我想吃辣"
SEMANTIC_ONLY = "火锅很带劲"   # 与 QUERY 词项零交集
LEXICAL_HIT = "我怕辣"         # 与 QUERY 有词项交集（辣）
ZERO_LOW = "篮球很好看"        # 与 QUERY 零交集，fake 向量正交


class FakeEmbedder:
    """可控向量注入：mapping 外的文本给零向量。"""

    def __init__(self, mapping: dict[str, list[float]], dim: int = 8):
        self.mapping = mapping
        self.dim = dim

    def embed(self, texts: list[str], *, namespace: str = "") -> list[list[float]]:
        return [list(self.mapping.get(t, [0.0] * self.dim)) for t in texts]


class BoomEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding service down")


def _seed(store) -> dict[str, str]:
    """写入三类记录，返回 {label: key}。"""
    curator = MemoryCurator(store)
    keys = {}
    keys["semantic"] = curator.remember(
        SEMANTIC_ONLY, source="chat", evidence_id="e1",
        importance=0.6, consent=True).key
    keys["lexical"] = curator.remember(
        LEXICAL_HIT, source="chat", evidence_id="e2",
        importance=0.6, consent=True).key
    keys["zero_low"] = curator.remember(
        ZERO_LOW, source="chat", evidence_id="e3",
        importance=0.6, consent=True).key
    return keys


def _fake() -> FakeEmbedder:
    # QUERY 与 SEMANTIC_ONLY 同向；ZERO_LOW 正交；LEXICAL_HIT 零向量。
    return FakeEmbedder({
        QUERY: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        SEMANTIC_ONLY: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        ZERO_LOW: [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    })


# ---------- cosine ----------

def test_cosine_identical_is_one():
    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_safe():
    assert cosine([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine([1.0, 2.0], [0.0, 0.0]) == 0.0
    assert cosine([], []) == 0.0


# ---------- HashEmbeddingProvider ----------

def test_hash_embedding_deterministic():
    provider = HashEmbeddingProvider()
    a = provider.embed(["我怕辣，尤其是火锅"])
    b = provider.embed(["我怕辣，尤其是火锅"])
    assert a == b


def test_hash_embedding_dim_and_normalized():
    provider = HashEmbeddingProvider(dim=64)
    (vec,) = provider.embed(["我喜欢安静的音乐"])
    assert len(vec) == 64
    assert math.sqrt(sum(v * v for v in vec)) == pytest.approx(1.0)


def test_hash_embedding_empty_text_is_zero_vector():
    provider = HashEmbeddingProvider(dim=32)
    (vec,) = provider.embed([""])
    assert vec == [0.0] * 32


def test_hash_embedding_rejects_bad_dim():
    with pytest.raises(ValueError):
        HashEmbeddingProvider(dim=0)


# ---------- 退化与一致性 ----------

def test_no_embedder_matches_memory_curator():
    store = InMemoryStore()
    _seed(store)
    curator_hits = MemoryCurator(store).recall(QUERY, limit=10)
    hybrid_hits = HybridCurator(store).recall(QUERY, limit=10)
    assert {h.record.key for h in hybrid_hits} == {h.record.key for h in curator_hits}
    # 纯词项模式下 embedding 分恒 0
    assert all(h.embedding_score == 0.0 for h in hybrid_hits)


def test_embedder_exception_degrades_to_lexical():
    store = InMemoryStore()
    keys = _seed(store)
    hits = HybridCurator(store, embedder=BoomEmbedder()).recall(QUERY, limit=10)
    hit_keys = {h.record.key for h in hits}
    assert keys["lexical"] in hit_keys            # 词项命中仍在
    assert keys["semantic"] not in hit_keys       # 向量通道失效 → 零交集不入选
    assert all(h.embedding_score == 0.0 for h in hits)
    assert any("退化" in r for h in hits for r in h.reasons)


# ---------- 融合召回 ----------

def test_fake_embedder_recalls_semantic_match_missed_by_lexical():
    store = InMemoryStore()
    keys = _seed(store)
    lexical_keys = {h.record.key for h in MemoryCurator(store).recall(QUERY, limit=10)}
    assert keys["semantic"] not in lexical_keys   # 纯词项法确实漏掉它
    hits = HybridCurator(store, embedder=_fake()).recall(QUERY, limit=10)
    by_key = {h.record.key: h for h in hits}
    assert keys["semantic"] in by_key
    hit = by_key[keys["semantic"]]
    assert hit.lexical_score == 0.0
    assert hit.embedding_score == pytest.approx(1.0)
    assert any("嵌入" in r for r in hit.reasons)


def test_zero_overlap_low_embedding_excluded():
    store = InMemoryStore()
    keys = _seed(store)
    hits = HybridCurator(store, embedder=_fake()).recall(QUERY, limit=10)
    assert keys["zero_low"] not in {h.record.key for h in hits}


def test_lexical_hit_carries_term_reason():
    store = InMemoryStore()
    keys = _seed(store)
    hits = HybridCurator(store, embedder=_fake()).recall(QUERY, limit=10)
    by_key = {h.record.key: h for h in hits}
    assert any("词项" in r for r in by_key[keys["lexical"]].reasons)


# ---------- 参数校验与边界 ----------

@pytest.mark.parametrize("bad_alpha", [0.0, 1.0, -0.1, 1.5])
def test_alpha_validation(bad_alpha):
    with pytest.raises(ValueError):
        HybridCurator(InMemoryStore(), alpha=bad_alpha)


def test_limit_zero_or_negative_returns_empty():
    store = InMemoryStore()
    _seed(store)
    hybrid = HybridCurator(store, embedder=_fake())
    assert hybrid.recall(QUERY, limit=0) == ()
    assert hybrid.recall(QUERY, limit=-3) == ()


def test_empty_query_only_stable_facts():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    stable = curator.remember("用户不吃香菜", source="chat", evidence_id="e9",
                              stable_fact=True, consent=True)
    curator.remember("今天聊了很多", source="chat", evidence_id="e10", consent=True)
    hits = HybridCurator(store, embedder=_fake()).recall("", limit=10)
    assert [h.record.key for h in hits] == [stable.key]
    assert any("稳定" in r for r in hits[0].reasons)


# ---------- sqlite 后端 ----------

def test_sqlite_backend_semantic_recall(tmp_path):
    store = SqliteMemoryStore(tmp_path / "hybrid.db")
    keys = _seed(store)
    hits = HybridCurator(store, embedder=_fake()).recall(QUERY, limit=10)
    hit_keys = {h.record.key for h in hits}
    assert keys["semantic"] in hit_keys
    assert keys["lexical"] in hit_keys
    assert keys["zero_low"] not in hit_keys


def test_sqlite_backend_no_embedder_matches_curator(tmp_path):
    store = SqliteMemoryStore(tmp_path / "hybrid_plain.db")
    _seed(store)
    curator_keys = {h.record.key for h in MemoryCurator(store).recall(QUERY, limit=10)}
    hybrid_keys = {h.record.key for h in HybridCurator(store).recall(QUERY, limit=10)}
    assert hybrid_keys == curator_keys


def test_legacy_embedder_without_namespace_degrades_without_second_call():
    store = InMemoryStore()
    keys = _seed(store)

    class LegacyEmbedder:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            raise AssertionError("unnamespaced embedding must not be called")

    legacy = LegacyEmbedder()
    hits = HybridCurator(store, embedder=legacy).recall(QUERY, limit=10)
    assert legacy.calls == 0
    assert keys["lexical"] in {h.record.key for h in hits}
    assert keys["semantic"] not in {h.record.key for h in hits}
    assert any("退化" in r for h in hits for r in h.reasons)
