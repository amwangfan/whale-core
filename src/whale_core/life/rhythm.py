"""G4：ConversationRhythm —— 会话节奏设置（下一阶段计划 §6.3 / PRD §7.1）。

- 字段：活跃模式（quiet/balanced/lively）、单条回复分段上限、免打扰开关；
- **只影响回复节奏**：不含任何工具权限、记忆授权或安全策略语义——那些走
  capabilities / memories 端点，节奏卡无权携带（PER-01）；
- 存储：kv ``bind:rhythm:{conversation_key}``，双后端一致；版本乐观锁与
  审计在 api/studio.py 的写路径上；
- 消费方：G3 分段/发送链（runner 在发送前读取）；quiet = 单气泡、
  dnd = 压制主动/补发消息（不拦截被问到的被动回复）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from whale_core.life.segments import MAX_SEGMENTS_GROUP, MAX_SEGMENTS_PRIVATE
from whale_core.memory.store import MemoryStore

#: 活跃模式枚举（lively 的主动发言配额由后续 TurnPolicy 落地；首版只承诺
#: quiet 的单气泡收敛与分段上限语义）。
ACTIVITY_MODES = ("quiet", "balanced", "lively")
DEFAULT_ACTIVITY = "balanced"

#: 分段上限的合法取值：1..MAX_SEGMENTS_LIMIT；0 = 未设置（跟随默认 群2/私3）。
MAX_SEGMENTS_LIMIT = 5

_KEY_PREFIX = "bind:rhythm:"


@dataclass(frozen=True)
class ConversationRhythm:
    """一条会话的节奏设置。``version`` 由存储层维护（乐观锁）。"""

    activity: str = DEFAULT_ACTIVITY
    max_segments: int = 0       # 0 = 跟随默认；1..5 = 硬上限
    dnd: bool = False           # 免打扰：压制主动/补发消息
    version: int = 0

    def effective_max_segments(self, *, is_group: bool) -> int:
        """实际生效的分段上限：quiet 收敛为单气泡；其余按配置（封顶 5）。"""
        if self.activity == "quiet":
            return 1
        base = self.max_segments or (
            MAX_SEGMENTS_GROUP if is_group else MAX_SEGMENTS_PRIVATE)
        return max(1, min(int(base), MAX_SEGMENTS_LIMIT))

    def as_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "max_segments": self.max_segments,
            "dnd": self.dnd,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConversationRhythm":
        """从 kv 数据还原；污染数据 fail closed 回默认（不抛给聊天链路）。"""
        try:
            activity = str(data.get("activity") or DEFAULT_ACTIVITY)
            if activity not in ACTIVITY_MODES:
                activity = DEFAULT_ACTIVITY
            max_segments = int(data.get("max_segments") or 0)
            if not 0 <= max_segments <= MAX_SEGMENTS_LIMIT:
                max_segments = 0
            dnd = data.get("dnd")
            return cls(
                activity=activity,
                max_segments=max_segments,
                dnd=dnd if isinstance(dnd, bool) else False,
                version=int(data.get("version") or 0),
            )
        except (TypeError, ValueError):
            return cls()


class RhythmStore:
    """ConversationRhythm 的 kv 存储（版本由 put 递增）。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    @staticmethod
    def _key(conversation_key: str) -> str:
        return f"{_KEY_PREFIX}{conversation_key}"

    def get(self, conversation_key: str) -> ConversationRhythm:
        """读取节奏设置；未配置或数据损坏 → 默认值（version 0）。"""
        data = self.store.get(self._key(conversation_key))
        if not isinstance(data, Mapping):
            return ConversationRhythm()
        return ConversationRhythm.from_dict(data)

    def put(self, conversation_key: str, rhythm: ConversationRhythm) -> int:
        """写入节奏设置（版本自动 +1）；返回新版本号。"""
        version = self.get(conversation_key).version + 1
        self.store.put(
            self._key(conversation_key),
            ConversationRhythm(
                activity=rhythm.activity,
                max_segments=rhythm.max_segments,
                dnd=rhythm.dnd,
                version=version,
            ).as_dict(),
        )
        return version

    def configured(self, conversation_key: str) -> bool:
        return isinstance(self.store.get(self._key(conversation_key)), Mapping)


def rhythm_key_for(channel: str, conversation_id: str) -> Optional[str]:
    """由渠道事实推导节奏键（``channel:kind:conversation_id``）。

    未知渠道/类型返回 None——节奏设置只挂可识别会话（与
    ``context_resolver.derive_kind`` 同一归一口径）。
    """
    from whale_core.life.context_resolver import derive_kind
    from whale_core.life.conversation_context import ConversationKind

    kind = derive_kind(channel, conversation_id)
    if kind is ConversationKind.UNKNOWN:
        return None
    return f"{channel}:{kind.value}:{conversation_id}"
