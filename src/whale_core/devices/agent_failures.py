"""安卓自主体编排层失败语义（core#58）。

与 ``life/autonomy.py`` / gateway 错误码对齐；编排循环据此决定重试或停止，
**不**平行发明另一套回执状态机。

稳定对外码（编排层只认这些）：

- ``failed/timeout``：等待回执/查询超时（gateway ``expired`` / receipt ``timeout``）
- ``failed/unavailable``：estop/暂停/离线/吊销/权限缺失等设备面不可用
- ``failed/scope_denied``：设备注册 scope 不足或会话未授该能力
- ``unknown``：结果不明——**禁止自动重放**（与 autonomy「unknown 不自动重试」一致）
"""
from __future__ import annotations

from typing import Any, Mapping

from whale_core.devices.actions import DeviceUnavailable
from whale_core.life.autonomy import validate_action_receipt, AutonomyError

FAILED_TIMEOUT = "failed/timeout"
FAILED_UNAVAILABLE = "failed/unavailable"
FAILED_SCOPE_DENIED = "failed/scope_denied"
UNKNOWN = "unknown"

#: 编排层稳定失败码全集
ORCH_FAILURE_CODES = frozenset({
    FAILED_TIMEOUT, FAILED_UNAVAILABLE, FAILED_SCOPE_DENIED, UNKNOWN,
})

#: 仅只读能力在 timeout 后允许有界自动重试（可用新幂等键）
SAFE_TIMEOUT_RETRY_CAPABILITIES = frozenset({"device.agent.observe"})

#: gateway / DeviceUnavailable / receipt.error_code → 编排码
_UNAVAILABLE_CODES = frozenset({
    "unavailable", "device_revoked", "auth_failed", "permission_missing",
    "background_restricted", "rate_limited", "internal",
})
_TIMEOUT_CODES = frozenset({"expired", "timeout"})
_SCOPE_CODES = frozenset({"scope_denied", "capability_unknown"})


def classify_gateway_code(code: str) -> str:
    """把 gateway / DeviceUnavailable.code 归一成编排失败码。"""
    c = (code or "").strip()
    if c in _TIMEOUT_CODES:
        return FAILED_TIMEOUT
    if c in _SCOPE_CODES:
        return FAILED_SCOPE_DENIED
    if c in _UNAVAILABLE_CODES or c == "device_revoked":
        return FAILED_UNAVAILABLE
    if c == "unknown":
        return UNKNOWN
    # 未识别的不可用类：保守按 unavailable，避免盲目重放
    return FAILED_UNAVAILABLE


def classify_receipt(payload: Mapping[str, Any] | None, *,
                     expect_action_id: str = "") -> tuple[str, str]:
    """校验回执并返回 ``(outcome, detail)``。

    outcome ∈ {``executed``, ``failed/<code>``, ``unknown``, ``deferred``}；
    failed 细分为 timeout / unavailable / scope_denied；其余 failed 归
    unavailable（estop 等端侧内部失败同样停环，不重放未知副作用）。
    """
    if not isinstance(payload, Mapping):
        return UNKNOWN, "invalid-receipt"
    try:
        state = validate_action_receipt(payload, expect_action_id=expect_action_id)
    except AutonomyError as exc:
        return UNKNOWN, exc.code
    if state == "executed":
        return "executed", "device-receipt-executed"
    if state == "unknown":
        return UNKNOWN, "device-receipt-unknown"
    if state in ("received", "accepted"):
        return "deferred", f"receipt-not-terminal:{state}"
    # state == failed
    err = str(payload.get("error_code") or "internal")
    orch = classify_gateway_code(err)
    if orch == FAILED_TIMEOUT:
        return FAILED_TIMEOUT, f"device-receipt-failed:{err}"
    if orch == FAILED_SCOPE_DENIED:
        return FAILED_SCOPE_DENIED, f"device-receipt-failed:{err}"
    return FAILED_UNAVAILABLE, f"device-receipt-failed:{err}"


def classify_exception(exc: BaseException) -> str:
    """``DeviceUnavailable`` → 编排码；其它异常 → unknown（不明不重放）。"""
    if isinstance(exc, DeviceUnavailable):
        return classify_gateway_code(exc.code)
    return UNKNOWN


def should_auto_retry(orch_code: str, *, capability: str = "") -> bool:
    """仅只读 ``device.agent.observe`` 的 ``failed/timeout`` 允许有界自动重试。

    control / shell 等有副作用能力必须先 reconcile，禁止盲目用新幂等键重放；
    ``unknown`` 永不自动重试。
    """
    if orch_code != FAILED_TIMEOUT:
        return False
    return (capability or "") in SAFE_TIMEOUT_RETRY_CAPABILITIES


def should_abort_loop(orch_code: str) -> bool:
    """停止循环（含 unknown）：不可用 / 缺权 / 不明结果。"""
    return orch_code in {
        FAILED_UNAVAILABLE, FAILED_SCOPE_DENIED, UNKNOWN,
    }
