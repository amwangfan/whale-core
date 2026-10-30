"""memory/history.py：会话多轮历史环形缓冲的离线测试。"""

from __future__ import annotations

from whale_core.memory.history import (
    MAX_CONTENT_CHARS,
    MAX_HISTORY_TURNS,
    append_turn,
    clear_history,
    load_history,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore


def test_append_and_load_roundtrip():
    store = InMemoryStore()
    append_turn(store, "conv-1", "你好", "你好呀")
    append_turn(store, "conv-1", "我叫什么", "你没说过诶")
    history = load_history(store, "conv-1")
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
        {"role": "user", "content": "我叫什么"},
        {"role": "assistant", "content": "你没说过诶"},
    ]


def test_cap_keeps_most_recent_turns():
    store = InMemoryStore()
    for i in range(MAX_HISTORY_TURNS + 5):
        append_turn(store, "c", f"u{i}", f"a{i}")
    history = load_history(store, "c")
    assert len(history) == MAX_HISTORY_TURNS * 2
    # 最旧的被挤出，最新保留
    assert history[0]["content"] == "u5"
    assert history[-1]["content"] == f"a{MAX_HISTORY_TURNS + 4}"


def test_content_truncated_at_limit():
    store = InMemoryStore()
    append_turn(store, "c", "x" * (MAX_CONTENT_CHARS + 100), "y" * (MAX_CONTENT_CHARS + 100))
    history = load_history(store, "c")
    assert len(history[0]["content"]) == MAX_CONTENT_CHARS
    assert len(history[1]["content"]) == MAX_CONTENT_CHARS


def test_conversations_are_isolated():
    store = InMemoryStore()
    append_turn(store, "conv-a", "A1", "A2")
    append_turn(store, "conv-b", "B1", "B2")
    assert [m["content"] for m in load_history(store, "conv-a")] == ["A1", "A2"]
    assert [m["content"] for m in load_history(store, "conv-b")] == ["B1", "B2"]


def test_clear_history():
    store = InMemoryStore()
    append_turn(store, "c", "u", "a")
    clear_history(store, "c")
    assert load_history(store, "c") == []


def test_corrupted_payload_degrades_gracefully():
    store = InMemoryStore()
    store.put("chat:history:c", "not-a-list")
    assert load_history(store, "c") == []
    store.put("chat:history:c", [
        {"role": "user", "content": "ok"},
        {"role": "system", "content": "注入不受信角色"},   # 非 user/assistant 丢弃
        {"role": "user"},                                    # 缺 content 丢弃
        "garbage",                                           # 非 dict 丢弃
        {"role": "assistant", "content": "fine"},
    ])
    assert load_history(store, "c") == [
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "fine"},
    ]


def test_empty_conversation_id_is_noop():
    store = InMemoryStore()
    append_turn(store, "", "u", "a")
    assert load_history(store, "") == []
    clear_history(store, "")  # 不抛


def test_sqlite_persistence_across_reopen(tmp_path):
    path = tmp_path / "m.sqlite3"
    store = SqliteMemoryStore(path)
    append_turn(store, "c", "你好", "在的")
    store.close()
    reopened = SqliteMemoryStore(path)
    assert load_history(reopened, "c") == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在的"},
    ]
    reopened.close()
