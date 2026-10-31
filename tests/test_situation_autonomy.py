"""G5：SituationState + 自主等级 + 任务监督 + 模拟设备闭环（计划 §7、§11 LIFE/SEC）。

覆盖验收口径：
- 过期情境不触发；撤销（单条/按事件）停止待执行；
- 伪造事件（未配对/吊销设备、schema 非法、不可信来源写事实）不触发；
- 每次执行前复核 预算/审批/scope/期限/主体（INV-09/LIFE-02）；
- 推断永不驱动动作；L4 直接拒绝；「接受」≠「已执行」；unknown 不重试；
- 父子任务 scope 只许子集、预算独立、失败退避、取消级联；
- 「已完成」必须有真实回执；完成续聊只走 ProactiveOutbox；
- 可重放模拟事件完成一条「事件→情境→提醒」闭环。
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from whale_core.devices.registry import DeviceRegistry
from whale_core.life.autonomy import (
    ApprovalBook,
    AutonomyController,
    AutonomyProposal,
    DeviceUnavailable,
    action_fingerprint,
)
from whale_core.life.context_resolver import (
    ConversationContextResolver,
    pairing_lookup_from_mapping,
)
from whale_core.life.conversation_context import (
    ConversationKind,
    ConversationRef,
    Principal,
    ResolvedContext,
    SCOPE_DEVICE_NOTIFY,
    SCOPE_OUTBOX_SUBMIT,
)
from whale_core.life.device_bridge import (
    BridgeError,
    SimulatedActionPort,
    SimulatedDeviceBridge,
)
from whale_core.life.initiative import ProactiveOutbox, ProactivePrefs
from whale_core.life.situation import (
    FACT,
    INFERENCE,
    SituationError,
    SituationStore,
)
from whale_core.life.supervisor import AgentSupervisor, SupervisorError
from whale_core.memory.store import InMemoryStore

NOW = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)

OWNER = Principal(principal_id="paired:qq:10001", paired=True,
                  is_owner=True, evidence="pairing:qq")
CONV = ConversationRef("qq", ConversationKind.QQ_PRIVATE, "private:10001")
NOTIFY = "phone.notification.post"


def owner_ctx(*extra_scopes: str) -> ResolvedContext:
    return ResolvedContext(
        conversation=CONV, principal=OWNER, persona_id=None,
        capability_scopes=(SCOPE_DEVICE_NOTIFY, SCOPE_OUTBOX_SUBMIT, *extra_scopes))


def resolved_owner_ctx() -> ResolvedContext:
    """走真实 resolver 的 owner 私聊上下文（PRIVATE_OWNER 含通知/出站 scope）。"""
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "10001"): "owner"}))
    ctx = resolver.resolve(channel="qq", sender_id="10001",
                           conversation_id="private:10001")
    assert SCOPE_DEVICE_NOTIFY in ctx.capability_scopes
    return ctx


def prefs(**kw) -> ProactivePrefs:
    base = dict(enabled=True,
                allowed_channels=frozenset({"hud", "direct_message"}),
                auto_send=True, daily_limit=2,
                quiet_start=dtime(0), quiet_end=dtime(0), tz="UTC")
    base.update(kw)
    return ProactivePrefs(**base)


def active_device(store, scopes=(NOTIFY,)):
    registry = DeviceRegistry(store)
    info = registry.request_pairing(label="phone", pubkey_hex="ab" * 32,
                                    scopes=scopes)
    registry.approve(info["code"])
    return registry, info["device_id"]


def event_payload(event_id="e111111111111111", kind="screen.memo.created",
                  summary="论文截止还剩 2 小时", occurred=1780000000000):
    return {"event_id": event_id, "kind": kind, "occurred_at": occurred,
            "privacy_class": "owner_private", "summary": summary,
            "schema_version": "1.0"}


def record_fact(situations: SituationStore, *, key="curfew.notice", source,
                event_id="ev-1", expires_at=None, subject_id="owner"):
    return situations.record(
        key=key, value="23:30 后锁屏", fact_or_inference=FACT,
        subject_id=subject_id, source=source, event_id=event_id,
        observed_at=NOW - timedelta(minutes=5), expires_at=expires_at,
        confidence="high")


def l3_action_proposal(device_id: str, *, approval_ref="ap-1",
                       idem="idem-12345678") -> AutonomyProposal:
    return AutonomyProposal(
        level=3, kind="action", reason="到点锁屏提醒",
        capability=NOTIFY, device_id=device_id,
        args={"text": "该休息了"}, idempotency_key=idem,
        approval_ref=approval_ref)


# ---------------------------------------------------------------------------
# SituationState 存储（G5.1 / LIFE-01）
# ---------------------------------------------------------------------------


def test_fact_and_inference_are_stored_separately():
    store = InMemoryStore()
    situations = SituationStore(store, now_fn=lambda: NOW)
    fact = record_fact(situations, source="calendar:work")
    inference = situations.record(
        key="user_may_be_leaving", value="可能快出门了",
        fact_or_inference=INFERENCE, subject_id="owner",
        source="qq_group:123", event_id="m-9", observed_at=NOW,
        expires_at=NOW + timedelta(hours=1), confidence="medium")
    assert fact.fact_or_inference == FACT
    assert inference.fact_or_inference == INFERENCE
    facts = situations.active_for("owner", fact_or_inference=FACT)
    assert [s.key for s in facts] == ["curfew.notice"]
    both = situations.active_for("owner")
    assert {s.key for s in both} == {"curfew.notice", "user_may_be_leaving"}


def test_untrusted_sources_cannot_assert_facts():
    situations = SituationStore(InMemoryStore(), now_fn=lambda: NOW)
    for bad_source in ("qq_group:123", "model:intent", "unpaired:web", "anon:cli"):
        with pytest.raises(SituationError) as exc:
            record_fact(situations, source=bad_source, event_id=f"e-{bad_source}")
        assert exc.value.code == "untrusted-source"
    # 同一不可信来源可以落推断——只是永远不能成为确定事实
    inf = situations.record(
        key="group_guess", value="群友在聊电脑故障", fact_or_inference=INFERENCE,
        subject_id="owner", source="qq_group:123", event_id="m-1",
        observed_at=NOW, expires_at=None, confidence="low")
    assert inf.fact_or_inference == INFERENCE


def test_expired_or_retracted_situation_is_invalid_and_invisible():
    situations = SituationStore(InMemoryStore(), now_fn=lambda: NOW)
    expired = record_fact(situations, source="calendar:work", event_id="cal-1",
                          expires_at=NOW - timedelta(minutes=1))
    live = record_fact(situations, key="weather.rain", source="calendar:work",
                       event_id="cal-2")
    situations.retract(live.situation_id)
    assert not expired.is_valid(NOW)
    fetched = situations.get(live.situation_id)  # 撤销落库，重取即失效
    assert fetched is not None and fetched.retracted_at is not None
    assert not fetched.is_valid(NOW)
    assert situations.active_for("owner") == ()


def test_retract_event_cascades_to_derived_situations():
    situations = SituationStore(InMemoryStore(), now_fn=lambda: NOW)
    a = record_fact(situations, source="device:dev-1", event_id="evt-1")
    b = situations.record(key="curfew.inferred", value="x",
                          fact_or_inference=INFERENCE, subject_id="owner",
                          source="qq_group:1", event_id="evt-1",
                          observed_at=NOW, expires_at=None, confidence="low")
    assert situations.retract_event("evt-1") == 2
    assert not situations.get(a.situation_id).is_valid(NOW)
    assert not situations.get(b.situation_id).is_valid(NOW)


def test_situation_record_is_idempotent_per_event():
    situations = SituationStore(InMemoryStore(), now_fn=lambda: NOW)
    first = record_fact(situations, source="device:dev-1", event_id="evt-dup")
    second = record_fact(situations, source="device:dev-1", event_id="evt-dup")
    assert first.situation_id == second.situation_id
    assert len(situations.active_for("owner")) == 1


# ---------------------------------------------------------------------------
# AutonomyController（G5.2 / LIFE-02 / INV-09）
# ---------------------------------------------------------------------------


def test_expired_situation_never_triggers():
    store = InMemoryStore()
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    controller.register_proposal("meeting_at", AutonomyProposal(
        level=1, kind="remind", reason="会议临近", text="14 点有会"))
    expired = record_fact(situations, key="meeting_at", source="calendar:work",
                          event_id="cal-x", expires_at=NOW - timedelta(seconds=1))
    decision = controller.evaluate(expired, owner_ctx())
    assert decision.kind == "observe"
    rec = controller.execute(decision, owner_ctx(),
                             outbox=ProactiveOutbox(store), prefs=prefs())
    assert rec.status == "refused"
    assert rec.reason == "situation-expired-or-retracted"


def test_retraction_stops_pending_action_and_preserves_approval():
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    approvals = ApprovalBook(store, now_fn=lambda: NOW)
    port = SimulatedActionPort(registry, online={device_id: True})
    proposal = l3_action_proposal(device_id)
    controller.register_proposal("curfew.notice", proposal)
    approvals.grant("ap-1", fingerprint=action_fingerprint(
        NOTIFY, device_id, {"text": "该休息了"}))
    ctx = owner_ctx(NOTIFY)

    sit = record_fact(situations, source=f"device:{device_id}", event_id="evt-9")
    decision = controller.evaluate(sit, ctx)
    assert decision.kind == "action"
    # 授权撤销（情境失效）→ 待执行动作必须停下，且当次审批不被消耗
    situations.retract(sit.situation_id)
    rec = controller.execute(decision, ctx, device_port=port, approvals=approvals)
    assert rec.status == "refused"
    assert rec.reason == "situation-expired-or-retracted"
    assert store.get("autonomy:approval:ap-1")["used"] is False

    # 同一审批对新的有效情境仍可用一次；再用即拒（一次性当次审批）
    sit2 = record_fact(situations, source=f"device:{device_id}", event_id="evt-10")
    decision2 = controller.evaluate(sit2, ctx)
    rec2 = controller.execute(decision2, ctx, device_port=port, approvals=approvals)
    assert rec2.status == "executed"
    assert store.get("autonomy:approval:ap-1")["used"] is True
    sit3 = record_fact(situations, source=f"device:{device_id}", event_id="evt-11")
    decision3 = controller.evaluate(sit3, ctx)
    rec3 = controller.execute(decision3, ctx, device_port=port, approvals=approvals)
    assert rec3.status == "refused" and rec3.reason == "approval-required"


def test_l3_action_requires_per_instance_approval():
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    controller.register_proposal("curfew.notice", l3_action_proposal(device_id))
    sit = record_fact(situations, source=f"device:{device_id}", event_id="evt-1")
    decision = controller.evaluate(sit, owner_ctx(NOTIFY))
    port = SimulatedActionPort(registry, online={device_id: True})
    rec = controller.execute(decision, owner_ctx(NOTIFY), device_port=port,
                             approvals=ApprovalBook(store))
    assert rec.status == "refused" and rec.reason == "approval-required"


def test_l2_action_requires_preauthorized_scope_and_idempotency():
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    proposal = AutonomyProposal(
        level=2, kind="action", reason="低风险设备动作",
        capability=NOTIFY, device_id=device_id, args={"text": "hi"},
        idempotency_key="idem-abcdefgh")
    controller.register_proposal("curfew.notice", proposal)
    sit = record_fact(situations, source=f"device:{device_id}", event_id="evt-2")
    ctx = owner_ctx(NOTIFY)
    decision = controller.evaluate(sit, ctx)
    assert decision.kind == "action"
    port = SimulatedActionPort(registry, online={device_id: True})
    # scope 复核：当次 ResolvedContext 不含该能力 → 拒
    rec = controller.execute(decision, owner_ctx(), device_port=port)
    assert rec.status == "refused" and rec.reason == f"scope-denied: {NOTIFY}"
    # 主体复核：评估与执行主体不一致 → 拒
    stranger_ctx = ResolvedContext(
        conversation=CONV, persona_id=None,
        principal=Principal("anon:qq:20002", False, False, "none"),
        capability_scopes=(SCOPE_DEVICE_NOTIFY, NOTIFY))
    rec = controller.execute(decision, stranger_ctx, device_port=port)
    assert rec.status == "refused" and rec.reason == "principal-mismatch"
    # 合规路径：预授权 scope + 幂等键 + 在线设备 → executed（有真实回执）
    rec = controller.execute(decision, ctx, device_port=port)
    assert rec.status == "executed" and "action:" in rec.receipt
    # 幂等键缺失 → 拒
    no_idem = AutonomyProposal(
        level=2, kind="action", reason="缺幂等键", capability=NOTIFY,
        device_id=device_id, args={"text": "hi"})
    controller.register_proposal("curfew.noidem", no_idem)
    sit2 = record_fact(situations, key="curfew.noidem",
                       source=f"device:{device_id}", event_id="evt-3")
    decision2 = controller.evaluate(sit2, ctx)
    rec = controller.execute(decision2, ctx, device_port=port)
    assert rec.status == "refused" and rec.reason == "idempotency-key-required"


def test_remind_rechecks_budget_and_notify_scope():
    store = InMemoryStore()
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    controller.register_proposal("meeting_at", AutonomyProposal(
        level=1, kind="remind", reason="会议临近", text="14 点有会"))
    sit = record_fact(situations, key="meeting_at", source="calendar:work",
                      event_id="cal-1",
                      expires_at=NOW + timedelta(hours=1))
    outbox = ProactiveOutbox(store, now_fn=lambda: NOW)
    # 打扰预算复核：daily_limit=0 → deferred(over_budget)，绝不直发
    decision = controller.evaluate(sit, owner_ctx())
    rec = controller.execute(decision, owner_ctx(), outbox=outbox,
                             prefs=prefs(daily_limit=0))
    assert rec.status == "deferred" and rec.reason.startswith("over_budget")
    # 通知许可复核：无 device.notify/outbox.submit → 拒
    anon_scopes = ResolvedContext(
        conversation=CONV, principal=OWNER, persona_id=None,
        capability_scopes=("chat.basic",))
    rec = controller.execute(decision, anon_scopes, outbox=outbox, prefs=prefs())
    assert rec.status == "refused" and rec.reason.startswith("scope-denied")
    # 预算恢复后同一草稿可经 dispatch 侧复核放行（重执行为幂等去重，不重复建档）
    rec = controller.execute(decision, owner_ctx(), outbox=outbox, prefs=prefs())
    assert rec.status == "proposed" and rec.reason == "duplicate-suppressed"
    draft = outbox.get(rec.draft_id)
    assert draft is not None
    verdict = outbox.inspect(draft.draft_id, prefs(), now=NOW)
    assert verdict.status == "ready"


def test_inference_never_drives_action_and_l4_refused():
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    inference = situations.record(
        key="user_may_be_leaving", value="可能快出门", fact_or_inference=INFERENCE,
        subject_id="owner", source="qq_group:1", event_id="m-7",
        observed_at=NOW, expires_at=NOW + timedelta(hours=1), confidence="medium")
    controller.register_proposal("user_may_be_leaving", AutonomyProposal(
        level=2, kind="action", reason="猜他要出门", capability=NOTIFY,
        device_id=device_id, args={}, idempotency_key="idem-87654321"))
    decision = controller.evaluate(inference, owner_ctx(NOTIFY))
    assert decision.kind == "refuse"
    assert decision.reason.startswith("inference-cannot-drive-action")
    # 推断可以提醒（L1）
    controller.register_proposal("user_may_be_leaving", AutonomyProposal(
        level=1, kind="remind", reason="询问确认", text="要出门了吗？"))
    decision = controller.evaluate(inference, owner_ctx())
    assert decision.kind == "remind"
    # L4 直接拒绝
    situations2 = record_fact(situations, key="shell.please",
                              source="owner:manual", event_id="evt-l4")
    controller.register_proposal("shell.please", AutonomyProposal(
        level=4, kind="action", reason="跑任意 shell", capability=NOTIFY,
        device_id=device_id, args={}, idempotency_key="idem-00000000"))
    decision = controller.evaluate(situations2, owner_ctx(NOTIFY))
    assert decision.kind == "refuse" and decision.reason.startswith("level-forbidden")


@pytest.mark.parametrize("state,expected", [
    ("executed", "executed"),
    ("failed", "failed"),
    ("unknown", "unknown"),
    ("received", "deferred"),   # 「接受」≠「已执行」
    ("accepted", "deferred"),
])
def test_action_receipt_states(state, expected):
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    proposal = AutonomyProposal(
        level=2, kind="action", reason="回执语义", capability=NOTIFY,
        device_id=device_id, args={"text": "x"}, idempotency_key=f"idem-{state}-0001")
    controller.register_proposal("curfew.notice", proposal)
    sit = record_fact(situations, source=f"device:{device_id}",
                      event_id=f"evt-{state}")
    ctx = owner_ctx(NOTIFY)
    decision = controller.evaluate(sit, ctx)
    receipt = {"state": state}
    if state == "failed":
        receipt["error_code"] = "internal"
    port = SimulatedActionPort(registry, online={device_id: True},
                               receipts={NOTIFY: receipt})
    rec = controller.execute(decision, ctx, device_port=port)
    assert rec.status == expected


def test_device_offline_timeout_unsupported_revoked_are_unavailable():
    store = InMemoryStore()
    registry, device_id = active_device(store, scopes=(NOTIFY,))
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    proposal = AutonomyProposal(
        level=2, kind="action", reason="不可用语义", capability=NOTIFY,
        device_id=device_id, args={"text": "x"}, idempotency_key="idem-offline1")
    controller.register_proposal("curfew.notice", proposal)
    sit = record_fact(situations, source=f"device:{device_id}", event_id="evt-off")
    ctx = owner_ctx(NOTIFY)
    decision = controller.evaluate(sit, ctx)
    # 离线（缺省全部离线，fail closed）
    rec = controller.execute(decision, ctx,
                             device_port=SimulatedActionPort(registry))
    assert rec.status == "unavailable" and rec.reason == "unavailable"
    # 超时（脚本化 expired）
    rec = controller.execute(decision, ctx, device_port=SimulatedActionPort(
        registry, online={device_id: True},
        receipts={NOTIFY: DeviceUnavailable("expired", "等待回执超时")}))
    assert rec.status == "unavailable" and rec.reason == "expired"
    # 不支持的能力（设备 scope 里没有）
    unsupported = AutonomyProposal(
        level=2, kind="action", reason="不支持", capability="phone.call.audio",
        device_id=device_id, args={}, idempotency_key="idem-unsupp01")
    controller.register_proposal("curfew.unsupported", unsupported)
    sit2 = record_fact(situations, key="curfew.unsupported",
                       source=f"device:{device_id}", event_id="evt-unsup")
    ctx2 = owner_ctx(NOTIFY, "phone.call.audio")
    decision2 = controller.evaluate(sit2, ctx2)
    rec = controller.execute(decision2, ctx2, device_port=SimulatedActionPort(
        registry, online={device_id: True}))
    assert rec.status == "unavailable" and rec.reason == "capability_unknown"
    # 吊销 → 明确 device_revoked
    registry.revoke(device_id)
    rec = controller.execute(decision, ctx, device_port=SimulatedActionPort(
        registry, online={device_id: True}))
    assert rec.status == "unavailable" and rec.reason == "device_revoked"


# ---------------------------------------------------------------------------
# AgentSupervisor（G5.3）
# ---------------------------------------------------------------------------


def ticking_clock(start=1000.0):
    box = {"t": start}
    return box, (lambda: box["t"])


def test_parent_child_scope_subset_and_independent_budgets():
    store = InMemoryStore()
    clock, now_fn = ticking_clock()
    sup = AgentSupervisor(store, now_fn=now_fn)
    parent = sup.spawn(goal="整理今日安排", scope={"calendar", "notify"},
                       steps_budget=5, time_budget_s=100)
    child = sup.spawn(goal="发提醒", scope={"notify"}, steps_budget=2,
                      time_budget_s=50, parent_task_id=parent["task_id"])
    # scope 只许子集；子任务时限不得越过父任务（500s 请求被父任务 1100 截断）
    capped = sup.spawn(goal="长活子任务", scope={"notify"}, steps_budget=9,
                       time_budget_s=500, parent_task_id=parent["task_id"])
    assert capped["deadline"] == parent["deadline"]
    assert child["deadline"] <= parent["deadline"]
    with pytest.raises(SupervisorError) as exc:
        sup.spawn(goal="越权", scope={"notify", "shell"},
                  steps_budget=1, time_budget_s=10,
                  parent_task_id=parent["task_id"])
    assert exc.value.code == "scope-widening-denied"
    # 子任务 steps 预算独立：超支失败不影响父任务
    sup.start(child["task_id"])
    sup.record_step(child["task_id"])
    sup.record_step(child["task_id"])
    over = sup.record_step(child["task_id"])
    assert over["phase"] == "failed"
    assert over["last_error"] == "steps-budget-exhausted"
    assert sup.get(parent["task_id"])["phase"] == "queued"
    # 时间预算独立：拨快时钟，先验证被父截断的子任务仍受自身 deadline 约束
    clock["t"] += 60  # t=1060
    assert sup.start(capped["task_id"])["phase"] == "running"  # 1060 < 1100
    short = sup.spawn(goal="短命", scope={"notify"}, steps_budget=5,
                      time_budget_s=30, parent_task_id=parent["task_id"])
    assert sup.start(short["task_id"])["phase"] == "running"  # 1060 < 1090
    clock["t"] += 35  # t=1095：越过 short 的 1090，未越过父的 1100
    rec = sup.record_step(short["task_id"])
    assert rec["phase"] == "failed" and rec["last_error"] == "time-budget-exhausted"
    assert sup.start(parent["task_id"])["phase"] == "running"


def test_failure_backoff_and_unknown_never_retried():
    store = InMemoryStore()
    clock, now_fn = ticking_clock()
    sup = AgentSupervisor(store, now_fn=now_fn, backoff_base_s=10,
                          backoff_cap_s=60, max_attempts=2)
    task = sup.spawn(goal="flaky", scope=set(), steps_budget=3, time_budget_s=1000)
    sup.start(task["task_id"])
    # 可重试失败：指数退避重入队
    rec = sup.fail(task["task_id"], "netflap", retryable=True)
    assert rec["phase"] == "queued" and rec["attempts"] == 1
    assert rec["backoff_until"] == pytest.approx(1000.0 + 10)
    with pytest.raises(SupervisorError) as exc:
        sup.start(task["task_id"])
    assert exc.value.code == "backoff"
    clock["t"] += 10
    assert sup.start(task["task_id"])["phase"] == "running"
    rec = sup.fail(task["task_id"], "netflap-again", retryable=True)
    assert rec["attempts"] == 2 and rec["backoff_until"] == pytest.approx(1010.0 + 20)
    clock["t"] += 20
    sup.start(task["task_id"])
    # 超过 max_attempts → failed 终态
    rec = sup.fail(task["task_id"], "still-down", retryable=True)
    assert rec["phase"] == "failed"
    # unknown 终态：外部结果不可知，永不自动重试
    task2 = sup.spawn(goal="mystery", scope=set(), steps_budget=3,
                      time_budget_s=1000)
    sup.start(task2["task_id"])
    rec = sup.mark_unknown(task2["task_id"], "restarted-during-execution")
    assert rec["phase"] == "unknown"
    with pytest.raises(SupervisorError) as exc:
        sup.fail(task2["task_id"], "retry", retryable=True)
    assert exc.value.code == "terminal"


def test_cancel_cascades_to_children():
    store = InMemoryStore()
    sup = AgentSupervisor(store, now_fn=lambda: 1000.0)
    parent = sup.spawn(goal="p", scope={"a"}, steps_budget=5, time_budget_s=100)
    child = sup.spawn(goal="c", scope={"a"}, steps_budget=5, time_budget_s=50,
                      parent_task_id=parent["task_id"])
    sup.start(parent["task_id"])
    sup.start(child["task_id"])
    sup.cancel(parent["task_id"])
    assert sup.get(parent["task_id"])["phase"] == "cancelled"
    assert sup.get(child["task_id"])["phase"] == "cancelled"


def test_completion_requires_real_receipt_and_followup_uses_outbox():
    store = InMemoryStore()
    outbox = ProactiveOutbox(store, now_fn=lambda: NOW)
    sup = AgentSupervisor(store, outbox=outbox, now_fn=lambda: 1000.0)
    task = sup.spawn(goal="搜索并汇报", scope={"web"}, steps_budget=4,
                     time_budget_s=100)
    sup.start(task["task_id"])
    # 「已完成」必须有真实回执：发起任务/口头声称都不算
    with pytest.raises(SupervisorError) as exc:
        sup.complete(task["task_id"], receipt="", summary="搜完了")
    assert exc.value.code == "receipt-required"
    record, draft_id = sup.complete(
        task["task_id"],
        receipt="action:web.search:req-42:executed",
        summary="搜完了：明天下午 2 点开会，资料已整理好。")
    assert record["phase"] == "succeeded"
    assert record["receipt"].startswith("action:web.search")
    # 续聊只走 ProactiveOutbox（预算/静默/审批策略不变），不另开通道
    assert draft_id is not None
    draft = outbox.get(draft_id)
    assert draft is not None and "搜完了" in draft.text
    assert f"supervisor:{task['task_id']}" in draft.evidence
    verdict = outbox.inspect(draft_id, prefs(), now=NOW)
    assert verdict.status == "ready"  # hud + auto_send，预算内


# ---------------------------------------------------------------------------
# 模拟设备闭环（G5.4）：事件 → 情境 → 提醒（可重放）
# ---------------------------------------------------------------------------


def test_event_to_situation_to_reminder_closed_loop_replayable():
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    controller.register_proposal("screen.memo.created", AutonomyProposal(
        level=1, kind="remind", reason="检测到论文截止临近",
        text="你上次说论文要交了，需要我帮你理一下剩下的步骤吗？",
        channel="hud", importance=0.7))
    outbox = ProactiveOutbox(store, now_fn=lambda: NOW)
    bridge = SimulatedDeviceBridge(
        store, registry, situations=situations, controller=controller,
        outbox=outbox, prefs=prefs())
    ctx = resolved_owner_ctx()

    payload = event_payload()
    result = bridge.ingest_sensor_event(device_id, payload, ctx=ctx)
    # 确认（ack）+ 幂等持久化
    assert result["ack"] == [payload["event_id"]]
    assert result["duplicate"] is False
    persisted = store.get(f"device:event:{device_id}:{payload['event_id']}")
    assert persisted is not None and persisted["kind"] == "screen.memo.created"
    # 事件 → 情境（已配对设备 → 可信事实来源）
    situation = situations.get(result["situation_id"])
    assert situation is not None and situation.fact_or_inference == FACT
    assert situation.source == f"device:{device_id}"
    assert situation.is_valid(NOW)
    # 情境 → 提醒（经 Outbox，预算内 ready）
    assert result["decision"]["kind"] == "remind"
    assert result["execution"]["status"] == "proposed"
    assert result["execution"]["reason"] == "ready-for-dispatch"
    draft = outbox.get(result["execution"]["draft_id"])
    assert draft is not None and "论文" in draft.text
    assert payload["event_id"] in draft.evidence

    # 重放同一事件：幂等去重，不产生第二个情境/草稿
    again = bridge.ingest_sensor_event(device_id, payload, ctx=ctx)
    assert again == {"ack": [payload["event_id"]], "duplicate": True}
    assert len(situations.active_for("owner")) == 1


def test_forged_device_events_never_trigger():
    store = InMemoryStore()
    registry, device_id = active_device(store)
    situations = SituationStore(store, now_fn=lambda: NOW)
    controller = AutonomyController(situations, now_fn=lambda: NOW)
    controller.register_proposal("screen.memo.created", AutonomyProposal(
        level=1, kind="remind", reason="不应触发", text="不应出现"))
    bridge = SimulatedDeviceBridge(
        store, registry, situations=situations, controller=controller,
        outbox=ProactiveOutbox(store), prefs=prefs())
    ctx = resolved_owner_ctx()
    # 未配对设备
    with pytest.raises(BridgeError) as exc:
        bridge.ingest_sensor_event("ff" * 16, event_payload(), ctx=ctx)
    assert exc.value.code == "auth_failed"
    # 已吊销设备
    registry.revoke(device_id)
    with pytest.raises(BridgeError) as exc:
        bridge.ingest_sensor_event(device_id, event_payload(), ctx=ctx)
    assert exc.value.code == "device_revoked"
    # schema 非法（缺 event_id）的已配对设备事件
    good_store = InMemoryStore()
    reg2, dev2 = active_device(good_store)
    bridge2 = SimulatedDeviceBridge(good_store, reg2,
                                    situations=SituationStore(good_store))
    bad = event_payload()
    bad.pop("event_id")
    with pytest.raises(BridgeError) as exc:
        bridge2.ingest_sensor_event(dev2, bad)
    assert exc.value.code == "invalid_message"
    # 全部零副作用：无情境、无草稿、无持久化事件
    assert situations.active_for("owner") == ()
    assert [k for k in store.keys() if k.startswith("situation:")] == []
    assert [k for k in store.keys() if k.startswith("device:event:")] == []
    assert [k for k in store.keys() if k.startswith("life:draft:")] == []
