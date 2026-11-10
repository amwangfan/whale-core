"""Rayneo 眼镜上下文存储与查询。

本模块实现 RayneoContextStore（独立 SQLite 库）与纯函数
query_rayneo_context。不依赖 life/model 层，仅用标准库。

安全约束：
- 查询仅允许 owner-working / GROUP_SPACE / PROTECTED_ORIGINAL 空间。
- 禁止 MEMBER_PRIVATE / SHAREABLE_FORK / space_id=''。
- 响应绝不返回 space_id / 群号 / uin / conversation_id。
- 日志不得包含 text / token。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rayneo_source (
  source_instance TEXT PRIMARY KEY,
  first_seen_ms   INTEGER NOT NULL,
  last_ingest_ms  INTEGER NOT NULL,
  last_client_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rayneo_segment (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  dedup_key       TEXT NOT NULL UNIQUE,
  source_instance TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('transcript','official_summary')),
  origin          TEXT NOT NULL CHECK (origin IN ('live','cache')),
  segment_id      TEXT NOT NULL,
  round_id        TEXT, role TEXT, session TEXT,
  revision        INTEGER NOT NULL DEFAULT 1,
  text            TEXT,
  text_hash       TEXT NOT NULL,
  title           TEXT, key_points_json TEXT,
  source_time_ms  INTEGER,
  received_ms     INTEGER NOT NULL,
  ingested_ms     INTEGER NOT NULL,
  expires_ms      INTEGER,
  state           TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live','expired','tombstone')),
  dup_of          INTEGER,
  sum_state       TEXT NOT NULL DEFAULT 'pending'
                  CHECK (sum_state IN ('pending','queued','done','skipped','failed','quarantined')),
  sum_job_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_seg_time ON rayneo_segment(received_ms);
CREATE INDEX IF NOT EXISTS idx_seg_sum ON rayneo_segment(sum_state, received_ms);
CREATE INDEX IF NOT EXISTS idx_seg_hash ON rayneo_segment(text_hash, received_ms);
CREATE INDEX IF NOT EXISTS idx_seg_expire ON rayneo_segment(expires_ms) WHERE expires_ms IS NOT NULL;
CREATE TABLE IF NOT EXISTS rayneo_summary_job (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_start_ms INTEGER NOT NULL,
  window_end_ms INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','done','failed','quarantined')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try_ms INTEGER NOT NULL DEFAULT 0,
  last_error_class TEXT,
  episode_key TEXT,
  segment_ids_json TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  finished_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_job_state ON rayneo_summary_job(state, next_try_ms);
CREATE TABLE IF NOT EXISTS rayneo_kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_KINDS = ("transcript", "official_summary")
_ORIGINS = ("live", "cache")
_MAX_TEXT_CHARS = 8000
_MAX_DEDUP_KEY = 256
_DUP_WINDOW_MS = 10 * 60 * 1000  # 跨 origin 同文去重窗口 ±10 分钟
_FRESH_RECENT_MS = 5 * 60 * 1000  # freshness recent 阈值 5 分钟
_QUERY_HARD_TIMEOUT_S = 3.0
_INGEST_ENABLED_KEY = "rayneo:ingest_enabled"

# 可见 ASCII（0x21-0x7E）
_DEDUP_RE = re.compile(r"^[\x21-\x7e]+$")
# 允许的控制字符：\t \n \r；其余 C0/C1 控制字符一律拒绝
_BAD_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# 允许查询的空间类型（fail-closed）
ALLOWED_SPACE_TYPES = ("owner_working_copy", "group_space", "protected_original")
# 明确禁止的空间类型
FORBIDDEN_SPACE_TYPES = ("member_private", "shareable_fork")

# 需要从 episode.content 中剔除的敏感字段
_SENSITIVE_CONTENT_KEYS = (
    "conversation_id",
    "uin",
    "space_id",
    "group",
    "group_id",
    "group_uin",
    "guild_id",
    "channel_id",
)
# 群空间只读 episode 中需要跳过的 key 前缀
_GROUP_SKIP_KEY_PREFIXES = ("timeline:", "chat:history:")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _chmod_600(path: Path) -> None:
    """尽力把文件权限设为 0600（含 -wal/-shm）。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        try:
            if p.exists():
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def _default_store_path() -> Path:
    """默认路径：<dirname(WHALE_GLASSES_STORE)>/rayneo_context.sqlite3。"""
    env = os.environ.get("WHALE_RAYNEO_STORE")
    if env:
        return Path(env)
    glasses = os.environ.get("WHALE_GLASSES_STORE", "glasses.sqlite3")
    base = Path(glasses).expanduser().resolve().parent
    return base / "rayneo_context.sqlite3"


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_ms(value: Any) -> Optional[int]:
    """宽松解析毫秒时间戳；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            v = int(value)
            return v if v >= 0 else None
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            v = int(float(s))
            return v if v >= 0 else None
        except (ValueError, OverflowError):
            pass
        # ISO-8601 简单支持
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _utf16_units(text: str) -> int:
    """UTF-16 编码单元数（代理对计 2）。"""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _token_cost(text: str) -> int:
    """预算 token 估算：ceil(UTF-16 单元 / 4)。"""
    if not text:
        return 0
    return max(1, math.ceil(_utf16_units(text) / 4))


def _cjk_terms(text: str) -> set[str]:
    """复制 memory.curation._terms：中文单字 + 二元组 + 拉丁/数字词。"""
    tokens = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", (text or "").lower())
    parts: set[str] = set()
    for tok in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", tok):
            parts.update(tok)
            parts.update(tok[i:i + 2] for i in range(max(0, len(tok) - 1)))
        else:
            parts.add(tok)
    return parts


def _sanitize_content(content: Any) -> Any:
    """递归剔除 episode.content 中的敏感字段。"""
    if isinstance(content, dict):
        out: dict[str, Any] = {}
        for k, v in content.items():
            kl = str(k).lower()
            if kl in _SENSITIVE_CONTENT_KEYS:
                continue
            out[k] = _sanitize_content(v)
        return out
    if isinstance(content, list):
        return [_sanitize_content(v) for v in content]
    return content


# ---------------------------------------------------------------------------
# RayneoContextStore
# ---------------------------------------------------------------------------

class RayneoContextStore:
    """Rayneo 眼镜上下文独立存储（SQLite，WAL）。"""

    def __init__(self, path: str | Path, *, raw_ttl_days: int = 90) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_ttl_days = int(raw_ttl_days)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path), timeout=5.0, isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        _chmod_600(self._path)

    # -- 生命周期 ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "RayneoContextStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- ingest ------------------------------------------------------------

    def ingest_batch(
        self, source_instance: str, items: list[dict], now_ms: int
    ) -> list[dict]:
        """单事务批量写入。不做网络/模型调用。

        每条结果包含 client_seq/dedup_key/segment_id/revision/effect，
        以及 raw_id/canonical_id（形如 rs-<id>）。
        """
        results: list[dict] = []
        if not isinstance(items, list):
            items = []
        src = str(source_instance or "")
        cur = self._conn
        with self._lock:
            try:
                cur.execute("BEGIN IMMEDIATE")
                max_seq = 0
                for item in items:
                    if not isinstance(item, dict):
                        item = {}
                    res = self._ingest_one(cur, src, item, now_ms)
                    seq = item.get("client_seq")
                    if isinstance(seq, (int, float)) and not isinstance(seq, bool):
                        max_seq = max(max_seq, int(seq))
                    results.append(res)
                self._touch_source(cur, src, now_ms, max_seq)
                cur.execute("COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return results

    def _touch_source(
        self, cur: sqlite3.Connection, src: str, now_ms: int, max_seq: int
    ) -> None:
        cur.execute(
            """
            INSERT INTO rayneo_source(source_instance, first_seen_ms, last_ingest_ms, last_client_seq)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_instance) DO UPDATE SET
              last_ingest_ms = excluded.last_ingest_ms,
              last_client_seq = MAX(rayneo_source.last_client_seq, excluded.last_client_seq)
            """,
            (src, now_ms, now_ms, max_seq),
        )

    def _ingest_one(
        self, cur: sqlite3.Connection, src: str, item: dict, now_ms: int
    ) -> dict:
        client_seq = item.get("client_seq")
        dedup_key = str(item.get("dedup_key") or "")
        segment_id = str(item.get("segment_id") or "")
        kind = str(item.get("kind") or "")
        origin = str(item.get("origin") or "")
        revision = item.get("revision", 1)
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            revision = 1

        base: dict[str, Any] = {
            "client_seq": client_seq,
            "dedup_key": dedup_key,
            "segment_id": segment_id,
            "revision": revision,
        }

        def reject(reason: str) -> dict:
            out = dict(base)
            out["effect"] = f"rejected: {reason}"
            out["reason"] = reason
            out["raw_id"] = None
            out["canonical_id"] = None
            return out

        # 基本校验
        if kind not in _KINDS:
            return reject("bad_kind")
        if origin not in _ORIGINS:
            return reject("bad_origin")
        if (
            not dedup_key
            or len(dedup_key) > _MAX_DEDUP_KEY
            or not _DEDUP_RE.match(dedup_key)
        ):
            return reject("bad_dedup_key")

        text = item.get("text")
        if text is None:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        if not text.strip():
            return reject("empty_text")
        if len(text) > _MAX_TEXT_CHARS:
            return reject("text_too_long")
        if _BAD_CTRL_RE.search(text):
            return reject("control_chars")

        # hash 校验
        expected_hash = _sha256_text(text)
        content_hash = str(item.get("content_hash") or "")
        if content_hash != expected_hash:
            return reject("hash_mismatch")

        # received_at 解析
        received_fixed = False
        received_ms = _parse_ms(
            item.get("received_at", item.get("received_ms"))
        )
        if received_ms is None:
            received_ms = now_ms
            received_fixed = True
        source_time_ms = _parse_ms(
            item.get("source_time_ms", item.get("source_time"))
        )

        # 幂等：dedup_key 已存在？
        row = cur.execute(
            "SELECT id, revision, text_hash, state FROM rayneo_segment WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()

        title = item.get("title")
        key_points = item.get("key_points")
        key_points_json = (
            json.dumps(key_points, ensure_ascii=False)
            if key_points is not None
            else None
        )
        expires_ms = (
            now_ms + self._raw_ttl_days * 86400000
            if self._raw_ttl_days > 0
            else None
        )

        if row is not None:
            existing_id = int(row["id"])
            if row["state"] == "tombstone":
                return reject("tombstoned")
            old_rev = int(row["revision"])
            old_hash = str(row["text_hash"])
            if revision > old_rev:
                cur.execute(
                    """
                    UPDATE rayneo_segment SET
                      kind=?, origin=?, segment_id=?, round_id=?, role=?, session=?,
                      revision=?, text=?, text_hash=?, title=?, key_points_json=?,
                      source_time_ms=?, received_ms=?, ingested_ms=?, expires_ms=?,
                      state='live', dup_of=NULL, sum_state='pending', sum_job_id=NULL
                    WHERE id=?
                    """,
                    (
                        kind,
                        origin,
                        segment_id,
                        item.get("round_id"),
                        item.get("role"),
                        item.get("session"),
                        revision,
                        text,
                        expected_hash,
                        title,
                        key_points_json,
                        source_time_ms,
                        received_ms,
                        now_ms,
                        expires_ms,
                        existing_id,
                    ),
                )
                out = dict(base)
                out.update(
                    {
                        "effect": "revision_applied",
                        "raw_id": f"rs-{existing_id}",
                        "canonical_id": f"rs-{existing_id}",
                    }
                )
                if received_fixed:
                    out["received_at_fixed"] = True
                return out
            if revision == old_rev and old_hash == expected_hash:
                out = dict(base)
                out.update(
                    {
                        "effect": "replay_idempotent",
                        "raw_id": f"rs-{existing_id}",
                        "canonical_id": f"rs-{existing_id}",
                    }
                )
                if received_fixed:
                    out["received_at_fixed"] = True
                return out
            # revision 更小，或同 revision 不同 hash
            out = dict(base)
            out.update(
                {
                    "effect": "stale_ignored",
                    "raw_id": f"rs-{existing_id}",
                    "canonical_id": f"rs-{existing_id}",
                }
            )
            if received_fixed:
                out["received_at_fixed"] = True
            return out

        # 新插入
        cur.execute(
            """
            INSERT INTO rayneo_segment(
              dedup_key, source_instance, kind, origin, segment_id,
              round_id, role, session, revision, text, text_hash,
              title, key_points_json, source_time_ms, received_ms,
              ingested_ms, expires_ms, state, dup_of, sum_state, sum_job_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'live',NULL,'pending',NULL)
            """,
            (
                dedup_key,
                src,
                kind,
                origin,
                segment_id,
                item.get("round_id"),
                item.get("role"),
                item.get("session"),
                revision,
                text,
                expected_hash,
                title,
                key_points_json,
                source_time_ms,
                received_ms,
                now_ms,
                expires_ms,
            ),
        )
        new_id = int(cur.execute("SELECT last_insert_rowid()").fetchone()[0])

        # 跨 origin 同文去重：±10 分钟内保留较早一条
        canonical_id = new_id
        dup_row = cur.execute(
            """
            SELECT id, received_ms FROM rayneo_segment
            WHERE text_hash = ? AND id != ? AND state = 'live' AND dup_of IS NULL
              AND received_ms BETWEEN ? AND ?
            ORDER BY received_ms ASC, id ASC
            LIMIT 1
            """,
            (
                expected_hash,
                new_id,
                received_ms - _DUP_WINDOW_MS,
                received_ms + _DUP_WINDOW_MS,
            ),
        ).fetchone()
        if dup_row is not None:
            other_id = int(dup_row["id"])
            other_ms = int(dup_row["received_ms"])
            if other_ms <= received_ms:
                # 已有更早的：新条标 dup
                cur.execute(
                    "UPDATE rayneo_segment SET dup_of=?, sum_state='skipped' WHERE id=?",
                    (other_id, new_id),
                )
                canonical_id = other_id
            else:
                # 新条更早：对方标 dup
                cur.execute(
                    "UPDATE rayneo_segment SET dup_of=?, sum_state='skipped' WHERE id=?",
                    (new_id, other_id),
                )
                canonical_id = new_id

        out = dict(base)
        out.update(
            {
                "effect": "ingested",
                "raw_id": f"rs-{new_id}",
                "canonical_id": f"rs-{canonical_id}",
            }
        )
        if received_fixed:
            out["received_at_fixed"] = True
        return out

    # -- 过期 / 删除 --------------------------------------------------------

    def expire_due(self, now_ms: int) -> int:
        """到期条目：text=NULL, state='expired'（不删除行）。"""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE rayneo_segment
                SET text=NULL, state='expired'
                WHERE state='live' AND expires_ms IS NOT NULL AND expires_ms <= ?
                """,
                (now_ms,),
            )
        return int(cur.rowcount if cur.rowcount is not None else 0)

    def delete_range(self, from_ms: int, to_ms: int) -> tuple[int, list[str]]:
        """真删区间内文本，写墓碑（保留 dedup_key）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, segment_id FROM rayneo_segment
                WHERE state IN ('live','expired')
                  AND COALESCE(source_time_ms, received_ms) BETWEEN ? AND ?
                """,
                (int(from_ms), int(to_ms)),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            seg_ids = [str(r["segment_id"]) for r in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""
                    UPDATE rayneo_segment
                    SET text=NULL, state='tombstone', sum_state='skipped'
                    WHERE id IN ({marks})
                    """,
                    ids,
                )
        return len(ids), seg_ids

    def purge_all(self) -> tuple[int, list[str]]:
        """全部原文改墓碑。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, segment_id FROM rayneo_segment WHERE state IN ('live','expired')"
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            seg_ids = [str(r["segment_id"]) for r in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""
                    UPDATE rayneo_segment
                    SET text=NULL, state='tombstone', sum_state='skipped'
                    WHERE id IN ({marks})
                    """,
                    ids,
                )
        return len(ids), seg_ids

    def get_ingest_enabled(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM rayneo_kv WHERE key=?", (_INGEST_ENABLED_KEY,)
            ).fetchone()
        if row is None:
            return True
        return str(row["value"]).lower() in ("1", "true", "yes", "on")

    def set_ingest_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rayneo_kv(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_INGEST_ENABLED_KEY, "1" if enabled else "0"),
            )

    def query_segments(
        self,
        *,
        segment_id: str | None = None,
        from_ms: int | None = None,
        to_ms: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """查询片段：跳过 tombstone 与 dup；expired 省略 text。"""
        sql = [
            "SELECT * FROM rayneo_segment",
            "WHERE state != 'tombstone' AND dup_of IS NULL",
        ]
        params: list[Any] = []
        if segment_id:
            sql.append("AND segment_id = ?")
            params.append(str(segment_id))
        if from_ms is not None:
            sql.append("AND COALESCE(source_time_ms, received_ms) >= ?")
            params.append(int(from_ms))
        if to_ms is not None:
            sql.append("AND COALESCE(source_time_ms, received_ms) <= ?")
            params.append(int(to_ms))
        sql.append("ORDER BY COALESCE(source_time_ms, received_ms) DESC, id DESC")
        sql.append("LIMIT ?")
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(" ".join(sql), params).fetchall()
        return [self._row_to_public(r) for r in rows]

    def _row_to_public(self, r: sqlite3.Row) -> dict:
        state = str(r["state"])
        expired = state == "expired"
        out: dict[str, Any] = {
            "id": int(r["id"]),
            "raw_id": f"rs-{r['id']}",
            "dedup_key": r["dedup_key"],
            "segment_id": r["segment_id"],
            "kind": r["kind"],
            "origin": r["origin"],
            "revision": int(r["revision"]),
            "round_id": r["round_id"],
            "role": r["role"],
            "session": r["session"],
            "title": r["title"],
            "source_time_ms": r["source_time_ms"],
            "source_time_unknown": r["source_time_ms"] is None,
            "received_ms": int(r["received_ms"]),
            "ingested_ms": int(r["ingested_ms"]),
            "state": state,
            "sum_state": r["sum_state"],
            "sum_job_id": r["sum_job_id"],
            "dup_of": r["dup_of"],
            "text_hash": r["text_hash"],
        }
        if r["key_points_json"]:
            try:
                out["key_points"] = json.loads(r["key_points_json"])
            except (ValueError, TypeError):
                out["key_points"] = None
        else:
            out["key_points"] = None
        if expired:
            out["expired"] = True
        else:
            out["text"] = r["text"]
        return out

    def pending_transcripts(self, limit: int = 200) -> list[dict]:
        """待总结的 live transcript（跳过 dup/tombstone/expired）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM rayneo_segment
                WHERE kind='transcript' AND state='live' AND dup_of IS NULL
                  AND sum_state='pending'
                ORDER BY COALESCE(source_time_ms, received_ms) ASC, id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_public(r) for r in rows]

    list_pending_transcripts = pending_transcripts

    def pending_official_summaries(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM rayneo_segment
                WHERE kind='official_summary' AND state='live' AND dup_of IS NULL
                  AND sum_state='pending'
                ORDER BY COALESCE(source_time_ms, received_ms) ASC, id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_public(r) for r in rows]

    def get_segments_by_ids(self, ids: Iterable[int]) -> list[dict]:
        vals = [int(i) for i in ids]
        if not vals:
            return []
        marks = ",".join("?" for _ in vals)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM rayneo_segment WHERE id IN ({marks})",
                vals,
            ).fetchall()
        found = {int(r["id"]): self._row_to_public(r) for r in rows}
        return [found[i] for i in vals if i in found]

    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM rayneo_kv WHERE key=?", (str(key),)).fetchone()
        return None if row is None else str(row["value"])

    def kv_put(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rayneo_kv(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )

    def mark_dup(self, dup_id: int, canonical_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE rayneo_segment SET dup_of=?, sum_state='skipped' WHERE id=?",
                (int(canonical_id), int(dup_id)),
            )

    def create_summary_job(
        self,
        segment_ids: Iterable[int],
        *,
        window_start_ms: int,
        window_end_ms: int,
        now_ms: int | None = None,
    ) -> int:
        ts = _now_ms() if now_ms is None else int(now_ms)
        ids = [int(i) for i in segment_ids]
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO rayneo_summary_job(
                  window_start_ms, window_end_ms, state, attempts, next_try_ms,
                  segment_ids_json, created_ms)
                VALUES (?, ?, 'queued', 0, 0, ?, ?)
                """,
                (int(window_start_ms), int(window_end_ms), json.dumps(ids), ts),
            )
            job_id = int(cur.lastrowid)
            if ids:
                marks = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE rayneo_segment SET sum_state='queued', sum_job_id=? WHERE id IN ({marks})",
                    [job_id, *ids],
                )
        return job_id

    def list_due_jobs(self, now_ms: int, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM rayneo_summary_job
                WHERE state='queued' AND next_try_ms <= ?
                ORDER BY id ASC LIMIT ?
                """,
                (int(now_ms), max(1, int(limit))),
            ).fetchall()
        out = []
        for r in rows:
            try:
                ids = json.loads(r["segment_ids_json"])
            except (ValueError, TypeError):
                ids = []
            out.append(
                {
                    "id": int(r["id"]),
                    "window_start_ms": int(r["window_start_ms"]),
                    "window_end_ms": int(r["window_end_ms"]),
                    "segment_ids": ids,
                    "state": r["state"],
                    "next_try_ms": int(r["next_try_ms"]),
                    "attempts": int(r["attempts"]),
                    "last_error_class": r["last_error_class"],
                    "episode_key": r["episode_key"],
                    "created_ms": int(r["created_ms"]),
                    "finished_ms": r["finished_ms"],
                }
            )
        return out

    def update_job(
        self,
        job_id: int,
        *,
        state: str | None = None,
        attempts: int | None = None,
        last_error_class: str | None = None,
        next_try_ms: int | None = None,
        episode_key: str | None = None,
        finished: bool = False,
        now_ms: int | None = None,
    ) -> None:
        ts = _now_ms() if now_ms is None else int(now_ms)
        sets: list[str] = []
        params: list[Any] = []
        if state is not None:
            sets.append("state=?")
            params.append(str(state))
        if attempts is not None:
            sets.append("attempts=?")
            params.append(int(attempts))
        if last_error_class is not None:
            # 只记错误类，不把模型原文/转写写进去
            sets.append("last_error_class=?")
            params.append(str(last_error_class)[:80])
        if next_try_ms is not None:
            sets.append("next_try_ms=?")
            params.append(int(next_try_ms))
        if episode_key is not None:
            sets.append("episode_key=?")
            params.append(str(episode_key)[:200])
        if finished:
            sets.append("finished_ms=?")
            params.append(ts)
        if not sets:
            return
        params.append(int(job_id))
        with self._lock:
            self._conn.execute(
                f"UPDATE rayneo_summary_job SET {', '.join(sets)} WHERE id=?",
                params,
            )

    def mark_segments_sum_state(
        self,
        segment_ids: Iterable[int],
        state: str,
        *,
        job_id: int | None = None,
    ) -> None:
        ids = [int(i) for i in segment_ids]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        with self._lock:
            if job_id is None:
                self._conn.execute(
                    f"UPDATE rayneo_segment SET sum_state=? WHERE id IN ({marks})",
                    [str(state), *ids],
                )
            else:
                self._conn.execute(
                    f"UPDATE rayneo_segment SET sum_state=?, sum_job_id=? WHERE id IN ({marks})",
                    [str(state), int(job_id), *ids],
                )

    def backup_to(self, destination: str | Path) -> Path:
        """使用 sqlite backup API 备份；目标 chmod 0600。不并入 memory/backup.py。"""
        dest = Path(destination).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(target)
        finally:
            target.close()
        _chmod_600(dest)
        return dest


# ---------------------------------------------------------------------------
# query_rayneo_context
# ---------------------------------------------------------------------------

def _allowed_space_map(memory_store: Any) -> dict[str, str]:
    """space_id -> space_type。仅按 SpaceStore 元数据分类，禁止前缀猜测。

    永远并入 owner-working；space_id='' 永不进入。
    列举失败时仍保留 owner-working（原文查询不依赖记忆空间）。
    """
    from whale_core.memory.spaces import OWNER_WORKING_SPACE_ID, SpaceStore
    from whale_core.memory.types import MemorySpaceType

    allowed_types = {
        MemorySpaceType.OWNER_WORKING_COPY,
        MemorySpaceType.GROUP_SPACE,
        MemorySpaceType.PROTECTED_ORIGINAL,
    }
    mapping: dict[str, str] = {OWNER_WORKING_SPACE_ID: MemorySpaceType.OWNER_WORKING_COPY.value}
    if memory_store is None:
        return mapping
    try:
        refs = SpaceStore(memory_store).list_spaces(include_protected=True)
    except Exception:
        return mapping
    for ref in refs:
        sid = str(getattr(ref, "space_id", "") or "")
        if not sid:
            continue
        typ = getattr(ref, "type", None)
        if typ in allowed_types:
            mapping[sid] = typ.value
    mapping.pop("", None)
    return mapping


def _record_fields(rec: Any) -> dict[str, Any]:
    """把 MemoryRecord 或 dict 归一成查询用字段。"""
    if rec is None:
        return {}
    if isinstance(rec, dict):
        content = rec.get("content") or {}
        created = rec.get("created_at")
        key = rec.get("key") or ""
        space_id = rec.get("space_id") or ""
        record_id = rec.get("record_id") or rec.get("id") or key
        tags = rec.get("tags") or ()
    else:
        content = dict(getattr(rec, "content", None) or {})
        created = getattr(rec, "created_at", None)
        key = str(getattr(rec, "key", "") or "")
        space_id = str(getattr(rec, "space_id", "") or "")
        record_id = str(getattr(rec, "record_id", "") or key)
        tags = getattr(rec, "tags", ()) or ()
    created_ms = _parse_ms(created.isoformat() if hasattr(created, "isoformat") else created)
    return {
        "key": key,
        "content": content if isinstance(content, dict) else {},
        "created_ms": created_ms,
        "space_id": space_id,
        "record_id": record_id,
        "tags": tuple(tags),
    }


def _fetch_episodes(memory_store: Any, allowed: dict[str, str]) -> list[dict]:
    if memory_store is None or not allowed:
        return []
    fn = getattr(memory_store, "list_by_kind", None)
    if not callable(fn):
        return []
    from whale_core.memory.store import MemoryKind

    try:
        rows = fn(
            MemoryKind.EPISODE,
            space_ids=tuple(allowed.keys()),
            include_protected=True,
        )
    except TypeError:
        try:
            rows = fn(MemoryKind.EPISODE, space_ids=tuple(allowed.keys()))
        except Exception:
            return []
    except Exception:
        return []
    out: list[dict] = []
    for r in rows or []:
        fields = _record_fields(r)
        sid = fields.get("space_id") or ""
        if sid not in allowed:
            continue
        fields["space_type"] = allowed[sid]
        out.append(fields)
    return out


def _freshness(source_time_ms: int | None, now_ms: int, *, unknown: bool = False) -> str:
    if unknown or source_time_ms is None:
        return "unknown_source_time"
    if now_ms - int(source_time_ms) <= _FRESH_RECENT_MS:
        return "recent"
    return "historical"


def query_rayneo_context(
    rayneo_store: Any,
    memory_store: Any,
    request: dict,
    *,
    now_ms: int | None = None,
    query_disabled: bool = False,
) -> dict:
    """查询 Rayneo 上下文（原文 + owner/群/受保护原件 episode）。

    query_disabled 由环境开关传入，与 ingest 停止开关无关。
    空间集合为空时 episode 失败关闭，原文仍可返回。
    """
    started = time.monotonic()
    now = _now_ms() if now_ms is None else int(now_ms)
    req = request if isinstance(request, dict) else {}

    source = str(req.get("source") or "")
    if source != "rayneo":
        raise ValueError("invalid source")

    def empty(*, error_class: str | None = None) -> dict:
        out: dict[str, Any] = {
            "ok": True,
            "source": "rayneo",
            "instruction_eligible": False,
            "query_disabled": bool(query_disabled),
            "items": [],
            "derived": [],
        }
        if error_class:
            out["error_class"] = error_class
        return out

    def timed_out() -> bool:
        return (time.monotonic() - started) > _QUERY_HARD_TIMEOUT_S

    if query_disabled:
        return empty()

    topic = str(req.get("topic") or "")
    segment_id = req.get("segment_id")
    from_ms = _parse_ms(req.get("from", req.get("from_ms")))
    to_ms = _parse_ms(req.get("to", req.get("to_ms")))
    try:
        limit = min(50, max(1, int(req.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    try:
        max_text_chars = max(0, int(req.get("max_text_chars", 4000)))
    except (TypeError, ValueError):
        max_text_chars = 4000
    try:
        max_total_tokens = max(0, int(req.get("max_total_tokens", 8000)))
    except (TypeError, ValueError):
        max_total_tokens = 8000
    include_derived = bool(req.get("include_derived", False))
    topic_terms = _cjk_terms(topic) if topic else set()

    if timed_out():
        return empty(error_class="timeout")

    raw_segments: list[dict] = []
    try:
        raw_segments = list(
            rayneo_store.query_segments(
                segment_id=str(segment_id) if segment_id else None,
                from_ms=from_ms,
                to_ms=to_ms,
                limit=max(limit * 2, limit),
            )
            or []
        )
    except Exception:
        raw_segments = []

    if timed_out():
        return empty(error_class="timeout")

    items: list[dict] = []
    for seg in raw_segments:
        if timed_out():
            return empty(error_class="timeout")
        kind = seg.get("kind")
        expired = bool(seg.get("expired"))
        if kind == "transcript":
            text_kind = "transcript"
            origin = f"rayneo:{seg.get('origin') or 'live'}"
        else:
            text_kind = "summary"
            origin = "rayneo:official"
        unknown = bool(seg.get("source_time_unknown"))
        src_time = seg.get("source_time_ms")
        if src_time is None:
            src_time = seg.get("received_ms")
            unknown = True
        item: dict[str, Any] = {
            "segment_id": seg.get("segment_id"),
            "text_kind": text_kind,
            "origin": origin,
            "instruction_eligible": False,
            "revision": int(seg.get("revision", 1) or 1),
            "freshness": _freshness(src_time, now, unknown=unknown),
            "source_time": None if unknown or seg.get("source_time_ms") is None else None,
            "source_time_unknown": unknown or seg.get("source_time_ms") is None,
            "received_at": None,
            "expired": expired,
            "_sort_ms": int(src_time or 0),
        }
        # ISO 时间：有真实源时间才填 source_time
        if seg.get("source_time_ms") is not None:
            item["source_time"] = _ms_to_iso(int(seg["source_time_ms"]))
            item["source_time_unknown"] = False
        if seg.get("received_ms") is not None:
            item["received_at"] = _ms_to_iso(int(seg["received_ms"]))
        if expired:
            item["expired"] = True
        else:
            item["text"] = seg.get("text") or ""
        if topic_terms and not expired:
            if not (_cjk_terms(item.get("text") or "") & topic_terms):
                continue
        items.append(item)

    space_map = _allowed_space_map(memory_store)
    episodes = _fetch_episodes(memory_store, space_map)
    derived: list[dict] = []
    for ep in episodes:
        if timed_out():
            return empty(error_class="timeout")
        st = ep.get("space_type")
        if st not in ("owner_working_copy", "group_space", "protected_original"):
            continue
        key = str(ep.get("key") or "")
        if key.startswith(_GROUP_SKIP_KEY_PREFIXES):
            continue
        content = _sanitize_content(ep.get("content") or {})
        summary_text = str(content.get("summary") or "")
        # remember_episode 把 extra 合并进 content
        is_derived = content.get("derived") == "derived-from-transcript"
        ep_time = _parse_ms(content.get("window_end") or content.get("window_start")) or ep.get("created_ms")
        if from_ms is not None and ep_time is not None and ep_time < from_ms:
            continue
        if to_ms is not None and ep_time is not None and ep_time > to_ms:
            continue
        if topic_terms and not (_cjk_terms(summary_text) & topic_terms):
            continue
        base_item: dict[str, Any] = {
            "segment_id": ep.get("record_id") or key,
            "text_kind": "summary",
            "origin": "whale:episode",
            "space_type": st,
            "instruction_eligible": False,
            "revision": 1,
            "freshness": _freshness(ep_time, now),
            "source_time": _ms_to_iso(ep_time) if ep_time is not None else None,
            "source_time_unknown": ep_time is None,
            "text": summary_text,
            "expired": False,
            "_sort_ms": int(ep_time or 0),
        }
        if is_derived:
            if include_derived:
                d = dict(base_item)
                d["kind"] = content.get("kind") or "summary"
                derived.append(d)
            # 总结 episode 主要出现在 derived[]，不再重复进 items
            continue
        items.append(base_item)

    if timed_out():
        return empty(error_class="timeout")

    items.sort(key=lambda x: -int(x.get("_sort_ms") or 0))
    derived.sort(key=lambda x: -int(x.get("_sort_ms") or 0))

    gaps: list[dict] = []
    total_tokens = 0
    final_items: list[dict] = []
    truncated_any = False
    for it in items:
        if len(final_items) >= limit:
            truncated_any = True
            break
        text = it.get("text")
        if isinstance(text, str) and max_text_chars > 0 and len(text) > max_text_chars:
            it["text"] = text[:max_text_chars]
            it["truncated"] = True
        cost = _token_cost(it.get("text") or "")
        if max_total_tokens > 0 and total_tokens + cost > max_total_tokens and final_items:
            truncated_any = True
            break
        total_tokens += cost
        it.pop("_sort_ms", None)
        final_items.append(it)

    if truncated_any:
        gaps.append({"kind": "truncated"})

    for d in derived:
        d.pop("_sort_ms", None)

    resp = empty()
    resp["items"] = final_items
    resp["derived"] = derived
    if gaps:
        resp["gaps"] = gaps
    return resp


def _ms_to_iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
