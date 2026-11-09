"""设备管理端 REST 路由（控制台 Bearer 鉴权；WS 长连走 contracts/v2）。

端点（请求/响应对齐 server.py 统一错误格式；REST 路径随全站 ``/api/v1`` 前缀，
设备**协议**已是 contracts/v2）：

- ``POST   /devices/pair``           设备提交公钥 → 6 位配对码（10min）
- ``POST   /devices/pair/approve``   主人批准配对码 → active
- ``GET    /devices``                设备列表（含在线状态，能力为 v2 §5.1 枚举）
- ``GET    /devices/{id}``           单设备详情
- ``DELETE /devices/{id}``           吊销（即时生效 + 踢在线连接，§4）
- ``POST   /devices/{id}/revoke``    同上（不便发 DELETE 的控制台）
- ``POST   /devices/{id}/scopes``    收窄/重设注册 scope（停止自主体等价手段）
- ``POST   /devices/{id}/query``     下发 sensor.query，deadline 内等 result
- ``POST   /devices/{id}/action``    下发 action.request，等终态回执；
                                     重复幂等键回已记录状态（§7.1，绝不重放）
- ``POST   /devices/{id}/action/status``  按幂等键查状态（只读，不重发）
- ``POST   /devices/{id}/confirm``   设备确认挑战（§14 device.confirm.*）
- ``GET    /devices/ws``             提示：WS 长连在独立监听端口（见 ws_url）

WS 端点本体在 devices.embedded_wss.EmbeddedWssGateway（websockets 可选依赖，
路径 /api/v2/devices/ws）；本模块负责懒装配与 revoke 时的即时踢线。
懒装配同时构建 DeviceGatewayV2 编排器并注册 EmbeddedWssTransport。
"""

from __future__ import annotations

import json
import re
from typing import Any

from whale_core.api.server import _error, _json_bytes
from whale_core.api.device_inputs import action_options, confirmation_options, query_options
from whale_core.devices.capabilities import action_capabilities, query_operations
from whale_core.devices.schema import SchemaUnsupported
from whale_core.devices.embedded_wss import (
    WS_PATH,
    EmbeddedWssGateway,
    GatewayError,
)
from whale_core.devices.registry import (
    DEVICE_CAPABILITIES,
    DeviceRegistry,
    DeviceRegistryError,
)

_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: DeviceRegistryError.code → HTTP 状态
_REGISTRY_STATUS = {
    "bad_request": 400, "not_found": 404, "conflict": 409, "expired": 410,
}
#: GatewayError.code（contracts/v2 §15 + not_found 管理面补充）→ HTTP 状态
_GATEWAY_STATUS = {
    "invalid_message": 400, "scope_denied": 403, "not_found": 404,
    "unavailable": 409, "expired": 504,
}


def ensure_device_services(ctx, server) -> tuple[DeviceRegistry, EmbeddedWssGateway | None]:
    """懒装配：registry 从 ctx.store 现场构建；gateway 需 websockets+cryptography。

    同时把 contracts/v2 编排面就位：``ctx.device_gateway_v2`` =
    DeviceGatewayV2（registry 注入做 scope 检查），并注册
    EmbeddedWssTransport（kind="embedded_wss"）。
    """
    if not isinstance(getattr(ctx, "device_registry", None), DeviceRegistry):
        with ctx.lock:
            if not isinstance(getattr(ctx, "device_registry", None), DeviceRegistry):
                ctx.device_registry = DeviceRegistry(ctx.store)
    registry = ctx.device_registry
    gateway = getattr(ctx, "device_gateway", None)
    if isinstance(gateway, EmbeddedWssGateway):
        return registry, gateway
    with ctx.lock:
        gateway = getattr(ctx, "device_gateway", None)
        if isinstance(gateway, EmbeddedWssGateway):
            return registry, gateway
        adapter = getattr(ctx, "device_adapter", None)
        if adapter is None:
            # EV-02：感知适配器带主动闭环（默认关；kv config:device_initiative 启用）
            from whale_core.devices.initiative_loop import DeviceInitiativeAdapter
            adapter = ctx.device_adapter = DeviceInitiativeAdapter(
                ctx.store,
                gateway_resolver=lambda: getattr(ctx, "device_gateway", None))
        host = getattr(server, "device_relay_host", "127.0.0.1")
        port = getattr(server, "device_relay_port", 0)
        # 原生通知网关（修正方案 §6）：WhaleWssProvider 首选通道。
        # 端口为惰性解析（gateway 与 provider 互为依赖：gateway 构造要
        # notification_gateway，provider 又要 gateway 的在线视图/发送口）。
        notification_gateway = getattr(ctx, "notification_gateway", None)

        class _EmbeddedWssPort:
            """DeviceWssPort 适配：经 ctx 惰性解析当前 gateway。"""

            def is_online(self, device_id: str) -> bool:
                gw = getattr(ctx, "device_gateway", None)
                return bool(gw is not None and gw.is_online(device_id))

            async def send_notification_event(self, device_id: str,
                                              payload: dict) -> None:
                gw = getattr(ctx, "device_gateway", None)
                if gw is None:
                    raise RuntimeError("device gateway 未就绪")
                # gateway 的同名方法是 async（websockets.sync 线程模型下
                # 内部同步写），直接 await，不要再 to_thread 包出未 await 的协程。
                await gw.send_notification_event(device_id, payload)

        if notification_gateway is None:
            from whale_core.notifications.gateway import NotificationGateway
            from whale_core.notifications.providers.whale_wss import WhaleWssProvider
            notification_gateway = ctx.notification_gateway = NotificationGateway(
                ctx.store,
                providers=[WhaleWssProvider(_EmbeddedWssPort())])
        gateway = EmbeddedWssGateway(
            registry, ctx.store, host=host, port=port,
            adapter=adapter,
            notification_gateway=notification_gateway,
            log_event=ctx.log_event)
        ctx.device_gateway = gateway
        # Device Gateway V2 编排器：注册 embedded_wss transport（V2-D4）
        from whale_core.devices.gateway_v2 import DeviceGatewayV2
        from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport
        gw2 = getattr(ctx, "device_gateway_v2", None)
        if gw2 is None:
            gw2 = ctx.device_gateway_v2 = DeviceGatewayV2(registry)
        gw2.register_transport(EmbeddedWssTransport(gateway))
    return registry, gateway


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
        send(503, _error("unavailable", "contracts/v2 schemas missing"))
        return
    except (SchemaUnsupported, json.JSONDecodeError):
        send(503, _error("unavailable", "contracts/v2 schemas invalid"))
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

        if len(parts) == 3 and parts[2] == "scopes" and method == "POST":
            # core 侧停止自主体：收窄 scope 后网关下一步 scope 检查即拒
            body = handler._body_json()
            if body is None:
                send(400, _error("bad_request", "JSON body required"))
                return
            scopes = body.get("scopes")
            if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                send(400, _error("bad_request", "scopes must be a list of strings"))
                return
            try:
                meta = registry.set_scopes(device_id, scopes)
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            ctx.log_event("device:scopes", device_id)
            send(200, _json_bytes({"result": {
                "device_id": device_id,
                "status": meta.get("status"),
                "scopes": list(meta.get("scopes") or []),
            }}))
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
        if len(parts) == 4 and parts[2:] == ["action", "status"] and method == "POST":
            body = handler._body_json()
            if body is None or not isinstance(body.get("idempotency_key"), str):
                send(400, _error("bad_request", "idempotency_key required"))
                return
            try:
                result = gateway.action_status(device_id, body["idempotency_key"])
            except GatewayError as exc:
                send(_GATEWAY_STATUS.get(exc.code, 500), _error(exc.code, str(exc)))
                return
            send(200, _json_bytes({"result": result}))
            return
        if len(parts) == 3 and parts[2] == "action" and method == "POST":
            _downlink(handler, gateway, device_id, op="action")
            return
        if len(parts) == 3 and parts[2] == "confirm" and method == "POST":
            _confirm(handler, gateway, registry, device_id)
            return
        if len(parts) == 3 and parts[2] == "notify" and method == "POST":
            _notify(handler, ctx, device_id)
            return

    send(404, _error("not_found", "unknown endpoint"))


def _notify(handler, ctx, device_id: str) -> None:
    """POST /devices/{id}/notify —— 原生通知下发（修正方案 §6.2 的服务端入口）。

    body: {category, title, body, target?, actions?, group_key?, collapse_key?,
           expires_at?} → NotificationService.notify（WSS 首选送达；离线入队）。
    """
    send = handler._send
    ng = getattr(ctx, "notification_gateway", None)
    if ng is None:
        send(503, _error("unavailable", "notification gateway not assembled"))
        return
    body = handler._body_json()
    if body is None:
        send(400, _error("bad_request", "JSON body required"))
        return
    category = str(body.get("category") or "")
    title = str(body.get("title") or "")
    if not category or not title:
        send(400, _error("bad_request", "category 与 title 必填"))
        return
    import asyncio
    from whale_core.notifications.models import (
        NotificationAction,
        NotificationTarget,
    )
    from whale_core.notifications.services.notification_service import (
        NotificationService,
    )
    target = None
    raw_target = body.get("target")
    if isinstance(raw_target, dict) and raw_target.get("type"):
        try:
            target = NotificationTarget(str(raw_target["type"]),
                                        str(raw_target.get("id") or ""))
        except ValueError as exc:
            send(400, _error("bad_request", str(exc)))
            return
    actions = None
    raw_actions = body.get("actions")
    if isinstance(raw_actions, list):
        actions = []
        for item in raw_actions:
            if not isinstance(item, dict) or not item.get("action_id"):
                send(400, _error("bad_request", "actions 项须含 action_id"))
                return
            actions.append(NotificationAction(
                str(item["action_id"]), str(item.get("label") or "")))
    svc = NotificationService(ng)
    try:
        notification_id = asyncio.run(svc.notify(
            device_id, category, title, str(body.get("body") or ""),
            target=target, actions=actions,
            group_key=body.get("group_key"),
            collapse_key=body.get("collapse_key"),
            expires_at=body.get("expires_at")))
    except Exception as exc:  # noqa: BLE001
        send(500, _error("internal", f"notify 失败: {type(exc).__name__}"))
        return
    send(200, _json_bytes({"result": {"notification_id": notification_id}}))


def _confirm(handler, gateway: EmbeddedWssGateway | None, registry: DeviceRegistry,
             device_id: str) -> None:
    """POST /devices/{id}/confirm —— 创建挑战 → 下发 device.confirm.request →
    等待设备确认 → 签名核验 → 原子核销签发限时租约（contracts/v2 §14）。
    拒绝/超时/离线一律不签发。"""
    send = handler._send
    if gateway is None:
        send(503, _error("unavailable", "device gateway not running"))
        return
    body = handler._body_json()
    if body is None:
        send(400, _error("bad_request", "JSON body required"))
        return
    try:
        body = confirmation_options(body)
    except ValueError as exc:
        send(400, _error("bad_request", str(exc)))
        return
    scopes = body.get("scopes")
    if not (isinstance(scopes, list)
            and all(isinstance(x, str) for x in scopes) and scopes):
        send(400, _error("bad_request", "scopes must be a non-empty list of strings"))
        return
    # v2 device.confirm.request.scopes 必须是 §5.1 能力枚举子集
    unknown = [s for s in scopes if s not in DEVICE_CAPABILITIES]
    if unknown:
        send(400, _error("bad_request",
                         f"scopes 须为 v2 能力枚举子集，未知: {unknown}"))
        return
    try:
        from whale_core.life.temporary_auth import (TemporaryAuthError,
                                                    TemporaryAuthService)
        auth = TemporaryAuthService(registry.store)
        ch = auth.create_device_challenge(
            device_id=device_id,
            principal_id=str(body.get("principal_id") or "owner"),
            channel=str(body.get("channel") or "api"),
            conversation_id=str(body.get("conversation_id") or ""),
            scopes=tuple(scopes),
            ttl_s=float(body.get("ttl_s") or 120),
            grant_ttl_s=float(body.get("grant_ttl_s") or 600),
            grant_max_uses=int(body.get("grant_max_uses") or 1),
            action_fingerprint=body.get("action_fingerprint"),
        )
    except TemporaryAuthError as exc:
        send(400, _error("bad_request", str(exc)))
        return
    except Exception:  # 不暴露内部细节
        send(500, _error("internal", "challenge create failed"))
        return
    payload = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "principal_id": str(body.get("principal_id") or "owner"),
        "channel": str(body.get("channel") or "api"),
        "scopes": list(ch["scopes"]),
        "expires_at": int(ch["expires_at"] * 1000),
    }
    if body.get("conversation_id"):
        payload["conversation_id"] = str(body["conversation_id"])
    if body.get("action_fingerprint"):
        payload["action_fingerprint"] = str(body["action_fingerprint"])
    try:
        resp = gateway.send_confirm_request(
            device_id, payload, timeout_s=float(body.get("ttl_s") or 120) + 5)
    except GatewayError as exc:
        send(_GATEWAY_STATUS.get(exc.code, 500), _error(exc.code, str(exc)))
        return
    if not resp.get("approved"):
        send(409, _error("denied",
                         f"设备未批准: {resp.get('reason') or 'user_denied'}"))
        return
    # 签名核验（Ed25519 over challenge_id UTF-8；设备注册公钥）→ 原子核销
    meta = registry.get(device_id) or {}
    pubkey = meta.get("pubkey_hex")
    if not pubkey:
        send(500, _error("internal", "device pubkey unavailable"))
        return

    def _verifier(challenge_id: str, dev_id: str) -> bool:
        sig = resp.get("signature")
        if not isinstance(sig, str):
            return False
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey)).verify(
                bytes.fromhex(sig), challenge_id.encode("utf-8"))
            return True
        except Exception:
            return False

    grant = auth.confirm_device_challenge(
        ch["challenge_id"], device_id=device_id, device_verifier=_verifier)
    if grant is None:
        send(409, _error("denied", "签名核验失败或挑战已被消费/过期"))
        return
    handler.ctx.log_event("device:confirm", f"{device_id}:{ch['challenge_id']}")
    send(200, _json_bytes({"result": {
        "ok": True,
        "grant_id": grant.grant_id,
        "scopes": list(grant.scopes),
        "expires_at": grant.expires_at,
        "max_uses": grant.max_uses,
        "proof_method": grant.proof_method,
    }}))


def _downlink(handler, gateway: EmbeddedWssGateway | None, device_id: str, *,
              op: str) -> None:
    send = handler._send
    if gateway is None:
        send(503, _error("unavailable", "device gateway not running"))
        return
    body = handler._body_json()
    if body is None:
        send(400, _error("bad_request", "JSON body required"))
        return
    try:
        if op == "query":
            operation = str(body.get("operation") or "")
            if operation not in query_operations():
                send(400, _error("bad_request",
                                 f"operation 须为 v2 查询子集之一: "
                                 f"{list(query_operations())}"))
                return
            result = gateway.send_query(device_id, operation=operation, **query_options(body))
            handler.ctx.log_event("device:query", f"{device_id}:{operation}")
            send(200, _json_bytes({"result": {"ok": True, "result": result}}))
        else:
            capability = str(body.get("capability") or "")
            if capability not in action_capabilities():
                send(400, _error("bad_request",
                                 f"capability 须为 v2 动作子集之一: "
                                 f"{list(action_capabilities())}"))
                return
            receipt = gateway.send_action(device_id, capability=capability, **action_options(body))
            handler.ctx.log_event("device:action", f"{device_id}:{capability}")
            # §7.1：重复幂等键回已记录状态（duplicate=True），200 而非 409
            send(200, _json_bytes({"result": {"ok": True, "receipt": receipt}}))
    except GatewayError as exc:
        send(_GATEWAY_STATUS.get(exc.code, 500), _error(exc.code, str(exc)))
    except ValueError as exc:
        # 出站帧不过 v2 schema（如 task_id/run_id 形状非法）→ 调用方错误
        send(400, _error("invalid_message", str(exc)[:300]))
