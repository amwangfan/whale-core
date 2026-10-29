"""Core domain types for perceive → decide → act."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Observation:
    """感知汇总：由 Perception 事件折叠而来，不含设备实现细节。"""

    event_ids: Sequence[str] = ()
    summary: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True)
class ActionIntent:
    """对基础设施的意图声明（由 Actions 网关落地，此处不连设备）。"""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    """一次行动选择结果。"""

    intents: Sequence[ActionIntent] = ()
    rationale: str = ""
    suppress: bool = False
    decided_at: datetime = field(default_factory=_utc_now)


@dataclass
class SessionState:
    """多设备会话连续性 stub：device_id + conversation_id + last_turn。

    Agent 可读；跨眼镜 / 手机会话键统一。不含 Token / 真实设备序列号要求。
    """

    device_id: str = ""
    conversation_id: str = ""
    last_turn: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utc_now)

    def touch(self, turn: str = "") -> SessionState:
        return SessionState(
            device_id=self.device_id,
            conversation_id=self.conversation_id,
            last_turn=turn or self.last_turn,
            meta=dict(self.meta),
            updated_at=_utc_now(),
        )
