"""issue #39：TraceLedger 持久化行为账本测试（全离线，tmp sqlite）。"""

from __future__ import annotations

import threading

from whale_core.life.trace import REASON_TEXT, TraceLedger, explain


def make(path, **kw):
    kw.setdefault("prune_every", 5)
    return TraceLedger(str(path), **kw)


class TestLedger:
    def test_begin_span_finish_roundtrip(self, tmp_path):
        led = make(tmp_path / "t.db")
        tid = led.begin("qq_inbound", conversation_id="private:1",
                        principal_id="anon:qq:1", message_id="m1")
        led.span(tid, "received", "ok")
        led.span(tid, "decide", "ok", reason_code="private")
        led.span(tid, "send", "executed", duration_ms=120.0)
        led.finish(tid, "replied", summary="2 段气泡", receipt_id="rc1")
        t = led.get(tid)
        assert t is not None
        assert t.status == "replied" and t.receipt_id == "rc1"
        assert [s.phase for s in t.spans] == ["received", "decide", "send"]
        assert t.spans[2].duration_ms == 120.0
        led.close()

    def test_persistence_across_reopen(self, tmp_path):
        p = tmp_path / "t.db"
        led = make(p)
        tid = led.begin("qq_inbound", message_id="m1")
        led.finish(tid, "silent", summary="旁观")
        led.close()
        led2 = make(p)
        t = led2.get(tid)
        assert t is not None and t.status == "silent"
        led2.close()

    def test_query_filters_and_pagination(self, tmp_path):
        now = [1000.0]
        led = make(tmp_path / "t.db", now=lambda: now[0])
        for i in range(8):
            now[0] += 10
            kind = "qq_inbound" if i % 2 == 0 else "task"
            status = "replied" if i % 3 else "silent"
            tid = led.begin(kind, conversation_id=f"group:{i % 3}",
                            message_id=f"m{i}")
            led.finish(tid, status)
        rows, total = led.query(kind="qq_inbound")
        assert total == 4
        rows, total = led.query(status="silent", limit=3, offset=0)
        assert total == 3 and len(rows) == 3
        rows, _ = led.query(conversation_id="group:1")
        assert all(r.conversation_id == "group:1" for r in rows)
        rows, total = led.query(since=1035.0)
        assert all(r.started_at >= 1035.0 for r in rows)
        led.close()

    def test_retention_by_age_and_rows(self, tmp_path):
        now = [1000.0]
        led = make(tmp_path / "t.db", now=lambda: now[0],
                   max_age_days=1, max_rows=5, prune_every=1)
        for i in range(10):
            now[0] += 100
            led.finish(led.begin("qq_inbound", message_id=f"m{i}"), "replied")
        # max_rows=5 → 只剩最新 5 条
        _, total = led.query(limit=200)
        assert total == 5
        # 时间推进 2 天 → 全部过期
        now[0] += 2 * 86400
        led.prune()
        _, total = led.query(limit=200)
        assert total == 0
        # spans 不残留孤儿
        orphan = led._db.execute(
            "SELECT COUNT(*) FROM spans").fetchone()[0]
        assert orphan == 0
        led.close()

    def test_redaction_no_secrets(self, tmp_path):
        led = make(tmp_path / "t.db")
        tid = led.begin("task")
        led.span(tid, "send", "failed",
                 detail="401 token=sk-abcdef123456, Authorization: Bearer xyz98765")
        led.finish(tid, "failed",
                   summary="send failed password=hunter2secret")
        t = led.get(tid)
        blob = t.spans[0].detail + t.summary
        assert "sk-abcdef123456" not in blob
        assert "xyz98765" not in blob
        assert "hunter2secret" not in blob
        assert "token=***" in blob or "Token=***" in blob
        led.close()

    def test_concurrent_writers(self, tmp_path):
        led = make(tmp_path / "t.db")
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    tid = led.begin("task", task_id=f"t{n}-{i}")
                    led.span(tid, "model", "ok")
                    led.finish(tid, "replied")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        _, total = led.query(limit=200)
        assert total == 80
        led.close()


class TestExplain:
    def test_replied_story(self, tmp_path):
        led = make(tmp_path / "t.db")
        tid = led.begin("qq_inbound", conversation_id="group:9",
                        message_id="m1")
        led.span(tid, "decide", "ok", reason_code="at_bot")
        led.span(tid, "send", "executed")
        led.finish(tid, "replied")
        lines = explain(led.get(tid))
        text = "\n".join(lines)
        assert "群聊" in text and "@" in text and "已回复" in text
        led.close()

    def test_silent_story_with_reason(self, tmp_path):
        led = make(tmp_path / "t.db")
        tid = led.begin("qq_inbound", conversation_id="group:9")
        led.span(tid, "decide", "observe", reason_code="cooldown")
        led.finish(tid, "silent")
        lines = explain(led.get(tid))
        assert any("冷却" in ln for ln in lines)
        assert any("没有回复" in ln for ln in lines)
        led.close()

    def test_all_known_codes_have_text_or_fallback(self):
        # REASON_TEXT 覆盖核心码；未覆盖码在 explain 中也能原样展示
        for code in ("private", "at_bot", "cooldown", "stranger_budget",
                     "silent_planned", "epoch_stale"):
            assert code in REASON_TEXT
