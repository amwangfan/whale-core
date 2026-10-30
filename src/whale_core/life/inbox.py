"""入站 owner 消息的 SQLite 持久化 inbox（W09 issue #14）。

为什么：内存队列在「已向上游 ACK、尚未处理」窗口内崩溃会丢消息；DeepSeek /
存储失败也需要**可观察的受控重试**而不是静默吞掉。本模块提供持久化任务队列：

- ``enqueue``：按 ``message_id`` 主键幂等去重（上游至少一次投递 + 进程重启后
  重复推送都不会产生第二份任务 → 不会重复生成草稿 / 重复写记忆）；
- ``claim_next``：pending → processing + 租约（lease）；单工作线程消费；
- ``fail``：attempts+1，只记**错误类别**（异常类名），未达上限回 pending 重试，
  达上限落 failed（可观察、可 ``requeue`` 人工恢复）；
- ``recover_stale``：进程重启后把租约过期的 processing 收回 pending
  （崩溃在「已 claim 未处理」窗口的消息不丢）。

边界（与仓内既有约定一致）：
- 只有**已配对 owner** 的消息进 inbox；未配对内容不落盘（身份门边界）；
- 库里只存重放所需最小字段（message_id / conversation / account / text /
  attachments 附件元数据 JSON——**只存路径与 URL 等引用，不存字节本体**），
  不存 token；日志与 last_error 不含私聊正文；
- 入站处理按受控策略重试（attempts 上限）；**出站发送结果 unknown 仍不可
  盲目重发**——那是 ProactiveOutbox 的既有语义，本模块不触碰出站；
- 数据库文件放私有数据目录（与记忆库同规），永不入仓。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass(frozen=True)
class InboxJob:
    message_id: str
    conversation: str
    account: str
    text: str
    attachments: tuple = ()  # 附件元数据（{"kind","file_path","url"}）
    status: str = STATUS_PENDING
    attempts: int = 0
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""


class SqliteInbox:
    """SQLite 持久化任务队列（WAL）。单工作线程消费；锁保护多线程入队。"""

    def __init__(self, path: str | Path, *, max_attempts: int = 3,
                 lease_seconds: int = 300) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be >= 1")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS inbox (
                 message_id TEXT PRIMARY KEY,
                 conversation TEXT NOT NULL,
                 account TEXT NOT NULL,
                 text TEXT NOT NULL,
                 status TEXT NOT NULL DEFAULT 'pending',
                 attempts INTEGER NOT NULL DEFAULT 0,
                 last_error TEXT NOT NULL DEFAULT '',
                 created_at TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 lease_expires_at TEXT
               )"""
        )
        # 附件列迁移（老库就地升级；只存附件元数据 JSON 引用，不存字节）
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(inbox)")}
        if "attachments_json" not in cols:
            self.db.execute(
                "ALTER TABLE inbox ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()
        if self.path != ":memory:" and os.name == "posix":
            # Inbox contains private message text; do not leave the main DB world-readable.
            os.chmod(self.path, 0o600)

    # ── 入队 / 认领 ────────────────────────────────────────────

    def enqueue(self, *, message_id: str, conversation: str, account: str, text: str,
                attachments=()) -> bool:
        """幂等入队：新任务返回 True；message_id 已存在返回 False（不重置状态）。

        ``attachments`` 只接受可 JSON 序列化的附件元数据（路径/URL 引用），
        非法内容抛 TypeError——附件字节本体永不落库。
        """
        import json as _json

        mid = (message_id or "").strip()
        if not mid:
            raise ValueError("message_id required")
        attachments_json = _json.dumps(
            [dict(a) for a in (attachments or ())][:8], ensure_ascii=False)
        if len(attachments_json) > 20000:
            raise ValueError("attachments metadata too large")
        now = _iso(_utc_now())
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (mid, conversation, account, text, STATUS_PENDING, 0, "",
                 now, now, None, attachments_json),
            )
            return cur.rowcount > 0

    def claim_next(self) -> InboxJob | None:
        """认领最早的 pending 任务（→ processing + 租约）；无任务返回 None。"""
        now = _utc_now()
        lease = _iso(now + timedelta(seconds=self.lease_seconds))
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT message_id, conversation, account, text, status, attempts, "
                "last_error, created_at, updated_at, attachments_json FROM inbox "
                "WHERE status=? ORDER BY created_at, rowid LIMIT 1",
                (STATUS_PENDING,),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE inbox SET status=?, updated_at=?, lease_expires_at=? WHERE message_id=?",
                (STATUS_PROCESSING, _iso(now), lease, row[0]),
            )
        import json as _json

        try:
            attachments = tuple(_json.loads(row[9] or "[]"))
        except (ValueError, TypeError):
            attachments = ()
        return InboxJob(
            message_id=row[0],
            conversation=row[1],
            account=row[2],
            text=row[3],
            status=STATUS_PROCESSING,
            attempts=row[5],
            last_error=row[6],
            created_at=row[7],
            updated_at=row[8],
            attachments=attachments,
        )

    # ── 结果落账 ───────────────────────────────────────────────

    def succeed(self, message_id: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE inbox SET status=?, updated_at=?, lease_expires_at=NULL "
                "WHERE message_id=?",
                (STATUS_SUCCEEDED, _iso(_utc_now()), message_id),
            )

    def fail(self, message_id: str, *, error_class: str) -> str:
        """处理失败：attempts+1；未达上限回 pending（受控重试），否则 failed。

        ``error_class`` 只记异常**类名**——不落错误消息全文（可能含正文/凭据）。
        返回落账后的状态。
        """
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT attempts FROM inbox WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is None:
                return ""
            attempts = int(row[0]) + 1
            status = STATUS_FAILED if attempts >= self.max_attempts else STATUS_PENDING
            self.db.execute(
                "UPDATE inbox SET status=?, attempts=?, last_error=?, updated_at=?, "
                "lease_expires_at=NULL WHERE message_id=?",
                (status, attempts, error_class[:80], _iso(_utc_now()), message_id),
            )
            return status

    # ── 恢复 / 人工干预 ────────────────────────────────────────

    def recover_stale(self) -> int:
        """租约过期的 processing → pending（进程崩溃恢复）；返回收回数量。"""
        now = _iso(_utc_now())
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE inbox SET status=?, updated_at=?, lease_expires_at=NULL "
                "WHERE status=? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (STATUS_PENDING, now, STATUS_PROCESSING, now),
            )
            return cur.rowcount

    def requeue(self, message_id: str) -> bool:
        """failed → pending 人工恢复；非 failed 状态返回 False。"""
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE inbox SET status=?, updated_at=?, lease_expires_at=NULL "
                "WHERE message_id=? AND status=?",
                (STATUS_PENDING, _iso(_utc_now()), message_id, STATUS_FAILED),
            )
            return cur.rowcount > 0

    def requeue_all_failed(self) -> int:
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE inbox SET status=?, updated_at=?, lease_expires_at=NULL WHERE status=?",
                (STATUS_PENDING, _iso(_utc_now()), STATUS_FAILED),
            )
            return cur.rowcount

    # ── 观测 ───────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.db.execute("SELECT status, COUNT(*) FROM inbox GROUP BY status").fetchall()
        out = {s: 0 for s in (STATUS_PENDING, STATUS_PROCESSING, STATUS_SUCCEEDED, STATUS_FAILED)}
        out.update({str(s): int(n) for s, n in rows})
        return out

    def failed_ids(self, limit: int = 50) -> list[tuple[str, int, str]]:
        """(message_id, attempts, last_error) 列表——不含正文。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT message_id, attempts, last_error FROM inbox "
                "WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (STATUS_FAILED, int(limit)),
            ).fetchall()
        return [(str(r[0]), int(r[1]), str(r[2])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.db.close()
