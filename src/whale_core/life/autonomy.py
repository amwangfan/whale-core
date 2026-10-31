"""G5.2：AutonomyController —— 自主等级 L0-L4 与执行前硬围栏（LIFE-02/INV-09）。

原则（计划 §7.2/§7.3 与 §11 LIFE-02）：

- 规则/模型只产出 ``AutonomyProposal``（"想做什么"）；**想做什么 ≠ 已获准**。
- ``evaluate`` 只做分级裁决；``execute`` 在**真正执行前**重新校验：
  情境仍有效（未过期/未撤销）、决定未过期、ResolvedContext 主体一致、
  scope/预授权、当次审批（L3，一次性）、打扰预算（L1 走 ProactiveOutbox
  既有 inspect：预算/静默时段/渠道许可）、幂等键（L2/L3 动作必带）。
- 推断（inference）最多驱动 L1（提醒/询问/建议），**永不驱动动作**。
- L4（任意 shell、删数据、改密钥、支付等）直接拒绝。
- 设备动作回执校验：``executed`` 才算完成；``received``/``accepted`` 只是
  中间态，不声称完成（「接受」≠「已执行」）；``unknown`` 不自动重试。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from whale_core.life.conversation_context import (
    SCOPE_DEVICE_NOTIFY,
    SCOPE_OUTBOX_SUBMIT,
)
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
from whale_core.life.situation import INFERENCE, SituationState, SituationStore

# ---- 自主等级（§7.2 表） -------------------------------------------------

LEVEL_OBSERVE = 0        # 观察已授权事件
LEVEL_REMIND = 1         # 提醒、询问、建议（通知许可 + 打扰预算 + 事实依据）
LEVEL_ACT_LOW_RISK = 2   # 低风险动作：能力预授权 + 可撤销 + 幂等
LEVEL_ACT_EXTERNAL = 3   # 外发消息/预约/下单：当次审批（或严格预授权）
LEVEL_FORBIDDEN = 4      # 任意 shell/删数据/改密钥/支付：直接拒绝

#: 决定动作词表
KIND_OBSERVE = "observe"
KIND_REMIND = "remind"
KIND_DRAFT = "draft"
KIND_ACTION = "action"
KIND_REFUSE = "refuse"
_DECISION_KINDS = frozenset(
    {KIND_OBSERVE, KIND_REMIND, KIND_DRAFT, KIND_ACTION, KIND_REFUSE})

#: action.receipt 终态（与 contracts/v1 §4 一致；received/accepted 为中间态）
TERMINAL_RECEIPT_STATES = frozenset({"executed", "failed", "unknown"})
RECEIPT_STATES = TERMINAL_RECEIPT_STATES | {"received", "accepted"}

DEFAULT_ACTION_TIMEOUT_S = 15.0


class AutonomyError(ValueError):
    """自主裁决/执行被拒绝（携带稳定 code）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeviceUnavailable(Exception):
    """设备面明确不可用：离线/超时/吊销/不支持（携带协议错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeviceActionPort(Protocol):
    """Core→设备动作通道（生产=GatewayActionPort，模拟=SimulatedActionPort）。"""

    def send_action(self, device_id: str, *, capability: str,
                    args: Mapping[str, Any], idempotency_key: str,
                    timeout_s: float) -> Mapping[str, Any]:
        """下发 action.request 并等待回执 payload；不可用抛 DeviceUnavailable。"""
        ...


def action_fingerprint(capability: str, device_id: str, args: Mapping[str, Any]) -> str:
    """精确动作指纹：审批/执行绑定到 能力+设备+参数，换参数即失效。"""
    canonical = json.dumps(
        {"capability": capability, "device_id": device_id, "args": dict(args)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_action_receipt(payload: Mapping[str, Any], *,
                            expect_action_id: str = "") -> str:
    """校验 action.receipt payload，返回 state；畸形回执抛 AutonomyError。"""
    if not isinstance(payload, Mapping):
        raise AutonomyError("invalid-receipt", "回执不是对象")
    state = str(payload.get("state") or "")
    if state not in RECEIPT_STATES:
        raise AutonomyError("invalid-receipt", f"未知回执状态 {state!r}")
    action_id = str(payload.get("action_id") or "")
    if not action_id:
        raise AutonomyError("invalid-receipt", "回执缺少 action_id")
    if expect_action_id and action_id != expect_action_id:
        raise AutonomyError("invalid-receipt", "回执 action_id 与请求不符")
    return state


class ApprovalBook:
    """L3 当次审批台账：一次性、绑定精确动作指纹（与 runtime 审批同构）。

    审批只能由可信宿主入口写入（``grant``）；``consume`` 原子消耗一次，
    重放/换参数/重复消费全部 fail closed。kv 键 ``autonomy:approval:{ref}``。
    """

    def __init__(self, store, *, now_fn: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def _key(self, approval_ref: str) -> str:
        return f"autonomy:approval:{approval_ref}"

    def grant(self, approval_ref: str, *, fingerprint: str) -> None:
        if not approval_ref or not fingerprint:
            raise AutonomyError("bad-request", "approval_ref 与 fingerprint 必填")
        if self.store.get(self._key(approval_ref)) is not None:
            raise AutonomyError("conflict", "approval_ref 已存在（审批一次性，需显式重签）")
        self.store.put(self._key(approval_ref), {
            "fingerprint": fingerprint, "used": False,
            "created_at": self._now().isoformat()})

    def consume(self, approval_ref: str, *, fingerprint: str) -> bool:
        """原子消耗一次性审批（G6 红队高-3：get→put 必须同事务，否则并发双花）。"""
        used_at = self._now().isoformat()

        def _tx(tx):
            record = tx.get(self._key(approval_ref))
            if not isinstance(record, Mapping):
                return False
            if record.get("used") or record.get("fingerprint") != fingerprint:
                return False
            record = dict(record)
            record["used"] = True
            record["used_at"] = used_at
            tx.put(self._key(approval_ref), record)
            return True

        return bool(self.store.transact(_tx))


@dataclass(frozen=True)
class AutonomyProposal:
    """一条自主提议（想做什么；不等于获准）。"""

    level: int
    kind: str                      # remind | draft | action（observe/refuse 由裁决给出）
    reason: str
    text: str = ""                 # remind/draft 文案
    channel: str = "hud"
    importance: float = 0.5
    topic: str = ""                # outbox 主题去重键
    capability: str = ""           # action 的设备能力（contracts/v1 §5 枚举）
    device_id: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""      # action 必填（幂等）
    approval_ref: str = ""         # L3 必填（当次审批引用）
    timeout_s: float = DEFAULT_ACTION_TIMEOUT_S
    expires_at: datetime | None = None  # 决定自身期限（缺省随情境期限）

    def __post_init__(self) -> None:
        if self.kind not in _DECISION_KINDS - {KIND_OBSERVE, KIND_REFUSE}:
            raise AutonomyError("bad-request", f"提议类型须为 remind|draft|action， got {self.kind!r}")
        if not (LEVEL_OBSERVE <= int(self.level) <= LEVEL_FORBIDDEN):
            raise AutonomyError("bad-request", "level 须在 0..4")
        if not self.reason.strip():
            raise AutonomyError("bad-request", "提议必须携带 reason（可审计依据）")


@dataclass(frozen=True)
class AutonomyDecision:
    """裁决结果：等级 + 动作 + 理由；绑定情境与（可选）评估时上下文身份。"""

    decision_id: str
    situation_id: str
    level: int
    kind: str                      # observe | remind | draft | action | refuse
    reason: str
    proposal: AutonomyProposal | None
    decided_at: datetime
    principal_id: str = ""         # 评估时 ResolvedContext 主体（执行前复核）
    conversation_key: str = ""


@dataclass(frozen=True)
class ExecutionRecord:
    """执行结局。status 词表：

    observed / proposed / awaiting_approval / executed / failed / refused /
    unavailable / deferred / unknown —— 没有回执绝不出现 executed。
    """

    decision_id: str
    status: str
    reason: str
    receipt: str = ""
    draft_id: str = ""


ProposalFn = Callable[[SituationState], AutonomyProposal | None]


class AutonomyController:
    """情境 → 分级决定 → 受门控执行。"""

    def __init__(self, situations: SituationStore, *,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        self.situations = situations
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._rules: dict[str, ProposalFn] = {}

    # ---- 规则注册（想做什么由规则/模型提议） -----------------------------

    def register_rule(self, situation_key: str, fn: ProposalFn) -> None:
        if not situation_key.strip():
            raise AutonomyError("bad-request", "rule key 必填")
        self._rules[situation_key.strip()] = fn

    def register_proposal(self, situation_key: str, proposal: AutonomyProposal) -> None:
        self.register_rule(situation_key, lambda _sit: proposal)

    # ---- 裁决 ------------------------------------------------------------

    def evaluate(self, situation: SituationState, ctx=None, *,
                 now: datetime | None = None) -> AutonomyDecision:
        """把情境裁决为 observe/remind/draft/action/refuse。

        过期/撤销情境 → observe（不触发）；无规则 → observe；
        L4 → refuse；推断提议动作（≥L2）→ refuse。
        """
        now = now or self._now()

        def decide(level: int, kind: str, reason: str,
                   proposal: AutonomyProposal | None = None) -> AutonomyDecision:
            return AutonomyDecision(
                decision_id=f"dec-{hashlib.sha256((situation.situation_id + kind + now.isoformat()).encode()).hexdigest()[:20]}",
                situation_id=situation.situation_id,
                level=level, kind=kind, reason=reason, proposal=proposal,
                decided_at=now,
                principal_id=(getattr(getattr(ctx, "principal", None), "principal_id", "") or ""),
                conversation_key=(ctx.conversation.as_key()
                                  if getattr(ctx, "conversation", None) is not None else ""),
            )

        if not situation.is_valid(now):
            return decide(LEVEL_OBSERVE, KIND_OBSERVE, "situation-inactive")
        rule = self._rules.get(situation.key)
        if rule is None:
            return decide(LEVEL_OBSERVE, KIND_OBSERVE, "no-rule")
        proposal = rule(situation)
        if proposal is None:
            return decide(LEVEL_OBSERVE, KIND_OBSERVE, "rule-declined")
        if proposal.level >= LEVEL_FORBIDDEN:
            return decide(LEVEL_FORBIDDEN, KIND_REFUSE,
                          "level-forbidden: L4 动作直接拒绝", proposal)
        if proposal.level <= LEVEL_OBSERVE:
            return decide(LEVEL_OBSERVE, KIND_OBSERVE, "rule-declined: L0 只观察")
        if proposal.kind == KIND_ACTION and proposal.level < LEVEL_ACT_LOW_RISK:
            # L1 只允许提醒/询问/建议；动作最低 L2
            return decide(proposal.level, KIND_REFUSE, "action-requires-L2", proposal)
        if situation.fact_or_inference == INFERENCE and proposal.level >= LEVEL_ACT_LOW_RISK:
            # 推断不是事实依据：最多提醒/询问，永不驱动动作
            return decide(proposal.level, KIND_REFUSE,
                          "inference-cannot-drive-action", proposal)
        if proposal.level >= LEVEL_REMIND and not (
                situation.event_id or situation.evidence):
            return decide(proposal.level, KIND_REFUSE,
                          "no-evidence: 无来源依据不提醒不动作", proposal)
        return decide(proposal.level, proposal.kind, proposal.reason, proposal)

    # ---- 执行前复核 + 执行（LIFE-02/INV-09） ------------------------------

    def execute(self, decision: AutonomyDecision, ctx, *,
                now: datetime | None = None,
                outbox: ProactiveOutbox | None = None,
                prefs: ProactivePrefs | None = None,
                device_port: DeviceActionPort | None = None,
                approvals: ApprovalBook | None = None,
                busy: bool = False) -> ExecutionRecord:
        """每次执行前重新校验：情境/期限/主体/scope/审批/预算/幂等/撤销。"""
        now = now or self._now()

        def done(status: str, reason: str, *, receipt: str = "",
                 draft_id: str = "") -> ExecutionRecord:
            return ExecutionRecord(decision_id=decision.decision_id, status=status,
                                   reason=reason, receipt=receipt, draft_id=draft_id)

        # 1. 情境复核：过期/撤销即失效——撤销必须能停止待执行动作
        situation = self.situations.get(decision.situation_id)
        if situation is None or not situation.is_valid(now):
            return done("refused", "situation-expired-or-retracted")
        # 2. 期限复核：决定/情境任一过期都不得执行
        proposal = decision.proposal
        deadline = (proposal.expires_at if proposal and proposal.expires_at
                    else situation.expires_at)
        if deadline is not None and now >= deadline:
            return done("refused", "decision-expired")
        if decision.kind in (KIND_OBSERVE,):
            return done("observed", decision.reason)
        if decision.kind == KIND_REFUSE:
            return done("refused", decision.reason)
        # 3. 上下文复核：执行必须携带服务端解析的 ResolvedContext，
        #    且主体/会话与评估时一致（跨会话/换人执行一律拒绝）
        if ctx is None:
            return done("refused", "no-context")
        principal_id = getattr(getattr(ctx, "principal", None), "principal_id", "")
        if decision.principal_id and principal_id != decision.principal_id:
            return done("refused", "principal-mismatch")
        conversation = getattr(ctx, "conversation", None)
        if decision.conversation_key and conversation is not None \
                and conversation.as_key() != decision.conversation_key:
            return done("refused", "conversation-mismatch")
        scopes = set(getattr(ctx, "capability_scopes", ()) or ())
        if proposal is None:
            return done("refused", "missing-proposal")

        if decision.kind in (KIND_REMIND, KIND_DRAFT):
            # 4a. 通知许可：提醒/草稿须具备通知或出站提交 scope
            if not (SCOPE_DEVICE_NOTIFY in scopes or SCOPE_OUTBOX_SUBMIT in scopes):
                return done("refused", f"scope-denied: {SCOPE_DEVICE_NOTIFY}")
            if outbox is None:
                return done("refused", "no-outbox")
            draft = ProactiveDraft(
                draft_id=f"auto:{decision.decision_id}",
                text=proposal.text or f"{situation.key}: {situation.value}",
                reason=proposal.reason,
                evidence=tuple(e for e in (situation.event_id,
                                           situation.situation_id) if e),
                channel=proposal.channel,
                importance=proposal.importance,
                expires_at=deadline,
                topic=proposal.topic,
            )
            created = outbox.propose(draft)
            if not created:
                # 幂等重放：同一决定重复执行不产生第二条草稿
                return done("proposed", "duplicate-suppressed", draft_id=draft.draft_id)
            if decision.kind == KIND_DRAFT:
                return done("proposed", "draft-recorded", draft_id=draft.draft_id)
            if prefs is None:
                return done("awaiting_approval", "no-prefs: 草稿已登记，待策略裁决",
                            draft_id=draft.draft_id)
            # 5a. 打扰预算/静默时段/渠道许可复核（既有 Outbox 策略，不另开口子）
            verdict = outbox.inspect(draft.draft_id, prefs, now=now, busy=busy)
            if verdict.status == "ready":
                return done("proposed", "ready-for-dispatch", draft_id=draft.draft_id)
            if verdict.status == "needs_approval":
                return done("awaiting_approval", verdict.reason, draft_id=draft.draft_id)
            return done("deferred", f"{verdict.status}: {verdict.reason}",
                        draft_id=draft.draft_id)

        # decision.kind == KIND_ACTION
        if decision.level < LEVEL_ACT_LOW_RISK:
            return done("refused", "action-requires-L2")
        capability = proposal.capability
        if not capability:
            return done("refused", "capability-required")
        # 4b. scope 复核：能力须在当次 ResolvedContext 内（预授权收紧在此生效）
        if capability not in scopes:
            return done("refused", f"scope-denied: {capability}")
        # 5b. 幂等键：L2/L3 动作必带，缺键拒绝（防重复副作用）
        if not proposal.idempotency_key:
            return done("refused", "idempotency-key-required")
        # 5c. L3 当次审批：一次性、绑定精确动作指纹
        fingerprint = action_fingerprint(capability, proposal.device_id, proposal.args)
        if decision.level >= LEVEL_ACT_EXTERNAL:
            if not proposal.approval_ref or approvals is None or not approvals.consume(
                    proposal.approval_ref, fingerprint=fingerprint):
                return done("refused", "approval-required")
        if device_port is None:
            return done("refused", "no-device-port")
        try:
            receipt = device_port.send_action(
                proposal.device_id, capability=capability,
                args=dict(proposal.args),
                idempotency_key=proposal.idempotency_key,
                timeout_s=proposal.timeout_s)
        except DeviceUnavailable as exc:
            # 设备离线/超时/吊销/不支持：明确不可用，不谎报、不盲目重试
            return done("unavailable", exc.code)
        try:
            state = validate_action_receipt(receipt)
        except AutonomyError as exc:
            return done("unknown", exc.code)
        receipt_ref = (f"action:{proposal.device_id}:{capability}:"
                       f"{receipt.get('action_id')}")
        if state == "executed":
            return done("executed", "device-receipt-executed", receipt=receipt_ref)
        if state == "failed":
            return done("failed",
                        f"device-receipt-failed:{receipt.get('error_code') or 'internal'}",
                        receipt=receipt_ref)
        if state == "unknown":
            return done("unknown", "device-receipt-unknown: 外部结果不可知，不自动重试",
                        receipt=receipt_ref)
        # received/accepted：中间态，「接受」≠「已执行」，不声称完成
        return done("deferred", f"receipt-not-terminal:{state}", receipt=receipt_ref)
