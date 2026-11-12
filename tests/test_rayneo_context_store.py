"""RayneoContextStore 离线测试：幂等、墓碑、TTL、并发、日志纪律。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from whale_core.memory.rayneo_context import RayneoContextStore, _sha256_text


def _item(seq, text, *, dedup=None, revision=1, kind="transcript", origin="live",
          segment_id=None, received_at="2026-11-10T02:15:00.000Z", **extra):
    payload = {
        "client_seq": seq,
        "dedup_key": dedup or f"rayneo:dev:{seq}",
        "kind": kind,
        "origin": origin,
        "revision": revision,
        "segment_id": segment_id or f"s-{seq}",
        "text": text,
        "content_hash": _sha256_text(text),
        "received_at": received_at,
        "source_time": extra.pop("source_time", received_at),
        "source_time_unknown": extra.pop("source_time_unknown", False),
    }
    payload.update(extra)
    return payload


def test_ingest_replay_and_revision(tmp_path):
    store = RayneoContextStore(tmp_path / "r.sqlite3", raw_ttl_days=90)
    item = _item(1, "今天下午讨论接口")
    r1 = store.ingest_batch("dev-abc", [item], now_ms=1_000)[0]
    assert r1["effect"] == "ingested" and r1["raw_id"].startswith("rs-")
    r2 = store.ingest_batch("dev-abc", [item], now_ms=1_100)[0]
    assert r2["effect"] == "replay_idempotent"
    assert r2["raw_id"] == r1["raw_id"]
    # 库行数不变
    assert len(store.query_segments(limit=50)) == 1

    upgraded = _item(1, "今天下午讨论接口（修订）", revision=2, dedup=item["dedup_key"],
                     segment_id=item["segment_id"])
    r3 = store.ingest_batch("dev-abc", [upgraded], now_ms=1_200)[0]
    assert r3["effect"] == "revision_applied"
    rows = store.query_segments(limit=50)
    assert len(rows) == 1 and rows[0]["text"].endswith("修订）")
    assert rows[0]["sum_state"] == "pending"

    stale = _item(1, "旧稿", revision=1, dedup=item["dedup_key"], segment_id=item["segment_id"])
    r4 = store.ingest_batch("dev-abc", [stale], now_ms=1_300)[0]
    assert r4["effect"] == "stale_ignored"

    clash = dict(item)
    clash["content_hash"] = _sha256_text("不同正文")
    r5 = store.ingest_batch("dev-abc", [clash], now_ms=1_400)[0]
    # 同 revision 不同 hash：stale；hash 字段本身不对：hash_mismatch
    assert r5["effect"] in ("stale_ignored", "rejected: hash_mismatch") or r5.get("reason") == "hash_mismatch"
    store.close()


def test_hash_mismatch_and_text_too_long_and_control(tmp_path):
    store = RayneoContextStore(tmp_path / "r.sqlite3")
    bad_hash = _item(1, "正常")
    bad_hash["content_hash"] = "sha256:" + "0" * 64
    r = store.ingest_batch("dev", [bad_hash], now_ms=1)[0]
    assert "hash_mismatch" in r["effect"] or r.get("reason") == "hash_mismatch"

    long_text = "哈" * 8001
    # hash 按超长正文计算，应先因长度拒绝
    r = store.ingest_batch("dev", [_item(2, long_text)], now_ms=2)[0]
    assert "text_too_long" in r["effect"]

    ctrl = _item(3, "有\x00控制符")
    r = store.ingest_batch("dev", [ctrl], now_ms=3)[0]
    assert "control_chars" in r["effect"]

    ok_tab = _item(4, "允许\t制表\n换行\r")
    r = store.ingest_batch("dev", [ok_tab], now_ms=4)[0]
    assert r["effect"] == "ingested"
    store.close()


def test_tombstone_and_delete_range(tmp_path):
    store = RayneoContextStore(tmp_path / "r.sqlite3")
    a = _item(1, "上午会议", received_at="2026-11-10T02:00:00Z",
              source_time="2026-11-10T02:00:00Z")
    b = _item(2, "晚上散步", received_at="2026-11-11T14:00:00Z",
              source_time="2026-11-11T14:00:00Z")
    store.ingest_batch("dev", [a, b], now_ms=10)
    # 只删 11-10
    from whale_core.memory.rayneo_context import _parse_ms
    n, segs = store.delete_range(_parse_ms("2026-11-10T00:00:00Z"),
                                 _parse_ms("2026-11-10T23:59:59Z"))
    assert n == 1
    visible = store.query_segments(limit=50)
    assert len(visible) == 1 and visible[0]["segment_id"] == "s-2"
    # 墓碑拒绝回灌
    r = store.ingest_batch("dev", [a], now_ms=20)[0]
    assert "tombstoned" in r["effect"]
    # purge 后全部查不到
    store.purge_all()
    assert store.query_segments(limit=50) == []
    r = store.ingest_batch("dev", [b], now_ms=30)[0]
    assert "tombstoned" in r["effect"]
    store.close()


def test_expire_due_omits_text(tmp_path):
    store = RayneoContextStore(tmp_path / "r.sqlite3", raw_ttl_days=1)
    store.ingest_batch("dev", [_item(1, "会过期")], now_ms=1_000)
    # 还没到期
    assert store.expire_due(now_ms=1_000) == 0
    n = store.expire_due(now_ms=1_000 + 86_400_000 + 1)
    assert n == 1
    rows = store.query_segments(limit=10)
    assert len(rows) == 1 and rows[0].get("expired") is True and "text" not in rows[0]
    store.close()


def test_cross_origin_dup_kept_but_skipped(tmp_path):
    store = RayneoContextStore(tmp_path / "r.sqlite3")
    live = _item(1, "同一句话", origin="live", dedup="k-live",
                 received_at="2026-11-10T02:00:00Z")
    cache = _item(2, "同一句话", origin="cache", dedup="k-cache",
                  received_at="2026-11-10T02:03:00Z")
    store.ingest_batch("dev", [live, cache], now_ms=50)
    visible = store.query_segments(limit=10)
    assert len(visible) == 1 and visible[0]["origin"] == "live"
    pending = store.pending_transcripts()
    assert len(pending) == 1
    store.close()


def test_concurrent_same_batch(tmp_path):
    store = RayneoContextStore(tmp_path / "r.sqlite3")
    item = _item(7, "并发写入")
    results = []

    def worker():
        results.append(store.ingest_batch("dev", [item], now_ms=99))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    effects = [r[0]["effect"] for r in results]
    assert effects.count("ingested") == 1
    assert all(e in ("ingested", "replay_idempotent") for e in effects)
    assert len(store.query_segments(limit=10)) == 1
    store.close()


def test_logs_do_not_contain_text_or_token(tmp_path, capsys):
    store = RayneoContextStore(tmp_path / "r.sqlite3")
    secret = "这是不该出现在日志里的转写正文 TOKENVALUE"
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        store.ingest_batch("dev-instance-abcdef", [_item(1, secret)], now_ms=1)
    out = buf.getvalue() + capsys.readouterr().out + capsys.readouterr().err
    assert "TOKENVALUE" not in out
    assert secret not in out
    store.close()


def test_backup_to_roundtrip(tmp_path):
    src = RayneoContextStore(tmp_path / "r.sqlite3")
    src.ingest_batch("dev", [_item(1, "备份内容")], now_ms=1)
    dest = src.backup_to(tmp_path / "bak.sqlite3")
    assert dest.exists()
    if os.name == "posix":
        assert dest.stat().st_mode & 0o777 == 0o600
    src.close()
    restored = RayneoContextStore(dest)
    rows = restored.query_segments(limit=5)
    assert len(rows) == 1 and rows[0]["text"] == "备份内容"
    restored.close()
