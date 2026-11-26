"""DeviceService: paired phones as first-class capability providers.

Owns the registry/ledger, the device gateway, the capability bridge, owner
notifications and the ``devices.*`` RPC surface. Bus topics:

    device.paired · device.revoked · device.updated · device.connected · device.disconnected
    device.status (heartbeat-visible changes) · device.event (new experience event)
    device.action (phase=request|receipt) · device.notification · device.notification_action
    device.trace · device.recording
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from ..capabilities.spec import CapabilitySpec, Risk, ToolContext, ToolResult, capability, obj
from ..capabilities.spec import i as int_param
from ..capabilities.spec import s as str_param
from .callbridge import BridgeSignalClient
from ..kernel.errors import WhaleError
from ..kernel.ids import new_id, now_ms, ulid
from ..link.protocol import Client, NoParams
from .bridge import CATALOG, action_result, check_arguments, query_result
from .contract import (ACTION_CAPS, CAPABILITIES, IDEM_RE, NOTIFY_CATEGORIES, PROTOCOL_VERSION, QUERY_OPS, WS_PATH,
                       ContractValidator)
from . import privacy
from .digest import PhoneDigest
from .gateway import DeviceGateway, Session, accept_capabilities
from .registry import DeviceLedger, DeviceRegistry

log = logging.getLogger("whale.devices")

_RUN_RE = re.compile(r"^run_[0-9A-HJKMNP-TV-Z]{26}$")
_TASK_RE = re.compile(r"^task_[0-9A-HJKMNP-TV-Z]{26}$")
_OBS_RE = re.compile(r"^obs_[0-9A-HJKMNP-TV-Z]{26}$")
NOTIFY_TTL_MS = {"chat.message": 3600_000, "approval.requested": 600_000}
LINK_TICKET_TTL_S = 900  # Link 票有效期（C 鉴权）；绑 device_id，撤销即失效
_KIND_CATEGORY = {"task": "task.completed", "approval": "approval.requested", "reminder": "reminder.triggered",
                  "message": "chat.message"}
OUTPUT_PAGE = 48 * 1024


# -- RPC parameter models (module level: the pydantic schema export needs them) ----------------------
class DeviceRef(BaseModel):
    id: str = Field(min_length=1, description="device id (32 hex) or its unique name")


class PairStart(BaseModel):
    name: str = Field("", max_length=64)
    scopes: list[str] | None = None
    ttl_s: int = Field(600, ge=60, le=3600)


class DeviceRename(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=64)


class DeviceScopes(BaseModel):
    id: str
    scopes: list[str]


class DeviceAction(BaseModel):
    id: str
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(15.0, ge=0.1, le=600)
    idempotency_key: str | None = Field(None, pattern=r"^[0-9A-Za-z:_-]{8,128}$")


class DeviceQuery(BaseModel):
    id: str
    operation: str
    filters: dict[str, Any] = Field(default_factory=dict)
    max_items: int = Field(10, ge=1, le=100)
    timeout_s: float = Field(15.0, gt=0, le=120)


class DeviceRole(BaseModel):
    id: str
    role: str = Field(pattern=r"^(agent|personal)$", description="agent = AI 专用手机；personal = 主人日常手机")


class DevicePrivacy(BaseModel):
    id: str
    excluded_apps: list[str] | None = None
    excluded_domains: list[str] | None = None
    redact_secrets: bool | None = None
    drop_sensitive: bool | None = None


class PhoneSearch(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    hours: float = Field(72, gt=0, le=24 * 90)
    limit: int = Field(20, ge=1, le=100)


class DevicePage(BaseModel):
    id: str
    limit: int = Field(50, ge=1, le=500)


class DeviceNotify(BaseModel):
    id: str
    title: str = Field(min_length=1, max_length=200)
    body: str = Field("", max_length=2000)
    category: str = "chat.message"
    priority: str = Field("normal", pattern=r"^(low|normal|high)$")


class DeviceConfirm(BaseModel):
    id: str
    scopes: list[str] = Field(min_length=1, max_length=32)
    channel: str = Field("console", min_length=1, max_length=64)
    action_fingerprint: str | None = Field(None, max_length=128)
    timeout_s: float = Field(120.0, ge=1, le=600)


def slugify(name: str, device_id: str) -> str:
    out = "".join(ch if (ch.isascii() and ch.isalnum()) else "_" for ch in (name or "").lower())
    out = re.sub(r"_+", "_", out).strip("_")[:16].strip("_")
    return out or device_id[:6]


class DeviceService:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.registry = DeviceRegistry(app.db)
        self.ledger = DeviceLedger(app.db)
        self.contracts = ContractValidator()
        self.gateway = DeviceGateway(self, self.contracts)
        dcfg = app.config.value.devices
        self.gateway.heartbeat_interval = dcfg.heartbeat_interval_s
        self.gateway.heartbeat_timeout = float(dcfg.heartbeat_timeout_s)
        self.gateway.challenge_ttl = dcfg.challenge_ttl_s
        self.receipt_grace_s = dcfg.receipt_grace_s  # core waits timeout_ms + this for a receipt, then → unknown
        self.digest = PhoneDigest(self)
        app.scheduler.handler("phone.digest", lambda args: self.digest.run())
        self._register_phone_memory()
        self._register_call_bridge()
        self.notify_receipt_s = 10.0
        self._watchdog: asyncio.Task | None = None
        self._inflight: dict[tuple[str, str], asyncio.Future] = {}   # (device, idempotency key) → outcome
        self._register_rpc()
        app.hub.add_notifier(self.notify_owner)

    # -- lifecycle --------------------------------------------------------------------------------
    async def start(self) -> None:
        swept = self.ledger.sweep_inflight()
        if swept:
            log.info("%d device actions were in flight before restart → unknown", swept)
        self._watchdog = asyncio.get_running_loop().create_task(self._watch())
        interval = max(5, self.app.config.value.devices.digest_interval_min)
        self.app.scheduler.ensure_system("sys_phone_digest", "手机经历整理成记忆", "phone.digest",
                                         {"every_min": interval}, True)

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        await self.gateway.close_all()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(max(0.05, min(1.0, self.gateway.heartbeat_timeout / 3)))
            try:
                await self.gateway.sweep()
            except Exception:
                log.exception("device heartbeat sweep failed")

    def routes(self) -> list[Any]:
        from starlette.routing import Route, WebSocketRoute

        return [WebSocketRoute(WS_PATH, self.gateway.endpoint),
                Route("/api/devices/pair", self._http_redeem, methods=["POST"]),
                Route("/api/devices/link_ticket", self._http_link_ticket, methods=["POST"])]

    # -- views ------------------------------------------------------------------------------------
    def _view(self, rec: dict[str, Any]) -> dict[str, Any]:
        sess = self.gateway.sessions.get(rec["id"])
        live = sess is not None and not sess.closed
        prefix = "device." + sess.slug + "." if live and sess.slug else ""
        return {
            "id": rec["id"], "name": rec["name"], "model": (sess.device.get("model") if live else None) or rec["model"],
            "flavor": (sess.flavor if live else "") or rec["flavor"],
            "app_version": (sess.app_version if live else "") or rec["app_version"],
            "android": (sess.device.get("android_release") if live else None) or rec["android"],
            "online": bool(live and not sess.offline), "status": sess.state() if live else "offline",
            "battery": sess.battery if live else None, "network": sess.network if live else None,
            "foreground": sess.foreground if live else None,
            "capabilities": list(sess.accepted) if live else [],
            "tools": [prefix + c for c in sess.accepted if c in CATALOG] if live else [],
            "features": dict(sess.features) if live else {},
            "scopes": rec["scopes"], "last_seen": rec["last_seen"],
            "connected_at": sess.connected_at if live else None, "session_id": sess.session_id if live else None,
            "revoked": rec["revoked"], "created_at": rec["created_at"], "paired_via": rec["paired_via"],
            "role": self.role_of(rec), "role_explicit": rec.get("role") in privacy.ROLES,
            "privacy": rec["privacy"] if self.role_of(rec) == "personal" else None,
        }

    def _register_phone_memory(self) -> None:
        @capability(self.app.capabilities, "phone.recall",
                    "在主人手机和鲸鱼自己手机的近期经历里查找（如“我刚才在手机上看的那个视频叫什么”）。",
                    params=obj(["query"], query=str_param("关键词，空格分隔"), hours=int_param("回看多少小时", default=72)),
                    risk=Risk.read, title="回想手机经历", group="phone", owner_only=True)
        async def phone_recall(args: dict[str, Any], ctx: ToolContext) -> Any:
            found = self.digest.search(args["query"], float(args.get("hours") or 72))
            return {"events": found, "summary": f"找到 {len(found)} 条手机经历"}

    # -- VoIP 呼出（一期：东京呼叫桥信令直发；二期设备链路原生 call.invite 随手机侧同启） ----------------
    def _call_bridge_client(self) -> BridgeSignalClient:
        """按活配置+secrets 现场构建（调用稀少，随用随建随关，无陈旧客户端）。"""
        cfg = self.app.config.value.devices.call_bridge
        url = str(cfg.url or "").strip()
        device_id = str(cfg.device_id or "").strip()
        if not url or not device_id:
            raise WhaleError("invalid_params", "未配置 devices.call_bridge（url/device_id）")
        token = self.app.config.secret("devices.call_bridge.token")
        return BridgeSignalClient(self.app, url, token), device_id

    async def call_start(self, name: str = "") -> ToolResult:
        client, device_id = self._call_bridge_client()
        try:
            outcome = await client.ring(device_id, name=name)
        finally:
            await client.close()
        status = outcome["status"]
        if status == "ok":
            return ToolResult("ok", data=outcome, summary="已呼叫主人手机（振铃中）")
        return ToolResult(status, data=outcome, error=outcome.get("error", ""))

    async def call_stop(self) -> ToolResult:
        client, device_id = self._call_bridge_client()
        try:
            outcome = await client.hangup(device_id)
        finally:
            await client.close()
        if outcome["status"] == "ok":
            return ToolResult("ok", data=outcome, summary="已挂断")
        return ToolResult("error", data=outcome, error=outcome.get("error", ""))

    def _register_call_bridge(self) -> None:
        @capability(self.app.capabilities, "phone.call.start",
                    "给主人手机打一通鲸鱼 VoIP 来电（接听即与鲸鱼通话）。来电匿名显示（不显名称）。",
                    params=obj(name=str_param("来电显示名（默认空=匿名不显名）")),
                    risk=Risk.external, title="打电话给主人", group="phone", owner_only=True)
        async def phone_call_start(args: dict[str, Any], ctx: ToolContext) -> Any:
            name = str(args.get("name") or "")           # 主人令 2026-11-26：默认匿名（空名恒传）
            return await self.call_start(name)

        @capability(self.app.capabilities, "phone.call.stop",
                    "挂断/取消主人手机上的鲸鱼来电或通话。",
                    params=obj(), risk=Risk.write, title="挂断电话", group="phone", owner_only=True)
        async def phone_call_stop(args: dict[str, Any], ctx: ToolContext) -> Any:
            return await self.call_stop()

    def list(self) -> list[dict[str, Any]]:
        return [self._view(r) for r in self.registry.list()]

    def get(self, ref: str) -> dict[str, Any]:
        return self._view(self._resolve(ref))

    def _resolve(self, ref: str) -> dict[str, Any]:
        rec = self.registry.get(ref)
        if rec is not None:
            return rec
        named = [r for r in self.registry.list() if r["name"] == ref or slugify(r["name"], r["id"]) == ref]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            raise WhaleError("conflict", "several devices are called " + ref + "; use the id")
        raise WhaleError("not_found", "device not found: " + ref, details={"id": ref})

    def _live(self, device_id: str) -> Session:
        sess = self.gateway.sessions.get(device_id)
        if sess is None or sess.closed:
            raise WhaleError("unavailable", "device is not connected", retryable=True)
        if sess.offline:
            raise WhaleError("unavailable", "device missed its heartbeats (offline)", retryable=True)
        return sess

    # -- pairing / management ---------------------------------------------------------------------
    def pair_start(self, name: str = "", scopes: list[str] | None = None, ttl_s: int = 600) -> dict[str, Any]:
        return self.registry.start_pairing(name, scopes, ttl_s)

    async def _paired(self, rec: dict[str, Any]) -> dict[str, Any]:
        view = self._view(rec)
        await self.app.bus.emit("device.paired", {"device": view})
        return view

    async def redeem(self, code: str, pubkey_hex: str, label: str = "") -> dict[str, Any]:
        return await self._paired(self.registry.redeem(code, pubkey_hex, label))

    async def revoke(self, ref: str) -> dict[str, Any]:
        rec = self.registry.revoke(self._resolve(ref)["id"])
        kicked = await self.gateway.kick(rec["id"], "device_revoked", "device has been revoked")
        self.app.capabilities.unregister_provider("device:" + rec["id"])
        # 撤销联动：踢掉该设备持有的 Link 聊天面连接（ticket 校验也随之拒绝）
        link_kicked = 0
        for cl in list(self.app.hub.clients.values()):
            if cl.meta.get("device_id") == rec["id"]:
                if await self.app.hub.kick_client(cl.id, "device_revoked", "device has been revoked"):
                    link_kicked += 1
        view = self._view(rec)
        await self.app.bus.emit("device.revoked", {"device": view, "kicked": kicked,
                                                   "link_kicked": link_kicked})
        return {**view, "kicked": kicked, "link_kicked": link_kicked}

    async def rename(self, ref: str, name: str) -> dict[str, Any]:
        rec = self.registry.rename(self._resolve(ref)["id"], name)
        sess = self.gateway.sessions.get(rec["id"])
        if sess is not None and not sess.closed:
            sess.name = rec["name"]
            self._publish_capabilities(sess)
        view = self._view(rec)
        await self.app.bus.emit("device.updated", {"device": view})
        return view

    async def set_scopes(self, ref: str, scopes: list[str]) -> dict[str, Any]:
        rec = self.registry.set_scopes(self._resolve(ref)["id"], scopes)
        sess = self.gateway.sessions.get(rec["id"])
        if sess is not None and not sess.closed:
            sess.accepted = accept_capabilities(sess.declared, rec["scopes"], sess.features)
            self._publish_capabilities(sess)
        view = self._view(rec)
        await self.app.bus.emit("device.updated", {"device": view})
        return view

    # -- session callbacks (from the gateway) ----------------------------------------------------------
    async def session_opened(self, sess: Session) -> None:
        self.registry.seen(sess.device_id, model=str(sess.device.get("model") or ""), flavor=sess.flavor,
                           app_version=sess.app_version, android=str(sess.device.get("android_release") or ""))
        self._publish_capabilities(sess)
        await self.app.bus.emit("device.connected", {"device": self._view(self.registry.require(sess.device_id))})

    async def session_closed(self, sess: Session, reason: str) -> None:
        self.app.capabilities.unregister_provider("device:" + sess.device_id)
        self.registry.seen(sess.device_id)
        await self.app.bus.emit("device.disconnected", {"id": sess.device_id, "name": sess.name, "reason": reason})

    async def heartbeat(self, sess: Session, *, changed: bool, recovered: bool) -> None:
        for spec in self.app.capabilities.list(pattern="device.*"):
            if spec.provider == "device:" + sess.device_id:
                spec.available = not sess.offline
        if recovered or changed:
            self.registry.seen(sess.device_id)
            await self.app.bus.emit("device.status", {
                "id": sess.device_id, "name": sess.name, "status": sess.state(), "online": not sess.offline,
                "battery": sess.battery, "network": sess.network, "modules": sess.modules})

    def role_of(self, rec: dict[str, Any] | None) -> str:
        """Explicit role, else: root build → the AI's own phone, daily build → the owner's phone."""
        if not rec:
            return "personal"
        if rec.get("role") in privacy.ROLES:
            return rec["role"]
        return "agent" if rec.get("flavor") == "root" else "personal"

    async def experience(self, sess: Session, p: dict[str, Any]) -> None:
        rec = self.registry.get(sess.device_id)
        role = self.role_of(rec)
        verdict = privacy.screen(p, role, rec["privacy"] if rec else privacy.default_settings())
        if not verdict.keep:
            # acked like any event (the phone stops resending) but nothing private is stored
            if self.ledger.add_tombstone(sess.device_id, p, verdict.reason):
                self.app.bus.publish_event("device.event_filtered", {"device_id": sess.device_id,
                                                                     "reason": verdict.reason})
            return
        p = verdict.event
        if not self.ledger.add_event(sess.device_id, p):
            return
        extras = p.get("extras") or {}
        if isinstance(extras.get("package"), str):
            sess.foreground = extras["package"][:128]
        await self.app.bus.emit("device.event", {"device_id": sess.device_id, "device": sess.name, "role": role,
                                                 "event": p})

    async def receipt(self, sess: Session, p: dict[str, Any]) -> dict[str, Any] | None:
        record, changed = self.ledger.record_receipt(sess.device_id, p)
        if record is None:
            log.warning("receipt for unknown action %s from %s", p.get("action_id"), sess.device_id)
            return None
        if changed and record["status"] not in ("pending", "received"):
            await self.app.bus.emit("device.action", {"phase": "receipt", "action": self.ledger.view(record)})
        return record

    # -- capability bridge -------------------------------------------------------------------------------
    def _publish_capabilities(self, sess: Session) -> None:
        provider = "device:" + sess.device_id
        self.app.capabilities.unregister_provider(provider)
        slug = slugify(sess.name, sess.device_id)
        taken = {s.slug for s in self.gateway.sessions.values() if s is not sess and not s.closed}
        sess.slug = slug if slug not in taken else slug + "_" + sess.device_id[:4]
        for cap in sess.accepted:
            d = CATALOG.get(cap)
            if d is None:
                continue
            spec = CapabilitySpec(
                name="device." + sess.slug + "." + cap, title=d.title + " · " + sess.name,
                description=d.description + " (phone: " + sess.name + ")", params=d.params, risk=d.risk,
                provider=provider, group="device:" + sess.name,
                timeout_s=d.max_timeout_s + self.receipt_grace_s + 5, owner_only=True)

            async def handler(args: dict[str, Any], ctx: ToolContext, _dev: str = sess.device_id,
                              _cap: str = cap) -> ToolResult:
                return await self.invoke(_dev, _cap, args, ctx)

            self.app.capabilities.register(spec, handler)

    async def invoke(self, device_id: str, capability: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        d = CATALOG[capability]
        if d.kind == "query":
            filters = dict(args)
            if capability == "device.output.read":
                text, truncated = await self.read_output(device_id, str(args["output_ref"]),
                                                         offset=int(args.get("offset") or 0))
                return ToolResult("ok", data={"text": text, "truncated": truncated}, summary=text[:200])
            res = query_result(await self.query(device_id, capability, filters))
            if capability == "device.status" and res.status == "ok" and res.data["items"]:
                self._note_status(device_id, res.data["items"][0])
            return res
        if capability == "phone.notification.post":
            return await self._post_notification(device_id, args)
        args = check_arguments(capability, args)
        key = None
        if ctx.run_id and ctx.step_id and ctx.run_id != "manual":
            key = _idem("agent:" + ctx.run_id + ":" + ctx.step_id)
        timeout = d.timeout_s
        if capability == "device.shell" and isinstance(args.get("timeout_ms"), int):
            timeout = min(d.max_timeout_s, max(1.0, args["timeout_ms"] / 1000 + 5))
        if capability == "device.wait":
            timeout = min(d.max_timeout_s, int(args.get("duration_ms") or 0) / 1000 + 2)
        view = await self.execute_action(device_id, capability, args, timeout_s=timeout, idempotency_key=key,
                                         run_id=ctx.run_id, origin="agent" if ctx.run_id != "manual" else "owner")
        result = action_result(view)
        if result.status == "ok" and view.get("output_ref") and capability in ("device.observe", "device.shell",
                                                                                 "device.file.read", "device.logcat",
                                                                                 "device.file.list"):
            await self._attach_output(device_id, capability, view, result)
        return result

    async def _attach_output(self, device_id: str, capability: str, view: dict[str, Any], result: ToolResult) -> None:
        try:
            text, truncated = await self.read_output(device_id, view["output_ref"], limit=256 * 1024)
        except WhaleError as exc:
            view["output_error"] = exc.message
            return
        if capability == "device.observe":
            try:
                obs = json.loads(text)
            except ValueError:
                view["output_error"] = "observation output is not JSON"
                return
            view["observation"] = compact_observation(obs)
            fg = view["observation"].get("foreground_package")
            if fg:
                sess = self.gateway.sessions.get(device_id)
                if sess is not None:
                    sess.foreground = fg
        else:
            view["output"] = text[:20000]
            view["output_truncated"] = truncated or len(text) > 20000
        result.data = view

    def _note_status(self, device_id: str, item: dict[str, Any]) -> None:
        sess = self.gateway.sessions.get(device_id)
        if sess is None:
            return
        fg = item.get("foreground_package") or item.get("foreground")
        if isinstance(fg, str):
            sess.foreground = fg[:128]
        if isinstance(item.get("battery"), int) and not isinstance(item.get("battery"), bool):
            sess.battery = item["battery"]

    # -- actions ----------------------------------------------------------------------------------------
    async def execute_action(self, ref: str, capability: str, arguments: dict[str, Any] | None = None, *,
                             timeout_s: float = 15.0, idempotency_key: str | None = None, run_id: str = "",
                             task_id: str = "", origin: str = "owner", observation_id: str | None = None,
                             approval_ref: str | None = None, correlation_id: str | None = None) -> dict[str, Any]:
        """action.request → truthful terminal outcome (a ledger view dict).

        A repeated idempotency key never re-executes: it returns the recorded
        state (or joins the in-flight wait). A wait timeout becomes ``unknown``.
        """
        rec = self._resolve(ref)
        device_id = rec["id"]
        if rec["revoked"]:
            raise WhaleError("forbidden", "device is revoked")
        if capability not in ACTION_CAPS:
            raise WhaleError("invalid_params", "not an action capability: " + capability)
        if capability not in rec["scopes"]:
            raise WhaleError("forbidden", "device scope does not grant " + capability, details={"code": "scope_denied"})
        if idempotency_key is not None and not IDEM_RE.match(idempotency_key):
            raise WhaleError("invalid_params", "idempotency_key must match ^[0-9A-Za-z:_-]{8,128}$")
        key = idempotency_key or ("core:" + ulid())
        existing = self.ledger.action_by_key(device_id, key)
        if existing is not None:
            return await self._replay(device_id, key, existing)
        sess = self._live(device_id)
        timeout_ms = int(max(0.1, min(600.0, float(timeout_s))) * 1000)
        args = dict(arguments or {})
        obs = observation_id or args.pop("observation_id", None)
        action_id = new_id("act")
        payload: dict[str, Any] = {"action_id": action_id, "capability": capability, "arguments": args,
                                   "timeout_ms": timeout_ms, "expires_at": now_ms() + timeout_ms,
                                   "idempotency_key": key, "authorized_scope": [capability]}
        if run_id and _RUN_RE.match(run_id):
            payload["run_id"] = run_id
        if task_id and _TASK_RE.match(task_id):
            payload["task_id"] = task_id
        if obs and _OBS_RE.match(str(obs)):
            payload["observation_id"] = str(obs)
        elif obs:
            raise WhaleError("invalid_params", "observation_id must look like obs_<ULID>")
        if approval_ref:
            payload["approval_ref"] = str(approval_ref)[:128]
        if correlation_id:
            payload["correlation_id"] = str(correlation_id)[:64]
        # Contract preflight before reserving the key: an invalid request must not burn it.
        err = self.contracts.validate("action.request", {"v": PROTOCOL_VERSION, "id": ulid(), "type": "action.request",
                                                         "ts": now_ms(), "seq": 0, "dir": "downlink",
                                                         "payload": payload})
        if err:
            raise WhaleError("invalid_params", "action.request rejected by contract: " + err)
        if not self.ledger.reserve_action({"action_id": action_id, "device_id": device_id, "idempotency_key": key,
                                           "capability": capability, "arguments": args, "origin": origin,
                                           "run_id": payload.get("run_id"), "expires_at": payload["expires_at"]}):
            return await self._replay(device_id, key, self.ledger.action_by_key(device_id, key) or {})
        outcome: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[(device_id, key)] = outcome
        record = self.ledger.action(action_id) or {}
        await self.app.bus.emit("device.action", {"phase": "request", "action": self.ledger.view(record)})
        try:
            try:
                await self.gateway.request(sess, action_id, "action.receipt", "action.request", payload,
                                           timeout_ms / 1000 + self.receipt_grace_s)
                record = self.ledger.action(action_id) or record
                if record.get("status") in ("pending", "received"):  # connection lost before any terminal
                    record = self.ledger.estimate(action_id, "connection-lost") or record
            except asyncio.TimeoutError:
                record = self.ledger.estimate(action_id, "wait-timeout") or record
            except WhaleError as exc:
                # Sending failed: the request may or may not have left the socket.
                record = self.ledger.estimate(action_id, "send-failed") or record
                log.warning("action.request send failed: %s", exc.message)
            view = self.ledger.view(record)
            if record.get("outcome_source", "").startswith("core:"):
                await self.app.bus.emit("device.action", {"phase": "receipt", "action": view})
            if not outcome.done():
                outcome.set_result(view)
            return view
        finally:
            self._inflight.pop((device_id, key), None)
            if not outcome.done():  # cancelled mid-wait: joiners get the recorded (non-final) state
                outcome.set_result(self.ledger.view(self.ledger.action(action_id) or record))

    async def _replay(self, device_id: str, key: str, existing: dict[str, Any]) -> dict[str, Any]:
        fut = self._inflight.get((device_id, key))
        if fut is not None:
            view = dict(await asyncio.shield(fut))
        else:
            if existing.get("status") in ("pending", "received") and now_ms() > existing.get("expires_at", 0) + 5000:
                existing = self.ledger.estimate(existing["action_id"], "expired") or existing
            view = self.ledger.view(existing)
        view["duplicate"] = True
        return view

    # -- queries ----------------------------------------------------------------------------------------
    async def query(self, ref: str, operation: str, filters: dict[str, Any] | None = None, *, max_items: int = 10,
                    timeout_s: float = 15.0) -> dict[str, Any]:
        rec = self._resolve(ref)
        if rec["revoked"]:
            raise WhaleError("forbidden", "device is revoked")
        if operation not in QUERY_OPS:
            raise WhaleError("invalid_params", "not a query operation: " + operation)
        if operation not in rec["scopes"]:
            raise WhaleError("forbidden", "device scope does not grant " + operation, details={"code": "scope_denied"})
        sess = self._live(rec["id"])
        request_id = ulid()
        payload = {"request_id": request_id, "operation": operation, "filters": dict(filters or {}),
                   "max_items": int(max_items), "deadline": now_ms() + int(timeout_s * 1000)}
        try:
            return await self.gateway.request(sess, request_id, "sensor.result", "sensor.query", payload, timeout_s)
        except asyncio.TimeoutError as exc:
            raise WhaleError("timeout", "device did not answer " + operation + " in time", retryable=True) from exc

    async def read_output(self, ref: str, output_ref: str, *, offset: int = 0,
                          limit: int = 64 * 1024) -> tuple[str, bool]:
        """Assemble a large output via device.output.read byte pages (§7.4)."""
        buf = bytearray()
        pos = max(0, int(offset))
        for _ in range(64):
            res = await self.query(ref, "device.output.read", {"output_ref": output_ref, "offset": pos,
                                                               "max_bytes": OUTPUT_PAGE, "page_format": "bytes.v1"},
                                   max_items=1, timeout_s=15.0)
            if res.get("status") != "ok" or not res.get("data"):
                raise WhaleError("unavailable", "device could not read " + output_ref + ": " + str(res.get("status")))
            item = res["data"][0]
            try:
                if isinstance(item.get("text"), str):
                    chunk = item["text"].encode("utf-8")
                else:
                    chunk = base64.b64decode(item.get("bytes_b64") or item.get("data_base64") or "", validate=True)
            except (binascii.Error, ValueError) as exc:
                raise WhaleError("invalid_params", "device returned an undecodable output page") from exc
            buf.extend(chunk)
            pos += len(chunk)
            more = bool(item.get("truncated", res.get("truncated")))
            if not more or not chunk:
                return buf.decode("utf-8", errors="replace"), bool(item.get("source_truncated"))
            if len(buf) >= limit:
                return buf[:limit].decode("utf-8", errors="replace"), True
        return buf.decode("utf-8", errors="replace"), True

    # -- device confirmation (§14) ---------------------------------------------------------------------
    async def confirm(self, ref: str, scopes: list[str], *, channel: str = "console", principal: str = "owner",
                      action_fingerprint: str | None = None, timeout_s: float = 120.0) -> dict[str, Any]:
        rec = self._resolve(ref)
        sess = self._live(rec["id"])
        challenge_id = ulid()
        payload: dict[str, Any] = {"challenge_id": challenge_id, "device_id": rec["id"], "principal_id": principal,
                                   "channel": channel, "scopes": list(dict.fromkeys(scopes)),
                                   "expires_at": now_ms() + int(timeout_s * 1000)}
        if action_fingerprint:
            payload["action_fingerprint"] = action_fingerprint
        try:
            res = await self.gateway.request(sess, challenge_id, "device.confirm.response", "device.confirm.request",
                                             payload, timeout_s)
        except asyncio.TimeoutError:
            res = {"approved": False, "reason": "timeout"}
        return {"challenge_id": challenge_id, **res}   # never approved by default

    # -- notifications (§13) ----------------------------------------------------------------------------
    def _notification(self, device_id: str, title: str, body: str, meta: dict[str, Any]) -> dict[str, Any]:
        category = meta.get("category")
        if category not in NOTIFY_CATEGORIES:
            category = _KIND_CATEGORY.get(str(meta.get("kind") or ""), "chat.message")
        priority = meta.get("priority") if meta.get("priority") in ("low", "normal", "high") else (
            "high" if category == "approval.requested" else "normal")
        now = now_ms()
        payload: dict[str, Any] = {"event_id": new_id("evt"), "notification_id": new_id("notif"),
                                   "device_id": device_id, "category": category,
                                   "title": (title or "Whale")[:200], "body": (body or "")[:2000],
                                   "priority": priority, "created_at": now,
                                   "expires_at": now + NOTIFY_TTL_MS.get(category, 86400_000)}
        target = meta.get("target")
        if not isinstance(target, dict) and meta.get("task_id"):
            target = {"type": "task", "id": meta["task_id"]}
        elif not isinstance(target, dict) and meta.get("approval_id"):
            target = {"type": "approval", "id": meta["approval_id"]}
        if isinstance(target, dict) and target.get("type") in ("task", "conversation", "device", "approval",
                                                               "chat", "none"):
            payload["target"] = {k: str(v)[:128] for k, v in target.items() if k in ("type", "id")}
        actions = meta.get("actions")
        if isinstance(actions, list):
            payload["actions"] = [{"action_id": str(a["action_id"])[:32], "label": str(a["label"])[:32]}
                                  for a in actions[:4] if isinstance(a, dict) and a.get("action_id") and a.get("label")
                                  and re.fullmatch(r"[a-z0-9_]{1,32}", str(a["action_id"]))]
        for key in ("group_key", "collapse_key"):
            if isinstance(meta.get(key), str):
                payload[key] = meta[key][:128]
        return payload

    def _notify_targets(self) -> list[dict[str, Any]]:
        return [r for r in self.registry.list() if not r["revoked"] and "phone.notification.post" in r["scopes"]]

    async def _push(self, sess: Session, payload: dict[str, Any]) -> bool:
        try:
            await self.gateway.send(sess, "notification.event", payload)
        except WhaleError as exc:
            log.warning("notification.event to %s failed: %s", sess.device_id, exc.message)
            return False
        return True

    async def notify_owner(self, title: str, body: str, meta: dict[str, Any]) -> str | None:
        """LinkHub notifier: hand the notification to every online phone that may show it."""
        delivered: list[str] = []
        for rec in self._notify_targets():
            payload = self._notification(rec["id"], title, body, meta or {})
            sess = self.gateway.sessions.get(rec["id"])
            live = sess is not None and not sess.closed and not sess.offline
            if live and "phone.notification.post" not in sess.accepted:
                continue      # connected, but this build/user did not enable native notifications
            if live and await self._push(sess, payload):
                self.ledger.add_notification(payload, "sent")
                delivered.append("device:" + sess.name)
            else:
                self.ledger.add_notification(payload, "queued")   # replayed on notification.sync
        return ", ".join(delivered) or None

    async def notify_device(self, ref: str, title: str, body: str = "", meta: dict[str, Any] | None = None,
                            wait_s: float | None = None) -> dict[str, Any]:
        rec = self._resolve(ref)
        if rec["revoked"] or "phone.notification.post" not in rec["scopes"]:
            raise WhaleError("forbidden", "device may not receive notifications", details={"code": "scope_denied"})
        sess = self._live(rec["id"])
        payload = self._notification(rec["id"], title, body, meta or {})
        self.ledger.add_notification(payload, "sent")
        key = "notif:" + payload["event_id"]
        try:
            receipt = await self.gateway.request(sess, key, "notification.receipt", "notification.event", payload,
                                                 self.notify_receipt_s if wait_s is None else wait_s)
            status = receipt["status"]
        except asyncio.TimeoutError:
            status = "unknown"
        return {"event_id": payload["event_id"], "notification_id": payload["notification_id"], "status": status}

    async def _post_notification(self, device_id: str, args: dict[str, Any]) -> ToolResult:
        res = await self.notify_device(device_id, str(args.get("title") or ""), str(args.get("body") or ""))
        if res["status"] in ("received", "displayed", "opened", "action_submitted", "dismissed"):
            return ToolResult("ok", data=res, summary="notification " + res["status"])
        if res["status"] in ("failed", "expired"):
            return ToolResult("error", data=res, error="phone reported " + res["status"])
        return ToolResult("unknown", data=res, error="handed to the phone, no receipt yet")

    async def revoke_notification(self, ref: str, notification_id: str, reason: str = "") -> bool:
        rec = self._resolve(ref)
        sess = self._live(rec["id"])
        payload = {"event_id": new_id("evt"), "notification_id": notification_id, "device_id": rec["id"]}
        if reason:
            payload["reason"] = reason[:200]
        await self.gateway.send(sess, "notification.revoke", payload)
        return True

    async def notification_receipt(self, sess: Session, p: dict[str, Any]) -> None:
        self.ledger.notification_status(sess.device_id, p["event_id"], p["status"])
        await self.app.bus.emit("device.notification", {"device_id": sess.device_id, **p})

    async def notification_action(self, sess: Session, p: dict[str, Any]) -> None:
        # A button press is a request, not an authorization: approvals are still resolved in core.
        n = self.ledger.notification(p["event_id"])
        await self.app.bus.emit("device.notification_action", {
            "device_id": sess.device_id, "event_id": p["event_id"], "notification_id": p["notification_id"],
            "action_id": p["action_id"], "category": (n or {}).get("category"),
            "target": ((n or {}).get("payload") or {}).get("target")})

    async def notification_sync(self, sess: Session, last_event_id: str | None) -> None:
        for payload in self.ledger.undelivered(sess.device_id, last_event_id):
            if await self._push(sess, payload):
                self.ledger.notification_status(sess.device_id, payload["event_id"], "sent")

    # -- HTTP pairing endpoints ---------------------------------------------------------------------------
    async def _body(self, request: Any) -> dict[str, Any]:
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise WhaleError("invalid_params", "body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise WhaleError("invalid_params", "body must be a JSON object")
        return body

    def _owner(self, request: Any) -> bool:
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return bool(token) and hmac.compare_digest(token, self.app.config.api_token)

    @staticmethod
    def _json(data: Any, status: int = 200) -> Any:
        from starlette.responses import JSONResponse

        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    def _fail(self, exc: WhaleError) -> Any:
        return self._json({"ok": False, "error": exc.to_dict()}, exc.http_status)

    async def _http_redeem(self, request: Any) -> Any:
        """Phone → core with an owner-issued code: ``{code, pubkey_hex, label}`` (no token needed)."""
        try:
            body = await self._body(request)
            view = await self.redeem(str(body.get("code") or ""), str(body.get("pubkey_hex") or ""),
                                     str(body.get("label") or ""))
        except WhaleError as exc:
            return self._fail(exc)
        out = {"device_id": view["id"], "name": view["name"], "scopes": view["scopes"], "ws_path": WS_PATH}
        return self._json({"ok": True, "result": out})

    # -- Link ticket（C 鉴权：设备线先验 → 签短期 Link 票；主 token 不上机） ------------------------
    #
    # POST /api/devices/link_ticket，仅签名头（§11 同构）：
    #   X-Whale-Device-Id（32-hex）/ X-Whale-Timestamp（Unix 秒，±300s）/
    #   X-Whale-Nonce（16..128，5min 防重放）/ X-Whale-Signature（Ed25519 对
    #   "device_id\nts\nnonce\nPOST\n/api/devices/link_ticket" 的 UTF-8 签名，128-hex）。
    # ticket 900s 有效、绑 device_id、可复用（重连）；撤销设备即刻失效（hello 拒绝 + 在线踢线）。

    def mint_link_ticket(self, device_id: str) -> dict[str, Any]:
        import secrets as pysecrets

        from ..kernel.ids import now_ms

        ticket = pysecrets.token_urlsafe(32)
        self.app.db.kv_set("link_ticket", ticket,
                           {"device_id": device_id, "expires_at": now_ms() + LINK_TICKET_TTL_S * 1000})
        return {"ticket": ticket, "expires_in": LINK_TICKET_TTL_S, "device_id": device_id}

    def validate_link_ticket(self, ticket: str) -> str:
        """ticket → device_id；无效/过期/设备撤销一律 unauthorized（fail-closed）。"""
        from ..kernel.ids import now_ms

        rec = self.app.db.kv_get("link_ticket", str(ticket or ""))
        if not isinstance(rec, dict) or int(rec.get("expires_at") or 0) < now_ms():
            raise WhaleError("unauthorized", "invalid or expired link ticket")
        device_id = str(rec.get("device_id") or "")
        dev = self.registry.get(device_id)
        if dev is None or dev["revoked"]:
            raise WhaleError("unauthorized", "device revoked or unknown")
        return device_id

    def _verify_device_headers(self, request: Any, path: str) -> str:
        """§11 设备签名头校验（fail-closed 401）。返回 device_id。"""
        import re
        import time

        from ..kernel.ids import now_ms
        from .gateway import verify_ed25519

        h = request.headers
        device_id = (h.get("x-whale-device-id") or "").strip().lower()
        ts_raw = (h.get("x-whale-timestamp") or "").strip()
        nonce = (h.get("x-whale-nonce") or "").strip()
        sig = (h.get("x-whale-signature") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", device_id):
            raise WhaleError("unauthorized", "bad device id")
        try:
            ts = int(ts_raw)
        except ValueError:
            raise WhaleError("unauthorized", "bad timestamp") from None
        if abs(time.time() - ts) > 300:
            raise WhaleError("unauthorized", "stale timestamp")
        if not (16 <= len(nonce) <= 128):
            raise WhaleError("unauthorized", "bad nonce")
        if not re.fullmatch(r"[0-9a-f]{128}", sig):
            raise WhaleError("unauthorized", "bad signature")
        rec = self.registry.get(device_id)
        if rec is None:
            raise WhaleError("unauthorized", "unknown device")
        if rec["revoked"]:
            raise WhaleError("unauthorized", "device revoked")
        nkey = f"{device_id}:{nonce}"
        if self.app.db.kv_get("lt_nonce", nkey) is not None:
            raise WhaleError("unauthorized", "nonce replay")
        message = f"{device_id}\n{ts_raw}\n{nonce}\n{request.method}\n{path}"
        if not verify_ed25519(rec["pubkey"], sig, message):
            raise WhaleError("unauthorized", "signature verification failed")
        self.app.db.kv_set("lt_nonce", nkey, now_ms())
        # 顺手清扫过期 nonce（5min 窗）
        for key, seen_at in self.app.db.kv_all("lt_nonce").items():
            try:
                if now_ms() - int(seen_at) > 300_000:
                    self.app.db.kv_delete("lt_nonce", key)
            except (TypeError, ValueError):
                self.app.db.kv_delete("lt_nonce", key)
        return device_id

    async def _http_link_ticket(self, request: Any) -> Any:
        try:
            device_id = self._verify_device_headers(request, "/api/devices/link_ticket")
            out = self.mint_link_ticket(device_id)
        except WhaleError as exc:
            return self._fail(exc)
        return self._json({"ok": True, "result": out})

    # -- RPC ----------------------------------------------------------------------------------------------
    def _register_rpc(self) -> None:
        m = self.app.rpc.method

        @m("devices.list", NoParams, "Paired devices with live status")
        async def devices_list(p: NoParams, c: Client | None) -> Any:
            return {"devices": self.list(), "capabilities": list(CAPABILITIES), "ws_path": WS_PATH}

        @m("devices.get", DeviceRef, "One device with its recent events and actions")
        async def devices_get(p: DeviceRef, c: Client | None) -> Any:
            rec = self._resolve(p.id)
            return {**self._view(rec), "events": self.ledger.events(rec["id"], 20),
                    "actions": self.ledger.actions(rec["id"], 20), "event_count": self.ledger.count_events(rec["id"])}

        @m("devices.pair_start", PairStart, "Issue a 6-digit pairing code to enter on the phone")
        async def devices_pair_start(p: PairStart, c: Client | None) -> Any:
            return self.pair_start(p.name, p.scopes, p.ttl_s)

        @m("devices.revoke", DeviceRef, "Revoke a device: closes its connection and removes its capabilities")
        async def devices_revoke(p: DeviceRef, c: Client | None) -> Any:
            return await self.revoke(p.id)

        @m("devices.rename", DeviceRename, "Rename a device")
        async def devices_rename(p: DeviceRename, c: Client | None) -> Any:
            return await self.rename(p.id, p.name)

        @m("devices.set_scopes", DeviceScopes, "Set the capabilities a device may be asked to perform")
        async def devices_set_scopes(p: DeviceScopes, c: Client | None) -> Any:
            return await self.set_scopes(p.id, p.scopes)

        @m("devices.action", DeviceAction, "Owner-initiated device action (recorded, idempotent)")
        async def devices_action(p: DeviceAction, c: Client | None) -> Any:
            args = check_arguments(p.capability, p.arguments) if p.capability in CATALOG else dict(p.arguments)
            view = await self.execute_action(p.id, p.capability, args, timeout_s=p.timeout_s,
                                             idempotency_key=p.idempotency_key, origin="owner")
            if view.get("status") == "succeeded" and view.get("output_ref") and p.capability in (
                    "device.observe", "device.shell", "device.file.read", "device.logcat", "device.file.list"):
                await self._attach_output(self._resolve(p.id)["id"], p.capability, view, ToolResult("ok"))
            return view

        @m("devices.query", DeviceQuery, "Read-only sensor query (screen.*, device.status, device.app.list …)")
        async def devices_query(p: DeviceQuery, c: Client | None) -> Any:
            return await self.query(p.id, p.operation, p.filters, max_items=p.max_items, timeout_s=p.timeout_s)

        @m("devices.events", DevicePage, "Recent experience events from a device")
        async def devices_events(p: DevicePage, c: Client | None) -> Any:
            rec = self._resolve(p.id)
            return {"events": self.ledger.events(rec["id"], p.limit)}

        @m("devices.set_role", DeviceRole, "agent = AI 专用手机（全部能力、不过滤）；personal = 主人日常手机（隐私过滤）")
        async def devices_set_role(p: DeviceRole, c: Client | None) -> Any:
            rec = self.registry.set_role(self._resolve(p.id)["id"], p.role)
            view = self._view(rec)
            await self.app.bus.emit("device.updated", {"device": view})
            return view

        @m("devices.privacy", DeviceRef, "Privacy settings, built-in rules and what has been filtered")
        async def devices_privacy(p: DeviceRef, c: Client | None) -> Any:
            rec = self._resolve(p.id)
            return {"role": self.role_of(rec), "settings": rec["privacy"], "builtin": privacy.describe(),
                    "filtered": self.ledger.drop_stats(rec["id"])}

        @m("devices.set_privacy", DevicePrivacy, "Excluded apps / domains and redaction for a personal phone")
        async def devices_set_privacy(p: DevicePrivacy, c: Client | None) -> Any:
            rec = self._resolve(p.id)
            patch = p.model_dump(exclude_none=True, exclude={"id"})
            rec = self.registry.set_privacy(rec["id"], patch)
            await self.app.bus.emit("device.updated", {"device": self._view(rec)})
            return {"settings": rec["privacy"]}

        @m("devices.digest_now", NoParams, "Turn pending phone experiences into memories now")
        async def devices_digest_now(p: NoParams, c: Client | None) -> Any:
            return await self.digest.run()

        @m("devices.search_events", PhoneSearch, "Search kept phone experiences across devices")
        async def devices_search_events(p: PhoneSearch, c: Client | None) -> Any:
            return {"events": self.digest.search(p.query, p.hours, p.limit)}

        @m("devices.actions", DevicePage, "Recent actions sent to a device with their receipts")
        async def devices_actions(p: DevicePage, c: Client | None) -> Any:
            rec = self._resolve(p.id)
            return {"actions": self.ledger.actions(rec["id"], p.limit)}

        @m("devices.notify", DeviceNotify, "Show a native notification on one phone")
        async def devices_notify(p: DeviceNotify, c: Client | None) -> Any:
            return await self.notify_device(p.id, p.title, p.body, {"category": p.category, "priority": p.priority})

        @m("devices.confirm", DeviceConfirm, "Ask the owner to confirm on the phone (signed)")
        async def devices_confirm(p: DeviceConfirm, c: Client | None) -> Any:
            bad = [s for s in p.scopes if s not in CAPABILITIES]
            if bad:
                raise WhaleError("invalid_params", "unknown scopes", details={"scopes": bad})
            return await self.confirm(p.id, p.scopes, channel=p.channel, action_fingerprint=p.action_fingerprint,
                                      timeout_s=p.timeout_s)


def _idem(raw: str) -> str:
    key = re.sub(r"[^0-9A-Za-z:_-]", "_", raw)[:128]
    return key if len(key) >= 8 else key + ":" + ulid()


def compact_observation(obs: Any, limit: int = 150) -> dict[str, Any]:
    """Observation payload → what a model needs (ids, labels, bounds, flags)."""
    if not isinstance(obs, dict):
        return {}
    out: dict[str, Any] = {k: obs[k] for k in ("observation_id", "foreground_package", "activity", "keyboard_shown",
                                               "locked", "screen_width", "screen_height", "screenshot_ref", "media")
                           if k in obs}
    elements = []
    for el in (obs.get("elements") or [])[:limit]:
        if not isinstance(el, dict):
            continue
        item = {k: el[k] for k in ("element_id", "text", "content_desc", "resource_id", "class_name", "bounds")
                if el.get(k) not in (None, "")}
        flags = [f for f in ("clickable", "editable", "scrollable", "long_clickable") if el.get(f) is True]
        if flags:
            item["flags"] = flags
        elements.append(item)
    out["elements"] = elements
    out["truncated"] = bool(obs.get("truncated")) or len(obs.get("elements") or []) > limit
    return out
