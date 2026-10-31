"""泛化插件事件：新输入点无需改核心事件类型。

分层：插件框架契约（framework，issue #31）。

``PluginEvent`` 是 ``PerceptionEvent`` 子类，直接进 ``WhaleAgent.perceive`` /
``ContextReasoner`` 既有链路（未知 kind 只做摘要，不触发核心情境规则——
插件语义由宿主在 router/policy 层消费）。

插件事件默认且固定为 external。此类型不接受调用方传入 trust=owner_verified；
只有宿主完成独立鉴权后，才能通过核心可信上下文处理 owner 身份。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from whale_core.perception.events import PerceptionEvent
from whale_core.plugins.manifest import (
    CORE_EVENT_KINDS,
    CORE_EVENT_PREFIXES,
    EVENT_KIND_RE,
    PLUGIN_ID_RE,
)


@dataclass(frozen=True)
class PluginEvent(PerceptionEvent):
    """插件产出的感知事件。

    - ``kind``：manifest 中声明的命名空间 kind（如 ``email.received``）；
    - ``plugin_id``：产出方插件；
    - ``payload``：插件自定义形状（邮件=发件人/主题摘要，日程=标题/时间，
      刷视频=内容元数据，雷鸟纪要=纪要片段文本——**不含原始音频字节**，
      沿用 ambient.speech_segment 的采集边界）；
    - ``trust``：固定为 external；插件事件不能自行声明 owner_verified。
    """

    plugin_id: str = ""
    trust: str = field(default="external", init=False)

    def __post_init__(self) -> None:
        if not PLUGIN_ID_RE.fullmatch(self.plugin_id or ""):
            raise ValueError(f"PluginEvent requires a valid plugin_id: {self.plugin_id!r}")
        if not EVENT_KIND_RE.fullmatch(self.kind or ""):
            raise ValueError(f"PluginEvent kind must be namespaced: {self.kind!r}")
        if self.kind in CORE_EVENT_KINDS or self.kind.startswith(CORE_EVENT_PREFIXES):
            raise ValueError(f"PluginEvent cannot impersonate core event kind: {self.kind!r}")
