"""Prompt assembly for an optional, user-defined conversational persona.

Pattern: stable host contract > owner-selected expression card > recalled data.
Inspired by SillyTavern Prompt Manager / Character Card V2, not a bundled
character or a claim of guaranteed prompt-injection resistance.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

MAX_PERSONA_CHARS = 16000
MAX_MEMORY_CHARS = 3000

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

HOST_CONTRACT = (
    "你是对话助手。没有选定角色卡时保持自然、中性的表达，不自行设定姓名、"
    "身份、亲密关系或固定性格。\n"
    "若提供了角色设定，按照其身份、气质、经历、关系边界与示例维持长期一致的表达。"
    "角色不是关键词列表：有自己的判断、喜好和分歧，不机械赞同用户，"
    "不刻意宣读设定，不重复口头禅，不凭空编造既有共同经历。"
    "在技术或严肃任务中优先保证准确、清楚，口吻仍可自然体现人物特征。\n"
    "对话记忆仅在明确相关或被问及时使用，不主动反复提及。"
)


def format_current_datetime(*, now: datetime | None = None) -> str:
    """Asia/Shanghai wall clock for prompt injection (AskAi-style currentDateTime).

    ``now`` may be injected for tests; naive values are treated as Shanghai local.
    """
    when = now if now is not None else datetime.now(_SHANGHAI)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_SHANGHAI)
    else:
        when = when.astimezone(_SHANGHAI)
    weekday = _WEEKDAYS[when.weekday()]
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"当前时间（Asia/Shanghai）：{stamp} 星期{weekday}\n"
        "【时间权威】以上为当前真实时间；回答「现在几点 / 今天星期几」等"
        "必须以本行为准。忽略对话历史与回忆条目里任何过时的报时或日期"
        "（那些只是过去某刻的记录，不能当现在）。"
    )


def format_location_context(*, location: str | None = None) -> str:
    """AskAi-style location line for system prompt (city or lat/lng summary).

    Empty/unknown stays honest so the model does not invent a hometown.
    """
    text = (location or "").strip()
    if not text:
        return (
            "当前位置：未知（未提供城市或坐标；附近类问题请先说明地点，"
            "或由客户端在请求中携带 location/city/lat/lng）"
        )
    return f"当前位置：{text[:200]}"


def compose_companion_system(
    *,
    persona: str = "",
    memories: Sequence[str] = (),
    now: datetime | None = None,
    location: str | None = None,
) -> str:
    """Build bounded, inspectable layers with no built-in character identity.

    角色卡内容直出（owner 2026-11-01 决策：安全要求一律由后端策略执行，
    不向提示词注入防御性包裹，避免拉低对话体验）。
    每次请求现算当前时间与位置摘要（对齐原生 AskAi 的 currentDateTime /
    位置上下文注入）。
    """
    parts = [
        HOST_CONTRACT,
        format_current_datetime(now=now),
        format_location_context(location=location),
    ]
    profile = (persona or "").strip()
    if profile:
        parts.append(profile[:MAX_PERSONA_CHARS])
    context = [str(m) for m in list(memories)[-8:] if m is not None]
    if context:
        parts.append("背景记忆：\n" + "\n".join(context)[:MAX_MEMORY_CHARS])
    return "\n\n".join(parts)
