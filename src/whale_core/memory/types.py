"""记忆域稳定类型（issue #26：自 life.conversation_context 下沉）。

本模块是 memory 子域的领域基石，只允许依赖标准库——memory 包任何模块
不得反向 import ``whale_core.life``（架构守卫见
``tests/test_arch_memory_boundary.py``）。

- ``MemorySpaceType`` / ``MemorySpaceRef`` 逐字迁自
  ``life.conversation_context``（枚举值 / 序列化值 / 字段一律不变；
  旧 import 路径由 conversation_context re-export 保持兼容）。
- ``principal_matches_private_conversation`` 是 G2 成员记忆归属谓词，
  随绑定层（memory.bindings）一同归属记忆域；其会话类型实参按
  ``ConversationKind`` 的**枚举值**比较，故枚举本体仍留在 life 而不必
  反向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MemorySpaceType(str, Enum):
    # MEM-HARD-01（owner 决策 2026-11-01 修订）：仅主人已认证本人私聊/管理
    # 会话可直连（见 allows_memory_read / BindingStore.owner_conversations）；
    # 其他成员、群聊、未认证会话、临时 sudo 一律不可接入。
    PROTECTED_ORIGINAL = "protected_original"
    OWNER_WORKING_COPY = "owner_working_copy"
    GROUP_SPACE = "group_space"
    SHAREABLE_FORK = "shareable_fork"
    # G2：群友个人记忆空间。按 Principal 归属，仅在本人已认证私聊召回；
    # 群聊、其他成员与未认证身份永不可读（MEM-02/03）。
    MEMBER_PRIVATE = "member_private"


@dataclass(frozen=True)
class MemorySpaceRef:
    space_id: str
    type: MemorySpaceType
    version: int  # 缓存分区键的一部分
    can_write: bool = False  # 会话绑定实际写权限；类型本身不构成授权


def _uid_to_uin(store, uid: str) -> str:
    """uid → uin 查询。与 ``life.channels.record_uid_mapping`` 共用 kv 键契约
    （``uidmap:uid:{uid}``）；memory 域不得 import life，故在此按契约直读。"""
    val = store.get(f"uidmap:uid:{uid}")
    return str(val) if isinstance(val, str) else ""


def principal_matches_private_conversation(
    channel: str,
    kind,
    conversation_id: str,
    principal_id: str,
    *,
    store=None,
) -> bool:
    """判断私聊会话是否可证明属于某 Principal（G2 成员记忆归属校验）。

    只允许结构性证据：QQ 私聊 conversation_id 形如 ``private:{uin}``，
    其 uid 必须与 principal 的渠道账号一致（``anon:{channel}:{uid}`` /
    ``paired:{channel}:{uid}``）。群会话一律 False；昵称、正文不构成证据。

    ``kind`` 接受 ``life.conversation_context.ConversationKind`` 成员或其
    线值字符串（``kind.value``；memory 不反向依赖 life，按值比较）。
    """
    kind_value = getattr(kind, "value", kind)
    if kind_value == "qq_group":  # ConversationKind.QQ_GROUP
        return False
    if kind_value not in ("qq_private", "web"):  # QQ_PRIVATE / WEB
        return False
    cid = conversation_id or ""
    uid = cid[len("private:"):] if cid.startswith("private:") else cid
    if not uid or not principal_id:
        return False
    direct = principal_id in (f"anon:{channel}:{uid}", f"paired:{channel}:{uid}")
    if direct or store is None:
        return direct
    # uid ↔ uin 映射（传输层同源证据）：会话 uid → uin 再比 principal 账号
    mapped = _uid_to_uin(store, uid)
    return bool(mapped) and principal_id in (
        f"anon:{channel}:{mapped}", f"paired:{channel}:{mapped}")
