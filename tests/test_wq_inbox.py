"""入站持久化 inbox（issue #14）：幂等入队、租约恢复、受控重试、隐私面、监听器集成。

全部离线：不起 HTTP 服务（async_dispatch=False 同步派发 + 直接方法调用）。
"""

from __future__ import annotations

import json

import pytest

from whale_core.life.channels import OwnerChannelRegistry
from whale_core.life.inbox import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SUCCEEDED,
    SqliteInbox,
)
from weq_channel.wq_push import WeQSsePushListener


@pytest.fixture()
def inbox(tmp_path):
    box = SqliteInbox(tmp_path / "inbox.sqlite3", max_attempts=3, lease_seconds=300)
    yield box
    box.close()


def make_listener(inbox, handler):
    registry = OwnerChannelRegistry(owner_id="owner", bindings={("qq", "100"): "owner"})
    return WeQSsePushListener(registry, expected_token="tok", self_uin="999",
                              on_owner_event=handler, async_dispatch=False, inbox=inbox)


def payload(msg_id, text="你好", sender="100"):
    return json.dumps(
        {"events": [{"type": "message", "senderUin": sender, "msgId": msg_id,
                    "chatType": "c2c", "convId": "c1", "text": text}]}
    ).encode()


AUTH = {"Authorization": "Bearer tok"}


# ── SqliteInbox 单元 ────────────────────────────────────────────────


def test_database_file_is_private_on_posix(tmp_path):
    import os
    import stat
    if os.name != "posix":
        pytest.skip("POSIX file permissions only")
    path = tmp_path / "private-inbox.sqlite3"
    db = SqliteInbox(path)
    db.enqueue(message_id="m1", conversation="private", account="owner", text="secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    db.close()


def test_invalid_lease_duration_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="lease_seconds"):
        SqliteInbox(tmp_path / "inbox.sqlite3", lease_seconds=0)


def test_enqueue_dedup_and_claim(inbox):
    assert inbox.enqueue(message_id="m1", conversation="private:c1", account="100", text="hi")
    assert not inbox.enqueue(message_id="m1", conversation="private:c1", account="100", text="hi")
    job = inbox.claim_next()
    assert job is not None and job.message_id == "m1" and job.status == STATUS_PROCESSING
    assert inbox.claim_next() is None
    inbox.succeed("m1")
    assert inbox.counts()[STATUS_SUCCEEDED] == 1


def test_fail_retries_then_failed_then_requeue(inbox):
    inbox.enqueue(message_id="m2", conversation="private:c1", account="100", text="x")
    inbox.claim_next()
    assert inbox.fail("m2", error_class="TimeoutError") == STATUS_PENDING
    inbox.claim_next()
    assert inbox.fail("m2", error_class="TimeoutError") == STATUS_PENDING
    inbox.claim_next()
    assert inbox.fail("m2", error_class="TimeoutError") == STATUS_FAILED
    assert inbox.failed_ids() == [("m2", 3, "TimeoutError")]  # 只有类名，无错误消息/正文
    assert inbox.requeue("m2")
    assert inbox.claim_next().message_id == "m2"
    assert not inbox.requeue("m2")  # processing 状态不可 requeue


def test_recover_stale_leases(inbox):
    inbox.enqueue(message_id="m3", conversation="private:c1", account="100", text="x")
    inbox.claim_next()
    assert inbox.recover_stale() == 0  # 租约未过期不收回
    with inbox._lock, inbox.db:
        inbox.db.execute("UPDATE inbox SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    assert inbox.recover_stale() == 1
    again = inbox.claim_next()
    assert again is not None and again.message_id == "m3"


# ── 监听器集成 ──────────────────────────────────────────────────────


def test_owner_message_persisted_and_dispatched(inbox):
    seen = []
    listener = make_listener(inbox, seen.append)
    code, n = listener.process_payload(AUTH, payload("m10"))
    assert (code, n) == (200, 1)
    assert seen and seen[0].message_id == "m10" and seen[0].owner_id == "owner"
    assert inbox.counts()[STATUS_SUCCEEDED] == 1


def test_drained_event_preserves_sender_id(inbox):
    """S1/S7 回归：持久化排水重建的 OwnerEvent 必须携带真实发送者。"""
    seen = []
    listener = make_listener(inbox, seen.append)
    code, n = listener.process_payload(AUTH, payload("m-sender", sender="100"))
    assert (code, n) == (200, 1)
    assert seen and seen[0].sender_id == "100"


def test_duplicate_delivery_single_effect(inbox):
    """上游至少一次投递：同一 msgId 重复推送只处理一次（幂等，不重复草稿）。"""
    seen = []
    listener = make_listener(inbox, seen.append)
    body = payload("m11")
    listener.process_payload(AUTH, body)
    listener.process_payload(AUTH, body)
    assert len(seen) == 1
    assert inbox.counts()[STATUS_SUCCEEDED] == 1


def test_crash_between_ack_and_process_recovers(tmp_path):
    """#14 核心验收：已 ACK 未处理即崩溃 → 重启后消息仍被处理。"""
    db = tmp_path / "inbox.sqlite3"
    box1 = SqliteInbox(db)
    box1.enqueue(message_id="m20", conversation="private:c1", account="100", text="在吗")
    box1.close()  # 「崩溃」：从未派发

    box2 = SqliteInbox(db)
    seen = []
    listener = make_listener(box2, seen.append)
    assert listener.inbox.recover_stale() == 0  # pending 无需租约恢复
    listener._drain_inbox()                     # 重启后的轮询直接续上
    assert seen and seen[0].message_id == "m20" and seen[0].text == "在吗"
    assert box2.counts()[STATUS_SUCCEEDED] == 1
    box2.close()


def test_handler_failure_retries_then_success(inbox):
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("deepseek down")

    listener = make_listener(inbox, flaky)
    listener.process_payload(AUTH, payload("m30"))
    # 受控重试：排空循环内连续认领，第三次成功（不静默丢失）
    assert calls["n"] == 3
    assert inbox.counts()[STATUS_SUCCEEDED] == 1
    assert inbox.counts()[STATUS_FAILED] == 0


def test_handler_always_fails_lands_in_failed_observably(inbox):
    def boom(event):
        raise RuntimeError("down")

    listener = make_listener(inbox, boom)
    listener.process_payload(AUTH, payload("m35"))
    assert inbox.counts()[STATUS_FAILED] == 1
    assert inbox.failed_ids() == [("m35", 3, "RuntimeError")]
    assert inbox.requeue_all_failed() == 1  # 人工恢复入口


def test_failure_records_error_class_not_message(inbox):
    """隐私面：错误只记类名——异常消息可能带正文/凭据，一律不落库。"""
    def boom(event):
        raise ValueError("secret body content 12345")

    listener = make_listener(inbox, boom)
    listener.process_payload(AUTH, payload("m40", text="private text"))
    failed = inbox.failed_ids()
    assert failed == [("m40", 3, "ValueError")]
    assert "secret body" not in str(failed) and "private text" not in str(failed)


def test_unpaired_messages_never_persisted(inbox):
    """身份门边界：未配对来件不落盘（只走元数据回调）。"""
    seen = []
    registry = OwnerChannelRegistry(owner_id="owner", bindings={("qq", "100"): "owner"})
    listener = WeQSsePushListener(registry, expected_token="tok", self_uin="999",
                                  on_owner_event=seen.append, async_dispatch=False,
                                  inbox=inbox)
    code, _ = listener.process_payload(AUTH, payload("m50", sender="777"))
    assert code == 200
    assert seen == []
    assert sum(inbox.counts().values()) == 0


def test_inbox_optional_legacy_path_unchanged():
    """不配 inbox 时保持旧内存队列行为（向后兼容既有部署/测试）。"""
    seen = []
    registry = OwnerChannelRegistry(owner_id="owner", bindings={("qq", "100"): "owner"})
    listener = WeQSsePushListener(registry, expected_token="tok", self_uin="999",
                                  on_owner_event=seen.append, async_dispatch=False)
    listener.process_payload(AUTH, payload("m60"))
    assert seen and seen[0].message_id == "m60"
