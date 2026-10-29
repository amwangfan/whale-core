"""行动意图：智能层只声明「要做什么」，由基础设施执行上镜 / 通话 / TTS / 控制桥。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from whale_core.core.types import ActionIntent


@dataclass(frozen=True)
class ShowCard:
    """在眼镜 HUD 展示卡片（真正上镜由 Turbo-IO / dashboard 执行）。"""

    title: str
    body: str = ""
    layout_hint: str = "default"
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="show_card",
            payload={
                "title": self.title,
                "body": self.body,
                "layout_hint": self.layout_hint,
            },
            priority=self.priority,
            reason="ui.show_card",
        )


@dataclass(frozen=True)
class PlaceCall:
    """发起 / 接听 VoIP 来电会话（由 VoIP 来电音频桥执行）。"""

    contact_label: str = "AI"
    mode: str = "incoming"  # incoming | outgoing
    extras: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="place_call",
            payload={
                "contact_label": self.contact_label,
                "mode": self.mode,
                "extras": dict(self.extras),
            },
            priority=self.priority,
            reason="voip.place_call",
        )


@dataclass(frozen=True)
class Speak:
    """语音播报意图（TTS / 通话侧实现；此处不写 API Key）。"""

    text: str
    voice_style: str = "default"
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="speak",
            payload={"text": self.text, "voice_style": self.voice_style},
            priority=self.priority,
            reason="voice.speak",
        )


@dataclass(frozen=True)
class PhoneAction:
    """对手机执行操作的意图（打开应用 / 通知等）。

    由**未来手机控制桥**执行；智能层不写 ADB / Accessibility。
    有监测（PhoneForeground）≠ 有控制——本意图填补控制侧契约。
    """

    action: str  # open_app | post_notification | other
    target: str = ""  # package 或通知渠道等语义标签
    extras: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="phone_action",
            payload={
                "action": self.action,
                "target": self.target,
                "extras": dict(self.extras),
                "executor": "phone_control_bridge",  # 标注落地侧
            },
            priority=self.priority,
            reason="phone.control",
        )


@dataclass(frozen=True)
class NavigateHint:
    """路线提醒卡片意图（配合 nav.route_progress 事件）。"""

    route_label: str
    message: str = ""
    eta_minutes: int | None = None
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="navigate_hint",
            payload={
                "route_label": self.route_label,
                "message": self.message,
                "eta_minutes": self.eta_minutes,
            },
            priority=self.priority,
            reason="nav.hint",
        )


@dataclass(frozen=True)
class AmbientListen:
    """启停环境听写——仅意图；采集在基础设施，本仓不碰麦克风。"""

    enabled: bool
    reason_note: str = ""
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="ambient_listen",
            payload={
                "enabled": self.enabled,
                "reason_note": self.reason_note,
            },
            priority=self.priority,
            reason="ambient.listen",
        )


@dataclass(frozen=True)
class ShellWhitelist:
    """声明执行 call-bridge 风格白名单工具的意图（非任意 shell）。

    由云端 Actions 网关落地为 mock/HTTP 控制意图；真正执行仍在
    infra/call-bridge 的 BRIDGE_TOOLS 白名单侧。智能层不拼命令、不读密钥。
    """

    tool_name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="shell_whitelist",
            payload={
                "tool_name": self.tool_name,
                "args": dict(self.args),
                "executor": "bridge_tools_whitelist",
            },
            priority=self.priority,
            reason="cloud.shell_whitelist",
        )


@dataclass(frozen=True)
class ShellCommand:
    """声明在 personal 档执行一条自由命令的意图（非高危，仅认证 owner 触发）。

    分档与高危裁决在 ``whale_core.cloud.shell_policy``（智能层只裁决不执行）；
    执行方是云宿主机沙箱 executor（非 root + systemd 隔离 + 超时强杀）。
    """

    command: str
    tier: str = "personal"
    priority: int = 0

    def to_intent(self) -> ActionIntent:
        return ActionIntent(
            kind="shell_command",
            payload={
                "command": self.command[:2000],
                "tier": self.tier,
                "executor": "cloud_host_sandbox",
            },
            priority=self.priority,
            reason="cloud.shell_command",
        )
