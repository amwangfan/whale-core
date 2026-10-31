"""issue #10：proof_method=device——手机可信确认与一次性授权凭证。"""

from __future__ import annotations

import threading
import time

import pytest

from whale_core.life.temporary_auth import (
    TemporaryAuthError,
    TemporaryAuthService,
)
from whale_core.memory.store import InMemoryStore


def _challenge(svc, **kw):
    args = dict(device_id="dev-phone-1", principal_id="owner",
                channel="qq", conversation_id="private:10001",
                scopes=("tool.web.search",))
    args.update(kw)
    return svc.create_device_challenge(**args)


def test_device_confirm_issues_scoped_grant():
    store = InMemoryStore()
    svc = TemporaryAuthService(store)
    ch = _challenge(svc)
    grant = svc.confirm_device_challenge(
        ch["challenge_id"], device_id="dev-phone-1",
        device_verifier=lambda cid, did: did == "dev-phone-1")
    assert grant is not None
    assert grant.proof_method == "device"
    assert grant.scopes == ("tool.web.search",)
    assert grant.max_uses == 1 and grant.expires_at > time.time()
    # 存储侧可核对
    rec = store.get(f"otp:grant:{grant.grant_id}")
    assert rec["proof_method"] == "device"
    assert rec["source_code_id"] == ch["challenge_id"]


def test_challenge_single_use_no_replay():
    store = InMemoryStore()
    svc = TemporaryAuthService(store)
    ch = _challenge(svc)
    verifier = lambda cid, did: True
    first = svc.confirm_device_challenge(
        ch["challenge_id"], device_id="dev-phone-1", device_verifier=verifier)
    assert first is not None
    # 重放被拒
    assert svc.confirm_device_challenge(
        ch["challenge_id"], device_id="dev-phone-1",
        device_verifier=verifier) is None


def test_concurrent_confirm_only_one_wins():
    store = InMemoryStore()
    svc = TemporaryAuthService(store)
    ch = _challenge(svc)
    results = []

    def go():
        results.append(svc.confirm_device_challenge(
            ch["challenge_id"], device_id="dev-phone-1",
            device_verifier=lambda cid, did: True))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    grants = [g for g in results if g is not None]
    assert len(grants) == 1  # 并发核销只有一人成功


def test_wrong_device_and_expired_and_denied_rejected():
    store = InMemoryStore()
    svc = TemporaryAuthService(store)
    verifier = lambda cid, did: True
    # 换设备
    ch = _challenge(svc)
    assert svc.confirm_device_challenge(
        ch["challenge_id"], device_id="dev-other",
        device_verifier=verifier) is None
    # 过期
    ch2 = _challenge(svc, ttl_s=1)
    rec = store.get(f"otp:device-challenge:{ch2['challenge_id']}")
    rec["expires_at"] = time.time() - 1
    store.put(f"otp:device-challenge:{ch2['challenge_id']}", rec)
    assert svc.confirm_device_challenge(
        ch2["challenge_id"], device_id="dev-phone-1",
        device_verifier=verifier) is None
    # 设备拒绝后不可再确认
    ch3 = _challenge(svc)
    assert svc.deny_device_challenge(ch3["challenge_id"],
                                     device_id="dev-phone-1") is True
    assert svc.confirm_device_challenge(
        ch3["challenge_id"], device_id="dev-phone-1",
        device_verifier=verifier) is None


def test_verifier_required_and_scopes_restricted():
    store = InMemoryStore()
    svc = TemporaryAuthService(store)
    ch = _challenge(svc)
    # 无 verifier → fail closed
    assert svc.confirm_device_challenge(
        ch["challenge_id"], device_id="dev-phone-1") is None
    # verifier 拒绝 → None
    assert svc.confirm_device_challenge(
        ch["challenge_id"], device_id="dev-phone-1",
        device_verifier=lambda cid, did: False) is None
    # 不可授权 scope 被剥离；全不可授权 → 拒绝创建
    ch2 = _challenge(svc, scopes=("tool.web.search", "shell.root"))
    assert ch2["scopes"] == ["tool.web.search"]
    with pytest.raises(TemporaryAuthError):
        _challenge(svc, scopes=("shell.root",))


def test_challenge_carries_no_private_content():
    """挑战载荷纪律：nonce/principal/会话/scope/指纹/过期，无正文。"""
    store = InMemoryStore()
    svc = TemporaryAuthService(store)
    ch = _challenge(svc, action_fingerprint="fp-1")
    rec = store.get(f"otp:device-challenge:{ch['challenge_id']}")
    assert set(rec) <= {"challenge_id", "state", "device_id", "principal_id",
                        "channel", "conversation_id", "scopes",
                        "action_fingerprint", "grant_ttl_s", "grant_max_uses",
                        "created_at", "expires_at", "resolved_at"}
