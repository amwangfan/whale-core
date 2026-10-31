"""Consistent full SQLite backups, including history, task and initiative state.

JSONL export is a portable memory export; this module is the complete local
recovery path. Restoring requires explicit trusted-host authorization. Never
restore a live runtime while tools or outbound workers are executing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime

from whale_core.memory.store import MemoryKind

from whale_core.memory.sqlite_store import SqliteMemoryStore


def _validate(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("backup failed SQLite integrity check")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
        raise ValueError("backup contains unsupported executable schema objects")
    expected = {
        "kv": {"key", "value"},
        "records": {"key", "kind", "content", "record_id", "created_at", "tags"},
    }
    for table, columns in expected.items():
        actual = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if not columns.issubset(actual):
            raise ValueError("backup has an incompatible memory schema")
    # Validate the serialized values before touching the target database.
    try:
        for row in db.execute("SELECT value FROM kv"):
            json.loads(row[0])
        for content, tags, kind, created_at in db.execute("SELECT content,tags,kind,created_at FROM records"):
            if not isinstance(json.loads(content), dict) or not isinstance(json.loads(tags), list):
                raise ValueError("backup contains malformed memory records")
            MemoryKind(kind)
            datetime.fromisoformat(created_at)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("backup contains malformed JSON") from exc


def create_backup(store: SqliteMemoryStore, destination: str | Path) -> Path:
    """Atomically write a mode-0600, integrity-checked SQLite snapshot."""
    if not isinstance(store, SqliteMemoryStore):
        raise TypeError("full backups require SqliteMemoryStore")
    target = Path(destination).expanduser().resolve()
    if target == Path(store.path).expanduser().resolve():
        raise ValueError("backup destination must differ from live database")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".whale-backup-", dir=target.parent)
    os.close(fd)
    try:
        with sqlite3.connect(temporary) as output, store._lock:
            store.db.backup(output)
            _validate(output)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def restore_backup(store: SqliteMemoryStore, source: str | Path) -> dict:
    """Validate then replace the contents of an existing store connection.

    The caller must stop inbound/active work and back up the current database
    before invoking this destructive restore. SQLite's backup API updates the
    open connection, so callers do not retain a stale connection after restore.
    """
    if not isinstance(store, SqliteMemoryStore):
        raise TypeError("full restore requires SqliteMemoryStore")
    path = Path(source).expanduser().resolve(strict=True)
    if path == Path(store.path).expanduser().resolve():
        raise ValueError("restore source must differ from live database")
    uri = path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as snapshot:
        _validate(snapshot)
        with store._lock:
            # An in-flight or interrupted action must never be replayed by a
            # restore. Mark running journals unknown before the host resumes.
            snapshot.backup(store.db)
            _validate(store.db)
            for key, value in store.db.execute("SELECT key,value FROM kv WHERE key LIKE 'task:%'").fetchall():
                record = json.loads(value)
                if isinstance(record, dict) and record.get("status") == "running":
                    record.update(status="unknown", error="restored-in-flight-action")
                    store.db.execute("UPDATE kv SET value=? WHERE key=?",
                                     (json.dumps(record, ensure_ascii=False), key))
            store.db.commit()
            return {"restored": True,
                    "records": store.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]}
