"""D2：WSS 设备端点 ``/api/v1/devices/ws``（长连，非票据中继）。

协议事实源：contracts/v1/PROTOCOL.md（本仓版本化副本）。语义移植自
whale-mobile ``server/dev_gateway.py``（参考实现），差异见 docs/devices.md：

- 线程模型：``websockets.sync.server``（与 api/voice.py 同一可选依赖），
  每连接一线程；离线判定由后台 sweeper 线程做（内存状态，90s 基线 §2）。
- 持久化：设备/事件/动作幂等键/回执全部走 MemoryStore kv（双后端一致），
  不另开 SQLite。
- 撤销即时生效双保险（§3）：吊销时 API 层调用 ``kick()`` 立即关连接；
  已认证连接每帧复查 registry 状态，revoked 回 ``error.device_revoked`` 并关。
- sensor.ack 幂等闭环（§6）：新收与去重命中都回 ack，端侧据此停发。
- action.receipt 状态机：received/accepted 中间态只记录落库，
  executed/failed/unknown 终态才 resolve 等待方（「接受」≠「已执行」）。

与 voice.py 的本质区别：那是 60s 一次性票据中继；这里是设备长连，
不套会话超时，只靠心跳超时判离线。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from typing import Any

from whale_core.devices.perception import DevicePerceptionAdapter
from whale_core.devices.registry import DeviceRegistry, now_ms
from whale_core.devices.schema import (
    PROTOCOL_VERSION,
    SUPPORTED_MAJOR,
    ContractValidator,
    parse_major,
)
from whale_core.memory.store import MemoryStore

log = logging.getLogger("whale.devices.gateway")

WS_PATH = "/api/v1/devices/ws"
MAX_FRAME_BYTES = 256 * 1024  # §2：单帧 ≤ 256 KiB
REF_ID_FALLBACK = "0" * 16
DEFAULT_HEARTBEAT_INTERVAL = 30   # hello_ack 告知端侧的心跳节奏（秒）
DEFAULT_HEARTBEAT_TIMEOUT = 90    # §2：90s 无心跳判定离线
DEFAULT_CHALLENGE_TTL = 30        # 秒；schema 限定 expires_in ∈ [1,300]

#: action.receipt 终态（§4：received/accepted 只是中间态）
TERMINAL_RECEIPT_STATES = frozenset({"executed", "failed", "unknown"})


class GatewayError(ValueError):
    """下行通道失败（code 对齐 §4 错误码 + device_offline 管理面补充）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


class _Pending:
    """跨线程的请求-应答对（管理 API 线程等，WS 线程 resolve）。"""

    __slots__ = ("event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None


class DeviceGateway:
    """设备控制长连网关。构造即监听（websockets 缺失时抛 ImportError）。"""

    def __init__(
        self,
        registry: DeviceRegistry,
        store: MemoryStore,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        adapter: DevicePerceptionAdapter | None = None,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT,
        challenge_ttl: int = DEFAULT_CHALLENGE_TTL,
        log_event: Any = None,  # ApiContext.log_event（可选；只记设备号，不记正文）
    ) -> None:
        from websockets.sync.server import serve

        self.registry = registry
        self.store = store
        self.host = host
        self.adapter = adapter or DevicePerceptionAdapter()
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
        self.stopping = threading.Event()
        logger = logging.getLogger("whale.devices.transport")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.server = serve(
            self._handle, host, port,
            max_size=MAX_FRAME_BYTES,
            ping_interval=None,  # 心跳语义由协议层 device.heartbeat 承担（§2）
            logger=logger,
        )
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="whale-devices")
        self.thread.start()
        self._sweeper = threading.Thread(
            target=self._sweep_loop, daemon=True, name="whale-devices-sweep")
        self._sweeper.start()

    # ------------------------------------------------------------------
    # 帧构造 / 校验（收发双向过 contracts schema，对齐 dev_gateway）
    # ------------------------------------------------------------------

    def build_message(self, msg_type: str, dir_: str, payload: dict, seq: int) -> dict:
        msg = {"v": PROTOCOL_VERSION, "id": uuid.uuid4().hex, "type": msg_type,
               "ts": now_ms(), "seq": seq, "dir": dir_, "payload": payload}
        err = self.contracts.validate(msg_type, msg)
        if err:  # 服务端自身构造的帧不应失败；失败即实现 bug
            raise ValueError(f"出站帧未通过 {msg_type} schema: {err}")
        return msg

    def send_msg(self, st: ConnectionState, msg_type: str, payload: dict) -> dict:
        st.down_seq += 1
        msg = self.build_message(msg_type, "downlink", payload, st.down_seq)
        st.ws.send(json.dumps(msg, ensure_ascii=False))
        return msg

    def send_error(self, st: ConnectionState, ref_id: Any, code: str, message: str):
        if not (isinstance(ref_id, str) and 16 <= len(ref_id) <= 64):
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
        # 1. 首帧下发挑战（§3 auth.challenge）
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
        if not self._handle_hello(st, raw):
            self._safe_close(ws)
            return
        # 2. 认证后主循环（长连，无会话超时；心跳超时只标离线不踢线）
        try:
            for raw in ws:
                if self.stopping.is_set():
                    break
                if not self._handle_frame(st, raw):
                    break
        except (ConnectionClosed, OSError):
            pass
        finally:
            with self.lock:
                if st.device_id and self.online.get(st.device_id) is st:
                    del self.online[st.device_id]
            if st.device_id:
                self._emit("device:offline", st.device_id)
                log.info("设备 %s 断开，标记离线", st.device_id)
            self._safe_close(ws)

    @staticmethod
    def _safe_close(ws, code: int = 1008) -> None:
        try:
            ws.close(code)
        except Exception:  # noqa: BLE001
            pass

    def _handle_hello(self, st: ConnectionState, raw: Any) -> bool:
        """验签握手。失败时发送 error 并返回 False（调用方负责关闭）。"""
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
        # 通过 → hello_ack（按设备 scopes 收窄能力，§4）
        scopes = set(meta.get("scopes") or ())
        accepted = [c for c in p["capabilities"] if c in scopes]
        st.device_id = p["device_id"]
        st.authed = True
        st.session_id = uuid.uuid4().hex
        st.status = "online"
        st.last_heartbeat = time.monotonic()
        st.expected_up_seq = msg["seq"] + 1
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
            "accepted_capabilities": accepted,
        })
        return True

    @staticmethod
    def _verify_signature(pubkey_hex: str, signature_hex: str, challenge: str) -> bool:
        """§3：签名输入 = challenge 十六进制字符串的 ASCII 字节。"""
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
            # §2：未知 major 拒绝并回 error.protocol_version
            self.send_error(st, ref_id, "protocol_version",
                            f"unsupported major version: {msg.get('v')}")
            return False
        # §3 撤销即时生效：已认证设备每帧复查状态，revoked 立即断线
        if st.device_id and self.registry.status_of(st.device_id) == "revoked":
            self.send_error(st, ref_id, "device_revoked", "device has been revoked")
            return False
        msg_type = msg.get("type")
        if not isinstance(msg_type, str) or msg_type not in self.contracts.validators:
            # §3：未知消息类型静默忽略（前向兼容）
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
        if st.offline_marked:
            st.offline_marked = False
            self._emit("device:online", st.device_id)
            log.info("设备 %s 心跳恢复，重新标记在线", st.device_id)

    def _on_sensor_event(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        key = f"device:event:{st.device_id}:{p['event_id']}"
        with self.lock:
            duplicate = self.store.get(key) is not None
            if not duplicate:
                # event_id 幂等键持久化（§6）；事件本体存 kv，供排障与重放
                self.store.put(key, {
                    "device_id": st.device_id,
                    "event_id": p["event_id"],
                    "kind": p["kind"],
                    "occurred_at": p["occurred_at"],
                    "privacy_class": p["privacy_class"],
                    "summary": p["summary"],
                    "evidence_ref": p.get("evidence_ref"),
                    "schema_version": p["schema_version"],
                    "received_at": now_ms(),
                })
        if duplicate:
            log.info("sensor.event 重复 event_id=%s（device=%s），幂等去重跳过",
                     p["event_id"], st.device_id)
        else:
            self.adapter.offer(device_id=st.device_id or "", payload=p)
            self._emit("device:sensor_event",
                       f"{st.device_id}:{p['kind']}:{p['event_id']}")
            # 脱敏日志：不记 summary 全文，只记长度
            log.info("sensor.event device=%s kind=%s event_id=%s privacy_class=%s"
                     " summary_len=%d", st.device_id, p["kind"], p["event_id"],
                     p["privacy_class"], len(p["summary"]))
        # §6 幂等闭环：无论新收还是去重命中都 ack，端侧据此停止重发
        self.send_msg(st, "sensor.ack", {"event_ids": [p["event_id"]]})

    def _on_sensor_result(self, st: ConnectionState, msg: dict) -> None:
        request_id = msg["payload"]["request_id"]
        pend = self.pending.pop(request_id, None)
        if pend is not None:
            pend.result = msg["payload"]
            pend.event.set()
        else:
            log.warning("收到无对应请求的 sensor.result request_id=%s，忽略",
                        request_id)

    def _on_action_receipt(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        action_id, state = p["action_id"], p.get("state")
        # 回执状态机：received/accepted 中间态记录落库；
        # executed/failed/unknown 终态才 resolve 等待方（§4「接受」≠「已执行」）。
        with self.lock:
            key = f"device:receipts:{st.device_id}:{action_id}"
            receipts = self.store.get(key) or []
            if isinstance(receipts, list):
                receipts = receipts + [{"state": str(state), "ts": now_ms()}]
                self.store.put(key, receipts)
        if state in TERMINAL_RECEIPT_STATES:
            pend = self.pending.pop(action_id, None)
            if pend is not None:
                pend.result = p
                pend.event.set()
            self._emit("device:action_receipt", f"{st.device_id}:{action_id}:{state}")
        else:
            log.info("action %s 中间态回执 %s（device=%s）", action_id, state,
                     st.device_id)

    def _on_error(self, st: ConnectionState, msg: dict) -> None:
        p = msg["payload"]
        log.warning("设备 %s 回报 error ref_id=%s code=%s message=%s",
                    st.device_id, p["ref_id"], p["code"], p["message"])

    # ------------------------------------------------------------------
    # 心跳离线扫描（内存状态即可，§2/§6）
    # ------------------------------------------------------------------

    def _sweep_loop(self) -> None:
        interval = max(0.2, min(self.heartbeat_timeout / 3, 10.0))
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

    # ------------------------------------------------------------------
    # 下行通道（管理 API 线程调用）
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
        """吊销即时踢线（§3）。返回是否有在线连接被关闭。"""
        st = self.online_state(device_id)
        if st is None:
            return False
        try:
            self.send_error(st, REF_ID_FALLBACK, "device_revoked",
                            "device has been revoked")
        finally:
            self._safe_close(st.ws)
        return True

    def _online_or_raise(self, device_id: str) -> ConnectionState:
        st = self.online_state(device_id)
        if st is None or st.offline_marked:
            raise GatewayError("unavailable", f"device {device_id} 不在线")
        return st

    def _check_scope(self, device_id: str, capability: str) -> None:
        if capability not in self.registry.scopes_of(device_id):
            raise GatewayError("scope_denied", f"设备未授予 {capability}")

    def send_query(self, device_id: str, *, operation: str,
                   filters: dict | None = None, max_items: int = 10,
                   timeout_s: float = 15.0) -> dict:
        """下发 sensor.query 并在 deadline 内等待 sensor.result（§2/§4）。"""
        st = self._online_or_raise(device_id)
        self._check_scope(device_id, operation)
        if not (1 <= int(max_items) <= 100):
            raise GatewayError("invalid_message", "max_items 须在 1..100")
        request_id = uuid.uuid4().hex
        payload = {
            "request_id": request_id,
            "operation": operation,
            "filters": dict(filters or {}),
            "max_items": int(max_items),
            "deadline": now_ms() + int(timeout_s * 1000),
        }
        pend = _Pending()
        self.pending[request_id] = pend
        try:
            self.send_msg(st, "sensor.query", payload)
            if not pend.event.wait(timeout_s + 5):
                raise GatewayError(
                    "expired", f"等待 sensor.result 超时（{timeout_s}s deadline）")
            return dict(pend.result or {})
        finally:
            self.pending.pop(request_id, None)

    def send_action(self, device_id: str, *, capability: str,
                    args: dict | None = None, idempotency_key: str | None = None,
                    authorized_scope: list[str] | None = None,
                    approval_ref: str | None = None,
                    timeout_s: float = 15.0) -> dict:
        """下发 action.request；幂等键查重（§6），等待终态回执。"""
        st = self._online_or_raise(device_id)
        self._check_scope(device_id, capability)
        idem_key = idempotency_key or f"idem-{uuid.uuid4().hex}"
        if not (8 <= len(idem_key) <= 128):
            raise GatewayError("invalid_message", "idempotency_key 长度须为 8..128")
        action_id = uuid.uuid4().hex
        idem_store_key = f"device:action:{device_id}:{idem_key}"
        with self.lock:
            if self.store.get(idem_store_key) is not None:
                raise GatewayError(
                    "duplicate", f"幂等键 {idem_key} 已对该设备下发过")
            self.store.put(idem_store_key, {
                "device_id": device_id, "idempotency_key": idem_key,
                "action_id": action_id, "capability": capability,
                "created_at": now_ms(),
            })
        payload: dict[str, Any] = {
            "action_id": action_id,
            "capability": capability,
            "args": dict(args or {}),
            "authorized_scope": list(authorized_scope or [capability]),
            "expires_at": now_ms() + int(timeout_s * 1000),
            "idempotency_key": idem_key,
        }
        if approval_ref:
            payload["approval_ref"] = approval_ref
        pend = _Pending()
        self.pending[action_id] = pend
        try:
            self.send_msg(st, "action.request", payload)
            if not pend.event.wait(timeout_s + 5):
                raise GatewayError(
                    "expired", f"等待 action.receipt 超时（{timeout_s}s）")
            return dict(pend.result or {})
        finally:
            self.pending.pop(action_id, None)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.stopping.set()
        with self.lock:
            sessions = [st.ws for st in self.online.values()]
            self.online.clear()
        for ws in sessions:
            self._safe_close(ws, 1001)
        self.server.shutdown()
        self.thread.join(timeout=3)
        self._sweeper.join(timeout=3)
