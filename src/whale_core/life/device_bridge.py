"""兼容 shim（issue #27）：本体已迁至 ``whale_core.devices.bridge``。

旧 import 路径 ``whale_core.life.device_bridge`` 继续可用（re-export 的是
同一批对象，``is`` 恒等）；新代码请改用 ``whale_core.devices.bridge``。
"""

from __future__ import annotations

from whale_core.devices.bridge import *  # noqa: F401,F403
from whale_core.devices.bridge import (
    BridgeError,
    GatewayActionPort,
    SimulatedActionPort,
    SimulatedDeviceBridge,
    SituationState,
    default_projector,
)

__all__ = [
    "BridgeError",
    "GatewayActionPort",
    "SimulatedActionPort",
    "SimulatedDeviceBridge",
    "SituationState",
    "default_projector",
]
