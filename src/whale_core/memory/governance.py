"""记忆治理（G2）：分层、去重、过期、更正、删除传播。

分层模型（记录在 ``content["layer"]`` 与 tags 双重标注）：

- ``transcript``：原始会话历史（memory/history.py 的会话轮转，不进召回）；
- ``episode``：近期情节（聊天回写，会衰减、会过期、可治理）；
- ``pending_fact``：需确认事实——模型推断/候选事实**停留在此层**，
  不参与召回，经 ``confirm_fact``（显式确认）才升级为 curated；
- ``preference``：稳定偏好（stable_fact，不衰减）；
- ``curated``：用户主动记忆（显式「记住」，consent=True）。

治理操作全部真实生效：去重/过期是**真删除**；更正（supersede）让旧记录
退出一切召回（含语义层 embedding 缓存传播）；删除无备份、不可恢复。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from whale_core.memory.store import MemoryKind, MemoryStore

GOVERNABLE_TAGS = ("chat", "call")  # 低价值情节的来源标签（curated 永不治理掉）


def layer_of(record) -> str:
    """一条记忆的分层判定（content.layer 优先，tags 兜底）。"""
    layer = str((record.content or {}).get("layer", ""))
    if layer:
        return layer
    if "curated" in record.tags:
        return "curated"
    if record.kind == MemoryKind.PREFERENCE:
        return "preference"
    if record.kind == MemoryKind.EPISODE:
        return "episode"
    return "other"


def dedupe_episodes(store: MemoryStore) -> int:
    """同 summary 的重复情节去重（保留最新一条）；只治理非 curated 情节。

    返回删除条数。
    """
    seen: set[str] = set()
    removed = 0
    # list_by_kind 按 created_at 倒序（新的在前）：先遇即最新，保留；
    # 之后遇到的同 summary 记录是更老的重复，删除。
    for record in store.list_by_kind(MemoryKind.EPISODE):
        if "curated" in record.tags:
            continue
        summary = str((record.content or {}).get("summary", "")).strip()
        if not summary:
            continue
        if summary in seen:
            store.delete(record.key)
            removed += 1
        else:
            seen.add(summary)
    return removed


def expire_episodes(store: MemoryStore, *, older_than_days: float = 90.0,
                    now: datetime | None = None) -> int:
    """过期治理：删除超过寿命的非 curated 情节（chat/call 标签）。

    curated / preference / persona 永不过期。返回删除条数。
    """
    if older_than_days <= 0:
        raise ValueError("older_than_days must be positive")
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=older_than_days)
    removed = 0
    for record in store.list_by_kind(MemoryKind.EPISODE):
        if "curated" in record.tags:
            continue
        if not any(tag in GOVERNABLE_TAGS for tag in record.tags):
            continue
        if record.created_at < cutoff:
            store.delete(record.key)
            removed += 1
    return removed


def propose_fact(store: MemoryStore, summary: str, *, source: str,
                 evidence_id: str, confidence: float = 0.5) -> str:
    """模型推断的事实**只进 pending_fact 层**：不召回、不升级。

    返回记录 key；经 confirm_fact 显式确认后才参与召回。
    """
    from uuid import uuid4
    from whale_core.memory.store import MemoryRecord

    if not summary.strip():
        raise ValueError("summary required")
    key = f"pending:{uuid4().hex}"
    record = MemoryRecord(
        kind=MemoryKind.EPISODE,
        key=key,
        content={"summary": summary[:1500], "source": source[:120],
                 "evidence_id": evidence_id[:160],
                 "confidence": max(0.0, min(1.0, confidence)),
                 "layer": "pending_fact"},
        tags=("pending_fact",),
    )
    store.put_record(record)
    return key


def confirm_fact(store: MemoryStore, key: str, *, curator=None,
                 confirmed_by: str = "owner") -> bool:
    """显式确认：pending_fact 升级为 curated 稳定事实（进入召回）。"""
    record = store.get_record(key)
    if record is None or "pending_fact" not in record.tags:
        return False
    content = dict(record.content)
    summary = str(content.pop("summary", ""))
    source = str(content.pop("source", ""))
    evidence = str(content.pop("evidence_id", ""))
    store.delete(key)
    if curator is None:
        from whale_core.memory.curation import MemoryCurator

        curator = MemoryCurator(store)
    curator.remember(summary, source=source, evidence_id=evidence,
                     confidence=float(content.get("confidence", 1.0)),
                     stable_fact=True, consent=True)
    return True


def supersede(store: MemoryStore, old_key: str, new_summary: str, *,
              source: str, evidence_id: str, registry=None) -> str:
    """更正记忆：旧记录标 superseded（退出一切召回），写入新 curated 记录。

    返回新记录 key。旧记录保留 30 天审计窗口后由 expire 清理
    （superseded 标记本身即治理标签，召回路径一律排除）。
    """
    from whale_core.memory.curation import MemoryCurator

    old = store.get_record(old_key)
    if old is None:
        raise KeyError(old_key)
    content = dict(old.content)
    content["layer"] = "superseded"
    content["superseded_at"] = datetime.now(timezone.utc).isoformat()
    store.put_record(type(old)(kind=old.kind, key=old.key, content=content,
                               record_id=old.record_id, created_at=old.created_at,
                               tags=tuple(set(old.tags) | {"superseded"})))
    _purge_embedding(registry, str(content.get("summary", "")))
    new = MemoryCurator(store).remember(
        new_summary, source=source, evidence_id=evidence_id,
        stable_fact=True, consent=True)
    return new.key


def _purge_embedding(registry, text: str) -> None:
    """embedding 缓存删除传播（与 DynamicHybridCurator.forget 同语义）。"""
    if registry is None or not text:
        return
    for _pid, provider in registry.providers_for("embedding.embed"):
        purge = getattr(provider, "forget_text", None)
        if callable(purge):
            try:
                purge(text)
            except Exception:
                pass


def forget_gone_everywhere(store: MemoryStore, key: str, *, registry=None) -> bool:
    """真删除 + embedding 缓存传播（「忘掉」指令之外的显式删除入口）。"""
    record = store.get_record(key)
    if record is None:
        return False
    summary = str((record.content or {}).get("summary", ""))
    store.delete(key)
    _purge_embedding(registry, summary)
    return True
