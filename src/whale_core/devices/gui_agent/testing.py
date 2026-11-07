"""Fake Device 状态机（借鉴 dsh-mobile-gui-agent src/testing/index.ts）。

FakeScreen 描述一个界面状态；execute 按当前屏 transitions 精确匹配切屏，
不匹配回 FAILED。可注入 stale_observation / timeout 响应。公开 operations
账本供断言。全部不依赖真机。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from whale_core.devices.transports.base import (
    ActionReceipt,
    ActionRequest,
    Observation,
    ObserveRequest,
    ReceiptStatus,
    UiElement,
    new_id,
    now_ms,
)


def _fingerprint(el: UiElement) -> str:
    import hashlib
    raw = "|".join([
        el.class_name, el.text, el.resource_id, el.content_desc,
        str(el.bounds), str(el.clickable), str(el.scrollable), str(el.editable),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_element(element_id: int, text: str = "", *, class_name: str = "android.widget.Button",
                 resource_id: str = "", bounds=(0, 0, 100, 50), clickable=True,
                 editable=False) -> UiElement:
    el = UiElement(element_id=element_id, class_name=class_name, text=text,
                   resource_id=resource_id, content_desc="", bounds=bounds,
                   clickable=clickable, editable=editable)
    return UiElement(**{**el.__dict__, "fingerprint": _fingerprint(el)})


@dataclass(frozen=True)
class FakeTransition:
    """匹配键：capability + arguments 子集匹配；to=下一屏 id。"""

    capability: str
    match: Mapping[str, Any]         # arguments 子集
    to: str                          # 目标屏 id；"" = 不切屏
    error: str | None = None         # 注入错误码（如 stale_observation）


@dataclass
class FakeScreen:
    screen_id: str
    foreground_package: str = "com.example.fake"
    activity: str = ".MainActivity"
    elements: list[UiElement] = field(default_factory=list)
    transitions: list[FakeTransition] = field(default_factory=list)
    locked: bool = False


class FakeTransport:
    """实现 DeviceTransport 语义的状态机假设备。

    kind 默认 "adb"（最常用 GUI 通道）；测试其他通道时传 kind="ssh" 等。
    """

    def __init__(self, screens: Sequence[FakeScreen], initial: str,
                 *, kind: str = "adb") -> None:
        self.kind = kind
        self._screens = {s.screen_id: s for s in screens}
        if initial not in self._screens:
            raise ValueError(f"初始屏不存在: {initial}")
        self._current = initial
        self.operations: list[dict[str, Any]] = []
        self.observe_calls = 0
        self.execute_calls = 0
        # 可注入的强制响应：{"stale_once": True} 等
        self.force_stale_once = False

    async def connect(self, device_id: str) -> None: ...

    async def disconnect(self, device_id: str) -> None: ...

    async def capabilities(self, device_id: str) -> Sequence[str]:
        return ["device.observe", "device.tap", "device.long_press",
                "device.swipe", "device.input_text", "device.key",
                "device.wait", "device.app.launch"]

    async def observe(self, device_id: str, request: ObserveRequest) -> Observation:
        self.observe_calls += 1
        screen = self._screens[self._current]
        elements = tuple(screen.elements)
        import json
        ui_sig = hashlib.sha256(json.dumps(
            [{k: v for k, v in el.__dict__.items() if k != "element_id"}
             for el in elements], sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return Observation(
            observation_id=new_id("obs"), device_id=device_id, taken_at=now_ms(),
            foreground_package=screen.foreground_package,
            activity=screen.activity, elements=elements,
            ui_signature=f"{self._current}:{ui_sig}",
            screenshot_ref=None,
            screenshot_signature=hashlib.sha256(
                self._current.encode()).hexdigest()[:16],
            screen_width=1080, screen_height=2400,
            locked=screen.locked,
        )

    async def execute(self, device_id: str, action: ActionRequest) -> ActionReceipt:
        self.execute_calls += 1
        self.operations.append({
            "capability": action.capability,
            "arguments": dict(action.arguments),
            "observation_id": action.observation_id,
            "idempotency_key": action.idempotency_key,
        })
        started = now_ms()
        if self.force_stale_once and action.capability in (
                "device.tap", "device.long_press", "device.input_text"):
            self.force_stale_once = False
            return ActionReceipt(
                action_id=action.action_id, status=ReceiptStatus.FAILED,
                summary="元素已过期", error_code="stale_observation",
                started_at=started, completed_at=now_ms())
        screen = self._screens[self._current]
        for tr in screen.transitions:
            if tr.capability != action.capability:
                continue
            if all(action.arguments.get(k) == v for k, v in tr.match.items()):
                if tr.error:
                    return ActionReceipt(
                        action_id=action.action_id,
                        status=ReceiptStatus.FAILED,
                        summary=f"注入错误 {tr.error}", error_code=tr.error,
                        started_at=started, completed_at=now_ms())
                if tr.to:
                    if tr.to not in self._screens:
                        raise ValueError(f"transition 目标屏不存在: {tr.to}")
                    self._current = tr.to
                return ActionReceipt(
                    action_id=action.action_id, status=ReceiptStatus.SUCCEEDED,
                    summary=f"fake:{action.capability} ok",
                    started_at=started, completed_at=now_ms())
        return ActionReceipt(
            action_id=action.action_id, status=ReceiptStatus.FAILED,
            summary=f"当前屏 {self._current} 无匹配 transition",
            error_code="internal", started_at=started, completed_at=now_ms())

    async def read_output(self, device_id: str, output_ref: str, *,
                          offset: int = 0, max_bytes: int = 65536):
        return b"", False

    async def health(self, device_id: str):
        from whale_core.devices.transports.base import TransportHealth
        return TransportHealth(connected=True, detail="fake", checked_at=now_ms())
