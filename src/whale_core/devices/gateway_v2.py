"""Device Gateway V2：多 transport 编排器（devices-v2-design.md §4/§5）。

职责：
- 设备维度登记多个 transport（同一设备可同时有 adb/ssh/embedded_wss）；
- 按能力类别选路（GUI 类 embedded>adb>ssh；shell/文件类 ssh>embedded>adb）；
- 幂等派发：同 device_id+idempotency_key 直接回已记录终态，绝不重放；
- scope 检查走 registry（设备真相仍在 registry，transport 健康另报）；
- 能力不可用时跳过该通道；动作一经下发，不根据模糊失败自动跨通道重放。

本类用于路由/演示，进程内回执有上限，不能替代生产 EmbeddedWssGateway 的持久化幂等记录。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, Mapping, Sequence

from whale_core.devices.capabilities import GUI_CAPS, SHELL_CAPS
from whale_core.devices.transports.base import (
    ActionReceipt,
    ActionRequest,
    DeviceTransport,
    Observation,
    ObserveRequest,
    ReceiptStatus,
    new_id,
    now_ms,
)

# 选路优先级表（V2-D4/V2-D5）
_GUI_ORDER = ("embedded_wss", "adb", "ssh")
_SHELL_ORDER = ("ssh", "embedded_wss", "adb")
_DEFAULT_ORDER = ("embedded_wss", "adb", "ssh")


class GatewayError(Exception):
    """code 对齐 contracts/v2 §14 错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeviceGatewayV2:
    def __init__(self, registry=None) -> None:
        # registry 可选：缺省跳过 scope 检查（开发/测试）；生产必须注入。
        self.registry = registry
        self._transports: dict[str, DeviceTransport] = {}
        self._receipts: OrderedDict[tuple[str, str], ActionReceipt] = OrderedDict()
        self._inflight: dict[tuple[str, str], asyncio.Task[ActionReceipt]] = {}
        self.max_cached_receipts = 4096

    # ---- transport 登记 ----

    def register_transport(self, transport: DeviceTransport) -> None:
        self._transports[transport.kind] = transport

    @property
    def transports(self) -> Mapping[str, DeviceTransport]:
        return dict(self._transports)

    # ---- 选路 ----

    def _route_order(self, capability: str) -> Sequence[str]:
        if capability in SHELL_CAPS:
            return _SHELL_ORDER
        if capability in GUI_CAPS:
            return _GUI_ORDER
        return _DEFAULT_ORDER

    async def _pick(self, device_id: str, capability: str) -> DeviceTransport | None:
        for kind in self._route_order(capability):
            t = self._transports.get(kind)
            if t is None:
                continue
            try:
                caps = await t.capabilities(device_id)
            except Exception:  # noqa: BLE001 — 单通道失败不阻塞选路
                continue
            if capability in caps:
                return t
        return None

    # ---- 幂等 ----

    def action_status(self, device_id: str,
                      idempotency_key: str) -> ActionReceipt | None:
        """只读对账（绝不重发）。"""
        return self._receipts.get((device_id, idempotency_key))

    # ---- 执行 ----

    async def execute(
        self,
        device_id: str,
        capability: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str,
        timeout_ms: int = 10_000,
        task_id: str | None = None,
        run_id: str | None = None,
        observation_id: str | None = None,
    ) -> ActionReceipt:
        # scope 检查（registry 注入时）
        if self.registry is not None:
            status = self.registry.status_of(device_id)
            if status != "active":
                raise GatewayError(
                    "device_revoked" if status == "revoked" else "unavailable",
                    f"设备状态 {status}")
            if capability not in set(self.registry.scopes_of(device_id)):
                raise GatewayError("scope_denied", f"设备未授予 {capability}")

        # 幂等：同键已有记录 → 直接回真实状态
        seen = self._receipts.get((device_id, idempotency_key))
        if seen is not None:
            return seen

        key = (device_id, idempotency_key)
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)

        async def dispatch() -> ActionReceipt:
            try:
                transport = await self._pick(device_id, capability)
                action_id = new_id("act")
                if transport is None:
                    receipt = ActionReceipt(
                        action_id=action_id, status=ReceiptStatus.FAILED,
                        task_id=task_id, summary=f"无可用通道执行 {capability}",
                        error_code="unavailable", completed_at=now_ms())
                else:
                    request = ActionRequest(
                        action_id=action_id, capability=capability,
                        arguments=dict(arguments), idempotency_key=idempotency_key,
                        timeout_ms=timeout_ms, task_id=task_id, run_id=run_id,
                        observation_id=observation_id)
                    # 动作一旦下发，失败可能发生在副作用执行之后。
                    # 不自动跨通道重放；调用方依据回执进行后续观察或人工核对。
                    try:
                        receipt = await transport.execute(device_id, request)
                    except Exception:
                        # 无法确认是否已发生副作用，记录 unknown，禁止同键重放。
                        receipt = ActionReceipt(
                            action_id=action_id, status=ReceiptStatus.UNKNOWN,
                            task_id=task_id, summary="传输异常，执行结果未知",
                            error_code="internal", completed_at=now_ms())

                self._receipts[key] = receipt
                self._receipts.move_to_end(key)
                if len(self._receipts) > self.max_cached_receipts:
                    self._receipts.popitem(last=False)
                return receipt
            finally:
                self._inflight.pop(key, None)

        task = asyncio.create_task(dispatch())
        self._inflight[key] = task
        return await asyncio.shield(task)

    async def observe(self, device_id: str,
                      request: ObserveRequest | None = None) -> Observation:
        transport = await self._pick(device_id, "device.observe")
        if transport is None:
            raise GatewayError("unavailable", "无可用观察通道")
        return await transport.observe(device_id, request or ObserveRequest())

    async def health_report(self, device_id: str) -> dict[str, str]:
        """各通道健康状态（一种通道断开 ≠ 设备离线）。"""
        report: dict[str, str] = {}
        for kind, t in self._transports.items():
            try:
                h = await t.health(device_id)
                report[kind] = "connected" if h.connected else f"down:{h.detail}"
            except Exception as exc:  # noqa: BLE001
                report[kind] = f"error:{type(exc).__name__}"
        return report
