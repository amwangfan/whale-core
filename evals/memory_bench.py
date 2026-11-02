"""记忆召回性能基线（issue #51）：先测量，再决定是否上持久向量索引。

测量矩阵：100 / 1,000 / 10,000 条合成记忆 × 两种存储（内存 / SQLite）×
三种召回路径：

- lexical：``MemoryCurator`` 词项召回（无 embedding 的现行兜底）；
- hybrid-hash：``HybridCurator`` + 确定性 HashEmbeddingProvider
  （离线可复现的"嵌入成本形状"代理——每次 embed 都要真算向量）；
- hybrid-cached：带 512 条文本缓存的 instrumented provider（贴近
  ``OpenAICompatibleEmbedding`` 的进程内缓存行为），统计 embed 调用量。

冷启动 = 新 curator + 空缓存的第一次召回；热启动 = 同查询再来一次。

手动运行（全部离线，无网络）::

    PYTHONPATH=src python3 -m evals.memory_bench
    PYTHONPATH=src python3 -m evals.memory_bench --sizes 100,1000 --out /tmp/bench.json

是否引入持久索引由实测收益决定：本脚本只产基线，不改任何召回语义。
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from whale_core.memory.curation import MemoryCurator
from whale_core.memory.hybrid import HashEmbeddingProvider, HybridCurator
from whale_core.memory.store import InMemoryStore, MemoryKind

QUERIES = ("喜欢吃什么", "上次说的那本书", "周末的计划", "帮忙看看代码", "生日")

_TOPICS = ("电影", "跑步", "咖啡", "代码", "旅行", "音乐", "做饭", "读书",
           "游戏", "猫", "工作", "考试", "健身", "天气", "股票", "摄影")
_DETAILS = ("说特别感兴趣", "提过一次", "每周都去", "最近刚迷上", "已经坚持了半年",
            "想下次试试", "觉得一般般", "强烈安利过")


def _synth_summary(i: int) -> str:
    """确定性合成摘要（不依赖随机数，跨进程可复现）。"""
    return f"主人{_TOPICS[i % len(_TOPICS)]}{_DETAILS[(i // len(_TOPICS)) % len(_DETAILS)]}（编号{i}）"


def _seed(store, n: int) -> None:
    from whale_core.memory.store import MemoryRecord
    from datetime import datetime, timezone
    for i in range(n):
        kind = MemoryKind.PREFERENCE if i % 5 == 0 else MemoryKind.EPISODE
        store.put_record(MemoryRecord(
            key=f"bench:{i}", kind=kind,
            content={"summary": _synth_summary(i), "importance": 0.5,
                     "confidence": 1.0, "stable_fact": i % 5 == 0},
            tags=("curated",), created_at=datetime.now(timezone.utc)))


class CountingCachedEmbedder:
    """带进程内文本缓存的计数嵌入器（模拟 OpenAICompatibleEmbedding 形状）。

    缓存键 = (namespace, text)；容量 512（与生产默认一致），LRU 淘汰。
    向量本身用 HashEmbeddingProvider 真算——保证 CPU 成本形状真实。
    """

    def __init__(self, *, cache_size: int = 512):
        self._hash = HashEmbeddingProvider()
        self._cache: dict[tuple[str, str], list[float]] = {}
        self.cache_size = cache_size
        self.embed_calls = 0          # embed() 被调次数
        self.texts_embedded = 0       # 真正计算向量的文本数（缓存未命中）

    def embed(self, texts, *, namespace: str = ""):
        self.embed_calls += 1
        out: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        missing: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            key = (namespace, t)
            if key in self._cache:
                out[i] = self._cache[key]
            else:
                missing.append((i, t))
        if missing:
            vecs = self._hash.embed([t for _, t in missing])
            self.texts_embedded += len(missing)
            for (i, t), vec in zip(missing, vecs):
                key = (namespace, t)
                self._cache[key] = vec
                out[i] = vec
                while len(self._cache) > self.cache_size:
                    self._cache.pop(next(iter(self._cache)))
        return out


def _measure(recall_fn, queries=QUERIES) -> dict:
    """同一 curator 上先冷后热；返回毫秒与计数。"""
    t0 = time.perf_counter()
    for q in queries[:1]:
        recall_fn(q)
    cold_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for q in queries:
        recall_fn(q)
    warm_ms = (time.perf_counter() - t0) * 1000 / len(queries)
    return {"cold_ms": round(cold_ms, 2), "warm_ms": round(warm_ms, 2)}


def _run_size(store, n: int, store_name: str) -> list[dict]:
    _seed(store, n)
    rows: list[dict] = []

    lexical = MemoryCurator(store)
    m = _measure(lambda q: lexical.recall(q, limit=5))
    rows.append({"store": store_name, "n": n, "path": "lexical", **m,
                 "embed_calls": 0, "texts_embedded": 0})

    hash_embedder = HashEmbeddingProvider()
    hybrid = HybridCurator(store, embedder=hash_embedder)
    m = _measure(lambda q: hybrid.recall(q, limit=5))
    rows.append({"store": store_name, "n": n, "path": "hybrid-hash", **m,
                 "embed_calls": "n/a", "texts_embedded": "n/a"})

    cached = CountingCachedEmbedder()
    hybrid_c = HybridCurator(store, embedder=cached)
    m = _measure(lambda q: hybrid_c.recall(q, limit=5))
    rows.append({"store": store_name, "n": n, "path": "hybrid-cached", **m,
                 "embed_calls": cached.embed_calls,
                 "texts_embedded": cached.texts_embedded})
    return rows


def run_bench(sizes: list[int]) -> list[dict]:
    rows: list[dict] = []
    for n in sizes:
        rows.extend(_run_size(InMemoryStore(), n, "memory"))
        from whale_core.memory.sqlite_store import SqliteMemoryStore
        with tempfile.TemporaryDirectory() as td:
            sq = SqliteMemoryStore(str(Path(td) / "bench.sqlite3"))
            try:
                rows.extend(_run_size(sq, n, "sqlite"))
            finally:
                sq.close()
    return rows


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "# 记忆召回性能基线（issue #51）",
        "",
        "离线可复现：`PYTHONPATH=src python3 -m evals.memory_bench`。"
        "合成记忆、确定性嵌入代理；真实网络嵌入的每次调用成本约为本地散列的"
        "数百到数千倍（HTTP+计费），缓存命中率形状一致。",
        "",
        "| 存储 | 记忆数 | 召回路径 | 冷启动 ms | 热启动 ms | embed 调用 | 实算文本数 |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {r['store']} | {r['n']} | {r['path']} | {r['cold_ms']} | "
            f"{r['warm_ms']} | {r['embed_calls']} | {r['texts_embedded']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m evals.memory_bench")
    parser.add_argument("--sizes", default="100,1000,10000",
                        help="逗号分隔的记忆规模（默认 100,1000,10000）")
    parser.add_argument("--out", default=None, help="JSON 结果输出路径")
    args = parser.parse_args(argv)
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    rows = run_bench(sizes)
    md = render_markdown(rows)
    print(md)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"JSON 已写入：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
