"""S2 记忆空间隔离测试（Whale Studio）。

对应 goal-chain S2 验收与 F2 负向矩阵（N01/N02/N03/N09 等）。
双后端（InMemory / Sqlite 临时文件）同一断言，保证行为一致。
"""

from __future__ import annotations

import pytest

from whale_core.life.conversation_context import MemorySpaceRef, MemorySpaceType
from whale_core.memory.bindings import BindingError, BindingStore
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.spaces import (
    DEFAULT_VAULT_ID,
    MemorySpaceError,
    SpaceStore,
    default_write_space,
    migrate_legacy_to_vault,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord


def _record(key, summary, *, space_id="", tags=("curated",), stable=False):
    return MemoryRecord(
        kind=MemoryKind.PREFERENCE if stable else MemoryKind.EPISODE,
        key=key,
        content={"summary": summary, "stable_fact": stable,
                 "importance": 0.8, "confidence": 1.0, "source": "test",
                 "evidence_id": "t:1"},
        tags=tags + (("stable",) if stable else ()),
        space_id=space_id,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        s = SqliteMemoryStore(tmp_path / "t.sqlite3")
        yield s
        s.close()


@pytest.fixture
def spaces(store):
    return SpaceStore(store)


def _mk_space(spaces, stype, sid):
    if stype is MemorySpaceType.PROTECTED_ORIGINAL:
        # 受保护库只能经迁移管道存在
        store_meta = spaces.store
        store_meta.put(f"space:{sid}", {
            "space_id": sid, "type": "protected_original", "name": "vault",
            "owner": "owner", "version": 1, "created_at": "t", "retired_at": None})
        return MemorySpaceRef(sid, stype, 1)
    return spaces.create(stype, name=sid, space_id=sid)


# ---------------------------------------------------------------------------
# 空间 CRUD 与 MEM-HARD-01
# ---------------------------------------------------------------------------


def test_protected_space_cannot_be_created_directly(spaces):
    with pytest.raises(MemorySpaceError):
        spaces.create(MemorySpaceType.PROTECTED_ORIGINAL, name="x")


def test_vault_records_never_listed(store, spaces):
    """MEM-HARD-01：vault 记录在任何列举路径都不出现（含 legacy None）。"""
    _mk_space(spaces, MemorySpaceType.PROTECTED_ORIGINAL, "vault-x")
    store.put_record(_record("k-secret", "私密内容", space_id="vault-x"))
    store.put_record(_record("k-open", "普通内容", space_id=""))
    for kind in (MemoryKind.EPISODE, MemoryKind.PREFERENCE):
        assert all(r.key != "k-secret" for r in store.list_by_kind(kind))
        assert all(r.key != "k-secret"
                   for r in store.list_by_kind(kind, space_ids=("vault-x",)))
    # get_record 直读是管理管道，允许（Fork 预览依赖它）
    assert store.get_record("k-secret") is not None


def test_explicit_space_filter(store):
    store.put_record(_record("a", "甲空间事实", space_id="s-a"))
    store.put_record(_record("b", "乙空间事实", space_id="s-b"))
    ids = [r.key for r in store.list_by_kind(MemoryKind.EPISODE, space_ids=("s-a",))]
    assert ids == ["a"]
    assert store.list_by_kind(MemoryKind.EPISODE, space_ids=()) == []


# ---------------------------------------------------------------------------
# Curator 召回限域（N02：跨空间检索在查询阶段拒绝）
# ---------------------------------------------------------------------------


def test_curator_recall_scoped(store):
    cur = MemoryCurator(store)
    store.put_record(_record("a", "主人怕辣", space_id="s-a"))
    store.put_record(_record("b", "乙群爱吃火锅", space_id="s-b"))
    ref_a = MemorySpaceRef("s-a", MemorySpaceType.GROUP_SPACE, 1)
    ref_b = MemorySpaceRef("s-b", MemorySpaceType.GROUP_SPACE, 1)
    hits_a = cur.recall("辣", allowed_spaces=(ref_a,))
    assert [h.record.key for h in hits_a] == ["a"]
    hits_b = cur.recall("辣", allowed_spaces=(ref_b,))
    assert [h.record.key for h in hits_b] == []  # 零命中，无泄漏
    assert cur.recall("辣", allowed_spaces=()) == ()


def test_curator_recall_legacy_excludes_vault(store):
    _mk_space(SpaceStore(store), MemorySpaceType.PROTECTED_ORIGINAL, "vault-x")
    store.put_record(_record("sec", "怕辣", space_id="vault-x"))
    store.put_record(_record("pub", "怕辣", space_id=""))
    hits = MemoryCurator(store).recall("怕辣")  # 兼容期 legacy 调用
    assert [h.record.key for h in hits] == ["pub"]


def test_vault_even_if_explicitly_requested(store):
    """纵深防御：调用方显式点名 vault 空间也被存储层拒绝。"""
    _mk_space(SpaceStore(store), MemorySpaceType.PROTECTED_ORIGINAL, "vault-x")
    store.put_record(_record("sec", "怕辣", space_id="vault-x"))
    vault_ref = MemorySpaceRef("vault-x", MemorySpaceType.PROTECTED_ORIGINAL, 1)
    assert MemoryCurator(store).recall("怕辣", allowed_spaces=(vault_ref,)) == ()


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_migration_moves_unspaced_to_vault(store):
    store.put_record(_record("old1", "旧记忆一"))
    store.put_record(_record("old2", "旧记忆二"))
    report = migrate_legacy_to_vault(store, dry_run=True)
    assert report["moved"] == 2
    # dry-run 不落库
    assert store.get_record("old1").space_id == ""
    report = migrate_legacy_to_vault(store)
    assert report["moved"] == 2
    assert store.get_record("old1").space_id == DEFAULT_VAULT_ID
    # vault 元数据存在且为 protected
    meta = SpaceStore(store).get(DEFAULT_VAULT_ID)
    assert meta["type"] == "protected_original"
    # 迁移后 legacy 召回不再返回这两条
    assert MemoryCurator(store).recall("旧记忆") == ()


def test_sqlite_migration_is_idempotent(tmp_path):
    """旧库（无 space_id 列）打开即迁移；重开幂等。"""
    import json, sqlite3
    db = tmp_path / "m.sqlite3"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("""CREATE TABLE records
      (key TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
       record_id TEXT NOT NULL, created_at TEXT NOT NULL, tags TEXT NOT NULL)""")
    raw.execute("INSERT INTO records VALUES (?,?,?,?,?,?)",
                ("x", "episode", json.dumps({"summary": "数据"}), "r1",
                 "2026-10-31T00:00:00+00:00", '["curated"]'))
    raw.commit()
    raw.close()
    s1 = SqliteMemoryStore(db)  # 触发 v0→v1 迁移
    assert s1.get_record("x").space_id == DEFAULT_VAULT_ID
    s1.close()
    s2 = SqliteMemoryStore(db)  # 重开：幂等，不报错不重复
    assert s2.get_record("x").space_id == DEFAULT_VAULT_ID
    assert s2.get("space:vault-default")["type"] == "protected_original"
    s2.close()


# ---------------------------------------------------------------------------
# 绑定层
# ---------------------------------------------------------------------------


def test_bind_vault_rejected(store, spaces):
    _mk_space(spaces, MemorySpaceType.PROTECTED_ORIGINAL, "vault-x")
    bindings = BindingStore(store, spaces)
    with pytest.raises(BindingError):
        bindings.bind_memory("qq:qq_group:group:1", "vault-x", granted_by="owner")


def test_bind_unknown_space_rejected(store, spaces):
    with pytest.raises(BindingError):
        BindingStore(store, spaces).bind_memory(
            "qq:qq_group:group:1", "nope", granted_by="owner")


def test_bind_and_lookup_roundtrip(store, spaces):
    ref = _mk_space(spaces, MemorySpaceType.GROUP_SPACE, "g-1")
    bindings = BindingStore(store, spaces)
    bindings.bind_memory("qq:qq_group:group:1", "g-1", can_write=True,
                         granted_by="owner")
    got = bindings.memory_spaces_for("qq:qq_group:group:1")
    assert [g.space_id for g in got] == ["g-1"]
    assert bindings.unbind_memory("qq:qq_group:group:1", "g-1") is True
    assert bindings.memory_spaces_for("qq:qq_group:group:1") == ()


def test_retired_space_fails_closed(store, spaces):
    _mk_space(spaces, MemorySpaceType.GROUP_SPACE, "g-1")
    bindings = BindingStore(store, spaces)
    bindings.bind_memory("c", "g-1", granted_by="owner")
    spaces.retire("g-1")
    assert bindings.memory_spaces_for("c") == ()


def test_write_space_selection():
    refs = (
        MemorySpaceRef("s-read", MemorySpaceType.SHAREABLE_FORK, 1),
        MemorySpaceRef("s-write", MemorySpaceType.GROUP_SPACE, 1, can_write=True),
    )
    assert default_write_space(refs) == "s-write"
    assert default_write_space(refs[:1]) == ""  # 共享副本不可写
    assert default_write_space(()) == ""
