"""Opt-in curated memories with evidence-aware, explainable retrieval.

Raw call transcripts in MemoryStore are NOT automatically promoted to facts.
This pure-Python implementation has no embeddings, LLM calls or hidden collection.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore


def _terms(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text.lower())
    parts: set[str] = set()
    for tok in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", tok):
            # 中文词项 = 单字 + 二元组：单字查询（如「辣」）也要能命中
            # 多字事实（「我怕辣」）；召回靠交集，精确度交给打分排序。
            parts.update(tok)
            parts.update(tok[i:i + 2] for i in range(max(0, len(tok) - 1)))
        else:
            parts.add(tok)
    return parts


@dataclass(frozen=True)
class RecallHit:
    record: MemoryRecord
    score: float
    reasons: tuple[str, ...]


def _space_ids(allowed_spaces) -> tuple[str, ...] | None:
    """从 MemorySpaceRef / 字符串元组提取 space_id；None 原样透传（兼容期）。"""
    if allowed_spaces is None:
        return None
    out: list[str] = []
    for item in allowed_spaces:
        out.append(str(getattr(item, "space_id", item)))
    return tuple(dict.fromkeys(out))


def _is_expired(content, now: datetime) -> bool:
    """G2/MEM-04：带 expires_at 的记录到期即退出召回（不删记录本身）。"""
    exp = (content or {}).get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(str(exp)) <= now
    except (ValueError, TypeError):
        return False  # 畸形时间戳不误杀（召回侧 fail open 仅限此字段）


class MemoryCurator:
    """Explicit candidate promotion and retrieval; never consumes audio or raw logs."""

    def __init__(self, store: MemoryStore, half_life_days: float = 30.0):
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self.store = store
        self.half_life_days = half_life_days

    def remember(self, summary: str, *, source: str, evidence_id: str,
                 importance: float = 0.5, confidence: float = 1.0,
                 stable_fact: bool = False, consent: bool = False,
                 key: str | None = None, now: datetime | None = None,
                 space_id: str = "") -> MemoryRecord:
        if not consent:
            raise PermissionError("Explicit memory consent required")
        if not summary.strip() or not source.strip() or not evidence_id.strip():
            raise ValueError("summary, source and evidence_id are required")
        if not 0 <= importance <= 1 or not 0 <= confidence <= 1:
            raise ValueError("importance and confidence must be within [0,1]")
        kind = MemoryKind.PREFERENCE if stable_fact else MemoryKind.EPISODE
        stamp = now or datetime.now(timezone.utc)
        record = MemoryRecord(
            kind=kind,
            key=key or f"curated:{uuid4().hex}",
            content={"summary": summary[:1500], "source": source[:120],
                     "evidence_id": evidence_id[:160], "importance": importance,
                     "confidence": confidence, "stable_fact": stable_fact,
                     # G2 分层元数据：来源/确认时间/同意/置信度显式可查
                     "layer": "preference" if stable_fact else "curated",
                     "consent": True,
                     "last_confirmed": stamp.isoformat()},
            created_at=stamp,
            tags=("curated", "stable" if stable_fact else "episode"),
            space_id=space_id,
        )
        self.store.put_record(record)
        return record

    def recall(self, query: str, *, limit: int = 5,
               now: datetime | None = None,
               allowed_spaces=None, include_protected: bool = False) -> tuple[RecallHit, ...]:
        """召回。S2：``allowed_spaces`` 为 MemorySpaceRef 元组（服务端
        ResolvedContext 提供）；空元组 fail closed；None 为兼容期旧行为
        （仍不含 protected_original，存储层硬排除）。"""
        if limit <= 0:
            return ()
        space_ids = _space_ids(allowed_spaces)
        if space_ids is not None and not space_ids:
            return ()  # 已解析但零授权空间：查询前即止
        current = now or datetime.now(timezone.utc)
        query_terms = _terms(query)
        matches: list[RecallHit] = []
        records = (self.store.list_by_kind(MemoryKind.EPISODE, space_ids=space_ids,
                                           include_protected=include_protected) +
                   self.store.list_by_kind(MemoryKind.PREFERENCE, space_ids=space_ids,
                                           include_protected=include_protected))
        for rec in records:
            if "curated" not in rec.tags:
                continue
            if "superseded" in rec.tags:
                continue  # 被更正的旧记忆不再召回（G2）
            if _is_expired(rec.content, current):
                continue  # TTL 到期退出召回（MEM-04）
            content = rec.content
            stable = bool(content.get("stable_fact"))
            terms = _terms(str(content.get("summary", "")))
            common = terms & query_terms
            if query_terms and not common:
                continue
            if not query_terms and not stable:
                continue
            relevance = len(common) / max(1, len(query_terms))
            age = max(0.0, (current - rec.created_at).total_seconds() / 86400)
            freshness = 1.0 if stable else math.pow(0.5, age / self.half_life_days)
            importance = float(content.get("importance", 0.5))
            confidence = float(content.get("confidence", 1.0))
            score = confidence * (0.65 * relevance + 0.25 * importance + 0.10 * freshness)
            reasons = ("词项匹配" if common else "稳定事实",
                       "长期事实不衰减" if stable else f"距今{age:.0f}天",
                       f"来源:{content.get('source', 'unknown')}",
                       f"证据:{content.get('evidence_id', 'unknown')}")
            matches.append(RecallHit(rec, score, reasons))
        matches.sort(key=lambda hit: (hit.score, hit.record.created_at), reverse=True)
        return tuple(matches[:limit])

    def forget(self, key: str) -> bool:
        record = self.store.get_record(key)
        if record is None or "curated" not in record.tags:
            return False
        self.store.delete(key)
        return True
