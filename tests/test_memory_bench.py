"""evals.memory_bench 的冒烟回归（issue #51）：小规模、全离线、秒级。"""
from __future__ import annotations


def test_bench_smoke_small_scale():
    from evals.memory_bench import QUERIES, render_markdown, run_bench
    rows = run_bench([50])
    # 2 种存储 × 3 条召回路径
    assert len(rows) == 6
    for row in rows:
        assert row["cold_ms"] >= 0 and row["warm_ms"] >= 0
    md = render_markdown(rows)
    assert "lexical" in md and "hybrid-cached" in md and "| 50 |" in md
    assert len(QUERIES) == 5


def test_counting_embedder_cache_bounds():
    from evals.memory_bench import CountingCachedEmbedder
    emb = CountingCachedEmbedder(cache_size=4)
    emb.embed([f"t{i}" for i in range(6)])
    assert len(emb._cache) == 4           # LRU 有界
    assert emb.texts_embedded == 6
    emb.embed(["t5"])                      # 已缓存 → 不再实算
    assert emb.texts_embedded == 6
