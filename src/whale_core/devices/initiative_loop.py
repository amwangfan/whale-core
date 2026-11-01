"""EV-02 活接线：sensor.event → 情境 → 裁决 → 手机通知（真实网关）。

whale-mobile #12 的 Core 运行时侧：感知事件经网关 ``adapter.offer`` 到达，
本适配器在落库（DurableDevicePerceptionAdapter）之后继续：

1. ``default_projector`` 投影为主体事实情境（设备已过配对鉴权）；
2. ``AutonomyController`` 按规则裁决（无规则 → observe，静默）；
3. remind/action 经既有门控执行——动作走 ``GatewayActionPort``（真实
   设备通道，phone.notification.post）。

**默认关**：kv ``config:device_initiative`` 未启用时不接控制器（纯落库）。
规则同样存于该键：``{"enabled": true, "rules": {"<situation.key>":
{"contains": "关键词", "text": "通知正文", "reason": "..."}}}``——命中即生成
L2 低风险动作（幂等键=event_id，可当次追溯）。

隐私：设备事件是机主私有面；闭环只走合成 owner API 上下文，不触任何群聊。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional

from whale_core.devices.bridge import GatewayActionPort, default_projector
from whale_core.devices.gateway import DeviceGateway
from whale_core.devices.perception import (DurableDevicePerceptionAdapter,
                                           DevicePerceptionAdapter,
                                           PerceptionEvent)
from whale_core.life.autonomy import (ApprovalBook, AutonomyController,
                                      AutonomyProposal, KIND_ACTION, KIND_REMIND,
                                      LEVEL_ACT_LOW_RISK)
from whale_core.life.conversation_context import (ConversationKind,
                                                  ConversationRef, Principal,
                                                  ResolvedContext,
                                                  SCOPE_DEVICE_NOTIFY,
                                                  SCOPE_PHONE_NOTIFICATION_POST)
from whale_core.life.initiative import ProactiveOutbox
from whale_core.life.situation import SituationStore

CONFIG_KEY = "config:device_initiative"

#: 设备事件闭环用的合成 owner API 上下文（不经过任何群聊上下文）。
def _owner_ctx() -> ResolvedContext:
    return ResolvedContext(
        conversation=ConversationRef(
            channel="api", kind=ConversationKind.API,
            conversation_id="device-initiative"),
        principal=Principal(
            principal_id="owner", paired=True, is_owner=True,
            evidence="device_initiative_loop"),
        persona_id=None,
        capability_scopes=(SCOPE_DEVICE_NOTIFY, SCOPE_PHONE_NOTIFICATION_POST),
    )


class DeviceInitiativeAdapter(DevicePerceptionAdapter):
    """持久化 + 主动闭环的事件适配器。"""

    def __init__(self, store, *, gateway_resolver: Callable[[], Any]) -> None:
        self._inner = DurableDevicePerceptionAdapter(store)
        self._store = store
        self._gateway_resolver = gateway_resolver

    # ---- 配置 ----

    def _config(self) -> dict[str, Any]:
        try:
            raw = self._store.get(CONFIG_KEY)
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                return json.loads(raw)
        except Exception:
            pass
        return {}

    # ---- 入口 ----

    def offer(self, *, device_id: str,
              payload: Mapping[str, Any]) -> PerceptionEvent:
        event = self._inner.offer(device_id=device_id, payload=payload)
        cfg = self._config()
        if not cfg.get("enabled"):
            return event
        try:
            self._drive(device_id, payload, cfg)
        except Exception:
            # 闭环故障不影响落库/ack 主路径
            pass
        return event

    # ---- 闭环 ----

    def _drive(self, device_id: str, payload: Mapping[str, Any],
               cfg: Mapping[str, Any]) -> None:
        spec = default_projector(device_id, payload)
        if spec is None:
            return
        situations = SituationStore(self._store)
        controller = AutonomyController(situations)
        rules = cfg.get("rules") or {}
        for key, rule in rules.items():
            if not isinstance(rule, dict):
                continue
            contains = str(rule.get("contains") or "")
            text = str(rule.get("text") or "")
            if not text:
                continue

            def _mk(r=rule, kw=contains, txt=text):
                def _fn(sit) -> AutonomyProposal | None:
                    if kw and kw not in (sit.value or ""):
                        return None
                    return AutonomyProposal(
                        level=LEVEL_ACT_LOW_RISK, kind=KIND_ACTION,
                        reason=str(r.get("reason") or f"设备事件规则 {sit.key}"),
                        device_id=device_id_of(sit),
                        capability=SCOPE_PHONE_NOTIFICATION_POST,
                        args={"title": str(r.get("title") or "Whale 提醒"),
                              "body": txt},
                        idempotency_key=f"init-{sit.event_id}",
                        text=txt,
                    )
                return _fn
            controller.register_rule(str(key), _mk())
        situation = situations.record(**dict(spec))
        ctx = _owner_ctx()
        decision = controller.evaluate(situation, ctx)
        if decision.kind not in (KIND_REMIND, KIND_ACTION):
            return
        gateway: Optional[DeviceGateway] = self._gateway_resolver()
        port = GatewayActionPort(gateway) if gateway is not None else None
        controller.execute(
            decision, ctx,
            outbox=ProactiveOutbox(self._store),
            prefs=None,
            device_port=port,
            approvals=ApprovalBook(self._store),
        )


def device_id_of(situation) -> str:
    """从情境 source（device:<id>）还原设备号。"""
    src = str(getattr(situation, "source", "") or "")
    return split_device_id(source=src)


def split_device_id(*, source: str) -> str:
    prefix = "device:"
    if source.startswith(prefix):
        return source[len(prefix):]
    return ""
