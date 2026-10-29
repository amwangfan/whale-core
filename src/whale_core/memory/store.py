"""MemoryStore 协议 + 进程内实现。

区分三类记忆：episode（情节）/ preference（偏好）/ persona_continuity（人格连续性）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


class MemoryKind(str, Enum):
    """记忆类别。"""

    EPISODE = "episode"
    PREFERENCE = "preference"
    PERSONA_CONTINUITY = "persona_continuity"


@dataclass(frozen=True)
class MemoryRecord:
    """一条记忆事件 / 记录（智能层侧；不含设备密钥）。"""

    kind: MemoryKind
    key: str
    content: Mapping[str, Any] = field(default_factory=dict)
    record_id: str = field(default_factory=_new_id)
    created_at: datetime = field(default_factory=_utc_now)
    tags: tuple[str, ...] = ()


@dataclass
class PersonaContinuity:
    """人格连续性快照：跨会话语气、称呼、禁忌与自由字段。

    由 MemoryStore 读写 stub；真正持久化后端可后续替换。
    """

    persona_name: str = "鲸鱼"
    tone: str = "冷静、简洁、略带幽默"
    address_user_as: str = "你"
    taboos: tuple[str, ...] = ()
    traits: Mapping[str, Any] = field(default_factory=dict)
    last_session_id: str = ""
    updated_at: datetime = field(default_factory=_utc_now)

    def as_record(self, key: str = "persona:default") -> MemoryRecord:
        return MemoryRecord(
            kind=MemoryKind.PERSONA_CONTINUITY,
            key=key,
            content={
                "persona_name": self.persona_name,
                "tone": self.tone,
                "address_user_as": self.address_user_as,
                "taboos": list(self.taboos),
                "traits": dict(self.traits),
                "last_session_id": self.last_session_id,
                "updated_at": self.updated_at.isoformat(),
            },
            tags=("persona",),
        )

    @classmethod
    def from_record(cls, record: MemoryRecord) -> PersonaContinuity:
        c = dict(record.content)
        taboos = c.get("taboos") or ()
        if isinstance(taboos, list):
            taboos = tuple(taboos)
        updated = c.get("updated_at")
        if isinstance(updated, str):
            try:
                updated_at = datetime.fromisoformat(updated)
            except ValueError:
                updated_at = _utc_now()
        elif isinstance(updated, datetime):
            updated_at = updated
        else:
            updated_at = _utc_now()
        return cls(
            persona_name=str(c.get("persona_name") or "鲸鱼"),
            tone=str(c.get("tone") or ""),
            address_user_as=str(c.get("address_user_as") or "你"),
            taboos=tuple(taboos),
            traits=dict(c.get("traits") or {}),
            last_session_id=str(c.get("last_session_id") or ""),
            updated_at=updated_at,
        )


@runtime_checkable
class MemoryStore(Protocol):
    def get(self, key: str) -> Any | None:
        ...

    def put(self, key: str, value: Any) -> None:
        ...

    def delete(self, key: str) -> None:
        ...

    def keys(self) -> list[str]:
        ...

    def put_record(self, record: MemoryRecord) -> None:
        ...

    def get_record(self, key: str) -> MemoryRecord | None:
        ...

    def list_by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        ...

    def get_persona_continuity(self, key: str = "persona:default") -> PersonaContinuity | None:
        ...

    def put_persona_continuity(
        self, persona: PersonaContinuity, key: str = "persona:default"
    ) -> None:
        ...


class InMemoryStore:
    """简单 dict 后端，供本地开发与测试。"""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._records: dict[str, MemoryRecord] = {}

    def get(self, key: str) -> Any | None:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._records.pop(key, None)

    def keys(self) -> list[str]:
        return sorted(set(self._data) | set(self._records))

    def put_record(self, record: MemoryRecord) -> None:
        # JSON 往返归一 content/tags（与 SqliteMemoryStore 序列化语义对齐）：
        # 非 JSON 可序列化内容在此抛 TypeError；tuple 等类型归一为 list。
        # created_at 保持 datetime 对象，不参与往返。
        content = json.loads(json.dumps(dict(record.content), ensure_ascii=False))
        tags = tuple(json.loads(json.dumps(list(record.tags), ensure_ascii=False)))
        normalized = replace(record, content=content, tags=tags)
        self._records[record.key] = normalized
        # 同步一份可序列化视图，便于旧 get() 调用方窥视
        self._data[record.key] = {
            "kind": normalized.kind.value,
            "content": dict(normalized.content),
            "record_id": normalized.record_id,
            "created_at": normalized.created_at.isoformat(),
            "tags": list(normalized.tags),
        }

    def get_record(self, key: str) -> MemoryRecord | None:
        return self._records.get(key)

    def list_by_kind(self, kind: MemoryKind) -> list[MemoryRecord]:
        # created_at 倒序（新的在前）；sorted 稳定，同 created_at 保持插入顺序。
        records = [r for r in self._records.values() if r.kind == kind]
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def get_persona_continuity(self, key: str = "persona:default") -> PersonaContinuity | None:
        rec = self.get_record(key)
        if rec is None or rec.kind != MemoryKind.PERSONA_CONTINUITY:
            return None
        return PersonaContinuity.from_record(rec)

    def put_persona_continuity(
        self, persona: PersonaContinuity, key: str = "persona:default"
    ) -> None:
        self.put_record(persona.as_record(key=key))
