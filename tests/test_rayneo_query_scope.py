"""query 限域：owner + 群 + protected_original；不含成员私人 / fork / 空 space_id。"""
from __future__ import annotations

from whale_core.memory.continuity import remember_episode
from whale_core.memory.rayneo_context import (
    RayneoContextStore, query_rayneo_context, _sha256_text,
)
from whale_core.memory.spaces import (
    OWNER_WORKING_SPACE_ID, SpaceStore, ensure_owner_working_copy,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.types import MemorySpaceType


def _ingest(store, text, segment_id="s-1"):
    item = {
        "client_seq": 1,
        "dedup_key": f"k-{segment_id}",
        "kind": "transcript",
        "origin": "live",
        "revision": 1,
        "segment_id": segment_id,
        "text": text,
        "content_hash": _sha256_text(text),
        "received_at": "2026-11-10T02:00:00Z",
        "source_time": "2026-11-10T02:00:00Z",
    }
    store.ingest_batch("dev", [item], now_ms=1_000)


def _setup(tmp_path):
    memory = SqliteMemoryStore(tmp_path / "m.sqlite3")
    rayneo = RayneoContextStore(tmp_path / "r.sqlite3")
    ensure_owner_working_copy(memory)
    spaces = SpaceStore(memory)
    g1 = spaces.create(MemorySpaceType.GROUP_SPACE, name="群一", space_id="g-one")
    g2 = spaces.create(MemorySpaceType.GROUP_SPACE, name="群二", space_id="g-two")
    mem = spaces.create(MemorySpaceType.MEMBER_PRIVATE, name="成员私",
                        space_id="mp-other", owner="member-x")
    # protected vault 由迁移管道写入元数据
    memory.put("space:vault-default", {
        "space_id": "vault-default", "type": "protected_original",
        "name": "默认私人原始库", "owner": "owner",
        "version": 1, "created_at": "2026-01-01T00:00:00+00:00", "retired_at": None,
    })
    fork = spaces.create(MemorySpaceType.SHAREABLE_FORK, name="fork", space_id="fk-1")
    remember_episode(memory, summary="OWNER_MARK 主人备忘", key="ep:owner",
                     tags=("note",), space_id=OWNER_WORKING_SPACE_ID)
    remember_episode(memory, summary="GROUP1_MARK 群一摘要", key="ep:g1",
                     tags=("note",), space_id=g1.space_id)
    remember_episode(memory, summary="GROUP2_MARK 群二摘要", key="ep:g2",
                     tags=("note",), space_id=g2.space_id)
    remember_episode(memory, summary="MEMBER_MARK 别人的私人记忆", key="ep:mp",
                     tags=("note",), space_id=mem.space_id)
    remember_episode(memory, summary="VAULT_MARK 受保护原文记忆", key="ep:vault",
                     tags=("note",), space_id="vault-default")
    remember_episode(memory, summary="FORK_MARK 可分享副本", key="ep:fork",
                     tags=("note",), space_id=fork.space_id)
    remember_episode(memory, summary="EMPTY_MARK 未分区", key="ep:empty",
                     tags=("note",), space_id="")
    # 群时间线原文不得出现
    remember_episode(memory, summary="TIMELINE_MARK 原始群时间线",
                     key="timeline:group:g-one", tags=("chat",), space_id=g1.space_id)
    _ingest(rayneo, "眼镜转写：今天下午体检")
    return rayneo, memory


def test_query_scope_includes_owner_group_and_protected(tmp_path):
    rayneo, memory = _setup(tmp_path)
    resp = query_rayneo_context(
        rayneo, memory,
        {"source": "rayneo", "limit": 50, "include_derived": True,
         "max_text_chars": 4000, "max_total_tokens": 8000},
    )
    assert resp["ok"] is True
    assert resp["instruction_eligible"] is False
    assert resp["query_disabled"] is False
    texts = " ".join(str(it.get("text") or "") for it in resp["items"])
    assert "OWNER_MARK" in texts
    assert "GROUP1_MARK" in texts and "GROUP2_MARK" in texts
    assert "VAULT_MARK" in texts
    assert "眼镜转写" in texts
    assert "MEMBER_MARK" not in texts
    assert "FORK_MARK" not in texts
    assert "EMPTY_MARK" not in texts
    assert "TIMELINE_MARK" not in texts
    blob = json_blob(resp)
    for forbidden in ("space_id", "conversation_id", "uin", "g-one", "g-two",
                      "mp-other", "vault-default", "owner-working"):
        # space_type 值允许出现；space_id 字段名与具体 id 不得出现
        if forbidden == "space_id":
            assert '"space_id"' not in blob
        else:
            assert forbidden not in blob
    types = {it.get("space_type") for it in resp["items"] if it.get("origin") == "whale:episode"}
    assert types <= {"owner_working_copy", "group_space", "protected_original"}
    assert "member_private" not in types and "shareable_fork" not in types
    rayneo.close(); memory.close()


def json_blob(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_query_disabled_returns_envelope(tmp_path):
    rayneo, memory = _setup(tmp_path)
    resp = query_rayneo_context(
        rayneo, memory, {"source": "rayneo", "limit": 10}, query_disabled=True)
    assert resp["query_disabled"] is True and resp["items"] == [] and resp["ok"] is True
    rayneo.close(); memory.close()


def test_query_wrong_source_raises(tmp_path):
    rayneo, memory = _setup(tmp_path)
    try:
        query_rayneo_context(rayneo, memory, {"source": "other"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    rayneo.close(); memory.close()


def test_expired_omits_text(tmp_path):
    memory = SqliteMemoryStore(tmp_path / "m.sqlite3")
    ensure_owner_working_copy(memory)
    rayneo = RayneoContextStore(tmp_path / "r.sqlite3", raw_ttl_days=1)
    _ingest(rayneo, "即将过期")
    rayneo.expire_due(now_ms=1_000 + 86_400_000 + 5)
    resp = query_rayneo_context(rayneo, memory, {"source": "rayneo", "limit": 10})
    expired = [it for it in resp["items"] if it.get("expired")]
    assert expired and all("text" not in it for it in expired)
    rayneo.close(); memory.close()


def test_topic_and_segment_filter(tmp_path):
    memory = SqliteMemoryStore(tmp_path / "m.sqlite3")
    ensure_owner_working_copy(memory)
    rayneo = RayneoContextStore(tmp_path / "r.sqlite3")
    _ingest(rayneo, "下午要去体检", segment_id="s-exam")
    _ingest(rayneo, "晚上吃火锅", segment_id="s-hotpot")
    resp = query_rayneo_context(
        rayneo, memory, {"source": "rayneo", "topic": "体检", "limit": 10})
    texts = [it.get("text") for it in resp["items"]]
    assert any("体检" in (t or "") for t in texts)
    assert not any("火锅" in (t or "") for t in texts)
    resp = query_rayneo_context(
        rayneo, memory, {"source": "rayneo", "segment_id": "s-hotpot", "limit": 10})
    assert all(it.get("segment_id") == "s-hotpot" or it.get("origin") != "rayneo:live"
               for it in resp["items"] if it.get("text_kind") == "transcript")
    rayneo.close(); memory.close()


def test_contract_file_loads():
    import json
    from pathlib import Path
    raw = Path(__file__).resolve().parents[1] / "contracts" / "rayneo-context-v1.json"
    data = json.loads(raw.read_text())
    assert data["id"] == "rayneo-context/v1"
    assert data["query_response"]["instruction_eligible"] is False
    assert data["ingest_envelope"]["source"] == "rayneo"
