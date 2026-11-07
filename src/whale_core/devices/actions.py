"""设备动作的稳定接口与回执状态；不依赖生活逻辑或具体传输实现。"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

# contracts/v1 action.receipt：received/accepted 只是中间态。
TERMINAL_RECEIPT_STATES = frozenset({"executed", "failed", "unknown"})
RECEIPT_STATES = TERMINAL_RECEIPT_STATES | {"received", "accepted"}


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
