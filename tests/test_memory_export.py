"""memory.export：JSONL 导出/导入（备份/迁移/恢复）契约测试。

同一组核心用例跑两种后端：InMemoryStore 与 SqliteMemoryStore(tmp_path)。
"""

import io
import json
from datetime import datetime, timezone

import pytest

from whale_core.memory.export import export_jsonl, import_jsonl
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord

T1 = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
T2 = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
T3 = datetime(2026, 3, 4, 5, 6, 7, 654321, tzinfo=timezone.utc)


@pytest.fixture(params=["inmemory", "sqlite"])
def make_store(request, tmp_path):
    """建库工厂：每次调用得到一个全新空库。"""
    seq = 0

    def _make():
        nonlocal seq
        if request.param == "inmemory":
            return InMemoryStore()
        seq += 1
        return SqliteMemoryStore(tmp_path / f"memory_{seq}.db")

    return _make


def _sample_records():
    return [
        MemoryRecord(kind=MemoryKind.EPISODE, key="ep:1",
                     content={"summary": "一起看了鲸鱼纪录片", "mood": "开心"},
                     record_id="rid-ep-1", created_at=T1, tags=("episode", "鲸鱼")),
        MemoryRecord(kind=MemoryKind.PREFERENCE, key="pref:color",
                     content={"like": "蓝色", "score": 0.9},
                     record_id="rid-pref-1", created_at=T2, tags=("pref",)),
        MemoryRecord(kind=MemoryKind.PERSONA_CONTINUITY, key="persona:default",
                     content={"persona_name": "鲸鱼", "traits": {"calm": True}},
                     record_id="rid-pc-1", created_at=T3, tags=("persona", "连续性")),
    ]


def _dump(store):
    buf = io.StringIO()
    count = export_jsonl(store, buf)
    return count, buf.getvalue()


def test_roundtrip_export_import(make_store):
    src = make_store()
    records = _sample_records()
    for rec in records:
        src.put_record(rec)

    count, text = _dump(src)
    assert count == 3
    lines = text.splitlines()
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert set(obj) == {"key", "kind", "record_id", "created_at", "tags", "content", "space_id"}  # S2
    assert "鲸鱼" in text  # ensure_ascii=False：非 ASCII 不转义

    dst = make_store()
    imported, skipped = import_jsonl(dst, io.StringIO(text))
    assert (imported, skipped) == (3, 0)
    for rec in records:
        got = dst.get_record(rec.key)
        assert got is not None
        assert got.kind == rec.kind
        assert dict(got.content) == dict(rec.content)
        assert got.record_id == rec.record_id
        assert got.created_at == rec.created_at
        assert tuple(got.tags) == tuple(rec.tags)


def test_import_skips_existing_by_default(make_store):
    src = make_store()
    for rec in _sample_records():
        src.put_record(rec)
    _, text = _dump(src)

    dst = make_store()
    dst.put_record(MemoryRecord(kind=MemoryKind.EPISODE, key="ep:1",
                                content={"summary": "旧内容"}, record_id="old-rid-1",
                                created_at=T1, tags=("old",)))
    dst.put_record(MemoryRecord(kind=MemoryKind.PREFERENCE, key="pref:color",
                                content={"like": "红色"}, record_id="old-rid-2",
                                created_at=T2))

    imported, skipped = import_jsonl(dst, io.StringIO(text))
    assert (imported, skipped) == (1, 2)
    # 已存在同 key 的两条保持原值，新 key 正常导入
    assert dst.get_record("ep:1").content["summary"] == "旧内容"
    assert dst.get_record("ep:1").record_id == "old-rid-1"
    assert dst.get_record("pref:color").content["like"] == "红色"
    assert dst.get_record("persona:default") is not None

    # 幂等：同一文件再导一遍，全部跳过，不产生变化
    imported, skipped = import_jsonl(dst, io.StringIO(text))
    assert (imported, skipped) == (0, 3)
    assert dst.get_record("ep:1").content["summary"] == "旧内容"


def test_import_overwrite_replaces_existing(make_store):
    src = make_store()
    for rec in _sample_records():
        src.put_record(rec)
    _, text = _dump(src)

    dst = make_store()
    dst.put_record(MemoryRecord(kind=MemoryKind.EPISODE, key="ep:1",
                                content={"summary": "旧内容"}, record_id="old-rid-1",
                                created_at=T1))

    imported, skipped = import_jsonl(dst, io.StringIO(text), overwrite=True)
    assert (imported, skipped) == (3, 0)
    got = dst.get_record("ep:1")
    assert got.content["summary"] == "一起看了鲸鱼纪录片"
    assert got.record_id == "rid-ep-1"
    assert got.created_at == T1
    assert tuple(got.tags) == ("episode", "鲸鱼")


def test_import_malformed_lines_skipped_and_counted(make_store):
    good1 = json.dumps({"key": "good:1", "kind": "episode",
                        "content": {"a": 1}, "record_id": "r1",
                        "created_at": T1.isoformat(), "tags": ["x"]},
                       ensure_ascii=False)
    good2 = json.dumps({"key": "good:2", "kind": "preference",
                        "content": {"b": 2}}, ensure_ascii=False)  # 缺可选字段，容错导入
    bad_lines = [
        "{not valid json",                                                   # JSON 畸形
        "[1, 2, 3]",                                                         # 非 JSON 对象
        json.dumps({"kind": "episode", "content": {}}),                      # 缺 key
        json.dumps({"key": "k:no-kind", "content": {}}),                     # 缺 kind
        json.dumps({"key": "k:bad-kind", "kind": "nope", "content": {}}),    # kind 不在 MemoryKind
        json.dumps({"key": "k:bad-content", "kind": "episode",
                    "content": "文本"}),                                     # content 非 dict
    ]
    text = "\n".join(["", good1, *bad_lines[:2], "   ", *bad_lines[2:], good2, ""])
    dst = make_store()
    imported, skipped = import_jsonl(dst, io.StringIO(text))
    assert (imported, skipped) == (2, 6)  # 空行/纯空白行跳过但不计数
    # 正常行不受影响
    g1 = dst.get_record("good:1")
    assert g1 is not None
    assert g1.kind == MemoryKind.EPISODE
    assert dict(g1.content) == {"a": 1}
    assert g1.record_id == "r1"
    assert g1.created_at == T1
    assert tuple(g1.tags) == ("x",)
    g2 = dst.get_record("good:2")
    assert g2 is not None
    assert g2.kind == MemoryKind.PREFERENCE
    assert dict(g2.content) == {"b": 2}
    # 坏行均未入库
    assert dst.get_record("k:no-kind") is None
    assert dst.get_record("k:bad-kind") is None
    assert dst.get_record("k:bad-content") is None


def test_export_empty_store_returns_zero(make_store):
    count, text = _dump(make_store())
    assert count == 0
    assert text == ""
    # 空文件导入也是 (0, 0)
    assert import_jsonl(make_store(), io.StringIO("")) == (0, 0)


def test_export_excludes_kv_entries(make_store):
    store = make_store()
    store.put("some:kv", {"draft": "账本数据", "quota": 3})
    for rec in _sample_records()[:2]:
        store.put_record(rec)
    count, text = _dump(store)
    assert count == 2
    keys = sorted(json.loads(line)["key"] for line in text.splitlines())
    assert keys == ["ep:1", "pref:color"]
    assert "some:kv" not in text
