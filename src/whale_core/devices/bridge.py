"""G5.4：Core 端模拟设备闭环 —— 可重放的 sensor.event → 情境 → 提醒/动作。

生产路径是 ``devices/gateway.py`` 的 WSS 长连（验签/去重/持久化/回执
状态机）。本模块提供**离线可重放**的同语义 Core 端通道，用于 CI 与
验收回放（计划 §7.4：Core 端必须能用可重放的模拟设备事件完成一条
「事件→情境→提醒」闭环；真实手机/眼镜效果另在 Mobile/IO 项目验收）。

与网关同语义的四条硬约束：

1. **鉴权**：设备必须在 DeviceRegistry 中 paired+active；未知/待批/吊销
   设备的事件一律拒绝（伪造事件不得进入情境）。
2. **去重 + 持久化**：event_id 幂等键落 ``device:event:{device}:{id}``
   （与网关同构 kv），重放命中只回 ack 不再消费。
3. **契约校验**：payload 套用 contracts/v1 ``sensor.event`` schema
   （复用 ``devices/schema.py`` 校验器，不另写规则）。
4. **明确不可用**：动作通道对离线/吊销/超时/不支持能力抛
   ``DeviceUnavailable``（携带协议错误码），绝不把「已发给设备」当作
   「设备已执行」（回执校验在 ``life/autonomy.py``）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from whale_core.devices.registry import DeviceRegistry, now_ms
from whale_core.devices.schema import PROTOCOL_VERSION, ContractValidator
from whale_core.life.autonomy import (
    AutonomyController,
    DeviceUnavailable,
)
from whale_core.life.situation import FACT, SituationState, SituationStore

DEFAULT_SUBJECT_ID = "owner"


class BridgeError(ValueError):
    """模拟设备桥拒绝（code 对齐 contracts/v1 §4 错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_projector(device_id: str, payload: Mapping[str, Any],
                      *, subject_id: str = DEFAULT_SUBJECT_ID,
                      privacy_space: str = "") -> dict[str, Any]:
    """默认投影：已配对设备事件 → 主体的一条**事实**情境。

    设备已过网关/桥的配对鉴权，来源类别 ``device`` 在可信事实来源默认集内；
    未配对设备根本到不了这里（``ingest_sensor_event`` 先拒绝）。
    """
    occurred_ms = int(payload.get("occurred_at") or 0)
    return {
        "key": str(payload.get("kind") or "device.unknown"),
        "value": str(payload.get("summary") or "")[:400],
        "fact_or_inference": FACT,
        "subject_id": subject_id,
        "source": f"device:{device_id}",
        "event_id": str(payload.get("event_id") or ""),
        "observed_at": datetime.fromtimestamp(occurred_ms / 1000, tz=timezone.utc),
        "expires_at": None,
        "confidence": "high",
        "privacy_space": privacy_space,
        "consent_version": "",
        "evidence": (str(payload.get("event_id") or ""),),
    }


class SimulatedActionPort:
    """可重放的模拟动作执行器：按 registry 校验设备状态/能力后给脚本化回执。

    - ``online``：``{device_id: bool}`` 或 callable；缺省**全部离线**
      （fail closed——不假装有真实设备）。
    - ``receipts``：``capability -> payload dict | Exception | callable``；
      缺省回 ``executed`` 终态。脚本化 ``DeviceUnavailable("expired")``
      即模拟超时；``received``/``accepted`` 可验证「接受≠已执行」。
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        online: Mapping[str, bool] | Callable[[str], bool] | None = None,
        receipts: Mapping[str, Any] | None = None,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self._online = online
        self._receipts = dict(receipts or {})
        self._now_ms = now_fn or now_ms

    def _is_online(self, device_id: str) -> bool:
        if self._online is None:
            return False
        if callable(self._online):
            return bool(self._online(device_id))
        return bool(self._online.get(device_id, False))

    def send_action(self, device_id: str, *, capability: str,
                    args: Mapping[str, Any], idempotency_key: str,
                    timeout_s: float) -> Mapping[str, Any]:
        status = self.registry.status_of(device_id)
        if status == "revoked":
            raise DeviceUnavailable("device_revoked", "设备已吊销")
        if status != "active":
            raise DeviceUnavailable("auth_failed", "设备未配对或未批准")
        if capability not in self.registry.scopes_of(device_id):
            raise DeviceUnavailable("capability_unknown", f"设备不支持 {capability}")
        if not self._is_online(device_id):
            raise DeviceUnavailable("unavailable", f"设备 {device_id} 不在线")
        scripted = self._receipts.get(capability)
        if callable(scripted):
            scripted = scripted(device_id=device_id, capability=capability,
                                args=dict(args), idempotency_key=idempotency_key)
        if isinstance(scripted, Exception):
            raise scripted
        receipt = dict(scripted or {"state": "executed"})
        receipt.setdefault("action_id", f"sim-{uuid4().hex[:24]}")
        if receipt.get("state") in ("executed", "failed"):
            receipt.setdefault("executed_at", self._now_ms())
        return receipt


class GatewayActionPort:
    """生产适配：把 DeviceGateway.send_action 包成 DeviceActionPort。

    GatewayError（离线/超时/scope/幂等冲突）统一转 DeviceUnavailable；
    回执 payload 原样上交，由 AutonomyController 校验终态。
    """

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    def send_action(self, device_id: str, *, capability: str,
                    args: Mapping[str, Any], idempotency_key: str,
                    timeout_s: float) -> Mapping[str, Any]:
        from whale_core.devices.gateway import GatewayError
        try:
            return self._gateway.send_action(
                device_id, capability=capability, args=dict(args),
                idempotency_key=idempotency_key, timeout_s=timeout_s)
        except GatewayError as exc:
            raise DeviceUnavailable(exc.code, str(exc)) from exc


class SimulatedDeviceBridge:
    """Core 端模拟设备面：可重放的 sensor.event 入口 + 可选自主接线。"""

    def __init__(
        self,
        store,
        registry: DeviceRegistry,
        *,
        situations: SituationStore | None = None,
        controller: AutonomyController | None = None,
        outbox=None,
        prefs=None,
        approvals=None,
        action_port=None,
        subject_id: str = DEFAULT_SUBJECT_ID,
        privacy_space: str = "",
        contracts: ContractValidator | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.situations = situations
        self.controller = controller
        self.outbox = outbox
        self.prefs = prefs
        self.approvals = approvals
        self.action_port = action_port
        self.subject_id = subject_id
        self.privacy_space = privacy_space
        self.contracts = contracts or ContractValidator()

    # ---- 上行：sensor.event ---------------------------------------------

    def ingest_sensor_event(
        self,
        device_id: str,
        payload: Mapping[str, Any],
        *,
        ctx=None,
        projector: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        busy: bool = False,
    ) -> dict[str, Any]:
        """接收一条模拟 sensor.event：鉴权 → 契约校验 → 去重/持久化 → ack。

        配置了 SituationStore/AutonomyController 时继续走 情境 → 裁决 →
        受门控执行，返回全链路证据。重放（重复 event_id）只回 ack。
        """
        # 1. 鉴权：未配对/待批/吊销设备的事件不得进入任何后续环节
        status = self.registry.status_of(device_id)
        if status == "revoked":
            self.registry.audit("sim.sensor_event", outcome="denied",
                                reason="revoked", device_id=device_id)
            raise BridgeError("device_revoked", "设备已吊销")
        if status != "active":
            self.registry.audit("sim.sensor_event", outcome="denied",
                                reason="not_paired", device_id=device_id)
            raise BridgeError("auth_failed", "设备未配对或未批准")
        # 2. 契约校验：套用 contracts/v1 sensor.event schema
        envelope = {"v": PROTOCOL_VERSION, "id": uuid4().hex, "type": "sensor.event",
                    "ts": now_ms(), "seq": 0, "dir": "uplink",
                    "payload": dict(payload)}
        err = self.contracts.validate("sensor.event", envelope)
        if err:
            raise BridgeError("invalid_message", f"sensor.event 契约校验失败: {err}")
        event_id = str(payload["event_id"])
        # 3. 去重 + 持久化（与网关同构 kv；§6 幂等闭环：新旧都回 ack）
        key = f"device:event:{device_id}:{event_id}"
        if self.store.get(key) is not None:
            return {"ack": [event_id], "duplicate": True}
        self.store.put(key, {
            "device_id": device_id,
            "event_id": event_id,
            "kind": payload["kind"],
            "occurred_at": payload["occurred_at"],
            "privacy_class": payload["privacy_class"],
            "summary": payload["summary"],
            "evidence_ref": payload.get("evidence_ref"),
            "schema_version": payload["schema_version"],
            "received_at": now_ms(),
        })
        result: dict[str, Any] = {"ack": [event_id], "duplicate": False}
        # 4. 投影 → 情境（投影仪返回 None = 该事件不产生情境）
        if self.situations is not None:
            project = projector or (
                lambda dev, p: default_projector(
                    dev, p, subject_id=self.subject_id,
                    privacy_space=self.privacy_space))
            spec = project(device_id, payload)
            if spec is not None:
                situation = self.situations.record(**dict(spec))
                result["situation_id"] = situation.situation_id
                # 5. 裁决 → 受门控执行（提醒走 ProactiveOutbox，动作走端口）
                if self.controller is not None and ctx is not None:
                    decision = self.controller.evaluate(situation, ctx)
                    result["decision"] = {"kind": decision.kind, "level": decision.level,
                                          "reason": decision.reason,
                                          "decision_id": decision.decision_id}
                    if decision.kind in ("remind", "draft", "action"):
                        record = self.controller.execute(
                            decision, ctx, outbox=self.outbox, prefs=self.prefs,
                            device_port=self.action_port, approvals=self.approvals,
                            busy=busy)
                        result["execution"] = {"status": record.status,
                                               "reason": record.reason,
                                               "receipt": record.receipt,
                                               "draft_id": record.draft_id}
        self.registry.audit("sim.sensor_event", device_id=device_id,
                            kind=str(payload.get("kind") or ""), event_id=event_id)
        return result


__all__ = [
    "BridgeError",
    "GatewayActionPort",
    "SimulatedActionPort",
    "SimulatedDeviceBridge",
    "SituationState",
    "default_projector",
]
