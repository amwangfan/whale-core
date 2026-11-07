"""IM 私聊 ↔ core 会话绑定（规划 §7 会话路由）。

- 一个 ``target_id`` 绑定一个 ``conversation_id``；首次出现时自动建立；
- ``/new`` 由 ``reset`` 重建绑定（旧 conversation_id 不再被该 target 引用，
  其生命周期归 core 会话侧管理，本模块不删除）；
- 群聊消息同样可绑定（群聊可接收通知），但审批命令由 ApprovalBridge
  拒绝（见 approval_bridge.py）。
- 存储是接口（SessionBindingStore），默认内存实现；接持久化时换成
  kv/SQLite 实现即可，路由逻辑不变。
"""
from __future__ import annotations

import threading
from typing import Callable, Protocol, runtime_checkable

from whale_core.devices.transports.base import new_id


@runtime_checkable
class SessionBindingStore(Protocol):
    """target_id → conversation_id 的存储接口。"""

    def get(self, target_id: str) -> str | None: ...
    def put(self, target_id: str, conversation_id: str) -> None: ...
    def delete(self, target_id: str) -> None: ...


class InMemorySessionBindingStore:
    """内存实现（单进程；带锁）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bindings: dict[str, str] = {}

    def get(self, target_id: str) -> str | None:
        with self._lock:
            return self._bindings.get(target_id)

    def put(self, target_id: str, conversation_id: str) -> None:
        with self._lock:
            self._bindings[target_id] = conversation_id

    def delete(self, target_id: str) -> None:
        with self._lock:
            self._bindings.pop(target_id, None)

    def snapshot(self) -> dict[str, str]:
        """只读快照（测试/观测用）。"""
        with self._lock:
            return dict(self._bindings)


class SessionRouter:
    """会话路由：resolve 按 target_id 取会话（缺则建），reset 重建。"""

    def __init__(self, store: SessionBindingStore | None = None,
                 id_factory: Callable[[], str] | None = None) -> None:
        self.store = store if store is not None else InMemorySessionBindingStore()
        self._new_conversation_id = id_factory or (lambda: new_id("conv"))

    def resolve(self, target_id: str) -> str:
        """取该 target 的 conversation_id；无绑定则建立新绑定。"""
        existing = self.store.get(target_id)
        if existing:
            return existing
        conversation_id = self._new_conversation_id()
        self.store.put(target_id, conversation_id)
        return conversation_id

    def reset(self, target_id: str) -> str:
        """``/new``：丢弃旧绑定，建立并返回新 conversation_id。"""
        conversation_id = self._new_conversation_id()
        self.store.put(target_id, conversation_id)
        return conversation_id

    def binding_for(self, target_id: str) -> str | None:
        """只查不建（观测用）。"""
        return self.store.get(target_id)
