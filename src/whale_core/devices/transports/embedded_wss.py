"""EmbeddedWssTransport：contracts/v2 embedded_wss 通道的 DeviceTransport 实现。

设备经手机 App 内建 WSS 客户端主动出站连到 ``devices/embedded_wss.py`` 的
``EmbeddedWssGateway``（``/api/v2/devices/ws``）；本 transport 是把网关的同步
阻塞下行口包装成 ``transports/base.py`` 的 async 接口，供 ``DeviceGatewayV2``
编排器选路。

纪律（contracts/v2 §7 + base.py）：
- ``execute`` 超时回 ``ReceiptStatus.TIMEOUT`` 终态，**绝不**抛超时异常
  （timeout ≠ 未执行，副作用可能已发生）；
- 设备不在线 → ``FAILED/unavailable`` 回执（不悬挂、不抛）；
- 幂等由网关持久表承担（同 idempotency_key 回已记录状态，绝不重放）；
- 大输出不落回执本体，经 ``read_output``（sensor.query device.output.read）
  分页拉取。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
from typing import Any, Sequence

from whale_core.devices.transports.base import (
    ActionReceipt,
    ActionRequest,
    Observation,
    ObserveRequest,
    ReceiptStatus,
    TransportHealth,
    UiElement,
    Verification,
    VerificationStatus,
    new_id,
    now_ms,
)

log = logging.getLogger("whale.devices.transports.embedded_wss")

#: observe 动作默认超时（端侧截屏+UI 树采集，宽于普通动作）
_OBSERVE_TIMEOUT_S = 20.0
#: read_output 默认查询超时
_READ_TIMEOUT_S = 15.0


class EmbeddedWssTransport:
    """kind="embedded_wss"。构造注入已就位的 EmbeddedWssGateway。"""

    kind = "embedded_wss"

    def __init__(self, gateway) -> None:
        # 延迟标注类型避免环：gateway 是 devices.embedded_wss.EmbeddedWssGateway
        self._gateway = gateway

    # ---- 连接生命周期（设备侧主动出站，core 不发起连接） -------------------

    async def connect(self, device_id: str) -> None:
        """embedded 通道由设备发起连接，core 侧 connect 为空操作。"""
        return None

    async def disconnect(self, device_id: str) -> None:
        self._gateway.drop(device_id)

    async def health(self, device_id: str) -> TransportHealth:
        st = self._gateway.online_state(device_id)
        connected = st is not None and not st.offline_marked
        return TransportHealth(
            connected=connected,
            detail="" if connected else ("offline" if st is not None else "not_connected"),
            checked_at=now_ms())

    async def capabilities(self, device_id: str) -> Sequence[str]:
        """hello 声明 ∩ 注册 scopes（在线视图；离线为空集，不参与选路）。"""
        return self._gateway.capabilities_of(device_id)

    # ---- 观察（§8） ---------------------------------------------------------

    async def observe(self, device_id: str, request: ObserveRequest) -> Observation:
        """下发 action.request device.observe，等终态回执并组装 Observation。

        观察负载本体按 §8 经 output_ref 拉取；拉不到时诚实降级为空观察
        （screenshot_ref 保留引用，绝不伪造元素/截图）。
        """
        from whale_core.devices.embedded_wss import GatewayError

        req = ActionRequest(
            action_id=new_id("act"), capability="device.observe",
            arguments={
                "screenshot": bool(request.screenshot),
                "ui_tree": bool(request.ui_tree),
                "max_elements": int(request.max_elements),
            },
            idempotency_key=f"observe:{new_id('obs')}",
            timeout_ms=int(_OBSERVE_TIMEOUT_S * 1000))
        receipt = await self.execute(device_id, req)
        if receipt.status is not ReceiptStatus.SUCCEEDED:
            raise GatewayError(
                receipt.error_code or "unavailable",
                f"device.observe 未成功：{receipt.status.value}"
                + (f"/{receipt.error_code}" if receipt.error_code else ""))
        # 优先解析 output_ref 指向的观察负载（§8：本体不占回执帧预算）
        payload: dict[str, Any] = {}
        if receipt.output_ref:
            try:
                raw, _ = await self.read_output(device_id, receipt.output_ref,
                                                max_bytes=256 * 1024)
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict):
                    payload = data
            except Exception:  # noqa: BLE001 - 拉取失败降级为空观察
                log.warning("observe output_ref 拉取失败（%s）",
                            receipt.output_ref, exc_info=True)
        elements = tuple(
            _parse_element(e) for e in (payload.get("elements") or [])
            if isinstance(e, dict))
        return Observation(
            observation_id=str(payload.get("observation_id") or new_id("obs")),
            device_id=device_id,
            taken_at=int(payload.get("taken_at") or now_ms()),
            foreground_package=str(payload.get("foreground_package") or ""),
            activity=(str(payload["activity"])
                      if payload.get("activity") else None),
            elements=elements,
            ui_signature=hashlib.sha256(
                json.dumps(
                    [_element_fingerprint_source(e) for e in
                     (payload.get("elements") or [])],
                    sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            screenshot_ref=(str(payload.get("screenshot_ref"))
                            if payload.get("screenshot_ref")
                            else receipt.output_ref),
            screenshot_signature=(str(payload["screenshot_signature"])
                                  if payload.get("screenshot_signature") else None),
            screen_width=int(payload.get("screen_width") or 0),
            screen_height=int(payload.get("screen_height") or 0),
            keyboard_shown=bool(payload.get("keyboard_shown") or False),
            locked=bool(payload.get("locked") or False),
            truncated=bool(payload.get("truncated") or False),
        )

    # ---- 执行（§7） ---------------------------------------------------------

    async def execute(self, device_id: str, action: ActionRequest) -> ActionReceipt:
        """经网关下发 action.request 并等终态；超时→TIMEOUT，离线→FAILED。"""
        from whale_core.devices.embedded_wss import GatewayError

        timeout_s = max(0.1, action.timeout_ms / 1000)
        try:
            result = await asyncio.to_thread(
                self._gateway.send_action, device_id,
                capability=action.capability, arguments=dict(action.arguments),
                idempotency_key=action.idempotency_key,
                task_id=action.task_id, run_id=action.run_id,
                observation_id=action.observation_id,
                approval_ref=action.approval_ref,
                correlation_id=action.correlation_id,
                timeout_s=timeout_s)
        except GatewayError as exc:
            if exc.code == "expired":
                # 等待回执超时：终态 TIMEOUT 回执（绝不抛给编排层）
                return ActionReceipt(
                    action_id=action.action_id, status=ReceiptStatus.TIMEOUT,
                    task_id=action.task_id, error_code="timeout",
                    summary=f"等待 action.receipt 超时（{timeout_s}s）",
                    completed_at=now_ms())
            # 离线/吊销/scope 拒绝：FAILED + 协议错误码（不悬挂 received）
            return ActionReceipt(
                action_id=action.action_id, status=ReceiptStatus.FAILED,
                task_id=action.task_id, error_code=exc.code, summary=str(exc),
                completed_at=now_ms())
        return _receipt_from_payload(action, result)

    # ---- 大输出拉取（§7.3/§10 agent:out: 等） --------------------------------

    async def read_output(self, device_id: str, output_ref: str, *,
                          offset: int = 0,
                          max_bytes: int = 65536) -> tuple[bytes, bool]:
        """sensor.query device.output.read 分页读取；返回 (chunk, truncated)。"""
        from whale_core.devices.embedded_wss import GatewayError

        try:
            result = await asyncio.to_thread(
                self._gateway.send_query, device_id,
                operation="device.output.read",
                filters={"output_ref": output_ref, "offset": int(offset),
                         "max_bytes": int(max_bytes)},
                max_items=1, timeout_s=_READ_TIMEOUT_S)
        except GatewayError as exc:
            raise GatewayError(exc.code, f"read_output 失败：{exc}") from exc
        if result.get("status") != "ok":
            raise GatewayError(
                str(result.get("status") or "unavailable"),
                f"sensor.result 状态非 ok：{result.get('status')}")
        data = result.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise GatewayError("invalid_message", "device.output.read 缺少输出对象")
        item = data[0]
        if isinstance(item.get("text"), str):
            chunk = item["text"].encode("utf-8")
        else:
            # Agent2 的图片输出使用 data_base64；保留既有 bytes_b64 数据源。
            encoded = item.get("data_base64", item.get("bytes_b64"))
            if not isinstance(encoded, str):
                raise GatewayError("invalid_message", "device.output.read 未声明输出编码")
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise GatewayError("invalid_message", "device.output.read base64 无效") from exc
        return chunk, bool(result.get("truncated") or item.get("truncated"))


def _parse_element(data: dict) -> UiElement:
    """端侧观察元素 dict → UiElement（缺省宽容，bounds 缺省零矩形）。"""
    bounds_raw = data.get("bounds")
    if not (isinstance(bounds_raw, list) and len(bounds_raw) == 4):
        bounds_raw = [0, 0, 0, 0]
    bounds = tuple(int(x) for x in bounds_raw)
    return UiElement(
        element_id=int(data.get("element_id") or 0),
        class_name=str(data.get("class_name") or ""),
        text=str(data.get("text") or ""),
        resource_id=str(data.get("resource_id") or ""),
        content_desc=str(data.get("content_desc") or ""),
        bounds=bounds,  # type: ignore[arg-type]
        clickable=bool(data.get("clickable") or False),
        long_clickable=bool(data.get("long_clickable") or False),
        scrollable=bool(data.get("scrollable") or False),
        editable=bool(data.get("editable") or False),
        enabled=bool(data.get("enabled", True)),
        fingerprint=str(data.get("fingerprint") or ""),
    )


def _element_fingerprint_source(data: Any) -> Any:
    """ui_signature 计算用的去 id 元素表（与 base.Observation 约定一致）。"""
    if not isinstance(data, dict):
        return data
    return {k: v for k, v in data.items() if k != "element_id"}


def _receipt_from_payload(action: ActionRequest, payload: dict) -> ActionReceipt:
    """v2 action.receipt payload / 幂等命中记录 → ActionReceipt。

    幂等命中（duplicate=True）时网关回的是动作记录（state + 内嵌 receipt），
    同样映射到真实终态，绝不重放（§7.1）。
    """
    if payload.get("duplicate"):
        record = payload
        embedded = record.get("receipt") if isinstance(
            record.get("receipt"), dict) else {}
        payload = {"action_id": record.get("action_id") or action.action_id,
                   "status": record.get("state") or "unknown",
                   "task_id": action.task_id, **(embedded or {})}
        payload.setdefault("provider_ref", "idempotent:replay-suppressed")
    status_raw = str(payload.get("status") or "unknown")
    try:
        status = ReceiptStatus(status_raw)
    except ValueError:
        status = ReceiptStatus.UNKNOWN
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    verification = None
    raw_ver = result.get("verification")
    if isinstance(raw_ver, dict) and raw_ver.get("status") in (
            "passed", "failed", "skipped"):
        verification = Verification(
            status=VerificationStatus(str(raw_ver["status"])),
            summary=str(raw_ver.get("summary") or ""))
    return ActionReceipt(
        action_id=str(payload.get("action_id") or action.action_id),
        status=status,
        task_id=payload.get("task_id") or action.task_id,
        summary=str(result.get("summary") or "")[:2000],
        output_ref=(str(result["output_ref"]) if result.get("output_ref") else None),
        verification=verification,
        error_code=(str(payload["error_code"]) if payload.get("error_code") else None),
        provider_ref=(str(payload["provider_ref"])
                      if payload.get("provider_ref") else None),
        started_at=(int(payload["started_at"]) if payload.get("started_at") is not None
                    else None),
        completed_at=(int(payload["completed_at"])
                      if payload.get("completed_at") is not None else now_ms()),
    )
