"""contracts/v2 服务端 WSS 端点 ``/api/v2/devices/ws``（embedded_wss 通道）。

协议事实源：contracts/v2/PROTOCOL.md（本仓镜像，whale-mobile 为 canonical）。
线程模型沿用 v1 网关：``websockets.sync.server`` 每连接一线程；离线判定由
后台 sweeper 线程做（内存状态，90s 基线 §1）；持久化全部走 MemoryStore kv。

与 v1 的差异（contracts/v2 §18）：

- ID 全面「前缀+ULID」（``devices/transports/base.py`` 的 new_id/new_ulid）；
- 回执状态机五终态 succeeded/failed/timeout/cancelled/unknown，received 是
  唯一非终态；终态 first-wins，落库不可翻案（§7.2）；
- 幂等语义（§7.1）：action_id=每次请求实例，idempotency_key=逻辑动作；
  重复键回已记录的真实状态，绝不重放（不再回 duplicate 错误）；
- sensor.event→experience.event、sensor.ack→event.ack（event_ids 批量≤200）；
- 新增 notification.*（§13，转 NotificationGateway）、run.trace.event /
  recording.state（§12，落库供录制对齐）、device.confirm.*（§14 设备确认）。

违例纪律（§4）：schema 违例回 error.invalid_message 不断线；未知 type 静默
忽略；未知 major 回 error.protocol_version 后关闭；撤销即踢（kick + 每帧复查）。

本类同时实现 ``notifications/providers/whale_wss.py`` 的 ``DeviceWssPort``
（send_notification_event / is_online，另加可选 send_notification_revoke）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import secrets
import threading
import time
from typing import Any, Mapping

from whale_core.devices.perception import (
    DevicePerceptionAdapter,
    DurableDevicePerceptionAdapter,
)
from whale_core.devices.registry import DeviceRegistry, now_ms
from whale_core.devices.schema import (
    PROTOCOL_VERSION,
    SUPPORTED_MAJOR,
    ContractValidator,
    parse_major,
)
from whale_core.devices.transports.base import new_id, new_ulid
from whale_core.memory.store import MemoryStore

log = logging.getLogger("whale.devices.embedded_wss")

WS_PATH = "/api/v2/devices/ws"
MAX_FRAME_BYTES = 256 * 1024  # §1：单帧 ≤ 256 KiB
REF_ID_FALLBACK = "0" * 26    # 合法 ULID 形（错误帧 ref_id 兜底）
DEFAULT_HEARTBEAT_INTERVAL = 30   # hello_ack 告知端侧的心跳节奏（秒）
DEFAULT_HEARTBEAT_TIMEOUT = 90    # §1：90s 无心跳判定离线（标离线，不踢线）
DEFAULT_CHALLENGE_TTL = 30        # 秒；schema 限定 expires_in ∈ [1,300]

#: action.receipt 终态（§7.2：received 是唯一非终态）
TERMINAL_RECEIPT_STATES = frozenset(
    {"succeeded", "failed", "timeout", "cancelled", "unknown"})

#: 通知分类默认值（event_consumer 提醒类下行）
NOTIFY_CAPABILITY = "phone.notification.post"


class GatewayError(ValueError):
    """下行通道失败（code 对齐 contracts/v2 §15 错误码 + not_found 管理面补充）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _timeout_ms(value: float, minimum: float, maximum: float) -> int:
    try:
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and minimum <= value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        raise GatewayError("invalid_message", f"timeout 须为 {minimum}..{maximum}s 内的有限数值")
    return int(value * 1000)


class ConnectionState:
    """单条设备 WS 连接的运行时状态（全部内存，重启即清）。"""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.device_id: str | None = None
        self.authed = False
        self.challenge: str | None = None
        self.challenge_deadline = 0.0
        self.session_id: str | None = None
        self.down_seq = 0
        self.expected_up_seq: int | None = None
        self.last_heartbeat = time.monotonic()
        self.offline_marked = False
        self.status = "connecting"
        # hello 声明（§5.2）：能力/特征/设备描述/应用版本
        self.declared_capabilities: tuple[str, ...] = ()
        self.accepted_capabilities: tuple[str, ...] = ()
        self.features: dict[str, bool] = {}
        self.device_info: dict[str, Any] = {}
        self.app_version = ""
        self.app_flavor = ""
        # 最近一次心跳带的模块状态摘要（§6 device.heartbeat.modules）
        self.modules: dict[str, str] = {}
        # 每连接发送锁：down_seq 分配与实际发送严格同序（PR#75 batch05 移植）
        self.send_lock = threading.Lock()


class _Pending:
    """跨线程请求-应答对，绑定发起请求的已认证设备身份与期望回包类型。

    只有同设备且同类型的回包才 resolve（PR#75 batch07 完整语义：
    sensor.result/action.receipt/confirm.response 之间不回包误匹配）。
    """

    __slots__ = ("event", "result", "device_id", "response_type", "connection", "error")

    def __init__(self, device_id: str | None = None,
                 response_type: str = "", connection: ConnectionState | None = None) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.device_id = device_id
        self.response_type = response_type
        self.connection = connection
        self.error: GatewayError | None = None


class EmbeddedWssGateway:
    """contracts/v2 设备控制长连网关。构造即监听（websockets 缺失时抛 ImportError）。"""

    kind = "embedded_wss"

    def __init__(
        self,
        registry: DeviceRegistry,
        store: MemoryStore,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        adapter: DevicePerceptionAdapter | None = None,
        notification_gateway: Any = None,  # notifications.gateway.NotificationGateway
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT,
        challenge_ttl: int = DEFAULT_CHALLENGE_TTL,
        log_event: Any = None,  # ApiContext.log_event（可选；只记设备号，不记正文）
    ) -> None:
        from websockets.sync.server import serve

        self.registry = registry
        self.store = store
        self.host = host
        self.adapter = adapter if adapter is not None else DurableDevicePerceptionAdapter(store)
        self.notification_gateway = notification_gateway
        if not (1 <= heartbeat_interval <= 3600):
            raise ValueError("heartbeat_interval 须在 1..3600（hello_ack schema）")
        if not (1 <= challenge_ttl <= 300):
            raise ValueError("challenge_ttl 须在 1..300（auth.challenge schema）")
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.challenge_ttl = challenge_ttl
        self._log_event = log_event
        self.contracts = ContractValidator()
        self.lock = threading.RLock()
        self.online: dict[str, ConnectionState] = {}
        self.pending: dict[str, _Pending] = {}
        # 设备确认（§14）：challenge_id → 待决确认
        self.pending_confirm: dict[str, _Pending] = {}
        self.stopping = threading.Event()
        logger = logging.getLogger("whale.devices.transport")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.server = serve(
            self._handle, host, port,
            max_size=MAX_FRAME_BYTES,
            ping_interval=None,  # 心跳语义由协议层 device.heartbeat 承担（§1）
            logger=logger,
        )
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="whale-devices-v2")
        self.thread.start()
        self._sweeper = threading.Thread(
            target=self._sweep_loop, daemon=True, name="whale-devices-v2-sweep")
        self._sweeper.start()

    # ------------------------------------------------------------------
    # 帧构造 / 校验（收发双向过 contracts v2 schema）
    # ------------------------------------------------------------------

    def build_message(self, msg_type: str, dir_: str, payload: dict, seq: int) -> dict:
        msg = {"v": PROTOCOL_VERSION, "id": new_ulid(), "type": msg_type,
               "ts": now_ms(), "seq": seq, "dir": dir_, "payload": payload}
        err = self.contracts.validate(msg_type, msg)
        if err:  # 服务端自身构造的帧不应失败；失败即实现 bug
            raise ValueError(f"出站帧未通过 {msg_type} schema: {err}")
        return msg

    def send_msg(self, st: ConnectionState, msg_type: str, payload: dict) -> dict:
        # 每连接串行：锁内分配 seq 并发送，保证序号与线上顺序一致
        with st.send_lock:
            st.down_seq += 1
            msg = self.build_message(msg_type, "downlink", payload, st.down_seq)
            st.ws.send(json.dumps(msg, ensure_ascii=False))
            return msg

    def send_error(self, st: ConnectionState, ref_id: Any, code: str, message: str):
        if not (isinstance(ref_id, str) and len(ref_id) == 26):
            ref_id = REF_ID_FALLBACK
        try:
            self.send_msg(st, "error",
                          {"ref_id": ref_id, "code": code, "message": message[:500]})
        except Exception:  # noqa: BLE001 - 对端可能已断开
            log.debug("发送 error 帧失败", exc_info=True)

    def _audit(self, action: str, *, outcome: str = "ok", reason: str = "", **meta):
        self.registry.audit(action, outcome=outcome, reason=reason, **meta)

    # ------------------------------------------------------------------
    # 连接主流程
    # ------------------------------------------------------------------

    def _handle(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        if getattr(getattr(ws, "request", None), "path", WS_PATH) != WS_PATH:
            ws.close(1008, f"only {WS_PATH} is served here")
            return
        st = ConnectionState(ws)
        # 1. 首帧下发挑战（§4 auth.challenge：64-hex + expires_in 秒）
        st.challenge = secrets.token_hex(32)
        st.challenge_deadline = time.monotonic() + self.challenge_ttl
        try:
            self.send_msg(st, "auth.challenge",
                          {"challenge": st.challenge, "expires_in": self.challenge_ttl})
            raw = ws.recv(timeout=self.challenge_ttl + 5)
        except (TimeoutError, ConnectionClosed):
            self._safe_close(ws)
            return
        except Exception:  # noqa: BLE001
            self._safe_close(ws)
            return
        # 2. 认证后主循环（长连，无会话超时；心跳超时只标离线不踢线）
        try:
            # hello 已登记会话后发送 ack 也可能断线，必须走同一清理路径。
            if not self._handle_hello(st, raw):
                return
            for raw in ws:
                if self.stopping.is_set():
                    break
                if not self._handle_frame(st, raw):
                    break
        except (ConnectionClosed, OSError):
            pass
        finally:
            self._finish_session(st)
            self._safe_close(ws)

    def _finish_session(self, st: ConnectionState) -> None:
        """Release only waiters belonging to this connection, never its replacement."""
        with self.lock:
            removed = bool(st.device_id and self.online.get(st.device_id) is st)
            if removed:
                del self.online[st.device_id]
            for pending in (self.pending, self.pending_confirm):
                for key, waiter in list(pending.items()):
                    if waiter.connection is not st:
                        continue
                    pending.pop(key, None)
                    if waiter.response_type == "action.receipt":
                        # Disconnect says nothing about effects. Keep the durable record for late receipts.
                        waiter.result = {"action_id": key, "status": "unknown",
                                         "provider_ref": "core:connection-lost"}
                    elif waiter.response_type == "device.confirm.response":
                        waiter.result = {"approved": False, "reason": "unavailable", "signature": None}
                    else:
                        waiter.error = GatewayError("unavailable", "设备连接已断开")
                    waiter.event.set()
        if removed:
            self._emit("device:offline", st.device_id)

    def _await_response(self, st: ConnectionState, key: str, msg_type: str,
                        payload: dict, timeout_s: float, response_type: str, *,
                        confirm: bool = False) -> dict:
        from websockets.exceptions import ConnectionClosed
        pending = self.pending_confirm if confirm else self.pending
        waiter = _Pending(st.device_id, response_type, st)
        with self.lock:
            if (self.stopping.is_set() or self.online.get(st.device_id) is not st
                    or st.offline_marked):
                raise GatewayError("unavailable", "设备会话已变化或服务正在关闭")
            if key in pending:
                raise GatewayError("invalid_message", "请求 ID 已有等待项")
            pending[key] = waiter
        try:
            try:
                self.send_msg(st, msg_type, payload)
            except (ConnectionClosed, OSError):
                self._finish_session(st)
                self._safe_close(st.ws)
            if not waiter.event.wait(timeout_s + 5):
                if confirm:
                    return {"approved": False, "reason": "timeout", "signature": None}
                raise GatewayError("expired", f"等待 {response_type} 超时（{timeout_s}s）")
            if waiter.error is not None:
                raise waiter.error
            return dict(waiter.result or ({"approved": False, "reason": "empty"} if confirm else {}))
        finally:
            with self.lock:
                if pending.get(key) is waiter:
                    pending.pop(key, None)

    @staticmethod
    def _safe_close(ws, code: int = 1008) -> None:
        try:
            ws.close(code)
        except Exception:  # noqa: BLE001
            pass

    def _handle_hello(self, st: ConnectionState, raw: Any) -> bool:
        """验签握手（§4/§5）。失败时发送 error 并返回 False（调用方负责关闭）。"""
        ref_id = REF_ID_FALLBACK

        def fail(code: str, message: str) -> bool:
            self.send_error(st, ref_id, code, message)
            log.warning("设备握手失败(%s): %s", code, message)
            return False

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return fail("auth_failed", "first frame is not valid JSON")
        if not isinstance(msg, dict):
            return fail("auth_failed", "first frame is not an object")
        if isinstance(msg.get("id"), str):
            ref_id = msg["id"]
        if parse_major(msg.get("v")) not in (None, SUPPORTED_MAJOR):
            return fail("protocol_version",
                        f"unsupported major version: {msg.get('v')}")
        if msg.get("type") != "device.hello":
            return fail("auth_failed", "first frame must be device.hello")
        # hello.payload.protocol_version 独立声明设备支持版本（§6）；major 检查
        # 先于 schema（schema 的 pattern 只认 2.x，会把它淹成 auth_failed）
        p_raw = msg.get("payload")
        if isinstance(p_raw, dict):
            pv_major = parse_major(p_raw.get("protocol_version"))
            if pv_major is not None and pv_major != SUPPORTED_MAJOR:
                return fail("protocol_version",
                            f"unsupported protocol_version: "
                            f"{p_raw.get('protocol_version')}")
        err = self.contracts.validate("device.hello", msg)
        if err:
            return fail("auth_failed", f"device.hello schema invalid: {err}")
        p = msg["payload"]
        if time.monotonic() > st.challenge_deadline:
            return fail("auth_failed", "challenge expired")
        if p["challenge"] != st.challenge:
            return fail("auth_failed", "challenge mismatch")
        meta = self.registry.get(p["device_id"])
        if meta is None:
            self._audit("device.hello", outcome="denied", reason="not_paired",
                        device_id=p["device_id"])
            return fail("auth_failed", "device not paired")
        if meta.get("status") == "revoked":
            self._audit("device.hello", outcome="denied", reason="revoked",
                        device_id=p["device_id"])
            return fail("device_revoked", "device has been revoked")
        if meta.get("status") != "active":
            self._audit("device.hello", outcome="denied", reason="not_approved",
                        device_id=p["device_id"])
            return fail("auth_failed", "device pairing not approved")
        if not self._verify_signature(meta["pubkey_hex"], p["signature"],
                                      st.challenge or ""):
            self._audit("device.hello", outcome="denied", reason="bad_signature",
                        device_id=p["device_id"])
            return fail("auth_failed", "signature verification failed")
        # 通过 → hello_ack（按设备注册 scopes 收窄能力，§5.2 hello_ack）
        scopes = set(meta.get("scopes") or ())
        accepted = tuple(c for c in p["capabilities"] if c in scopes)
        st.device_id = p["device_id"]
        st.authed = True
        st.session_id = new_ulid()
        st.status = "online"
        st.last_heartbeat = time.monotonic()
        st.expected_up_seq = msg["seq"] + 1
        st.declared_capabilities = tuple(p["capabilities"])
        st.accepted_capabilities = accepted
        st.features = dict(p["features"])
        st.device_info = dict(p["device"])
        st.app_version = str(p["app_version"])
        st.app_flavor = str(p["app_flavor"])
        with self.lock:
            old = self.online.get(st.device_id)
            self.online[st.device_id] = st
        if old is not None:  # 同设备新连顶替旧连接
            self._safe_close(old.ws, 1000)
        self._audit("device.hello", device_id=st.device_id)
        self._emit("device:online", st.device_id)
        self.send_msg(st, "device.hello_ack", {
            "session_id": st.session_id,
            "server_time": now_ms(),
            "heartbeat_interval": self.heartbeat_interval,
            "accepted_capabilities": list(accepted),
        })
        return True

    @staticmethod
    def _verify_signature(pubkey_hex: str, signature_hex: str, challenge: str) -> bool:
        """§4：签名输入 = challenge 十六进制字符串的 ASCII（UTF-8）字节。"""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
                bytes.fromhex(signature_hex), challenge.encode("ascii"))
            return True
        except (InvalidSignature, ValueError):
            return False

    def _handle_frame(self, st: ConnectionState, raw: Any) -> bool:
        """认证后的常规帧。返回 False 表示应关闭连接。"""
        ref_id = REF_ID_FALLBACK
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.send_error(st, ref_id, "invalid_message", "frame is not valid JSON")
            return True
        if not isinstance(msg, dict):
            self.send_error(st, ref_id, "invalid_message", "frame is not an object")
            return True
        if isinstance(msg.get("id"), str):
            ref_id = msg["id"]
        major = parse_major(msg.get("v"))
        if major is not None and major != SUPPORTED_MAJOR:
            # §2：未知 major 拒绝并回 error.protocol_version 后关闭
            self.send_error(st, ref_id, "protocol_version",
                            f"unsupported major version: {msg.get('v')}")
            return False
        # §4 撤销即时生效：已认证设备每帧复查状态，revoked 立即断线
        if st.device_id and self.registry.status_of(st.device_id) == "revoked":
            self.send_error(st, ref_id, "device_revoked", "device has been revoked")
            return False
        msg_type = msg.get("type")
        if not isinstance(msg_type, str) or msg_type not in self.contracts.validators:
            # §4：未知消息类型静默忽略（前向兼容）
            log.warning("忽略未知消息类型: %r", msg_type)
            return True
        err = self.contracts.validate(msg_type, msg)
        if err:
            self.send_error(st, ref_id, "invalid_message",
                            f"schema validation failed: {err}"[:500])
            return True
        if isinstance(msg.get("seq"), int):
            if st.expected_up_seq is not None and msg["seq"] != st.expected_up_seq:
                log.warning("设备 %s 上行 seq 跳变: 期望 %s 实到 %s（仅记录不拒绝）",
                            st.device_id, st.expected_up_seq, msg["seq"])
            st.expected_up_seq = msg["seq"] + 1
        handler = getattr(self, f"_on_{msg_type.replace('.', '_')}", None)
        if handler is None:
            log.warning("类型 %s 无处理逻辑，忽略", msg_type)
            return True
        handler(st, msg)
        return True

    # ------------------------------------------------------------------
    # 上行消息处理
    # ------------------------------------------------------------------

    def _on_device_heartbeat(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        if p["device_id"] != st.device_id:
            log.warning("心跳 device_id 与连接身份不符（%s != %s），忽略",
                        p["device_id"], st.device_id)
            return
        st.last_heartbeat = time.monotonic()
        st.status = p["status"]
        modules = p.get("modules")
        if isinstance(modules, dict):
            st.modules = {str(k): str(v) for k, v in modules.items()}
        if st.offline_marked:
            st.offline_marked = False
            self._emit("device:online", st.device_id)
            log.info("设备 %s 心跳恢复，重新标记在线", st.device_id)

    def _on_experience_event(self, st: ConnectionState, msg: dict) -> None:
        """§9：event_id 即幂等键；新收与去重命中都回 event.ack（§16 闭环）。"""
        p = msg["payload"]
        key = f"device:event:{st.device_id}:{p['event_id']}"
        with self.lock:
            duplicate = self.store.get(key) is not None
            if not duplicate:
                # event_id 幂等键持久化；事件本体存 kv，供排障与重放
                self.store.put(key, {
                    "device_id": st.device_id,
                    "event_id": p["event_id"],
                    "kind": p["kind"],
                    "occurred_at": p["occurred_at"],
                    "source": p["source"],
                    "privacy_class": p["privacy_class"],
                    "summary": p["summary"],
                    "evidence_ref": p.get("evidence_ref"),
                    "task_id": p.get("task_id"),
                    "run_id": p.get("run_id"),
                    "extras": p.get("extras"),
                    "schema_version": p["schema_version"],
                    "received_at": now_ms(),
                })
        if duplicate:
            log.info("experience.event 重复 event_id=%s（device=%s），幂等去重跳过",
                     p["event_id"], st.device_id)
        else:
            self.adapter.offer(device_id=st.device_id or "", payload=p)
            self._emit("device:experience_event",
                       f"{st.device_id}:{p['kind']}:{p['event_id']}")
            # 脱敏日志：不记 summary 全文，只记长度
            log.info("experience.event device=%s kind=%s event_id=%s "
                     "privacy_class=%s summary_len=%d",
                     st.device_id, p["kind"], p["event_id"],
                     p["privacy_class"], len(p["summary"]))
        self.send_msg(st, "event.ack", {"event_ids": [p["event_id"]]})

    def _on_sensor_result(self, st: ConnectionState, msg: dict) -> None:
        request_id = msg["payload"]["request_id"]
        with self.lock:
            pend = self.pending.get(request_id)
            if pend is not None and (pend.device_id != st.device_id
                                     or pend.response_type != "sensor.result"):
                mismatch = True
            else:
                mismatch = False
                if pend is not None:
                    self.pending.pop(request_id, None)
        if mismatch:
            log.warning(
                "sensor.result 设备身份不匹配 request_id=%s expected=%s actual=%s，忽略",
                request_id, pend.device_id, st.device_id)
            return
        if pend is not None:
            pend.result = msg["payload"]
            pend.event.set()
        else:
            log.warning("收到无对应请求的 sensor.result request_id=%s，忽略",
                        request_id)

    def _on_action_receipt(self, st: ConnectionState, msg: dict) -> None:
        """§7.2：received 中间态只落库；五终态才 resolve 等待方，first-wins。"""
        p = msg["payload"]
        action_id, status = p["action_id"], p.get("status")
        # 未知 action_id 的回执直接忽略（防认证设备灌入任意回执记录）
        lookup = self.store.get(f"device:action-id:{st.device_id}:{action_id}")
        if not isinstance(lookup, str):
            log.warning("unmapped action receipt for device=%s action=%s",
                        st.device_id, action_id)
            return
        with self.lock:
            key = f"device:receipts:{st.device_id}:{action_id}"
            receipts = self.store.get(key) or []
            if isinstance(receipts, list):
                receipts = (receipts + [{"status": str(status), "ts": now_ms()}])[-32:]
                self.store.put(key, receipts)
        # 终态对原幂等键记录落库：迟到回执/进程重启均可对账，绝不重放
        action_key = f"device:action:{st.device_id}:{lookup}"
        with self.lock:
            record = self.store.get(action_key)
            if isinstance(record, dict) and record.get("action_id") == action_id:
                # First terminal wins：已落终态不被迟到/冲突终态翻案
                estimated_unknown = (
                    record.get("state") == "unknown"
                    and record.get("outcome_source") == "retention_timeout")
                if (record.get("state") not in TERMINAL_RECEIPT_STATES
                        or (estimated_unknown
                            and status in TERMINAL_RECEIPT_STATES)):
                    record.update(state=status, updated_at=now_ms())
                    record.pop("outcome_source", None)
                    if status in TERMINAL_RECEIPT_STATES:
                        record["receipt"] = {
                            k: p[k] for k in (
                                "status", "task_id", "result", "error_code",
                                "provider_ref", "started_at", "completed_at")
                            if k in p
                        }
                    self.store.put(action_key, record)
        if status in TERMINAL_RECEIPT_STATES:
            with self.lock:
                pend = self.pending.get(action_id)
                if pend is not None and (pend.device_id != st.device_id
                                         or pend.response_type != "action.receipt"):
                    mismatch = True
                else:
                    mismatch = False
                    if pend is not None:
                        self.pending.pop(action_id, None)
            if mismatch:
                log.warning(
                    "action.receipt 设备身份不匹配 action_id=%s expected=%s actual=%s，忽略等待方",
                    action_id, pend.device_id, st.device_id)
            elif pend is not None:
                pend.result = p
                pend.event.set()
            self._emit("device:action_receipt", f"{st.device_id}:{action_id}:{status}")
        else:
            log.info("action %s 中间态回执 %s（device=%s）", action_id, status,
                     st.device_id)

    # ---- 通知子协议（§13）：转 NotificationGateway -------------------------

    def _on_notification_receipt(self, st: ConnectionState, msg: dict) -> None:
        gw = self.notification_gateway
        if gw is None:
            log.warning("notification.receipt 到达但未装配 NotificationGateway，忽略")
            return
        try:
            gw.handle_receipt(msg["payload"])
        except Exception:  # noqa: BLE001 - 回执处理故障不影响连接主路径
            log.warning("notification.receipt 处理失败", exc_info=True)

    def _on_notification_action(self, st: ConnectionState, msg: dict) -> None:
        gw = self.notification_gateway
        if gw is None:
            log.warning("notification.action 到达但未装配 NotificationGateway，忽略")
            return
        try:
            gw.handle_action(msg["payload"])
        except Exception:  # noqa: BLE001
            log.warning("notification.action 处理失败", exc_info=True)

    def _on_notification_sync(self, st: ConnectionState, msg: dict) -> None:
        gw = self.notification_gateway
        if gw is None:
            log.warning("notification.sync 到达但未装配 NotificationGateway，忽略")
            return
        p = msg["payload"]
        try:
            # handle_sync 是 async（router.dispatch）；本线程是同步 WS 线程
            sent = asyncio.run(gw.handle_sync(
                st.device_id or p["device_id"], p.get("last_event_id")))
            log.info("notification.sync device=%s 补传 %d 条", st.device_id, sent)
        except Exception:  # noqa: BLE001
            log.warning("notification.sync 处理失败", exc_info=True)

    # ---- 执行轨迹 / 录制（§12）：落库供 run 存储与录制对齐 -------------------

    def _on_run_trace_event(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        key = (f"device:run-trace:{st.device_id}:{p['run_id']}:"
               f"{int(p['seq']):08d}")
        try:
            self.store.put(key, {
                "device_id": st.device_id, "run_id": p["run_id"],
                "seq": int(p["seq"]), "timestamp_ms": p["timestamp_ms"],
                "kind": p["kind"], "payload": p, "received_at": now_ms(),
            })
        except Exception:  # noqa: BLE001
            log.warning("run.trace.event 落库失败", exc_info=True)

    def _on_recording_state(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        key = f"device:recording-state:{st.device_id}:{p['run_id']}"
        try:
            self.store.put(key, {
                "device_id": st.device_id, "run_id": p["run_id"],
                "state": p["state"], "file_ref": p.get("file_ref"),
                "duration_ms": p.get("duration_ms"),
                "error_code": p.get("error_code"), "received_at": now_ms(),
            })
        except Exception:  # noqa: BLE001
            log.warning("recording.state 落库失败", exc_info=True)

    # ---- 设备确认（§14） -------------------------------------------------

    def send_confirm_request(self, device_id: str, payload: dict,
                             timeout_s: float = 130.0) -> dict:
        """下发 device.confirm.request 并等待 device.confirm.response。

        返回 {"approved": bool, "reason": str|None, "signature": str|None}。
        超时/断线 → approved=False reason=timeout/unavailable（绝不默认批准）。
        """
        st = self._online_or_raise(device_id)
        _timeout_ms(timeout_s, 0.001, threading.TIMEOUT_MAX - 5)
        self._preflight("device.confirm.request", payload)
        if payload["device_id"] != device_id:
            raise GatewayError("invalid_message", "确认请求的 device_id 与目标设备不一致")
        challenge_id = payload["challenge_id"]
        return self._await_response(st, challenge_id, "device.confirm.request", payload,
                                    timeout_s, "device.confirm.response", confirm=True)

    def _on_device_confirm_response(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        challenge_id = str(p["challenge_id"])
        with self.lock:
            pend = self.pending_confirm.get(challenge_id)
            if pend is not None and pend.device_id != st.device_id:
                mismatch = True
            else:
                mismatch = False
                if pend is not None:
                    self.pending_confirm.pop(challenge_id, None)
        if mismatch:
            log.warning(
                "confirm.response 设备身份不匹配 challenge=%s expected=%s actual=%s，忽略",
                challenge_id, pend.device_id, st.device_id)
            return
        if pend is None:
            log.warning("收到无对应挑战的 confirm.response challenge=%s，忽略",
                        challenge_id)
            return
        # 条件必填（§6，schema 子集表达不了）：approved=true 时 signature 必填
        if p.get("approved") and not p.get("signature"):
            log.warning("confirm.response approved=true 但缺 signature，按拒绝处理")
        pend.result = {
            "approved": bool(p.get("approved")) and bool(p.get("signature")),
            "reason": p.get("reason"),
            "signature": p.get("signature"),
        }
        pend.event.set()

    def _on_error(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        log.warning("设备 %s 回报 error ref_id=%s code=%s message=%s",
                    st.device_id, p["ref_id"], p["code"], p["message"])

    #: 终态/未知态动作记录的保留窗口（默认 7 天）——保留期内迟到回执可核对、
    #: 重复幂等键可命中查重；过期才清理。
    ACTION_RETENTION_MS = 7 * 24 * 3600 * 1000
    #: 非终态记录过 expires_at 后的宽限期（默认 24h，§16「pending 超 24h 标
    #: unknown」）：有效重试窗口内幂等键绝不提前释放。
    ACTION_PENDING_GRACE_MS = 24 * 3600 * 1000

    def sweep_action_retention(self, *, now: int | None = None,
                               max_deletes: int = 500) -> dict:
        """有界清理设备动作回执/幂等记录/反向索引。

        - 终态且过保留窗口 → 删除动作记录 + 反向索引 + 回执流水；
        - 非终态且过 expires_at + 24h 宽限 → 落 ``unknown`` 过期标记（§16），
          保留窗口内仍可由 action_status 核对，不删除；
        - 仍在有效重试窗口内的记录一律不动（幂等键不提前释放）；
        - 孤儿回执流水直接清；单趟删除量封顶 ``max_deletes``。
        """
        now = now if now is not None else now_ms()
        scanned = deleted = marked = 0
        for key in list(self.store.keys()):
            if not key.startswith("device:action:"):
                continue
            scanned += 1
            record = self.store.get(key)
            if not isinstance(record, dict):
                continue
            state = str(record.get("state") or "")
            created = int(record.get("created_at") or 0)
            expires = int(record.get("expires_at") or 0)
            device_id = str(record.get("device_id") or "")
            action_id = str(record.get("action_id") or "")
            if state in TERMINAL_RECEIPT_STATES:
                if created + self.ACTION_RETENTION_MS >= now:
                    continue  # 保留窗口内：可核对、可被查重
                if deleted >= max_deletes:
                    continue
                self.store.delete(key)
                if device_id and action_id:
                    self.store.delete(f"device:action-id:{device_id}:{action_id}")
                    self.store.delete(f"device:receipts:{device_id}:{action_id}")
                deleted += 1
            elif now > expires + self.ACTION_PENDING_GRACE_MS:
                # 过宽限的非终态：落 unknown 过期标记（下轮按终态清理）。
                # 这是清理任务的**推定**而非设备确认——打 outcome_source 标记，
                # 迟到的真实终态回执仍可补齐（PR#75 batch08 移植）。
                record["state"] = "unknown"
                record["outcome_source"] = "retention_timeout"
                record["updated_at"] = now
                self.store.put(key, record)
                marked += 1
        # 孤儿回执流水（动作记录已不在）一并清，同受删除上限约束
        for key in list(self.store.keys()):
            if deleted >= max_deletes:
                break
            if not key.startswith("device:receipts:"):
                continue
            parts = key.split(":", 3)
            if len(parts) < 4:
                continue
            device_id, action_id = parts[2], parts[3]
            if self.store.get(f"device:action-id:{device_id}:{action_id}") is None:
                self.store.delete(key)
                deleted += 1
        return {"scanned": scanned, "deleted": deleted, "marked_unknown": marked}

    # ------------------------------------------------------------------
    # 心跳离线扫描（内存状态即可，§1）
    # ------------------------------------------------------------------

    def _sweep_loop(self) -> None:
        interval = max(0.2, min(self.heartbeat_timeout / 3, 10.0))
        last_retention_sweep = 0.0
        while not self.stopping.wait(interval):
            now = time.monotonic()
            with self.lock:
                snapshot = list(self.online.items())
            for device_id, st in snapshot:
                if not st.offline_marked \
                        and now - st.last_heartbeat > self.heartbeat_timeout:
                    st.offline_marked = True
                    st.status = "offline"
                    self._emit("device:heartbeat_timeout", device_id)
                    log.warning("设备 %s 超过 %ss 无心跳，标记离线",
                                device_id, self.heartbeat_timeout)
            # 动作回执保留清理搭既有 sweep 线程（不另建后台系统），
            # 每小时一趟、每趟删除量封顶。
            if now - last_retention_sweep >= 3600:
                last_retention_sweep = now
                try:
                    stats = self.sweep_action_retention()
                    if stats.get("deleted") or stats.get("marked_unknown"):
                        log.info("设备动作保留清理：%s", stats)
                except Exception:
                    log.warning("设备动作保留清理失败（下轮重试）", exc_info=True)

    # ------------------------------------------------------------------
    # 下行通道（管理 API / transport / NotificationGateway 线程调用）
    # ------------------------------------------------------------------

    def _emit(self, kind: str, detail: str) -> None:
        if self._log_event is not None:
            try:
                self._log_event(kind, detail)
            except Exception:  # noqa: BLE001 - 事件总线故障不影响协议路径
                log.debug("log_event 失败", exc_info=True)

    def online_state(self, device_id: str) -> ConnectionState | None:
        with self.lock:
            return self.online.get(device_id)

    def is_online(self, device_id: str) -> bool:
        st = self.online_state(device_id)
        return st is not None and not st.offline_marked

    def kick(self, device_id: str) -> bool:
        """吊销即时踢线（§4）。返回是否有在线连接被关闭。"""
        st = self.online_state(device_id)
        if st is None:
            return False
        try:
            self.send_error(st, REF_ID_FALLBACK, "device_revoked",
                            "device has been revoked")
        finally:
            self._finish_session(st)
            self._safe_close(st.ws)
        return True

    def drop(self, device_id: str) -> bool:
        """主动断开当前连接（非撤销，不发 error 帧；端侧会按退避重连）。"""
        st = self.online_state(device_id)
        if st is None:
            return False
        self._finish_session(st)
        self._safe_close(st.ws, 1000)
        return True

    def _online_or_raise(self, device_id: str) -> ConnectionState:
        st = self.online_state(device_id)
        if st is None or st.offline_marked:
            raise GatewayError("unavailable", f"device {device_id} 不在线")
        return st

    def _check_scope(self, device_id: str, capability: str) -> None:
        meta = self.registry.get(device_id)
        if not meta or meta.get("status") != "active":
            raise GatewayError("scope_denied", "设备未激活或已撤销")
        if capability not in self.registry.scopes_of(device_id):
            raise GatewayError("scope_denied", f"设备未授予 {capability}")

    def capabilities_of(self, device_id: str) -> tuple[str, ...]:
        """该设备经 hello 声明且被注册 scope 接受的在线能力（离线为空）。"""
        st = self.online_state(device_id)
        if st is None or st.offline_marked:
            return ()
        scopes = set(self.registry.scopes_of(device_id))
        return tuple(c for c in st.accepted_capabilities if c in scopes)

    def send_query(self, device_id: str, *, operation: str,
                   filters: dict | None = None, max_items: int = 10,
                   timeout_s: float = 15.0) -> dict:
        """下发 sensor.query 并在 deadline 内等待 sensor.result（§5.3/§6）。"""
        st = self._online_or_raise(device_id)
        self._check_scope(device_id, operation)
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 100:
            raise GatewayError("invalid_message", "max_items 须在 1..100")
        if filters is not None and not isinstance(filters, dict):
            raise GatewayError("invalid_message", "filters 须为对象")
        maximum = min((4102444800000 - now_ms()) / 1000, threading.TIMEOUT_MAX - 5)
        timeout_ms = _timeout_ms(timeout_s, 0.001, maximum)
        request_id = new_ulid()
        payload = {
            "request_id": request_id,
            "operation": operation,
            "filters": dict(filters or {}),
            "max_items": int(max_items),
            "deadline": now_ms() + timeout_ms,
        }
        self._preflight("sensor.query", payload)
        return self._await_response(st, request_id, "sensor.query", payload, timeout_s, "sensor.result")

    def send_action(self, device_id: str, *, capability: str,
                    arguments: dict | None = None,
                    idempotency_key: str | None = None,
                    authorized_scope: list[str] | None = None,
                    approval_ref: str | None = None,
                    correlation_id: str | None = None,
                    task_id: str | None = None,
                    run_id: str | None = None,
                    observation_id: str | None = None,
                    timeout_s: float = 15.0) -> dict:
        """下发 action.request；幂等键查重（§7.1），等待终态回执。

        重复幂等键：回已记录的 action_id 真实状态（``duplicate=True`` 标记），
        绝不重放（§7.1）。
        """
        self._check_scope(device_id, capability)
        idem_key = f"agent:{new_ulid()}" if idempotency_key is None else idempotency_key
        if not isinstance(idem_key, str) or not (8 <= len(idem_key) <= 128):
            raise GatewayError("invalid_message", "idempotency_key 长度须为 8..128")
        idem_store_key = f"device:action:{device_id}:{idem_key}"
        existing = self.store.get(idem_store_key)
        if isinstance(existing, dict):
            # §7.1：重复键回已记录状态，绝不重放；过有效期的非终态按 unknown 报
            result = self.action_status(device_id, idem_key)
            result["duplicate"] = True
            return result
        st = self._online_or_raise(device_id)
        action_id = new_id("act")
        timeout_ms = _timeout_ms(timeout_s, 0.1, 600)
        if arguments is not None and not isinstance(arguments, dict):
            raise GatewayError("invalid_message", "arguments 须为对象")

        def reserve(tx):
            if tx.get(idem_store_key) is not None:
                return False
            tx.put(idem_store_key, {
                "device_id": device_id, "idempotency_key": idem_key,
                "action_id": action_id, "capability": capability,
                "created_at": now_ms(), "expires_at": now_ms() + timeout_ms,
                "state": "pending",
            })
            tx.put(f"device:action-id:{device_id}:{action_id}", idem_key)
            return True

        payload: dict[str, Any] = {
            "action_id": action_id,
            "capability": capability,
            "arguments": dict(arguments or {}),
            "timeout_ms": timeout_ms,
            "expires_at": now_ms() + timeout_ms,
            "idempotency_key": idem_key,
            "authorized_scope": list(authorized_scope or [capability]),
        }
        if task_id:
            payload["task_id"] = task_id
        if run_id:
            payload["run_id"] = run_id
        if observation_id:
            payload["observation_id"] = observation_id
        if approval_ref:
            payload["approval_ref"] = approval_ref
        if correlation_id:
            payload["correlation_id"] = correlation_id
        # 先组装完整帧做 schema 预检（非法动作不占幂等键；PR#75 batch05）
        self._preflight("action.request", payload)
        if not self.store.transact(reserve):
            # 并发下另一线程先占了键：同上，回已记录状态
            result = self.action_status(device_id, idem_key)
            result["duplicate"] = True
            return result
        return self._await_response(st, action_id, "action.request", payload, timeout_s, "action.receipt")

    def _preflight(self, msg_type: str, payload: dict) -> None:
        try:
            self.build_message(msg_type, "downlink", payload, 0)
        except ValueError as exc:
            raise GatewayError("invalid_message", str(exc)) from exc

    def action_status(self, device_id: str, idempotency_key: str) -> dict:
        """只读对账（§7.1/§16）；过期的非终态记录按 unknown 报，绝不重发。"""
        if not isinstance(idempotency_key, str) or not (8 <= len(idempotency_key) <= 128):
            raise GatewayError("invalid_message", "invalid idempotency_key")
        record = self.store.get(f"device:action:{device_id}:{idempotency_key}")
        if not isinstance(record, dict):
            raise GatewayError("not_found", "unknown action idempotency key")
        result = copy.deepcopy(record)
        if (result.get("state") not in TERMINAL_RECEIPT_STATES
                and now_ms() > int(result.get("expires_at") or 0)):
            result["state"] = "unknown"
            result["outcome_source"] = "expires_at"
        return result

    # ---- 通知下行（DeviceWssPort 实现，§13.4） -----------------------------

    def _send_notification(self, device_id: str, msg_type: str,
                           payload: Mapping[str, Any]) -> None:
        st = self._online_or_raise(device_id)
        self.send_msg(st, msg_type, dict(payload))
        self._emit("device:notification", f"{device_id}:{msg_type}")

    async def send_notification_event(self, device_id: str, payload: dict) -> None:
        """DeviceWssPort：向在线设备写 notification.event payload（§13.1）。"""
        await asyncio.to_thread(self._send_notification, device_id, "notification.event", payload)

    async def send_notification_revoke(self, device_id: str, payload: dict) -> None:
        """whale_wss 可选撤回口（§13.3 notification.revoke）。"""
        await asyncio.to_thread(self._send_notification, device_id, "notification.revoke", payload)

    # ---- 录制控制（§12.2） -------------------------------------------------

    def send_recording_control(self, device_id: str, *, run_id: str, op: str,
                               options: dict | None = None) -> None:
        """下发 recording.control（fire-and-forget；状态经 recording.state 回报）。"""
        st = self._online_or_raise(device_id)
        payload: dict[str, Any] = {"run_id": run_id, "op": op}
        if options:
            payload["options"] = dict(options)
        self.send_msg(st, "recording.control", payload)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.stopping.set()
        with self.lock:
            sessions = list(self.online.values())
        for st in sessions:
            self._finish_session(st)
            self._safe_close(st.ws, 1001)
        self.server.shutdown()
        self.thread.join(timeout=3)
        self._sweeper.join(timeout=3)
