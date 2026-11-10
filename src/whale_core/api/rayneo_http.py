"""glasses API 的 /v1/rayneo/{ingest,query,control} 处理函数。

鉴权在调用前完成。日志只记 source_instance 前 8 位、条数、字节数、耗时，
不记 text / token。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from whale_core.memory.rayneo_context import RayneoContextStore, query_rayneo_context
from whale_core.memory.spaces import OWNER_WORKING_SPACE_ID
from whale_core.memory.store import MemoryKind

INGEST_MAX_BYTES = 320 * 1024
QUERY_MAX_BYTES = 64 * 1024
CONTROL_MAX_BYTES = 64 * 1024
MAX_ITEMS = 50
_ACK_EFFECTS = (
    "ingested", "revision_applied", "replay_idempotent",
)
_RATE_LOCK = Lock()
_RATE: dict[str, deque] = defaultdict(deque)
_RATE_WINDOW_S = 60.0
_RATE_MAX = 5  # 每 source_instance 每分钟批次数


def rayneo_store_path(env: dict) -> str:
    override = (env.get("WHALE_RAYNEO_STORE") or "").strip()
    if override:
        return override
    base = (env.get("WHALE_GLASSES_STORE") or env.get("WHALE_STORE") or "").strip()
    if not base:
        return "rayneo_context.sqlite3"
    return str(Path(base).expanduser().parent / "rayneo_context.sqlite3")


def raw_ttl_days(env: dict) -> int:
    raw = str(env.get("WHALE_RAYNEO_RAW_TTL_DAYS", "90")).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 90


def env_flag(env: dict, name: str, default: bool = True) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip() not in ("0", "false", "False", "no", "off")


def _server_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _rate_limited(source_instance: str) -> bool:
    now = time.monotonic()
    key = source_instance or "-"
    with _RATE_LOCK:
        q = _RATE[key]
        while q and now - q[0] > _RATE_WINDOW_S:
            q.popleft()
        if len(q) >= _RATE_MAX:
            return True
        q.append(now)
        return False


def _ack_cursor(results: list[dict]) -> int | None:
    indexed = []
    for r in results:
        seq = r.get("client_seq")
        if isinstance(seq, bool) or not isinstance(seq, (int, float)):
            continue
        indexed.append((int(seq), r))
    indexed.sort(key=lambda x: x[0])
    cursor = None
    for seq, r in indexed:
        effect = str(r.get("effect") or "")
        if effect in _ACK_EFFECTS or effect.startswith("rejected"):
            cursor = seq
            continue
        break
    return cursor


def _log(kind: str, source_instance: str, n: int, nbytes: int, ms: int, extra: str = "") -> None:
    inst = (source_instance or "")[:8]
    print(f"[rayneo] {kind} src={inst} items={n} bytes={nbytes} ms={ms}{extra}",
          flush=True)


def handle_ingest(body: dict, *, store: RayneoContextStore, env: dict,
                  body_bytes: int = 0) -> tuple[int, dict]:
    if not env_flag(env, "WHALE_RAYNEO_INGEST", True) or not store.get_ingest_enabled():
        return 503, {"error": "ingest_disabled"}
    if not isinstance(body, dict):
        return 400, {"error": "invalid request"}
    if str(body.get("source") or "") != "rayneo":
        return 400, {"error": "invalid request"}
    items = body.get("items")
    if not isinstance(items, list) or not (1 <= len(items) <= MAX_ITEMS):
        return 400, {"error": "invalid request"}
    source_instance = str(body.get("source_instance") or "")
    if _rate_limited(source_instance):
        return 429, {"error": "rate_limited", "retry_after_s": 30}
    t0 = time.perf_counter()
    try:
        results = store.ingest_batch(source_instance, items, now_ms=int(time.time() * 1000))
    except Exception:
        _log("ingest", source_instance, len(items), body_bytes,
             int((time.perf_counter() - t0) * 1000), " error=store")
        return 503, {"error": "store_unavailable"}
    elapsed = int((time.perf_counter() - t0) * 1000)
    _log("ingest", source_instance, len(items), body_bytes, elapsed)
    return 200, {
        "ok": True,
        "source": "rayneo",
        "batch_id": body.get("batch_id"),
        "results": results,
        "ack_cursor": _ack_cursor(results),
        "server_time": _server_time(),
        "retry_after_s": None,
    }


def handle_query(body: dict, *, rayneo_store: RayneoContextStore, memory_store,
                 env: dict, body_bytes: int = 0) -> tuple[int, dict]:
    if not isinstance(body, dict) or str(body.get("source") or "") != "rayneo":
        return 400, {"error": "invalid request"}
    disabled = not env_flag(env, "WHALE_RAYNEO_QUERY", True)
    t0 = time.perf_counter()
    try:
        resp = query_rayneo_context(
            rayneo_store, memory_store, body, query_disabled=disabled)
    except ValueError:
        return 400, {"error": "invalid request"}
    except Exception:
        _log("query", "", 0, body_bytes, int((time.perf_counter() - t0) * 1000),
             " error=store")
        return 200, {
            "ok": True, "source": "rayneo", "instruction_eligible": False,
            "query_disabled": disabled, "items": [], "derived": [],
            "error_class": "timeout",
        }
    _log("query", "", len(resp.get("items") or []), body_bytes,
         int((time.perf_counter() - t0) * 1000))
    return 200, resp


def _delete_derived_episodes(memory_store, segment_ids: list[str]) -> int:
    if memory_store is None or not segment_ids:
        return 0
    wanted = set(segment_ids) | {f"rs-{s}" for s in segment_ids} | {
        s[3:] for s in segment_ids if str(s).startswith("rs-")
    }
    deleted = 0
    try:
        rows = memory_store.list_by_kind(
            MemoryKind.EPISODE, space_ids=(OWNER_WORKING_SPACE_ID,))
    except Exception:
        return 0
    for rec in rows or []:
        content = dict(getattr(rec, "content", None) or {})
        if content.get("derived") != "derived-from-transcript":
            continue
        ids = content.get("segment_ids") or []
        if wanted.intersection({str(x) for x in ids}):
            try:
                memory_store.delete(rec.key)
                deleted += 1
            except Exception:
                pass
    return deleted


def handle_control(body: dict, *, rayneo_store: RayneoContextStore,
                   memory_store, env: dict) -> tuple[int, dict]:
    if not isinstance(body, dict) or str(body.get("source") or "") != "rayneo":
        return 400, {"error": "invalid request"}
    action = str(body.get("action") or "")
    if action == "stop_ingest":
        rayneo_store.set_ingest_enabled(False)
        return 200, {"ok": True, "ingest_enabled": False}
    if action == "resume_ingest":
        rayneo_store.set_ingest_enabled(True)
        return 200, {"ok": True, "ingest_enabled": True}
    if action == "purge_all":
        n, segs = rayneo_store.purge_all()
        m = _delete_derived_episodes(memory_store, segs)
        return 200, {"ok": True, "deleted_segments": n, "deleted_episodes": m}
    if action == "delete_range":
        from whale_core.memory.rayneo_context import _parse_ms
        frm = _parse_ms(body.get("from"))
        to = _parse_ms(body.get("to"))
        if frm is None or to is None:
            return 400, {"error": "invalid request"}
        n, segs = rayneo_store.delete_range(frm, to)
        m = _delete_derived_episodes(memory_store, segs)
        return 200, {"ok": True, "deleted_segments": n, "deleted_episodes": m}
    return 400, {"error": "invalid request"}
