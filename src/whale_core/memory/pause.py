"""空间级「停止继续记录」开关（issue #38）。

kv ``space:paused:{space_id}`` → {"paused": bool, "at": iso, "by": str}。
自动写入路径（聊天情节沉淀、成员记忆提炼）在写入前检查；管理面
（Studio memory/pause）负责设置与审计。成员级退出仍是 consent opt-out
（既有机制，语义不同：opt-out 按主体，暂停按空间）。
"""

from __future__ import annotations

from typing import Any

PAUSED_PREFIX = "space:paused:"


def is_recording_paused(store: Any, space_id: str) -> bool:
    """该空间是否被暂停记录（自动写入路径的闸；空 space_id 永不暂停）。"""
    if not space_id:
        return False
    data = store.get(f"{PAUSED_PREFIX}{space_id}")
    return isinstance(data, dict) and data.get("paused") is True
