"""插件动作门控：按风险分级裁决，PluginGateway 执行前的唯一策略点。

分层：插件框架契约（framework，issue #31）。

门控矩阵（与 ProactiveOutbox / ShellPolicy 的保守默认对齐）：

===========  ==========  =====================  =========================
风险级        非 owner     owner 未显式批准        owner 显式批准
===========  ==========  =====================  =========================
read_only    denied      ready                  ready
communicate  denied      needs_approval         ready
transaction  denied      needs_approval         ready（且过限额/日限）
未声明动作    denied（任何情况）
===========  ==========  =====================  =========================

- transaction 的批准**不可由 auto_send 类配置代劳**——每笔单独人审，
  与 ProactiveOutbox「call/direct_message 强制 needs_approval」同款语义；
- 未注册插件 / 未声明动作 / 未声明风险级 → 一律 denied；
- 台账（audit）只记动作与裁决，不记 args 全文（可能含隐私 / 凭据形状）。

信任级别钩子（G4，manifest.trust）：``trust="external"`` 的插件执行
**非 read_only** 动作时强制 needs_approval——宿主的 auto_approve_risks
放宽对 external 插件不生效；显式批准后仍走原有 ready 路径（不新造状态机）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from threading import Lock
from typing import Any, Callable, Mapping

from whale_core.plugins.manifest import RiskClass
from whale_core.plugins.registry import PluginRegistry


@dataclass(frozen=True)
class PluginVerdict:
    status: str  # ready | needs_approval | denied
    reason: str
    risk: str = ""
    plugin_id: str = ""
    action: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "risk": self.risk,
            "plugin_id": self.plugin_id,
            "action": self.action,
        }


class PluginActionPolicy:
    def __init__(
        self,
        registry: PluginRegistry,
        *,
        max_transaction_amount: float | None = None,
        max_daily_transactions: int | None = None,
        transaction_count_today: Callable[[], int] | None = None,
        auto_approve_risks: frozenset[str] | None = None,
    ) -> None:
        if max_transaction_amount is not None and (
            not math.isfinite(max_transaction_amount) or max_transaction_amount < 0
        ):
            raise ValueError("max_transaction_amount must be finite and non-negative")
        if max_daily_transactions is not None and max_daily_transactions < 0:
            raise ValueError("max_daily_transactions must be non-negative")
        # 按风险级的免审配置（gap-analysis §4 R1，owner 定「合理放宽」）：
        # 单 owner 场景可把 communicate 配成自动；**transaction 永不可自动**
        # （钱的边界不松）；read_only 本就免审。配置在宿主装配层注入并公示。
        auto = frozenset(auto_approve_risks or ())
        if RiskClass.TRANSACTION.value in auto:
            raise ValueError("transaction risk can never be auto-approved")
        self.auto_approve_risks = frozenset(
            RiskClass(r).value for r in auto  # 非法值在此抛 ValueError
        )
        self.registry = registry
        self.max_transaction_amount = max_transaction_amount
        self.max_daily_transactions = max_daily_transactions
        self._txn_count = transaction_count_today
        self._txn_lock = Lock()
        self._reservation_day = datetime.now(timezone.utc).date()
        self._reserved_transactions = 0

    def evaluate(
        self,
        plugin_id: str,
        action: str,
        *,
        is_owner: bool = False,
        explicitly_approved: bool = False,
        args: Mapping[str, Any] | None = None,
        reserve_transaction: bool = False,
    ) -> PluginVerdict:
        manifest = self.registry.manifest(plugin_id)
        if manifest is None:
            return PluginVerdict("denied", "unknown plugin", plugin_id=plugin_id, action=action)
        if action not in manifest.actions:
            return PluginVerdict(
                "denied", "action not declared in manifest", risk="", plugin_id=plugin_id, action=action
            )
        risk = manifest.risk_of(action)
        if risk is None:
            return PluginVerdict(
                "denied", "action has no declared risk class", plugin_id=plugin_id, action=action
            )
        if not is_owner:
            return PluginVerdict(
                "denied",
                "plugin actions require authenticated owner",
                risk=risk.value,
                plugin_id=plugin_id,
                action=action,
            )

        if risk is RiskClass.READ_ONLY:
            return PluginVerdict("ready", "read-only action", risk.value, plugin_id, action)

        # G4 信任钩子：external 插件的非 read_only 动作强制人审，
        # 宿主级 auto_approve 放宽对其不适用（approval 通道不变）。
        external = getattr(manifest, "trust", "bundled") == "external"
        if external and not explicitly_approved:
            return PluginVerdict(
                "needs_approval",
                "external-trust plugin requires explicit approval for non-read-only action",
                risk.value, plugin_id, action,
            )

        if risk is RiskClass.COMMUNICATE:
            if explicitly_approved:
                return PluginVerdict("ready", "approved outbound", risk.value, plugin_id, action)
            if risk.value in self.auto_approve_risks:
                return PluginVerdict(
                    "ready", "communicate auto-approved by host policy (R1)",
                    risk.value, plugin_id, action,
                )
            return PluginVerdict(
                "needs_approval", "outbound message requires human review",
                risk.value, plugin_id, action,
            )

        # TRANSACTION
        if not explicitly_approved:
            return PluginVerdict(
                "needs_approval", "transactions always require explicit per-item approval",
                risk.value, plugin_id, action,
            )
        if self.max_transaction_amount is None:
            return PluginVerdict(
                "denied", "transaction amount cap not configured",
                risk.value, plugin_id, action,
            )
        amount = _amount_of(args)
        if amount is None:
            return PluginVerdict(
                "denied", "transaction amount missing or invalid",
                risk.value, plugin_id, action,
            )
        if amount > self.max_transaction_amount:
            return PluginVerdict(
                "denied", f"amount {amount:g} over cap {self.max_transaction_amount:g}",
                risk.value, plugin_id, action,
            )
        if self.max_daily_transactions is None:
            return PluginVerdict(
                "denied", "daily transaction cap not configured",
                risk.value, plugin_id, action,
            )
        if self.max_daily_transactions is not None:
            if self._txn_count is None:
                return PluginVerdict(
                    "denied", "daily transaction counter not configured",
                    risk.value, plugin_id, action,
                )
            # Serialize check+reservation so concurrent dispatches cannot all pass
            # against the same count. Reservations are conservative: a failed executor
            # still consumes this process's daily allowance.
            with self._txn_lock:
                today = datetime.now(timezone.utc).date()
                if today != self._reservation_day:
                    self._reservation_day = today
                    self._reserved_transactions = 0
                try:
                    external_count = int(self._txn_count())
                except Exception:
                    return PluginVerdict(
                        "denied", "daily transaction counter unavailable",
                        risk.value, plugin_id, action,
                    )
                if external_count < 0:
                    return PluginVerdict(
                        "denied", "daily transaction counter invalid",
                        risk.value, plugin_id, action,
                    )
                used = max(external_count, self._reserved_transactions)
                if used >= self.max_daily_transactions:
                    return PluginVerdict(
                        "denied", "daily transaction cap reached",
                        risk.value, plugin_id, action,
                    )
                if reserve_transaction:
                    self._reserved_transactions = used + 1
        return PluginVerdict("ready", "approved transaction within caps", risk.value, plugin_id, action)

    def audit(self, target: str, verdict: PluginVerdict, *, source: str = "") -> dict:
        """台账记录：不含 args / 消息正文（隐私与凭据面最小化）。"""
        return {
            "target": (target or "")[:200],
            "source": source,
            "at": datetime.now(timezone.utc).isoformat(),
            **verdict.as_dict(),
        }


def _amount_of(args: Mapping[str, Any] | None) -> float | None:
    if not isinstance(args, Mapping) or "amount" not in args:
        return None
    raw = args.get("amount")
    if isinstance(raw, bool):
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return amount
