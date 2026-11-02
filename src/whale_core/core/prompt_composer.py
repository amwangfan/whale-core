"""Prompt assembly for an optional, user-defined conversational persona.

Pattern: stable host contract > owner-selected expression card > recalled data.
Inspired by SillyTavern Prompt Manager / Character Card V2, not a bundled
character or a claim of guaranteed prompt-injection resistance.
"""
from __future__ import annotations

from typing import Sequence

MAX_PERSONA_CHARS = 16000
MAX_MEMORY_CHARS = 3000

HOST_CONTRACT = (
    "你是对话助手。没有选定角色卡时保持自然、中性的表达，不自行设定姓名、"
    "身份、亲密关系或固定性格。\n"
    "若提供了角色设定，按照其身份、气质、经历、关系边界与示例维持长期一致的表达。"
    "角色不是关键词列表：有自己的判断、喜好和分歧，不机械赞同用户，"
    "不刻意宣读设定，不重复口头禅，不凭空编造既有共同经历。"
    "在技术或严肃任务中优先保证准确、清楚，口吻仍可自然体现人物特征。\n"
    "对话记忆仅在明确相关或被问及时使用，不主动反复提及。"
)


def compose_companion_system(*, persona: str = "", memories: Sequence[str] = ()) -> str:
    """Build bounded, inspectable layers with no built-in character identity.

    角色卡内容直出（owner 2026-11-01 决策：安全要求一律由后端策略执行，
    不向提示词注入防御性包裹，避免拉低对话体验）。
    """
    parts = [HOST_CONTRACT]
    profile = (persona or "").strip()
    if profile:
        parts.append(profile[:MAX_PERSONA_CHARS])
    context = [str(m) for m in list(memories)[-8:] if m is not None]
    if context:
        parts.append("背景记忆：\n" + "\n".join(context)[:MAX_MEMORY_CHARS])
    return "\n\n".join(parts)
