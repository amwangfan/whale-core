"""S2：受控 Fork —— 从受保护原始库/工作副本物理复制出独立空间。

纪律（PRD §5.2，逐条落代码）：

- Fork 是**物理复制**：新记录 = 新 key + 新 record_id + 目标 space_id +
  独立内容；不是引用、视图、软链接或动态查询。
- 默认**选择性复制**：调用方显式给出记录键清单；先 preview 再 fork；
  ``confirmed=True`` 是硬门槛。
- 无自动同步：复制完成后与原库零关联（仅存派生元数据供管理追溯，
  不含原记录键——普通会话不可见私有源键）。
- 脱敏：``redact`` 回调在复制前应用于内容；preview 与 fork 用同一回调，
  保证「预览所见即所得」。
- 鉴权在调用方（S5 API / 可信管理入口验证主人身份）；本服务只做
  类型与事务正确性。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import uuid4

from whale_core.life.conversation_context import MemorySpaceType
from whale_core.memory.spaces import FORKABLE_TARGET_TYPES, MemorySpaceError, SpaceStore
from whale_core.memory.store import MemoryRecord, MemoryStore

#: redaction 回调类型：content dict → 脱敏后的 content dict
Redactor = Optional[Callable[[dict], dict]]


class ForkError(ValueError):
    """Fork 被拒绝（未确认 / 类型非法 / 记录不存在）。"""


class ForkService:
    def __init__(self, store: MemoryStore, spaces: SpaceStore):
        self.store = store
        self.spaces = spaces

    # -- 预览（可信管理界面渲染用） -----------------------------------------

    def preview(
        self,
        source_space: str,
        record_keys: tuple[str, ...],
        *,
        redact: Redactor = None,
    ) -> tuple[dict[str, Any], ...]:
        """返回将要复制的记录预览（已应用脱敏）。

        记录不存在 / 不属于源空间 → ForkError（不允许跨空间夹带）。
        """
        self._require_source(source_space)
        items = []
        for key in record_keys:
            rec = self._require_record(source_space, key)
            content = dict(rec.content)
            if redact is not None:
                content = dict(redact(content))
            items.append({
                "key": key,
                "kind": rec.kind.value,
                "tags": list(rec.tags),
                "content": content,
            })
        return tuple(items)

    # -- 执行（事务性物理复制） ---------------------------------------------

    def fork(
        self,
        source_space: str,
        record_keys: tuple[str, ...],
        *,
        target_type: MemorySpaceType,
        target_name: str,
        created_by: str,
        redact: Redactor = None,
        confirmed: bool = False,
    ) -> str:
        """复制所选记录到新空间，返回新 space_id。

        confirmed=False → 拒绝（必须先预览与明确确认）。
        """
        if not confirmed:
            raise ForkError("Fork 需要显式确认（confirmed=True）")
        if target_type not in FORKABLE_TARGET_TYPES:
            raise ForkError(f"不允许 Fork 出 {target_type.value} 类型空间")
        self._require_source(source_space)
        if not record_keys:
            raise ForkError("未选择任何记录")

        # Source lookup, content transformation, target metadata and all record
        # writes must commit as ONE unit. A failed redactor/JSON serializer
        # cannot leave a half-populated, readable memory space.
        now = datetime.now(timezone.utc).isoformat()
        target_id = f"sp-{uuid4().hex[:16]}"
        target_meta = {
            "space_id": target_id, "type": target_type.value,
            "name": (target_name or "").strip()[:120]
                    or _default_target_name(target_type, source_space),
            "owner": created_by, "version": 2,
            "created_at": now, "retired_at": None,
        }
        source_kind = self._source_type(source_space)

        def _commit(tx):
            if tx.get(f"space:{target_id}") is not None:
                raise ForkError("new memory space id collision")
            records = []
            for key in record_keys:
                rec = tx.get_record(key)
                if rec is None or rec.space_id != source_space:
                    raise ForkError("source record changed before Fork confirmation")
                content = dict(rec.content)
                if redact is not None:
                    content = dict(redact(content))
                content["fork_provenance"] = {
                    "source_space_type": source_kind,
                    "forked_at": now,
                }
                records.append(MemoryRecord(
                    kind=rec.kind,
                    key=f"sp:{target_id}:{uuid4().hex}",
                    content=content,
                    created_at=rec.created_at,
                    tags=tuple(t for t in rec.tags if t != "superseded") + ("forked",),
                    space_id=target_id,
                ))
            tx.put(f"space:{target_id}", target_meta)
            for record in records:
                tx.put_record(record)
            return target_id

        return self.store.transact(_commit)

    # -- 内部 ---------------------------------------------------------------

    def _source_type(self, space_id: str) -> str:
        meta = self.spaces.get(space_id)
        return str(meta.get("type")) if meta else "unknown"

    def _require_source(self, space_id: str) -> None:
        meta = self.spaces.get(space_id)
        if meta is None:
            raise ForkError(f"unknown or retired space: {space_id}")
        # G2/MEM-03：成员个人空间不参与 Fork——它只属于成员本人，连管理通道
        # 也不能把他人私有记忆复制成可绑定的副本。
        if meta.get("type") == MemorySpaceType.MEMBER_PRIVATE.value:
            raise ForkError("member_private 空间不参与 Fork（成员私有记忆不复制）")

    def _require_record(self, source_space: str, key: str) -> MemoryRecord:
        rec = self.store.get_record(key)
        if rec is None:
            raise ForkError(f"record not found: {key}")
        if rec.space_id != source_space:
            raise ForkError("记录不属于源空间（拒绝跨空间夹带）")
        return rec


def _default_target_name(target_type: MemorySpaceType, source_space: str) -> str:
    label = {
        MemorySpaceType.OWNER_WORKING_COPY: "主人工作副本",
        MemorySpaceType.GROUP_SPACE: "群组记忆",
        MemorySpaceType.SHAREABLE_FORK: "共享副本",
    }.get(target_type, "副本")
    return f"{label}（源自 {source_space}）"
