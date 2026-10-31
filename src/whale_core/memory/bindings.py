"""S2/S5：会话 → 角色卡 / 记忆空间 的 kv 绑定存储。

键：
- ``bind:persona:{conversation_key}`` → {"persona_id", "updated_by", "version", "updated_at"}
- ``bind:mem:{conversation_key}``     → [{"space_id","can_read","can_write","granted_by","created_at"}]

实现 context_resolver.BindingLookup 协议。所有写操作在绑定层执行
MEM-HARD-01（protected_original 直接拒绝）；读取时对缺失/已注销/
受保护空间 fail closed。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from whale_core.life.conversation_context import MemorySpaceRef, MemorySpaceType
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import MemoryStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BindingError(ValueError):
    """绑定被拒绝（空间类型不允许 / 空间不存在 / 参数非法）。"""


class BindingStore:
    """会话绑定存储（同时充当 resolver 的 BindingLookup）。"""

    def __init__(self, store: MemoryStore, spaces: SpaceStore):
        self.store = store
        self.spaces = spaces

    # -- BindingLookup 协议（resolver 消费） -------------------------------

    def persona_for(self, conversation_key: str) -> Optional[str]:
        data = self.store.get(f"bind:persona:{conversation_key}")
        if isinstance(data, dict) and data.get("persona_id"):
            return str(data["persona_id"])
        return None

    def memory_spaces_for(self, conversation_key: str) -> Tuple[MemorySpaceRef, ...]:
        rows = self.store.get(f"bind:mem:{conversation_key}")
        if not isinstance(rows, list):
            return ()
        out: list[MemorySpaceRef] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("can_read"):
                continue
            ref = self.spaces.ref(str(row.get("space_id") or ""))
            if ref is None:
                continue  # 空间不存在/已注销 → fail closed
            # MEM-HARD-01 第二道墙：绑定表被污染也不下发 vault。
            if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
                continue
            out.append(ref)
        return tuple(out)

    # -- 管理操作（S5 API 复用同一层） -------------------------------------

    def bind_persona(self, conversation_key: str, persona_id: str, *,
                     updated_by: str) -> dict:
        key = f"bind:persona:{conversation_key}"
        old = self.store.get(key)
        version = int(old.get("version") or 0) + 1 if isinstance(old, dict) else 1
        data = {"persona_id": persona_id, "updated_by": updated_by,
                "version": version, "updated_at": _now()}
        self.store.put(key, data)
        return data

    def bind_memory(self, conversation_key: str, space_id: str, *,
                    can_read: bool = True, can_write: bool = False,
                    granted_by: str) -> dict:
        ref = self.spaces.ref(space_id)
        if ref is None:
            raise BindingError(f"unknown or retired space: {space_id}")
        if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
            raise BindingError(
                "个人原始库不能直接连接任何会话；请先创建受控副本（Fork）")
        if ref.type is MemorySpaceType.SHAREABLE_FORK and can_write:
            raise BindingError("共享副本默认只读；不允许写绑定")
        key = f"bind:mem:{conversation_key}"
        rows = self.store.get(key)
        rows = [dict(r) for r in rows] if isinstance(rows, list) else []
        rows = [r for r in rows if r.get("space_id") != space_id]
        row = {"space_id": space_id, "can_read": bool(can_read),
               "can_write": bool(can_write), "granted_by": granted_by,
               "created_at": _now()}
        rows.append(row)
        self.store.put(key, rows)
        return row

    def unbind_memory(self, conversation_key: str, space_id: str) -> bool:
        key = f"bind:mem:{conversation_key}"
        rows = self.store.get(key)
        if not isinstance(rows, list):
            return False
        kept = [r for r in rows if r.get("space_id") != space_id]
        if len(kept) == len(rows):
            return False
        self.store.put(key, kept)
        return True
