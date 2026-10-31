"""跨设备 owner 身份域与会话绑定（G2）。

目标：网页、QQ、VoIP、手机、眼镜是**同一逻辑 owner** 的多个渠道，
而不是各自为政的身份。本模块只做显式绑定与解析，绝不从昵称/行为
推断身份（与 channels.OwnerChannelRegistry 的配对原则一致）：

- ``bind``：渠道账号 ↔ owner 域（显式配对，宿主侧完成认证后调用）；
- ``bind_session``：一次通话/网页会话 ↔ owner 域 + 该会话被授权的
  范围（scopes，如 ``("task_progress",)``、``("reminders",)``）——
  跨渠道查询任务进度时按 scope 过滤，不跨权限域泄漏私有上下文；
- 全部可撤销（unbind/revoke），删除即真删。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

_PREFIX = "identity:"
_MAX_BINDINGS_PER_OWNER = 32


@dataclass(frozen=True)
class SessionBinding:
    """一次会话与 owner 域的绑定及授权范围。"""

    session_id: str
    owner_id: str
    channel: str
    scopes: tuple[str, ...] = ()
    created_at: float = 0.0

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class OwnerRegistry:
    """显式配对表：渠道账号 → owner 域；会话 → owner 域 + scopes。"""

    def __init__(self, store) -> None:
        self.store = store

    # ---- 渠道账号绑定 ----

    def bind(self, owner_id: str, channel: str, channel_user_id: str) -> None:
        owner_id, channel, channel_user_id = (
            owner_id.strip(), channel.strip(), channel_user_id.strip())
        if not owner_id or not channel or not channel_user_id:
            raise ValueError("owner_id/channel/channel_user_id 均不能为空")
        key = f"{_PREFIX}channel:{channel}:{channel_user_id}"
        existing = self.store.get(key)
        if isinstance(existing, dict) and existing.get("owner_id") != owner_id:
            raise ValueError("该渠道账号已绑定到其它 owner（先解绑再改绑）")
        bindings = self.channels_for(owner_id)
        if len(bindings) >= _MAX_BINDINGS_PER_OWNER and existing is None:
            raise ValueError("单个 owner 的渠道绑定数量超限")
        self.store.put(key, {"owner_id": owner_id, "channel": channel,
                             "channel_user_id": channel_user_id,
                             "bound_at": time.time()})

    def unbind(self, channel: str, channel_user_id: str) -> bool:
        key = f"{_PREFIX}channel:{channel}:{channel_user_id}"
        if self.store.get(key) is None:
            return False
        self.store.delete(key)
        return True

    def resolve(self, channel: str, channel_user_id: str) -> str | None:
        record = self.store.get(f"{_PREFIX}channel:{channel}:{channel_user_id}")
        return record.get("owner_id") if isinstance(record, dict) else None

    def channels_for(self, owner_id: str) -> list[dict]:
        out = []
        for key in self.store.keys():
            if not key.startswith(f"{_PREFIX}channel:"):
                continue
            record = self.store.get(key)
            if isinstance(record, dict) and record.get("owner_id") == owner_id:
                out.append(record)
        return out

    # ---- 会话绑定 ----

    def bind_session(self, session_id: str, owner_id: str, channel: str,
                     scopes=()) -> SessionBinding:
        session_id, owner_id, channel = (
            session_id.strip(), owner_id.strip(), channel.strip())
        if not session_id or not owner_id or not channel:
            raise ValueError("session_id/owner_id/channel 均不能为空")
        binding = SessionBinding(
            session_id=session_id, owner_id=owner_id, channel=channel,
            scopes=tuple(str(s) for s in scopes), created_at=time.time())
        self.store.put(f"{_PREFIX}session:{session_id}", {
            "session_id": binding.session_id, "owner_id": binding.owner_id,
            "channel": binding.channel, "scopes": list(binding.scopes),
            "created_at": binding.created_at})
        return binding

    def resolve_session(self, session_id: str) -> SessionBinding | None:
        record = self.store.get(f"{_PREFIX}session:{session_id}")
        if not isinstance(record, dict):
            return None
        return SessionBinding(
            session_id=str(record.get("session_id", "")),
            owner_id=str(record.get("owner_id", "")),
            channel=str(record.get("channel", "")),
            scopes=tuple(record.get("scopes") or ()),
            created_at=float(record.get("created_at", 0.0)))

    def revoke_session(self, session_id: str) -> bool:
        key = f"{_PREFIX}session:{session_id}"
        if self.store.get(key) is None:
            return False
        self.store.delete(key)
        return True
