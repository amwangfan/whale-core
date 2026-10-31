"""whale-mobile 设备面（D1-D4 的 core 侧落地）。

- ``registry.DeviceRegistry``：设备注册 / 6 位配对码 / 吊销（kv 持久化 + 审计）
- ``gateway.DeviceGateway``：WSS 长连端点 /api/v1/devices/ws（挑战-应答验签、
  逐帧 schema 校验、心跳离线、sensor/action 上下行）
- ``perception.DevicePerceptionAdapter``：sensor.event → perception 事件流
- ``schema.ContractValidator``：contracts/v1 帧校验（纯 stdlib）

协议事实源：contracts/v1/PROTOCOL.md（whale-mobile 仓的版本化副本）。
"""

from whale_core.devices.perception import DevicePerceptionAdapter
from whale_core.devices.registry import (
    DEVICE_CAPABILITIES,
    DeviceRegistry,
    DeviceRegistryError,
    device_id_from_pubkey_hex,
)

__all__ = [
    "DEVICE_CAPABILITIES",
    "DevicePerceptionAdapter",
    "DeviceRegistry",
    "DeviceRegistryError",
    "device_id_from_pubkey_hex",
]
