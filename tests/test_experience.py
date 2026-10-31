"""issue #21：经验提炼——可溯源、可纠正、作用域隔离、只调建议权重。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_core.memory.experience import Experience, ExperienceStore
from whale_core.memory.store import InMemoryStore

A, B = "anon:qq:10001", "anon:qq:10002"


def test_receipt_and_feedback_weights():
    """多次「提醒太早」负反馈 → 提醒时机权重下降；成功回执正贡献。"""
    store = InMemoryStore()
    exps = ExperienceStore(store)
    exps.record_user_feedback(A, feedback_id="1", action="remind.lead_time:30m",
                              outcome="rejected", evidence="msg:m1")
    exps.record_user_feedback(A, feedback_id="2", action="remind.lead_time:30m",
                              outcome="rejected", evidence="msg:m2")
    exps.record_receipt_outcome(A, task_id="t1", action="remind.lead_time:10m",
                                success=True, receipt="r-1")
    w = exps.suggested_weights(A)
    assert w["remind.lead_time:30m"] < -0.5     # 本人反馈 ×2 累积
    assert w["remind.lead_time:10m"] > 0


def test_candidate_sources_never_update_weights():
    """未验证外部来源只作候选：不进权重。"""
    store = InMemoryStore()
    exps = ExperienceStore(store)
    exps.record(Experience(
        experience_id="c1", principal_id=A, source="candidate:ocr",
        evidence="screen:x", action="remind.lead_time:60m",
        outcome="success", confidence=0.9, ttl_s=0,
        confirmed_by_user=False,
        recorded_at=datetime.now(timezone.utc).isoformat()))
    data = store.get(f"exp:{A}:c1")
    assert data["confidence"] <= 0.3 and data["confirmed_by_user"] is False
    assert exps.suggested_weights(A) == {}


def test_idempotent_and_correctable_and_deletable():
    store = InMemoryStore()
    exps = ExperienceStore(store)
    for _ in range(3):  # 重复回执幂等
        exps.record_receipt_outcome(A, task_id="t1", action="a",
                                    success=True, receipt="r")
    assert len(exps.list_for(A)) == 1
    assert exps.correct(A, "rcpt-t1", action="remind.lead_time:15m")
    data = store.get(f"exp:{A}:rcpt-t1")
    assert data["action"] == "remind.lead_time:15m"
    assert data["outcome"] == "corrected"
    assert exps.delete(A, "rcpt-t1") and exps.list_for(A) == ()


def test_scope_isolation_and_purge():
    store = InMemoryStore()
    exps = ExperienceStore(store)
    exps.record_receipt_outcome(A, task_id="t1", action="a", success=True,
                                receipt="r")
    exps.record_receipt_outcome(B, task_id="t2", action="a", success=False,
                                receipt="r")
    assert exps.list_for(A) != exps.list_for(B)
    assert exps.suggested_weights(A)["a"] != exps.suggested_weights(B)["a"]
    assert exps.purge_principal(A) == 1
    assert exps.list_for(A) == () and len(exps.list_for(B)) == 1


def test_expired_experience_fades_out():
    store = InMemoryStore()
    exps = ExperienceStore(store)
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    exps.record(Experience(
        experience_id="old1", principal_id=A, source="receipt",
        evidence="task:t9", action="a", outcome="failed",
        confidence=0.8, ttl_s=90 * 86400, confirmed_by_user=False,
        recorded_at=old))
    assert exps.suggested_weights(A) == {}  # TTL 消退
