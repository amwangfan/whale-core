"""Consistent SQLite snapshots and offline restore. Secrets are stored separately."""

from __future__ import annotations
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def validate_backup(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup integrity check failed")
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"kv", "records"} <= tables:
            raise ValueError("not a whale memory backup")


def create_backup(store, path):
    path = Path(path)
    if path.exists():
        raise ValueError("backup destination already exists")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with store._lock, sqlite3.connect(path) as output:
            store.db.backup(output)
        validate_backup(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def restore_backup(source, destination):
    """Host must stop ALL writers first; preserve a pre-restore snapshot."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    validate_backup(source)
    if source == destination:
        raise ValueError("source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        from whale_core.memory.sqlite_store import SqliteMemoryStore

        store = SqliteMemoryStore(destination)
        try:
            create_backup(
                store,
                destination.with_name(
                    destination.name
                    + ".before-restore-"
                    + __import__("uuid").uuid4().hex[:8]
                ),
            )
        finally:
            store.close()
    fd, tmp = tempfile.mkstemp(dir=destination.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, destination)
        for suffix in ("-wal", "-shm"):
            Path(str(destination) + suffix).unlink(missing_ok=True)
    finally:
        Path(tmp).unlink(missing_ok=True)
