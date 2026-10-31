"""InMemoryStore 与 SqliteMemoryStore 行为对齐（parity）参数化测试。

同一组断言跑两套后端，锁定冻结契约：
- list_by_kind 统一 created_at 倒序（稳定：同 created_at 保持插入顺序）
- get() 对 record key 的窥视视图形状一致
- put_record 的 JSON 序列化语义一致（类型归一 / TypeError）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from whale_core.memory.store import (
    InMemoryStore,
    MemoryKind,
    MemoryRecord,
    PersonaContinuity,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        s = SqliteMemoryStore(tmp_path / "memory.db")
        yield s
        s.close()


def _record(key: str, **kwargs) -> MemoryRecord:
    defaults = {"kind": MemoryKind.EPISODE, "content": {"summary": key}}
    defaults.update(kwargs)
    return MemoryRecord(key=key, **defaults)


def test_kv_put_get_delete_keys(store):
    assert store.get("missing") is None
    store.put("a", {"n": 1})
    store.put("b", [1, "两", None])
    assert store.get("a") == {"n": 1}
    assert store.get("b") == [1, "两", None]
    assert store.keys() == ["a", "b"]
    store.delete("a")
    assert store.get("a") is None
    assert store.keys() == ["b"]
    # delete 不存在的 key 不报错
    store.delete("never-existed")


def test_put_record_get_record_normalizes_types(store):
    rec = _record(
        "ep:1",
        content={"items": (1, "两"), "nested": {"pair": (3, 4)}},
        tags=("alpha", "beta"),
    )
    store.put_record(rec)
    got = store.get_record("ep:1")
    assert got is not None
    # JSON 往返归一：content 内 tuple -> list（两边一致）
    assert got.content == {"items": [1, "两"], "nested": {"pair": [3, 4]}}
    assert got.kind == MemoryKind.EPISODE
    assert got.record_id == rec.record_id
    assert got.created_at == rec.created_at
    assert isinstance(got.created_at, datetime)
    assert tuple(got.tags) == ("alpha", "beta")
    assert store.get_record("missing") is None


def test_get_peeks_record_view_same_shape(store):
    rec = _record("ep:peek", content={"summary": "窥视", "n": 1}, tags=("t1",))
    store.put_record(rec)
    view = store.get("ep:peek")
    assert isinstance(view, dict)
    assert view == {
        "kind": MemoryKind.EPISODE.value,
        "content": {"summary": "窥视", "n": 1},
        "record_id": rec.record_id,
        "created_at": rec.created_at.isoformat(),
        "tags": ["t1"],
        "space_id": "",  # S2：窥视视图携带空间归属（双后端同形状）
    }


def test_list_by_kind_desc_order(store):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    old = _record("ep:old", created_at=base)
    mid = _record("ep:mid", created_at=base + timedelta(hours=1))
    new = _record("ep:new", created_at=base + timedelta(hours=2))
    other = _record("pref:x", kind=MemoryKind.PREFERENCE, created_at=base + timedelta(hours=3))
    # 故意乱序插入
    for r in (mid, old, other, new):
        store.put_record(r)
    got = store.list_by_kind(MemoryKind.EPISODE)
    assert [r.key for r in got] == ["ep:new", "ep:mid", "ep:old"]
    assert store.list_by_kind(MemoryKind.PREFERENCE)[0].key == "pref:x"


def test_list_by_kind_tie_keeps_insertion_order(store):
    same = datetime(2024, 1, 1, tzinfo=timezone.utc)
    a = _record("ep:a", created_at=same)
    b = _record("ep:b", created_at=same)
    c = _record("ep:c", created_at=same)
    for r in (a, b, c):
        store.put_record(r)
    got = store.list_by_kind(MemoryKind.EPISODE)
    assert [r.key for r in got] == ["ep:a", "ep:b", "ep:c"]


def test_delete_clears_record_and_peek_view(store):
    rec = _record("ep:del")
    store.put_record(rec)
    assert store.get("ep:del") is not None
    assert store.get_record("ep:del") is not None
    store.delete("ep:del")
    assert store.get("ep:del") is None
    assert store.get_record("ep:del") is None
    assert "ep:del" not in store.keys()
    assert store.list_by_kind(MemoryKind.EPISODE) == []


def test_persona_continuity_roundtrip(store):
    assert store.get_persona_continuity() is None
    persona = PersonaContinuity(
        persona_name="鲸鱼",
        tone="冷静、简洁",
        address_user_as="船长",
        taboos=("别喊全名",),
        traits={"hobby": "观星"},
        last_session_id="sess-1",
        updated_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
    )
    store.put_persona_continuity(persona)
    got = store.get_persona_continuity()
    assert got == persona


def test_non_json_serializable_content_raises_type_error(store):
    bad = _record("ep:bad", content={"x": object()})
    with pytest.raises(TypeError):
        store.put_record(bad)
    assert store.get_record("ep:bad") is None
