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

        # 先校验全部记录存在且属于源空间，再建空间——不产生残空间。
        records = [self._require_record(source_space, k) for k in record_keys]
        target = self.spaces.create(
            target_type, name=_default_target_name(target_type, source_space),
            owner=created_by)
        now = datetime.now(timezone.utc).isoformat()
        for rec in records:
            content = dict(rec.content)
            if redact is not None:
                content = dict(redact(content))
            # 派生元数据：只保留空间类型与时间，不含原记录键/摘要。
            content["fork_provenance"] = {
                "source_space_type": self._source_type(source_space),
                "forked_at": now,
            }
            forked = MemoryRecord(
                kind=rec.kind,
                key=f"sp:{target.space_id}:{uuid4().hex}",
                content=content,
                created_at=rec.created_at,
                tags=tuple(t for t in rec.tags if t != "superseded") + ("forked",),
                space_id=target.space_id,
            )
            self.store.put_record(forked)
        self.spaces.bump_version(target.space_id)
        return target.space_id

    # -- 内部 ---------------------------------------------------------------

    def _source_type(self, space_id: str) -> str:
        meta = self.spaces.get(space_id)
        return str(meta.get("type")) if meta else "unknown"

    def _require_source(self, space_id: str) -> None:
        meta = self.spaces.get(space_id)
        if meta is None:
            raise ForkError(f"unknown or retired space: {space_id}")

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
