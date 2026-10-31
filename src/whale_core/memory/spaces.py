"""S2：MemorySpace 元信息、ACL 与存量迁移。

空间元数据存放在 kv 区（``space:{space_id}``），InMemoryStore 与
SqliteMemoryStore 双后端天然一致，无需额外表。硬规则：

- MEM-HARD-01：``protected_original`` 永不绑定会话、永不进入召回
  （存储层已排除列举；本层在绑定/解析时再次拒绝，纵深防御）。
- Fork 只能产生``非受保护``新空间；受保护原始库只由迁移/管理管道创建。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple
from uuid import uuid4

from whale_core.life.conversation_context import MemorySpaceRef, MemorySpaceType
from whale_core.memory.store import MemoryKind, MemoryStore

DEFAULT_VAULT_ID = "vault-default"

#: Fork 允许的目标类型（永不产生新的 protected_original）。
FORKABLE_TARGET_TYPES = frozenset({
    MemorySpaceType.OWNER_WORKING_COPY,
    MemorySpaceType.GROUP_SPACE,
    MemorySpaceType.SHAREABLE_FORK,
})

#: 可作为会话写回目标的类型。
WRITABLE_TYPES = frozenset({
    MemorySpaceType.OWNER_WORKING_COPY,
    MemorySpaceType.GROUP_SPACE,
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemorySpaceError(ValueError):
    """空间操作被拒绝（类型不允许 / 空间不存在 / 已注销）。"""


class SpaceStore:
    """空间元数据 CRUD。所有写入校验类型合法性。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def _key(self, space_id: str) -> str:
        return f"space:{space_id}"

    def get(self, space_id: str) -> Optional[dict[str, Any]]:
        meta = self.store.get(self._key(space_id))
        if not isinstance(meta, Mapping) or meta.get("space_id") != space_id:
            return None
        if meta.get("retired_at"):
            return None  # 已注销空间对解析不可见（fail closed）
        return dict(meta)

    def create(
        self,
        space_type: MemorySpaceType,
        *,
        name: str,
        owner: str = "",
        space_id: str | None = None,
    ) -> MemorySpaceRef:
        if space_type is MemorySpaceType.PROTECTED_ORIGINAL:
            raise MemorySpaceError(
                "protected_original 只能由迁移管道创建，不允许普通创建")
        sid = space_id or f"sp-{uuid4().hex[:16]}"
        if self.store.get(self._key(sid)) is not None:
            raise MemorySpaceError(f"space already exists: {sid}")
        meta = {
            "space_id": sid,
            "type": space_type.value,
            "name": name[:120],
            "owner": owner,
            "version": 1,
            "created_at": _now(),
            "retired_at": None,
        }
        self.store.put(self._key(sid), meta)
        return MemorySpaceRef(sid, space_type, 1)

    def bump_version(self, space_id: str) -> int:
        meta = self.get(space_id)
        if meta is None:
            raise MemorySpaceError(f"unknown space: {space_id}")
        meta["version"] = int(meta.get("version") or 1) + 1
        self.store.put(self._key(space_id), meta)
        return meta["version"]

    def retire(self, space_id: str) -> None:
        meta = self.get(space_id)
        if meta is None:
            return
        meta["retired_at"] = _now()
        self.store.put(self._key(space_id), meta)

    def ref(self, space_id: str) -> Optional[MemorySpaceRef]:
        meta = self.get(space_id)
        if meta is None:
            return None
        try:
            stype = MemorySpaceType(str(meta.get("type")))
        except ValueError:
            return None
        return MemorySpaceRef(space_id, stype, int(meta.get("version") or 1))

    def list_spaces(self, *, include_protected: bool = False) -> Tuple[MemorySpaceRef, ...]:
        out: list[MemorySpaceRef] = []
        for key in self.store.keys():
            if not key.startswith("space:"):
                continue
            ref = self.ref(key[6:])
            if ref is None:
                continue
            if ref.type is MemorySpaceType.PROTECTED_ORIGINAL and not include_protected:
                continue
            out.append(ref)
        return tuple(sorted(out, key=lambda r: r.space_id))


OWNER_WORKING_SPACE_ID = "owner-working"


def ensure_owner_working_copy(store: MemoryStore, *, owner: str = "owner") -> MemorySpaceRef:
    """确保主人的默认工作副本存在（幂等）。

    首版策略：主人已认证的私聊/控制台会话默认获得这个工作副本（空内容、
    独立空间），vault 内容仍须经 Fork 显式复制——既不破坏 MEM-HARD-01，
    又保住主人私聊的记忆读写连续性。
    """
    spaces = SpaceStore(store)
    existing = spaces.ref(OWNER_WORKING_SPACE_ID)
    if existing is not None:
        return existing
    return spaces.create(MemorySpaceType.OWNER_WORKING_COPY,
                         name="主人工作副本", owner=owner,
                         space_id=OWNER_WORKING_SPACE_ID)


def assert_bindable(ref: MemorySpaceRef) -> None:
    """MEM-HARD-01：绑定会话前的最后关卡。"""
    if ref.type is MemorySpaceType.PROTECTED_ORIGINAL:
        raise MemorySpaceError(
            "个人原始库不能直接连接任何会话；请先创建受控副本（Fork）")


def default_write_space(
    spaces: Tuple[MemorySpaceRef, ...],
) -> str:
    """PRD §12 首版决策：写回当前授权工作空间。

    返回第一个可写空间 id；无可写空间返回空串（调用方据此跳过写入，
    fail closed——未授权会话不产生记忆沉淀）。
    """
    for ref in spaces:
        if ref.type in WRITABLE_TYPES:
            return ref.space_id
    return ""


def migrate_legacy_to_vault(
    store: MemoryStore, *, vault_id: str = DEFAULT_VAULT_ID, dry_run: bool = False
) -> dict[str, Any]:
    """把未分区（space_id=''）的存量记录收敛进受保护原始库。

    幂等；dry_run 只统计不落库。SqliteMemoryStore 打开时已自动执行
    等价迁移；本函数服务 InMemoryStore、CLI dry-run 与审计核验。
    """
    moved = 0
    for kind in MemoryKind:
        if kind is MemoryKind.PERSONA_CONTINUITY:
            continue  # F-05：人格连续性快照是全局基线人格，不收进 vault
        for rec in store.list_by_kind(kind, space_ids=("",)):
            moved += 1
            if not dry_run:
                store.put_record(replace_space(rec, vault_id))
    if not dry_run and store.get(f"space:{vault_id}") is None:
        store.put(f"space:{vault_id}", {
            "space_id": vault_id, "type": "protected_original",
            "name": "默认私人原始库", "owner": "owner",
            "version": 1, "created_at": _now(), "retired_at": None,
        })
    return {"vault_id": vault_id, "moved": moved, "dry_run": dry_run}


def replace_space(record, space_id: str):
    """返回改挂到目标空间的同记录副本（保留 key/record_id——空间内迁移）。"""
    from dataclasses import replace
    return replace(record, space_id=space_id)
