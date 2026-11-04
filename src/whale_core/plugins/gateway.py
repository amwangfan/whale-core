"""插件网关：kind ``plugin_action`` 的单一执行点（策略内嵌，不可绕过）。

分层：插件框架契约（framework，issue #31）。

回执约定（可观测闭环，与 shell_denied 同款——拒绝不静默）：

- denied        → ``plugin-denied:{plugin}:{action}:{reason}``
- needs_approval→ ``plugin-hold:{plugin}:{action}``（宿主据此走人审 / Outbox 草稿）
- ready         → ``plugin:{plugin}:{action}:ok|fail:{receipt}``

executor 抛异常按 fail 处理，不炸派发循环；非 plugin_action 意图原样忽略
（组合网关里可与其它子网关并存）。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from whale_core.core.types import ActionIntent
from whale_core.plugins.policy import PluginActionPolicy, PluginVerdict
from whale_core.plugins.registry import NullPluginExecutor, PluginRegistry


class PluginGateway:
    """唯一插件动作执行点。

    owner_verifier / approval_verifier 必须查询宿主拥有的会话/审批状态，
    不得把 ActionIntent.payload 中的字段当作身份或审批证明。默认全拒绝。
    """

    def __init__(
        self,
        registry: PluginRegistry,
        policy: PluginActionPolicy,
        *,
        owner_verifier: Callable[[ActionIntent], bool] | None = None,
        approval_verifier: Callable[[ActionIntent], bool] | None = None,
        grant_verifier: Callable[[ActionIntent], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self._owner_verifier = owner_verifier or (lambda _intent: False)
        self._approval_verifier = approval_verifier or (lambda _intent: False)
        self._grant_verifier = grant_verifier or (lambda _intent: False)
        self.last_verdict: PluginVerdict | None = None
        self.dispatch_log: list[str] = []

    def dispatch(self, intent: ActionIntent) -> str:
        if intent.kind != "plugin_action":
            return f"plugin-gateway:ignored:{intent.kind}"
        raw_payload = intent.payload
        if not isinstance(raw_payload, Mapping):
            receipt = "plugin-denied::invalid-payload"
            self.dispatch_log.append(receipt)
            return receipt
        payload: Mapping[str, Any] = raw_payload
        plugin_id = str(payload.get("plugin_id") or "")
        action = str(payload.get("action") or "")
        target = str(payload.get("target") or "")
        raw_args = payload.get("args") or {}
        if not isinstance(raw_args, Mapping):
            receipt = f"plugin-denied:{plugin_id}:{action}:invalid-args"
            self.dispatch_log.append(receipt)
            return receipt
        args = dict(raw_args)
        # Authorization is resolved from trusted host state, never payload flags.
        try:
            is_owner = bool(self._owner_verifier(intent))
            explicitly_approved = bool(self._approval_verifier(intent)) if is_owner else False
        except Exception:
            # A failed auth/approval lookup must fail closed.
            is_owner = False
            explicitly_approved = False
        try:
            grant_authorized = bool(self._grant_verifier(intent)) if not is_owner else False
        except Exception:
            grant_authorized = False
        verdict = self.policy.evaluate(
            plugin_id,
            action,
            is_owner=is_owner,
            explicitly_approved=explicitly_approved,
            grant_authorized=grant_authorized,
            args=args,
            reserve_transaction=True,
        )
        self.last_verdict = verdict
        if verdict.status == "denied":
            receipt = f"plugin-denied:{plugin_id}:{action}:{verdict.reason}"
            self.dispatch_log.append(receipt)
            return receipt
        if verdict.status == "needs_approval":
            receipt = f"plugin-hold:{plugin_id}:{action}"
            self.dispatch_log.append(receipt)
            return receipt

        executor = self.registry.executor(plugin_id) or NullPluginExecutor()
        # 仅 shell 执行器需要宿主身份标志；其它插件保持原 args（测例/契约兼容）
        exec_args = dict(args)
        if plugin_id == "shell_sandbox":
            exec_args["_whale_is_owner"] = bool(is_owner)
            exec_args["_whale_shell_grant"] = bool(grant_authorized)
        try:
            ok, text = executor.execute(action, target, exec_args)
        except Exception:
            ok, text = False, "executor error"
        receipt = f"plugin:{plugin_id}:{action}:{'ok' if ok else 'fail'}:{str(text)[:120]}"
        self.dispatch_log.append(receipt)
        return receipt
