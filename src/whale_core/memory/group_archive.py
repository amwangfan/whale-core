"""Local, group-scoped archive of imported QQ messages.

The archive keeps the original source separate from curated memory.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
class GroupArchive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            group_id TEXT NOT NULL, message_id TEXT NOT NULL,
            sent_at TEXT NOT NULL, sender_id TEXT NOT NULL,
            sender_name TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY (group_id, message_id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_group_archive_time "
                        "ON messages(group_id, sent_at)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()
