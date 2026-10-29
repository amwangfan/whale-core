"""Memory：情节 / 偏好 / 人格连续性存储协议。"""

from whale_core.memory.continuity import (
    ContinuityContext,
    build_continuity_context,
    remember_episode,
    remember_preference,
)
from whale_core.memory.store import (
    InMemoryStore,
    MemoryKind,
    MemoryRecord,
    MemoryStore,
    PersonaContinuity,
)

__all__ = [
    "MemoryStore",
    "InMemoryStore",
    "MemoryKind",
    "MemoryRecord",
    "PersonaContinuity",
    "ContinuityContext",
    "build_continuity_context",
    "remember_episode",
    "remember_preference",
]
