"""Persistent local memory. Keep database on private disk, never commit it.

S2（Whale Studio）：records 增加 space_id 列与 (space_id, kind) 索引；
引入 PRAGMA user_version 迁移机制（v0→v1）。所有 list_by_kind 列举在
SQL 层限域，且 protected_original 空间在存储层即被排除（MEM-HARD-01）。
"""
from __future__ import annotations
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from whale_core.memory.store import MemoryKind, MemoryRecord, PersonaContinuity

_SCHEMA_VERSION = 1

# S2：迁移遗留记录（space_id=''）默认收敛进的受保护原始库 id。
DEFAULT_VAULT_ID = "vault-default"


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
        self._migrate()
        if self.path != ':memory:':
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(self.path + suffix)
                if candidate.exists():
                    os.chmod(candidate, 0o600)

    # -- S2：schema 与数据迁移（幂等，打开即校验） -------------------------

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= _SCHEMA_VERSION:
            return
        with self._lock, self.db:
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(records)")}
            if "space_id" not in columns:
                self.db.execute(
                    "ALTER TABLE records ADD COLUMN space_id TEXT NOT NULL DEFAULT ''")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_space_kind "
                "ON records(space_id, kind)")
            # 存量记录 → 受保护原始库（最小权限；不给任何会话自动授权）。
            # 仅当确有遗留记录时创建 vault 元数据——空库不引入多余键。
            unspaced = self.db.execute(
                "SELECT COUNT(*) FROM records WHERE space_id=''").fetchone()[0]
            if unspaced:
                self._ensure_vault_meta_locked()
                self.db.execute("UPDATE records SET space_id=? WHERE space_id=''",
                                (DEFAULT_VAULT_ID,))
            self.db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _ensure_vault_meta_locked(self) -> None:
        if self.db.execute("SELECT 1 FROM kv WHERE key=?",
                           (f"space:{DEFAULT_VAULT_ID}",)).fetchone():
            return
        meta = {
            "space_id": DEFAULT_VAULT_ID,
            "type": "protected_original",
            "name": "默认私人原始库",
            "owner": "owner",
            "version": 1,
            "created_at": datetime.now().isoformat(),
            "retired_at": None,
        }
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                        (f"space:{DEFAULT_VAULT_ID}",
                         json.dumps(meta, ensure_ascii=False)))

    def _protected_space_ids(self) -> set[str]:
        """protected_original 空间集合（kv 元数据驱动；存储层硬排除依据）。"""
        rows = self.db.execute(
            "SELECT value FROM kv WHERE key LIKE 'space:%'").fetchall()
        protected: set[str] = set()
        for (raw,) in rows:
            try:
                meta = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(meta, dict) and meta.get("type") == "protected_original":
                protected.add(str(meta.get("space_id") or ""))
        protected.discard("")
        return protected

    # -- 基础 KV ----------------------------------------------------------

    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
            # 回落 records 表，返回与 InMemoryStore 相同的窥视视图形状
            row = self.db.execute(
                "SELECT kind,content,record_id,created_at,tags,space_id FROM records WHERE key=?",
                (key,),
            ).fetchone()
            if row:
                return {
                    "kind": row[0],
                    "content": json.loads(row[1]),
                    "record_id": row[2],
                    "created_at": row[3],
                    "tags": json.loads(row[4]),
                    "space_id": row[5],
                }
            return None

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

    def transact(self, fn):
        """S4：单事务执行 fn(tx)（BEGIN IMMEDIATE），tx 提供 kv get/put。

        校验+核销+签发在同一事务内提交，并发/崩溃不会留下半状态
        （INV-05 的 SQLite 落地）。
        """
        store = self

        class _Tx:
            def get(self, key: str):
                row = store.db.execute(
                    "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else None

            def put(self, key: str, value) -> None:
                store.db.execute(
                    "INSERT OR REPLACE INTO kv VALUES (?,?)",
                    (key, json.dumps(value, ensure_ascii=False)))

        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = fn(_Tx())
                self.db.commit()
                return result
            except BaseException:
                self.db.rollback()
                raise

    # -- 记忆记录 ----------------------------------------------------------

    def put_record(self, record: MemoryRecord) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,?)",
                (record.key, record.kind.value, json.dumps(dict(record.content), ensure_ascii=False),
                 record.record_id, record.created_at.isoformat(),
                 json.dumps(record.tags, ensure_ascii=False), record.space_id))

    def get_record(self, key: str) -> MemoryRecord | None:
        """按键直接读取（管理/Fork 管道用；召回路径一律走 list_by_kind 限域）。"""
        with self._lock:
            row = self.db.execute(
                "SELECT key,kind,content,record_id,created_at,tags,space_id FROM records WHERE key=?",
                (key,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row: tuple) -> MemoryRecord:
        return MemoryRecord(key=row[0], kind=MemoryKind(row[1]), content=json.loads(row[2]),
                            record_id=row[3], created_at=datetime.fromisoformat(row[4]),
                            tags=tuple(json.loads(row[5])), space_id=row[6])

    def list_by_kind(
        self, kind: MemoryKind, *, space_ids: tuple[str, ...] | None = None
    ) -> list[MemoryRecord]:
        """按类别列举，SQL 层限域（S2：查询前隔离，不做事后过滤）。

        - space_ids=None：全部非受保护空间（兼容期；vault 仍被排除）；
        - space_ids=()：空集，fail closed 返回空；
        - 显式集合：先剔除受保护空间（纵深防御），再 IN 过滤。
        """
        with self._lock:
            protected = self._protected_space_ids()
            if space_ids is None:
                if not protected:
                    rows = self.db.execute(
                        "SELECT key,kind,content,record_id,created_at,tags,space_id "
                        "FROM records WHERE kind=? ORDER BY created_at DESC, rowid ASC",
                        (kind.value,)).fetchall()
                else:
                    marks = ",".join("?" for _ in protected)
                    rows = self.db.execute(
                        f"SELECT key,kind,content,record_id,created_at,tags,space_id "
                        f"FROM records WHERE kind=? AND space_id NOT IN ({marks}) "
                        "ORDER BY created_at DESC, rowid ASC",
                        (kind.value, *sorted(protected))).fetchall()
            else:
                allowed = [s for s in dict.fromkeys(space_ids) if s not in protected]
                if not allowed:
                    return []
                marks = ",".join("?" for _ in allowed)
                rows = self.db.execute(
                    f"SELECT key,kind,content,record_id,created_at,tags,space_id "
                    f"FROM records WHERE kind=? AND space_id IN ({marks}) "
                    "ORDER BY created_at DESC, rowid ASC",
                    (kind.value, *allowed)).fetchall()
        return [self._decode(row) for row in rows]

    def get_persona_continuity(self, key: str = "persona:default") -> PersonaContinuity | None:
        rec = self.get_record(key)
        return PersonaContinuity.from_record(rec) if rec and rec.kind == MemoryKind.PERSONA_CONTINUITY else None

    def put_persona_continuity(self, persona: PersonaContinuity, key: str = "persona:default") -> None:
        self.put_record(persona.as_record(key))

    def close(self) -> None:
        with self._lock:
            self.db.close()
