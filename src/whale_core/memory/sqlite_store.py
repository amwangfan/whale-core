"""Persistent local memory. Keep database on private disk, never commit it."""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from whale_core.memory.store import MemoryKind, MemoryRecord, PersonaContinuity

class SqliteMemoryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS records
          (key TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
           record_id TEXT NOT NULL, created_at TEXT NOT NULL, tags TEXT NOT NULL)""")
        self.db.commit()

    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False)
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, data))

    def delete(self, key: str) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM kv WHERE key=?", (key,))
            self.db.execute("DELETE FROM records WHERE key=?", (key,))

    def keys(self) -> list[str]:
        with self._lock:
            rows = self.db.execute("SELECT key FROM kv UNION SELECT key FROM records ORDER BY key").fetchall()
            return [row[0] for row in rows]

    def put_record(self, record: MemoryRecord) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)",
                (record.key, record.kind.value, json.dumps(dict(record.content), ensure_ascii=False),
                 record.record_id, record.created_at.isoformat(), json.dumps(record.tags, ensure_ascii=False)))

    def get_record(self, key: str) -> MemoryRecord | None:
        with self._lock:
            row = self.db.execute("SELECT key,kind,content,record_id,created_at,tags FROM records WHERE key=?", (key,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row: tuple) -> MemoryRecord:
        return MemoryRecord(key=row[0], kind=MemoryKind(row[1]), content=json.loads(row[2]),
                            record_id=row[3], created_at=datetime.fromisoformat(row[4]),
                            tags=tuple(json.loads(row[5])))

    def list_by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        with self._lock:
            rows = self.db.execute("SELECT key,kind,content,record_id,created_at,tags FROM records WHERE kind=? ORDER BY created_at DESC", (kind.value,)).fetchall()
        return [self._decode(row) for row in rows]

    def get_persona_continuity(self, key: str = "persona:default") -> PersonaContinuity | None:
        rec = self.get_record(key)
        return PersonaContinuity.from_record(rec) if rec and rec.kind == MemoryKind.PERSONA_CONTINUITY else None

    def put_persona_continuity(self, persona: PersonaContinuity, key: str = "persona:default") -> None:
        self.put_record(persona.as_record(key))

    def close(self) -> None:
        with self._lock:
            self.db.close()
