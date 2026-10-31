"""D1/D2/D5-lite：/api/v1/devices/* 管理端路由（全部走控制台 Bearer 鉴权）。

端点（请求/响应对齐 server.py 统一错误格式）：

- ``POST   /devices/pair``           设备提交公钥 → 6 位配对码（10min）
- ``POST   /devices/pair/approve``   主人批准配对码 → active
- ``GET    /devices``                设备列表（含在线状态）
- ``GET    /devices/{id}``           单设备详情
- ``DELETE /devices/{id}``           吊销（即时生效 + 踢在线连接，§3）
- ``POST   /devices/{id}/revoke``    同上（不便发 DELETE 的控制台）
- ``POST   /devices/{id}/query``     下发 sensor.query，deadline 内等 result
- ``POST   /devices/{id}/action``    下发 action.request，等终态回执
- ``GET    /devices/ws``             提示：WS 长连在独立监听端口（见 ws_url）

WS 端点本体在 devices.gateway.DeviceGateway（websockets 可选依赖）；
本模块负责懒装配与 revoke 时的即时踢线。
"""

from __future__ import annotations

import json
import re
from typing import Any

from whale_core.api.server import _error, _json_bytes
from whale_core.devices.gateway import WS_PATH, DeviceGateway, GatewayError
from whale_core.devices.registry import (
    DEVICE_CAPABILITIES,
    DeviceRegistry,
    DeviceRegistryError,
)

_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_TIMEOUT_S = 60.0

#: DeviceRegistryError.code → HTTP 状态
_REGISTRY_STATUS = {
    "bad_request": 400, "not_found": 404, "conflict": 409, "expired": 410,
}
#: GatewayError.code（§4 错误码 + device_offline 场景）→ HTTP 状态
_GATEWAY_STATUS = {
    "invalid_message": 400, "scope_denied": 403, "not_found": 404,
    "duplicate": 409, "unavailable": 409, "expired": 504,
}


def ensure_device_services(ctx, server) -> tuple[DeviceRegistry, DeviceGateway | None]:
    """懒装配：registry 从 ctx.store 现场构建；gateway 需 websockets+cryptography。"""
    if not isinstance(getattr(ctx, "device_registry", None), DeviceRegistry):
        with ctx.lock:
            if not isinstance(getattr(ctx, "device_registry", None), DeviceRegistry):
                ctx.device_registry = DeviceRegistry(ctx.store)
    registry = ctx.device_registry
    gateway = getattr(ctx, "device_gateway", None)
    if isinstance(gateway, DeviceGateway):
        return registry, gateway
    with ctx.lock:
        gateway = getattr(ctx, "device_gateway", None)
        if isinstance(gateway, DeviceGateway):
            return registry, gateway
        from whale_core.devices.perception import DevicePerceptionAdapter
        adapter = getattr(ctx, "device_adapter", None)
        if adapter is None:
            adapter = ctx.device_adapter = DevicePerceptionAdapter()
        host = getattr(server, "device_relay_host", "127.0.0.1")
        port = getattr(server, "device_relay_port", 0)
        gateway = DeviceGateway(
            registry, ctx.store, host=host, port=port,
            adapter=adapter, log_event=ctx.log_event)
        ctx.device_gateway = gateway
    return registry, gateway


def _clamp_timeout(body: dict) -> float:
    try:
        value = float(body.get("timeout", 15))
    except (TypeError, ValueError):
        value = 15.0
    return max(1.0, min(_MAX_TIMEOUT_S, value))


def dispatch_devices(handler, method: str, parts: list[str]) -> None:
    send = handler._send
    ctx = handler.ctx
    try:
        registry, gateway = ensure_device_services(ctx, handler.server)
    except ImportError:
        send(503, _error("unavailable",
                         "install whale-core[devices] to enable device gateway"))
        return
    except FileNotFoundError:
        send(503, _error("unavailable", "contracts/v1 schemas missing"))
        return

    # -- WS 端点提示（升级发生在 gateway 独立端口，不在 HTTP 服务上） ----
    if parts == ["devices", "ws"] and method == "GET":
        send(400, _error("bad_request",
                         f"device WebSocket endpoint is served by the device "
                         f"gateway on port {gateway.port if gateway else 'n/a'} "
                         f"path {WS_PATH}"))
        return

    # -- 配对 ----
    if parts == ["devices", "pair"] and method == "POST":
        body = handler._body_json()
        if body is None:
            send(400, _error("bad_request", "JSON body required"))
            return
        scopes = body.get("scopes")
        if scopes is not None and not (
                isinstance(scopes, list)
                and all(isinstance(s, str) for s in scopes)):
            send(400, _error("bad_request", "scopes must be a list of strings"))
            return
        try:
            result = registry.request_pairing(
                label=str(body.get("label") or ""),
                pubkey_hex=str(body.get("pubkey_hex") or ""),
                scopes=scopes)
        except DeviceRegistryError as exc:
            send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
            return
        ctx.log_event("device:pair_request", result["device_id"])
        send(200, _json_bytes({"result": result}))
        return

    if parts == ["devices", "pair", "approve"] and method == "POST":
        body = handler._body_json()
        if body is None:
            send(400, _error("bad_request", "JSON body required"))
            return
        try:
            meta = registry.approve(str(body.get("code") or ""))
        except DeviceRegistryError as exc:
            send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
            return
        ctx.log_event("device:pair_approve", meta["device_id"])
        send(200, _json_bytes({"result": DeviceRegistry.public_view(meta)}))
        return

    # -- 列表 / 详情 ----
    if parts == ["devices"] and method == "GET":
        devices = []
        for meta in registry.list_devices():
            view = DeviceRegistry.public_view(meta)
            view["online"] = gateway.is_online(meta["device_id"]) if gateway else False
            devices.append(view)
        send(200, _json_bytes({"result": {
            "devices": devices,
            "capabilities": list(DEVICE_CAPABILITIES),
            "ws_path": WS_PATH,
            "ws_port": gateway.port if gateway else None,
        }}))
        return

    if len(parts) >= 2 and parts[0] == "devices":
        device_id = parts[1]
        if not _DEVICE_ID_RE.match(device_id):
            send(400, _error("bad_request", "device_id 须为 32 位小写 hex"))
            return

        if len(parts) == 2 and method == "GET":
            meta = registry.get(device_id)
            if meta is None:
                send(404, _error("not_found", f"未知设备 {device_id}"))
                return
            view = DeviceRegistry.public_view(meta)
            if gateway is not None:
                st = gateway.online_state(device_id)
                view["online"] = gateway.is_online(device_id)
                view["session"] = (
                    {"session_id": st.session_id, "status": st.status}
                    if st is not None else None)
            send(200, _json_bytes({"result": view}))
            return

        if (len(parts) == 2 and method == "DELETE") or \
                (len(parts) == 3 and parts[2] == "revoke" and method == "POST"):
            try:
                meta = registry.revoke(device_id)
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            kicked = gateway.kick(device_id) if gateway is not None else False
            ctx.log_event("device:revoke", device_id)
            send(200, _json_bytes({"result": {
                "device_id": device_id, "status": meta["status"],
                "kicked": kicked}}))
            return

        if len(parts) == 3 and parts[2] == "purge" and method == "POST":
            # 硬删除（须先 revoked）：删除后设备须重新走配对码审批仪式
            try:
                registry.purge(device_id)
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            if gateway is not None:
                gateway.kick(device_id)
            ctx.log_event("device:purge", device_id)
            send(200, _json_bytes({"result": {"device_id": device_id,
                                              "purged": True}}))
            return

        if len(parts) == 3 and parts[2] == "query" and method == "POST":
            _downlink(handler, gateway, device_id, op="query")
            return
        if len(parts) == 3 and parts[2] == "action" and method == "POST":
            _downlink(handler, gateway, device_id, op="action")
            return

    send(404, _error("not_found", "unknown endpoint"))


def _downlink(handler, gateway: DeviceGateway | None, device_id: str, *,
              op: str) -> None:
    send = handler._send
    if gateway is None:
        send(503, _error("unavailable", "device gateway not running"))
        return
    body = handler._body_json()
    if body is None:
        send(400, _error("bad_request", "JSON body required"))
        return
    timeout = _clamp_timeout(body)
    try:
        if op == "query":
            operation = str(body.get("operation") or "")
            if operation not in DEVICE_CAPABILITIES:
                send(400, _error("bad_request",
                                 f"operation 须为 V1 能力之一: {list(DEVICE_CAPABILITIES)}"))
                return
            filters = body.get("filters") or {}
            if not isinstance(filters, dict):
                send(400, _error("bad_request", "filters must be an object"))
                return
            try:
                max_items = int(body.get("max_items", 10))
            except (TypeError, ValueError):
                max_items = 10
            result = gateway.send_query(
                device_id, operation=operation, filters=filters,
                max_items=max_items, timeout_s=timeout)
            handler.ctx.log_event("device:query", f"{device_id}:{operation}")
            send(200, _json_bytes({"result": {"ok": True, "result": result}}))
        else:
            capability = str(body.get("capability") or "")
            if capability not in DEVICE_CAPABILITIES:
                send(400, _error("bad_request",
                                 f"capability 须为 V1 能力之一: {list(DEVICE_CAPABILITIES)}"))
                return
            args = body.get("args") or {}
            if not isinstance(args, dict):
                send(400, _error("bad_request", "args must be an object"))
                return
            authorized_scope = body.get("authorized_scope")
            if authorized_scope is not None and not (
                    isinstance(authorized_scope, list)
                    and all(isinstance(s, str) for s in authorized_scope)):
                send(400, _error("bad_request",
                                 "authorized_scope must be a list of strings"))
                return
            receipt = gateway.send_action(
                device_id, capability=capability, args=args,
                idempotency_key=body.get("idempotency_key"),
                correlation_id=body.get("correlation_id"),
                authorized_scope=authorized_scope,
                approval_ref=body.get("approval_ref"),
                timeout_s=timeout)
            handler.ctx.log_event("device:action", f"{device_id}:{capability}")
            send(200, _json_bytes({"result": {"ok": True, "receipt": receipt}}))
    except GatewayError as exc:
        send(_GATEWAY_STATUS.get(exc.code, 500), _error(exc.code, str(exc)))
