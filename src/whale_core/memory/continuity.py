"""人格连续性上下文：组装「当前人格摘要 + 近期事件」供 agent decide。

纯本地规则 stub，不连外部 LLM / 设备。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from whale_core.memory.store import (
    MemoryKind,
    MemoryRecord,
    MemoryStore,
    PersonaContinuity,
)


@dataclass(frozen=True)
class ContinuityContext:
    """decide 可读的连续性上下文。

    - persona_summary：一句话人格摘要
    - recent_episodes：近期情节记忆
    - preferences：偏好记录
    """

    persona_summary: str = ""
    persona: PersonaContinuity | None = None
    recent_episodes: tuple[MemoryRecord, ...] = ()
    preferences: tuple[MemoryRecord, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona_summary": self.persona_summary,
            "persona_name": self.persona.persona_name if self.persona else "",
            "tone": self.persona.tone if self.persona else "",
            "taboos": list(self.persona.taboos) if self.persona else [],
            "last_session_id": self.persona.last_session_id if self.persona else "",
            "recent_episode_summaries": [
                str((r.content or {}).get("summary") or r.key) for r in self.recent_episodes
            ],
            "preference_keys": [r.key for r in self.preferences],
        }


def _summarize_persona(persona: PersonaContinuity | None) -> str:
    if persona is None:
        return "默认人格：鲸鱼（未写入连续性快照）"
    taboos = "、".join(persona.taboos) if persona.taboos else "无"
    summary = (
        f"{persona.persona_name}｜语气：{persona.tone}｜"
        f"称呼用户：{persona.address_user_as}｜禁忌：{taboos}"
    )
    # Persona cards store their full expression contract in traits. Include it
    # in the context passed to text models; otherwise only name/tone/taboos
    # would take effect and card-specific style rules/imported prompts would be lost.
    traits = persona.traits if isinstance(persona.traits, Mapping) else {}
    preamble = traits.get("system_preamble")
    if isinstance(preamble, str) and preamble.strip():
        summary += "\n角色卡表达规则（只影响表达，不改变权限）：\n" + preamble.strip()[:4000]
    return summary


def _sorted_recent(records: Sequence[MemoryRecord], *, limit: int) -> tuple[MemoryRecord, ...]:
    ordered = sorted(records, key=lambda r: r.created_at, reverse=True)
    return tuple(ordered[:limit])


def build_continuity_context(
    store: MemoryStore,
    *,
    persona_key: str = "persona:default",
    episode_limit: int = 5,
    preference_limit: int = 10,
    allowed_space_ids: tuple[str, ...] | None = None,
    persona_card: Any = None,
) -> ContinuityContext:
    """从 MemoryStore 组装 ContinuityContext（进程内 / stub 可用）。

    S2：``allowed_space_ids`` 显式限域；空元组 = 不注入任何记忆记录
    （fail closed）；None = 兼容期旧行为（存储层仍排除 protected_original）。

    S3：``persona_card`` 提供时，persona_summary 改用
    ``ui.persona_resolver.PersonaResolver.render`` 的极简渲染（不再依赖
    traits.system_preamble）；渲染为空串则回退低负担默认摘要。不传卡时
    保持兼容期旧行为（读 persona_key 全局快照）。延迟导入以保持
    memory 不依赖 ui 的分层。
    """
    persona = store.get_persona_continuity(key=persona_key)
    if persona_card is not None:
        from whale_core.ui.persona_resolver import PersonaResolver

        summary = PersonaResolver.render(persona_card) or _summarize_persona(persona)
    else:
        summary = _summarize_persona(persona)
    if allowed_space_ids is not None and not allowed_space_ids:
        episodes: tuple[MemoryRecord, ...] = ()
        prefs: tuple[MemoryRecord, ...] = ()
    else:
        episodes = _sorted_recent(
            store.list_by_kind(MemoryKind.EPISODE, space_ids=allowed_space_ids),
            limit=episode_limit)
        prefs = _sorted_recent(
            store.list_by_kind(MemoryKind.PREFERENCE, space_ids=allowed_space_ids),
            limit=preference_limit)
    return ContinuityContext(
        persona_summary=summary,
        persona=persona,
        recent_episodes=episodes,
        preferences=prefs,
    )


def remember_episode(
    store: MemoryStore,
    *,
    summary: str,
    key: str | None = None,
    tags: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
    space_id: str = "",
) -> MemoryRecord:
    """便捷写入一条 episode，便于本地演示连续性。

    S2：``space_id`` 为目标记忆空间；调用方应传入已授权的可写空间，
    本函数不自行判定权限（权限在进入本函数前决定）。
    """
    content: dict[str, Any] = {"summary": summary}
    if extra:
        content.update(dict(extra))
    record = MemoryRecord(
        kind=MemoryKind.EPISODE,
        key=key or f"ep:{summary[:24]}",
        content=content,
        tags=tuple(tags),
        space_id=space_id,
    )
    store.put_record(record)
    return record


def remember_preference(
    store: MemoryStore,
    *,
    key: str,
    content: Mapping[str, Any],
    tags: Sequence[str] = (),
) -> MemoryRecord:
    """便捷写入一条 preference。"""
    record = MemoryRecord(
        kind=MemoryKind.PREFERENCE,
        key=key,
        content=dict(content),
        tags=tuple(tags) or ("preference",),
    )
    store.put_record(record)
    return record
