"""设备管理端 REST 路由（控制台 Bearer 鉴权；WS 长连走 contracts/v2）。

端点（请求/响应对齐 server.py 统一错误格式；REST 路径随全站 ``/api/v1`` 前缀，
设备**协议**已是 contracts/v2）：

- ``POST   /devices/pair``           设备提交公钥 → 6 位配对码（10min）
- ``POST   /devices/pair/approve``   主人批准配对码 → active
- ``GET    /devices``                设备列表（含在线状态，能力为 v2 §5.1 枚举；
                                     ``?transports=1`` 附带每通道健康/有效能力）
- ``GET    /devices/{id}``           单设备详情（附带每通道健康/有效能力）
- ``DELETE /devices/{id}``           吊销（即时生效 + 踢在线连接，§4）
- ``POST   /devices/{id}/revoke``    同上（不便发 DELETE 的控制台）
- ``POST   /devices/{id}/scopes``    收窄/重设注册 scope（停止自主体等价手段）
- ``POST   /devices/{id}/query``     下发 sensor.query，deadline 内等 result
- ``POST   /devices/{id}/action``    下发动作，等终态回执；
                                     重复幂等键回已记录状态（§7.1，绝不重放）
- ``POST   /devices/{id}/action/status``  按幂等键查状态（只读，不重发）
- ``POST   /devices/{id}/confirm``   设备确认挑战（§14 device.confirm.*）
- ``GET    /devices/ws``             提示：WS 长连在独立监听端口（见 ws_url）
- ``GET    /devices/transports``     传输配置视图 + 已注册通道（``?health=1``
                                     附带配置内设备的逐通道健康探测）
- ``PUT    /devices/transports``     重设 adb/ssh 传输配置（校验→落 kv→热替换）
- ``POST   /devices/{id}/gui_tasks`` 启动 GUI 自主任务（goal 文本；经
                                     DeviceGatewayV2 统一选路，任务记录进
                                     既有任务面；#58）
- ``GET    /devices/{id}/gui_tasks`` 设备 GUI 任务列表（新→旧）
- ``POST   /devices/{id}/gui_tasks/{task_id}/stop``  停止任务（协程取消 +
                                     best-effort device.stop 急停）

#77 统一选路：query/action 不再直调 EmbeddedWssGateway，而是经
``ctx.device_gateway_v2``（DeviceGatewayV2）按能力选 embedded_wss/adb/ssh；
幂等动作记录落与网关相同的 kv 域，跨重启可对账；ADB/SSH 连接参数来自 kv
``config:device_transports``（transport_config.py 校验装配），不硬编码。

WS 端点本体在 devices.embedded_wss.EmbeddedWssGateway（websockets 可选依赖，
路径 /api/v2/devices/ws）；本模块负责懒装配与 revoke 时的即时踢线。
懒装配同时构建 DeviceGatewayV2 编排器并注册 EmbeddedWssTransport 及
配置驱动的 adb/ssh transport。
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
from whale_core.devices.gui_agent.service import GuiTaskError
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
    "invalid_message": 400, "scope_denied": 403, "device_revoked": 403,
    "not_found": 404, "unavailable": 409, "expired": 504,
}


def ensure_device_services(ctx, server) -> tuple[DeviceRegistry, EmbeddedWssGateway | None]:
    """懒装配：registry 从 ctx.store 现场构建；gateway 需 websockets+cryptography。

    同时把 contracts/v2 编排面就位：``ctx.device_gateway_v2`` =
    DeviceGatewayV2（registry 注入做 scope 检查，store 注入做持久化幂等），
    注册 EmbeddedWssTransport（kind="embedded_wss"），并按 kv
    ``config:device_transports`` 装配配置驱动的 adb/ssh transport（#77）。
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
                gateway_resolver=lambda: getattr(ctx, "device_gateway", None),
                gateway_v2_resolver=lambda: getattr(ctx, "device_gateway_v2", None))
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
        # Device Gateway V2 编排器：注册 embedded_wss transport（V2-D4）+
        # 配置驱动的 adb/ssh transport（#77；无配置/非法项跳过不阻塞上线）
        from whale_core.devices.gateway_v2 import DeviceGatewayV2
        from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport
        gw2 = getattr(ctx, "device_gateway_v2", None)
        if gw2 is None:
            gw2 = ctx.device_gateway_v2 = DeviceGatewayV2(registry, store=ctx.store)
        gw2.register_transport(EmbeddedWssTransport(gateway))
        _load_configured_transports(ctx, registry, gw2)
        # #58：GUI 任务生产入口（任务记录复用 tasks:recent/task: 面）
        if getattr(ctx, "gui_task_service", None) is None:
            from whale_core.devices.gui_agent.service import GuiTaskService
            ctx.gui_task_service = GuiTaskService(ctx.store, gw2)
    return registry, gateway


def _load_configured_transports(ctx, registry, gw2=None) -> None:
    """启动时装配 kv 配置的 adb/ssh 通道（每进程一次；PUT 时热重载）。"""
    if getattr(ctx, "device_transports_loaded", False):
        return
    ctx.device_transports_loaded = True
    try:
        from whale_core.devices import transport_config as tc
        loaded = tc.load_config(ctx.store)
        if not loaded:
            return
        validated = tc.validate_config(loaded, registry, strict=False)
        gw2 = gw2 if gw2 is not None else getattr(ctx, "device_gateway_v2", None)
        if gw2 is None:
            return
        if validated.get("adb") or validated.get("ssh"):
            summary = tc.apply_transports(gw2, validated)
            ctx.log_event("device:transports",
                          f"boot:registered={summary['registered']}")
        for item in validated.get("skipped") or []:
            ctx.log_event(
                "device:transports",
                f"boot:skip:{item.get('kind')}:{item.get('device_id')}"[:160])
    except Exception:  # noqa: BLE001 — 配置错误不阻塞设备面装配
        import logging
        logging.getLogger("whale.api.devices").warning(
            "设备传输配置装载失败", exc_info=True)


def dispatch_devices(handler, method: str, parts: list[str],
                     query: dict | None = None) -> None:
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

    # -- 传输配置（#77：ADB/SSH 参数来自配置，不落私人地址硬编码） ----
    if parts == ["devices", "transports"]:
        _transports_endpoint(handler, ctx, registry, method, query or {})
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
        with_transports = bool(query) and (query.get("transports") == ["1"])
        devices = []
        for meta in registry.list_devices():
            view = DeviceRegistry.public_view(meta)
            view["online"] = gateway.is_online(meta["device_id"]) if gateway else False
            if with_transports:
                _augment_transports(ctx, view, meta["device_id"])
            devices.append(view)
        send(200, _json_bytes({"result": {
            "devices": devices,
            "capabilities": list(DEVICE_CAPABILITIES),
            "ws_path": WS_PATH,
            "ws_port": gateway.port if gateway else None,
            "retention": gateway.retention_status() if gateway and callable(getattr(gateway, "retention_status", None)) else {"state": "unavailable"},
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
            _augment_transports(ctx, view, device_id)
            view["activity"] = _device_activity(ctx, device_id)
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

        if len(parts) == 3 and parts[2] == "rename" and method == "POST":
            body = handler._body_json()
            if body is None:
                send(400, _error("bad_request", "JSON body required"))
                return
            try:
                meta = registry.rename(device_id, str(body.get("label") or ""))
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            ctx.log_event("device:rename", device_id)
            send(200, _json_bytes({"result": DeviceRegistry.public_view(meta)}))
            return

        if len(parts) == 3 and parts[2] == "role" and method == "POST":
            body = handler._body_json()
            if body is None:
                send(400, _error("bad_request", "JSON body required"))
                return
            try:
                meta = registry.set_role(device_id, str(body.get("role") or ""))
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            ctx.log_event("device:role", device_id)
            send(200, _json_bytes({"result": DeviceRegistry.public_view(meta)}))
            return

        if len(parts) == 3 and parts[2] == "pause" and method == "POST":
            # 暂停：注册保留、连接可在线，动作/查询全被拒（fail-closed）
            try:
                meta = registry.pause(device_id)
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            ctx.log_event("device:pause", device_id)
            send(200, _json_bytes({"result": {
                "device_id": device_id, "status": meta["status"]}}))
            return

        if len(parts) == 3 and parts[2] == "resume" and method == "POST":
            try:
                meta = registry.resume(device_id)
            except DeviceRegistryError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            ctx.log_event("device:resume", device_id)
            send(200, _json_bytes({"result": {
                "device_id": device_id, "status": meta["status"]}}))
            return

        if len(parts) == 3 and parts[2] == "gui_tasks" and method == "POST":
            body = handler._body_json()
            if body is None:
                send(400, _error("bad_request", "JSON body required"))
                return
            svc = getattr(ctx, "gui_task_service", None)
            if svc is None:
                send(503, _error("unavailable", "gui task service not assembled"))
                return
            try:
                record = svc.start(
                    device_id, str(body.get("goal") or ""),
                    max_steps=int(body.get("max_steps") or 50),
                    task_timeout_s=int(body.get("task_timeout_s") or 600),
                    action_timeout_s=int(body.get("action_timeout_s") or 10),
                    observe_timeout_s=int(body.get("observe_timeout_s") or 45),
                    screenshot=bool(body.get("screenshot", False)))
            except GuiTaskError as exc:
                send(_GATEWAY_STATUS.get(exc.code, 400)
                     if exc.code != "bad_request" else 400,
                     _error(exc.code, str(exc)))
                return
            ctx.log_event("device:gui_task", f"{device_id}:{record['task_id']}")
            send(202, _json_bytes({"result": record}))
            return

        if len(parts) == 3 and parts[2] == "gui_tasks" and method == "GET":
            svc = getattr(ctx, "gui_task_service", None)
            if svc is None:
                send(503, _error("unavailable", "gui task service not assembled"))
                return
            send(200, _json_bytes({"result": {
                "device_id": device_id,
                "tasks": svc.list_for_device(device_id)}}))
            return

        if len(parts) == 4 and parts[2] == "gui_tasks" and method == "GET":
            svc = getattr(ctx, "gui_task_service", None)
            record = svc.get(parts[3]) if svc is not None else None
            if record is None or record.get("device_id") != device_id:
                send(404, _error("not_found", f"未知任务 {parts[3]}"))
                return
            send(200, _json_bytes({"result": record}))
            return

        if len(parts) == 5 and parts[2] == "gui_tasks" and parts[4] == "stop" \
                and method == "POST":
            svc = getattr(ctx, "gui_task_service", None)
            if svc is None:
                send(503, _error("unavailable", "gui task service not assembled"))
                return
            try:
                record = svc.stop(parts[3])
            except GuiTaskError as exc:
                send(_REGISTRY_STATUS.get(exc.code, 400), _error(exc.code, str(exc)))
                return
            if record.get("device_id") != device_id:
                send(404, _error("not_found", f"未知任务 {parts[3]}"))
                return
            ctx.log_event("device:gui_task_stop", f"{device_id}:{parts[3]}")
            send(200, _json_bytes({"result": record}))
            return

        if len(parts) == 3 and parts[2] == "receipts" and method == "GET":
            limit = 20
            if query and query.get("limit"):
                try:
                    limit = int(query["limit"][0])
                except (ValueError, TypeError, IndexError):
                    send(400, _error("bad_request", "limit 须为整数"))
                    return
                if not 1 <= limit <= 50:
                    send(400, _error("bad_request", "limit 须在 1..50"))
                    return
            send(200, _json_bytes({"result": {
                "device_id": device_id,
                "receipts": _list_receipts(ctx, registry, device_id, limit),
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


def _scan_all(store, prefix: str, *, cap: int = 5000) -> list[str]:
    """分页扫完一个键前缀（scan_keys 不可用时退化 keys()）；cap 防爆。"""
    scan = getattr(store, "scan_keys", None)
    if not callable(scan):
        return [k for k in store.keys() if k.startswith(prefix)][:cap]
    out: list[str] = []
    cursor = ""
    while len(out) < cap:
        batch = scan(prefix, after=cursor, limit=500)
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1]
        if len(batch) < 500:
            break
    return out[:cap]


def _receipt_view(device_id: str, key: str, record: dict, now: int) -> dict:
    """动作记录 → 管理端回执视图（unknown 口径与网关一致：过期非终态按
    unknown 呈现并带 outcome_source；终态 first-wins 不重述）。"""
    state = str(record.get("state") or "unknown")
    outcome_source = record.get("outcome_source")
    if state not in TERMINAL_RECORD_STATES and now > int(record.get("expires_at") or 0):
        state = "unknown"
        outcome_source = outcome_source or "expires_at"
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
    view: dict[str, Any] = {
        "idempotency_key": str(record.get("idempotency_key")
                               or key.rsplit(":", 1)[-1]),
        "action_id": str(record.get("action_id") or ""),
        "capability": str(record.get("capability") or ""),
        "state": state,
        "route": str(record.get("route") or "embedded_wss"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at", record.get("created_at")),
    }
    if outcome_source:
        view["outcome_source"] = outcome_source
    if record.get("task_id"):
        view["task_id"] = record["task_id"]
    if record.get("run_id"):
        view["run_id"] = record["run_id"]
    summary = result.get("summary") or receipt.get("summary") or ""
    if summary:
        view["summary"] = str(summary)[:200]
    error_code = receipt.get("error_code")
    if error_code:
        view["error_code"] = str(error_code)
    return view


#: kv 动作记录终态（单一事实源在 devices.action_records）
from whale_core.devices.action_records import (
    TERMINAL_STATES as TERMINAL_RECORD_STATES,
)


def _list_receipts(ctx, registry, device_id: str, limit: int) -> list[dict]:
    """设备最近回执（新→旧）；设备未知时由调用方 404 前置。"""
    from whale_core.devices.transports.base import now_ms
    now = now_ms()
    prefix = f"device:action:{device_id}:"
    views = []
    for key in _scan_all(ctx.store, prefix):
        record = ctx.store.get(key)
        if isinstance(record, dict):
            views.append(_receipt_view(device_id, key, record, now))
    views.sort(key=lambda v: (v.get("updated_at") or 0), reverse=True)
    return views[:limit]


def _device_activity(ctx, device_id: str) -> dict:
    """设备上/下行真实活动聚合（#60 拓扑数据源；全部来自 kv 与在线状态）。

    - uplink：experience.event 落库计数与最近一次时间（感知上行）；
    - downlink：动作记录在飞数（pending 未过期）与最近一次终态（动作下行）。
    """
    from whale_core.devices.transports.base import now_ms
    now = now_ms()
    uplink_count = 0
    uplink_last = None
    for key in _scan_all(ctx.store, f"device:event:{device_id}:"):
        record = ctx.store.get(key)
        if isinstance(record, dict):
            uplink_count += 1
            received = record.get("received_at")
            if isinstance(received, (int, float)) and (
                    uplink_last is None or received > uplink_last):
                uplink_last = int(received)
    pending = 0
    last: dict | None = None
    for key in _scan_all(ctx.store, f"device:action:{device_id}:"):
        record = ctx.store.get(key)
        if not isinstance(record, dict):
            continue
        state = str(record.get("state") or "unknown")
        if state not in TERMINAL_RECORD_STATES:
            if now <= int(record.get("expires_at") or 0):
                pending += 1
        updated = int(record.get("updated_at") or record.get("created_at") or 0)
        if last is None or updated > int(last.get("updated_at") or 0):
            view = _receipt_view(device_id, key, record, now)
            view["updated_at"] = updated
            last = view
    return {
        "uplink": {"events": uplink_count, "last_at": uplink_last},
        "downlink": {"pending": pending, "last_receipt": last},
    }


def _augment_transports(ctx, view: dict, device_id: str) -> None:
    """列表/详情增强（#77）：每通道健康 + 有效能力（真实连接视图）。

    失败静默降级（缺字段），不阻塞设备管理主路径；adb/ssh 探测可能秒级，
    列表端点需显式 ``?transports=1`` 才附带。
    """
    gw2 = getattr(ctx, "device_gateway_v2", None)
    if gw2 is None:
        return
    from whale_core.devices.sync_bridge import run_sync
    try:
        view["transports"] = run_sync(gw2.health_report(device_id))
    except Exception:  # noqa: BLE001
        pass
    try:
        view["capabilities_effective"] = run_sync(gw2.capabilities(device_id))
    except Exception:  # noqa: BLE001
        pass


def _transports_endpoint(handler, ctx, registry, method: str, query: dict) -> None:
    """GET/PUT /devices/transports —— adb/ssh 通道配置的读与重设（#77）。

    PUT 语义：整份替换；strict 校验（设备须 active、ssh 必须 host key pinning）；
    通过后落 kv 并热替换进 DeviceGatewayV2（embedded_wss 不动）。
    """
    send = handler._send
    from whale_core.devices import transport_config as tc
    if method == "GET":
        try:
            config = tc.load_config(ctx.store)
        except tc.TransportConfigError as exc:
            send(500, _error("internal", f"配置读取失败: {exc}"))
            return
        gw2 = getattr(ctx, "device_gateway_v2", None)
        result: dict[str, Any] = {
            "config": config,
            "registered": sorted(gw2.transports.keys()) if gw2 else [],
        }
        if query.get("health") == ["1"] and gw2 is not None:
            from whale_core.devices.sync_bridge import run_sync
            health: dict[str, Any] = {}
            device_ids = sorted({
                dev for kind in tc.CONFIG_KINDS
                for dev in (config.get(kind) or {})})
            for device_id in device_ids:
                try:
                    health[device_id] = run_sync(gw2.health_report(device_id))
                except Exception as exc:  # noqa: BLE001
                    health[device_id] = {"error": type(exc).__name__}
            result["health"] = health
        send(200, _json_bytes({"result": result}))
        return
    if method == "PUT":
        body = handler._body_json()
        if body is None or not isinstance(body, dict):
            send(400, _error("bad_request", "JSON object body required"))
            return
        config = body.get("config", body)
        try:
            validated = tc.validate_config(config, registry, strict=True)
        except tc.TransportConfigError as exc:
            send(400, _error(exc.code, str(exc)))
            return
        clean = {kind: validated[kind] for kind in tc.CONFIG_KINDS}
        ctx.store.put(tc.CONFIG_KEY, clean)
        gw2 = getattr(ctx, "device_gateway_v2", None)
        summary = tc.apply_transports(gw2, validated) if gw2 is not None else {
            "registered": [], "configured": {}, "replaced": {}}
        ctx.log_event("device:transports",
                      f"put:registered={summary['registered']}")
        send(200, _json_bytes({"result": {"ok": True, **summary}}))
        return
    send(405, _error("method_not_allowed", "use GET or PUT"))


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
    """query/action 下行（#77：经 DeviceGatewayV2 统一选路）。

    编排器未就绪（极端装配顺序）时回退旧的 EmbeddedWssGateway 直发路径，
    行为与口径完全一致（同一 kv 幂等域）。
    """
    send = handler._send
    if gateway is None:
        send(503, _error("unavailable", "device gateway not running"))
        return
    body = handler._body_json()
    if body is None:
        send(400, _error("bad_request", "JSON body required"))
        return
    from whale_core.devices.gateway_v2 import GatewayError as Gw2Error
    from whale_core.devices.sync_bridge import run_sync
    gw2 = getattr(handler.ctx, "device_gateway_v2", None)
    try:
        if op == "query":
            operation = str(body.get("operation") or "")
            if operation not in query_operations():
                send(400, _error("bad_request",
                                 f"operation 须为 v2 查询子集之一: "
                                 f"{list(query_operations())}"))
                return
            options = query_options(body)
            if gw2 is not None:
                result = run_sync(gw2.query(device_id, operation, **options))
            else:
                result = gateway.send_query(device_id, operation=operation, **options)
            handler.ctx.log_event("device:query", f"{device_id}:{operation}")
            send(200, _json_bytes({"result": {"ok": True, "result": result}}))
        else:
            capability = str(body.get("capability") or "")
            if capability not in action_capabilities():
                send(400, _error("bad_request",
                                 f"capability 须为 v2 动作子集之一: "
                                 f"{list(action_capabilities())}"))
                return
            options = action_options(body)
            if gw2 is not None:
                from whale_core.devices.receipt_codec import receipt_payload
                from whale_core.devices.transports.base import new_ulid
                idem = options.get("idempotency_key") or f"agent:{new_ulid()}"
                # §7.1：同键已有持久记录 → 回记录本体（state+receipt 形状，
                # 与旧网关口径一致），绝不重放、不再走 execute
                existing = gw2.action_record(device_id, idem)
                if existing is not None:
                    handler.ctx.log_event("device:action",
                                          f"{device_id}:{capability}:duplicate")
                    recorded = dict(existing)
                    recorded["duplicate"] = True
                    send(200, _json_bytes(
                        {"result": {"ok": True, "receipt": recorded}}))
                    return
                receipt = run_sync(gw2.execute(
                    device_id, capability, options.get("arguments") or {},
                    idempotency_key=idem,
                    timeout_ms=max(1, int(options.get("timeout_s", 15.0) * 1000)),
                    task_id=options.get("task_id"),
                    run_id=options.get("run_id"),
                    observation_id=options.get("observation_id"),
                    approval_ref=options.get("approval_ref"),
                    correlation_id=options.get("correlation_id"),
                    authorized_scope=options.get("authorized_scope")))
                handler.ctx.log_event("device:action", f"{device_id}:{capability}")
                send(200, _json_bytes(
                    {"result": {"ok": True, "receipt": receipt_payload(receipt)}}))
            else:
                receipt = gateway.send_action(device_id, capability=capability, **options)
                handler.ctx.log_event("device:action", f"{device_id}:{capability}")
                send(200, _json_bytes({"result": {"ok": True, "receipt": receipt}}))
    except (GatewayError, Gw2Error) as exc:
        send(_GATEWAY_STATUS.get(exc.code, 500), _error(exc.code, str(exc)))
    except ValueError as exc:
        # 出站帧不过 v2 schema（如 task_id/run_id 形状非法）→ 调用方错误
        send(400, _error("invalid_message", str(exc)[:300]))
