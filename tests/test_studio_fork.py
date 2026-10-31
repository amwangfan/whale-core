"""S2 Fork 服务测试（Whale Studio）。

对应 goal-chain S2 验收：物理复制独立性、预览脱敏、确认门槛、
无自动同步、副本删除不影响原库、派生元数据不含私有源键。
"""

from __future__ import annotations

import pytest

from whale_core.life.conversation_context import MemorySpaceType
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.fork import ForkError, ForkService
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord


@pytest.fixture
def env():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    # 迁移管道建 vault
    store.put("space:vault", {
        "space_id": "vault", "type": "protected_original", "name": "原始库",
        "owner": "owner", "version": 1, "created_at": "t", "retired_at": None})
    for i in range(1, 6):
        store.put_record(MemoryRecord(
            kind=MemoryKind.EPISODE, key=f"secret-{i}",
            content={"summary": f"私密事实{i}：电话 1380000000{i}",
                     "importance": 0.9, "confidence": 1.0},
            tags=("curated",), space_id="vault"))
    fork = ForkService(store, spaces)
    return store, spaces, fork


def _redact_phone(content: dict) -> dict:
    out = dict(content)
    out["summary"] = str(out.get("summary", "")).replace("138", "***")
    return out


def test_fork_requires_confirmation(env):
    store, spaces, fork = env
    with pytest.raises(ForkError):
        fork.fork("vault", ("secret-1",),
                  target_type=MemorySpaceType.SHAREABLE_FORK,
                  target_name="x", created_by="owner")


def test_fork_rejects_protected_target(env):
    store, spaces, fork = env
    with pytest.raises(ForkError):
        fork.fork("vault", ("secret-1",),
                  target_type=MemorySpaceType.PROTECTED_ORIGINAL,
                  target_name="x", created_by="owner", confirmed=True)


def test_fork_empty_selection_rejected(env):
    store, spaces, fork = env
    with pytest.raises(ForkError):
        fork.fork("vault", (), target_type=MemorySpaceType.SHAREABLE_FORK,
                  target_name="x", created_by="owner", confirmed=True)


def test_fork_rejects_cross_space_smuggling(env):
    store, spaces, fork = env
    other = spaces.create(MemorySpaceType.GROUP_SPACE, name="g", space_id="g-1")
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="other-1",
        content={"summary": "别群内容"}, tags=("curated",), space_id="g-1"))
    with pytest.raises(ForkError):
        fork.fork("vault", ("other-1",),
                  target_type=MemorySpaceType.SHAREABLE_FORK,
                  target_name="x", created_by="owner", confirmed=True)


def test_fork_preview_applies_redaction(env):
    store, spaces, fork = env
    items = fork.preview("vault", ("secret-1",), redact=_redact_phone)
    assert len(items) == 1
    assert "***" in items[0]["content"]["summary"]
    assert "138" not in items[0]["content"]["summary"]
    # 预览不复制（list_spaces 默认不含 vault；含保护视角也只有 vault 一个）
    assert spaces.list_spaces() == ()
    assert len(spaces.list_spaces(include_protected=True)) == 1


def test_fork_physical_independence(env):
    """T02/N09：复制后原库变更不影响副本；副本删除不影响原库。"""
    store, spaces, fork = env
    target = fork.fork("vault", ("secret-1", "secret-2", "secret-3"),
                       target_type=MemorySpaceType.SHAREABLE_FORK,
                       target_name="课程副本", created_by="owner",
                       redact=_redact_phone, confirmed=True)
    # 物理独立：新 key、新 record_id、目标 space
    copies = store.list_by_kind(MemoryKind.EPISODE, space_ids=(target,))
    assert len(copies) == 3
    for c in copies:
        assert c.key.startswith(f"sp:{target}:")
        assert c.space_id == target
        assert "forked" in c.tags
        # 派生元数据不含私有源键
        prov = c.content.get("fork_provenance", {})
        assert "secret-" not in str(prov)
        assert prov.get("source_space_type") == "protected_original"
    # 原库新增秘密 → 副本不可见（无隐形同步）
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="secret-new",
        content={"summary": "新秘密"}, tags=("curated",), space_id="vault"))
    cur = MemoryCurator(store)
    ref = spaces.ref(target)
    assert all("新秘密" not in str(h.record.content)
               for h in cur.recall("", allowed_spaces=(ref,)))
    # 删除原库记录 → 副本仍在（PRD 验收 10）
    store.delete("secret-1")
    assert len(store.list_by_kind(MemoryKind.EPISODE, space_ids=(target,))) == 3
    # 删除副本 → 原库其余记录不动（vault 本体不出现在任何列举路径）
    for c in store.list_by_kind(MemoryKind.EPISODE, space_ids=(target,)):
        store.delete(c.key)
    assert len(store.list_by_kind(MemoryKind.EPISODE)) == 0  # vault 永不入列
    assert len(store.list_by_kind(MemoryKind.EPISODE, space_ids=(target,))) == 0
    # 原库记录经管理管道直读仍在（sec-1 已删，sec-2..5+new 完好）
    for k in ("secret-2", "secret-3", "secret-4", "secret-5", "secret-new"):
        assert store.get_record(k) is not None
    assert store.get_record("secret-1") is None


def test_fork_vault_records_recallable_only_in_copy(env):
    """Fork 出的副本可正常召回；vault 本体依旧不可召回。"""
    store, spaces, fork = env
    target = fork.fork("vault", ("secret-3",),
                       target_type=MemorySpaceType.SHAREABLE_FORK,
                       target_name="x", created_by="owner", confirmed=True)
    cur = MemoryCurator(store)
    ref = spaces.ref(target)
    hits = cur.recall("私密事实3", allowed_spaces=(ref,))
    assert len(hits) == 1
    assert hits[0].record.space_id == target
    # 兼容期 legacy 召回也看不到 vault 里的原件
    assert all("secret-3" != h.record.key for h in cur.recall("私密事实3"))


def test_fork_bumps_space_version(env):
    store, spaces, fork = env
    target = fork.fork("vault", ("secret-1",),
                       target_type=MemorySpaceType.SHAREABLE_FORK,
                       target_name="x", created_by="owner", confirmed=True)
    assert spaces.get(target)["version"] == 2  # create=1，fork 落记录后 bump
