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
from dataclasses import replace
from typing import Optional, Tuple

from whale_core.life.conversation_context import (
    ConversationKind,
    MemorySpaceRef,
    MemorySpaceType,
    principal_matches_private_conversation,
)
from whale_core.memory.spaces import MemorySpaceError, SpaceStore
from whale_core.memory.store import MemoryStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BindingError(ValueError):
    """绑定被拒绝（空间类型不允许 / 空间不存在 / 参数非法）。"""


def _assert_member_private_bindable(
        spaces: SpaceStore, space_id: str, conversation_key: str) -> None:
    """G2/MEM-03：member_private 只允许绑定到**本人已认证私聊**。

    群聊一律拒绝；私聊必须能结构性证明属于空间 owner（principal）。
    """
    ref = spaces.ref(space_id)
    if ref is None:
        raise BindingError(f"unknown or retired space: {space_id}")
    if ref.type is not MemorySpaceType.MEMBER_PRIVATE:
        return
    owner = str((spaces.get(space_id) or {}).get("owner") or "")
    parts = conversation_key.split(":", 2)
    if len(parts) != 3:
        raise BindingError("member_private 绑定需要完整 conversation_key")
    channel, kind_s, cid = parts
    try:
        kind = ConversationKind(kind_s)
    except ValueError:
        raise BindingError(f"未知会话类型 {kind_s!r}")
    if kind is ConversationKind.QQ_GROUP:
        raise BindingError("member_private 空间永不绑定群聊（MEM-03）")
    if not owner or not principal_matches_private_conversation(
            channel, kind, cid, owner):
        raise BindingError(
            "member_private 只能绑定空间所属成员本人的已认证私聊")


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

    def capabilities_for(self, conversation_key: str) -> Optional[Tuple[str, ...]]:
        """会话能力允许列表（S5 配置的 bind:caps:）。

        None = 未配置（按会话类型默认上限）；元组 = 收窄到该集合
        （只能收窄，永不放宽——S5 API 已校验上限）。
        """
        data = self.store.get(f"bind:caps:{conversation_key}")
        if isinstance(data, dict) and isinstance(data.get("scopes"), list):
            return tuple(str(s) for s in data["scopes"])
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
            out.append(replace(ref, can_write=row.get('can_write') is True))
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
        # G2：member_private 只许本人私聊（群聊在这里一律被拒）。
        _assert_member_private_bindable(self.spaces, space_id, conversation_key)
        if can_write and not can_read:
            raise BindingError("写权限必须同时授予读取权限")
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

    # -- G2：PrincipalMemoryBinding（principal → member_private 空间） ------
    #
    # 键：``member:bind:{principal_id}`` → {"space_id", "principal_id",
    # "conversation_key", "granted_by", "created_at"}。这是**按主体**的绑定
    # （而非 group_id → space 或 nickname → space）；入口只能是管理通道或
    # 本人私聊流程，且 conversation_key 必须可证明属于该成员本人私聊。

    def member_binding_for(self, principal_id: str) -> Optional[dict]:
        data = self.store.get(f"member:bind:{principal_id}")
        if isinstance(data, dict) and data.get("principal_id") == principal_id \
                and data.get("space_id"):
            return dict(data)
        return None

    def bind_member_space(self, principal_id: str, space_id: str, *,
                          conversation_key: str, granted_by: str) -> dict:
        """建立 principal → member_private 空间绑定（校验空间类型与私聊归属）。"""
        if not principal_id or not space_id:
            raise BindingError("principal_id 与 space_id 必填")
        ref = self.spaces.ref(space_id)
        if ref is None:
            raise BindingError(f"unknown or retired space: {space_id}")
        if ref.type is not MemorySpaceType.MEMBER_PRIVATE:
            raise BindingError("PrincipalMemoryBinding 只接受 member_private 空间")
        meta = self.spaces.get(space_id) or {}
        if str(meta.get("owner") or "") != principal_id:
            raise BindingError("member_private 空间 owner 与 principal 不一致")
        _assert_member_private_bindable(self.spaces, space_id, conversation_key)
        data = {"space_id": space_id, "principal_id": principal_id,
                "conversation_key": conversation_key,
                "granted_by": granted_by, "created_at": _now()}
        self.store.put(f"member:bind:{principal_id}", data)
        return data

    def unbind_member_space(self, principal_id: str) -> bool:
        key = f"member:bind:{principal_id}"
        if self.store.get(key) is None:
            return False
        self.store.delete(key)
        return True

    def ensure_member_space(self, principal_id: str, *,
                            conversation_key: str, granted_by: str) -> MemorySpaceRef:
        """确保成员的 member_private 空间与绑定存在（授权流程调用，幂等）。

        空间 id 由 principal 派生（确定性、可重建）；首次授权时创建。
        """
        import hashlib

        existing = self.member_binding_for(principal_id)
        if existing:
            ref = self.spaces.ref(str(existing["space_id"]))
            if ref is not None and ref.type is MemorySpaceType.MEMBER_PRIVATE:
                return ref
        sid = "member-" + hashlib.sha256(
            principal_id.encode("utf-8")).hexdigest()[:16]
        ref = self.spaces.ref(sid)
        if ref is None:
            try:
                ref = self.spaces.create(MemorySpaceType.MEMBER_PRIVATE,
                                         name="成员个人记忆", owner=principal_id,
                                         space_id=sid)
            except MemorySpaceError:
                # 同名旧空间已注销（kv 残留）：另起新空间，不复用残迹。
                ref = self.spaces.create(MemorySpaceType.MEMBER_PRIVATE,
                                         name="成员个人记忆", owner=principal_id)
        # G6 红队低-2：残迹分支另起了新空间——必须绑定实际空间 id，
        # 否则绑到已注销的旧 sid 抛错，该成员永远无法再授权。
        self.bind_member_space(principal_id, ref.space_id,
                               conversation_key=conversation_key,
                               granted_by=granted_by)
        return ref
