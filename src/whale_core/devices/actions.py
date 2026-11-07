"""设备动作的稳定接口与回执状态；不依赖生活逻辑或具体传输实现。"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

# contracts/v1 action.receipt：received/accepted 只是中间态。
TERMINAL_RECEIPT_STATES = frozenset({"executed", "failed", "unknown"})
RECEIPT_STATES = TERMINAL_RECEIPT_STATES | {"received", "accepted"}


class ActionReceiptError(ValueError):
    """设备回执格式或关联动作不合法。"""

    code = "invalid-receipt"


def validate_action_receipt(payload: Mapping[str, Any], *,
                            expect_action_id: str = "") -> str:
    """校验 action.receipt payload，返回 state；不负责自主权限裁决。"""
    if not isinstance(payload, Mapping):
        raise ActionReceiptError("回执不是对象")
    state = str(payload.get("state") or "")
    if state not in RECEIPT_STATES:
        raise ActionReceiptError(f"未知回执状态 {state!r}")
    action_id = str(payload.get("action_id") or "")
    if not action_id:
        raise ActionReceiptError("回执缺少 action_id")
    if expect_action_id and action_id != expect_action_id:
        raise ActionReceiptError("回执 action_id 与请求不符")
    return state


class DeviceUnavailable(Exception):
    """设备面明确不可用：离线/超时/吊销/不支持（携带协议错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeviceActionPort(Protocol):
    """Core→设备动作通道，真实网关与模拟设备共用。

    可选 action_status(device_id, idempotency_key) 只查询原动作，不重新下发。
    调用方按能力探测该方法；不能把超时视为未执行后自动重发。
    """

    def send_action(self, device_id: str, *, capability: str,
                    args: Mapping[str, Any], idempotency_key: str,
                    timeout_s: float) -> Mapping[str, Any]:
        """下发 action.request 并等待回执；不可用抛 DeviceUnavailable。"""
        ...
