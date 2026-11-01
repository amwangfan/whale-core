"""策略裁决与审批绑定（issue #30 拆分，安全敏感——逻辑逐行等价搬运）。

- ``_action_fingerprint``：动作精确指纹（payload 规范化 sha256）；
- ``runtime_owner_verifier``：识别来自已认证本地宿主 runtime 的动作；
- ``runtime_approval_verifier``：宿主持有的一次性精确动作审批查找——
  意图/模型标志永远不能自批，重放 fail-closed；
- ``validate_tool_manifest``：handler 之前先裁决插件风险声明是否匹配。
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Callable

from whale_core.core.types import ActionIntent
from whale_core.memory.store import MemoryStore
from whale_core.plugins.manifest import RiskClass
from whale_core.core.runtime.tools import ActionGatewayLike, ToolSpec

_APPROVAL_LOCK = threading.Lock()


def _action_fingerprint(intent: ActionIntent) -> str:
    return hashlib.sha256(json.dumps(dict(intent.payload), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def runtime_owner_verifier(store: MemoryStore) -> Callable[[ActionIntent], bool]:
    """Recognize actions originating in the authenticated local host runtime.

    source is supplied by host code, never read from user/model slots. External
    channel adapters must perform their own binding before calling this runtime.
    """
    def verify(intent: ActionIntent) -> bool:
        parts = intent.reason.split(":")
        if len(parts) != 3 or parts[0] != "runtime-task":
            return False
        record = store.get(f"task:{parts[1]}")
        journal = store.get(f"runtime:task:{parts[1]}")
        return (isinstance(record, dict) and isinstance(journal, dict)
                and journal.get("source") in {"api", "cli", "runtime"}
                and record.get("status") == "running" and not record.get("cancel_requested")
                and record.get("approval_token") == intent.reason
                and record.get("pending_fingerprint") == _action_fingerprint(intent))
    return verify


def runtime_approval_verifier(store: MemoryStore) -> Callable[[ActionIntent], bool]:
    """Host-owned exact-action, one-use approval lookup for PluginGateway.

    An intent/model flag can never approve itself. Only decide_task, exposed by
    an authenticated host, creates the decision. Replays fail closed.
    """
    def verify(intent: ActionIntent) -> bool:
        parts = intent.reason.split(":")
        if len(parts) != 3 or parts[0] != "runtime-task":
            return False
        with _APPROVAL_LOCK:
            key = f"task:{parts[1]}"
            record = store.get(key)
            if not isinstance(record, dict):
                return False
            valid = (record.get("status") == "running" and record.get("decision") == "approve"
                     and not record.get("approval_used") and not record.get("cancel_requested")
                     and record.get("approval_token") == intent.reason
                     and record.get("pending_fingerprint") == _action_fingerprint(intent))
            if valid:
                record["approval_used"] = True
                store.put(key, record)
            return valid
    return verify


def validate_tool_manifest(spec: ToolSpec, gateway: ActionGatewayLike | None) -> str | None:
    """Reject mismatched plugin risk declarations before touching a handler."""
    if spec.plugin_id and spec.action:
        registry = getattr(gateway, "registry", None)
        if registry is not None:
            manifest = registry.manifest(spec.plugin_id)
            actual = manifest.risk_of(spec.action) if manifest is not None else None
            if actual is None or actual.value != spec.risk_class:
                return "manifest-risk-mismatch"
        elif spec.risk_class == RiskClass.READ_ONLY.value:
            return "unverified-plugin-risk"
    return None
