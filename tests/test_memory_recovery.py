import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.memory.backup import create_backup, restore_backup
from whale_core.memory.history import append_turn, load_history, delete_conversation
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord


def test_full_backup_restores_history_tasks_and_memory_on_existing_connection(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.put_record(MemoryRecord(MemoryKind.PREFERENCE, "p", {"summary": "事实"}))
    append_turn(store, "c", "hello", "world")
    store.put("task:t", {"task_id": "t", "status": "running"})
    snapshot = create_backup(store, tmp_path / "backup.db")
    assert os.stat(snapshot).st_mode & 0o777 == 0o600
    store.delete("p")
    store.delete("chat:history:c")
    store.put("extra", "remove me")
    restored = restore_backup(store, snapshot)
    assert restored["records"] == 1
    assert store.get_record("p").content["summary"] == "事实"
    assert load_history(store, "c")[0]["content"] == "hello"
    assert store.get("extra") is None
    assert store.get("task:t")["status"] == "unknown"
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()


def test_bad_backup_leaves_live_memory_untouched(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.put("safe", "untouched")
    bad = tmp_path / "bad.db"
    with sqlite3.connect(bad) as db:
        db.execute("CREATE TABLE something (id TEXT)")
    with pytest.raises(ValueError): restore_backup(store, bad)
    assert store.get("safe") == "untouched"
    with pytest.raises(ValueError): create_backup(store, store.path)
    store.close()


def test_malformed_json_backup_rejected_before_live_write(tmp_path):
    source = SqliteMemoryStore(tmp_path / "source.db")
    source.db.execute("INSERT INTO kv VALUES ('bad', 'not JSON')")
    source.db.commit()
    live = SqliteMemoryStore(tmp_path / "live.db")
    live.put("p", "keep")
    with pytest.raises(ValueError): restore_backup(live, source.path)
    assert live.get("p") == "keep"
    source.close(); live.close()


def test_concurrent_history_append_does_not_lose_turns():
    store = InMemoryStore()
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: append_turn(store, "c", f"user{i}", f"answer{i}"), range(12)))
    history = load_history(store, "c")
    assert len(history) == 24
    for user, answer in zip(history[::2], history[1::2]):
        assert user["content"].removeprefix("user") == answer["content"].removeprefix("answer")


def test_same_conversation_model_history_is_serialized():
    import time
    class Brain:
        def __init__(self): self.histories = []
        def reply(self, text, **kwargs):
            self.histories.append(len(kwargs["history"]))
            time.sleep(.01)
            return "reply"
    brain = Brain()
    companion = WhaleCompanion(InMemoryStore(), brain)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: companion.chat(str(i), conversation_id="c"), range(4)))
    assert brain.histories == [0, 2, 4, 6]


def test_conversation_delete_removes_only_linked_history_and_episodes():
    class Brain:
        def reply(self, text, **kwargs): return "answer"
    store = InMemoryStore()
    companion = WhaleCompanion(store, Brain())
    companion.chat("secret", conversation_id="a")
    companion.chat("unrelated", conversation_id="b")
    store.put("runtime:task:t", {"conversation_id": "a", "text": "secret"})
    store.put("task:t", {"status": "answered"})
    result = delete_conversation(store, "a")
    assert result == {"history_deleted": True, "episodes_deleted": 1}
    assert not load_history(store, "a")
    assert load_history(store, "b")
    assert store.get("runtime:task:t") is None
    remaining = store.list_by_kind(MemoryKind.EPISODE)
    assert len(remaining) == 1 and "unrelated" in remaining[0].content["summary"]
