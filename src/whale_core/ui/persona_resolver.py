"""S3：会话级角色卡解析与极简人格渲染（Whale Studio）。

职责（PRD §4 / goal-chain S3）：

- ``PersonaResolver.card_for``：按 ResolvedContext.persona_id 解析会话绑定卡；
  未绑定或绑定 id 失效时 fail soft 回退 kv ``persona:active_card`` → 内置
  default，任何解析失败都不阻塞对话（只记录，不抛给聊天链路）。
- ``PersonaResolver.render``：**极简渲染**——仅 name / summary / temperament /
  relationship_style / voice_hint 的自然语言拼接；**不注入**权限、工具许可、
  安全规则或任何格式硬命令（INV-07）。卡为空 → 返回空串，调用方用低负担默认。
- ``to_simple_card``：第三方长卡的「提炼预览」——只生成建议，永远
  ``requires_confirmation=True``，绝不自动改写原卡（PRD §4.3）。

本模块没有任何权限读取路径：卡文本只是表达层数据，不改变 scopes / 记忆绑定 /
审批（INV-07 由解析与渲染的纯函数性保证）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from whale_core.memory.store import MemoryStore
from whale_core.ui.persona_cards import (
    DEFAULT_CARD_ID,
    PersonaCard,
    PersonaCardLibrary,
    _load_stored_card,
    active_card,
)

logger = logging.getLogger(__name__)

#: 极简渲染各字段的截断上限（PRD §4.1：少量自然语言，非硬性长度限制）。
_MAX_FIELD_CHARS = 300


def _clip(text: str) -> str:
    return (text or "").strip()[:_MAX_FIELD_CHARS]


class PersonaResolver:
    """会话角色卡解析器。无状态（fallbacks 仅作观测记录）。"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        library: PersonaCardLibrary | None = None,
    ) -> None:
        self.store = store
        self.library = library or PersonaCardLibrary()
        #: (requested_persona_id, reason)——绑定失效等回退事件的进程内记录，
        #: 便于审计/测试断言；不写入长期存储。
        self.fallbacks: list[tuple[str, str]] = []

    # -- 解析 ------------------------------------------------------------

    def card_for(
        self, ctx_persona_id: str | None, *, fallback: bool = True
    ) -> PersonaCard | None:
        """解析会话应使用的角色卡。

        优先级：会话绑定卡（内置库 → kv ``persona:card:{id}`` 导入卡）
        > kv ``persona:active_card`` 全局回退（含 env）> 内置 default。
        绑定 id 不存在 / 卡数据损坏时回退并记录（fail soft）。
        ``fallback=False`` 时不回退，未命中返回 None。
        """
        pid = (ctx_persona_id or "").strip()
        if pid:
            card = self.library.get(pid) or _load_stored_card(self.store, pid)
            if card is not None:
                return card
            self.fallbacks.append((pid, "bound card not found"))
            logger.warning(
                "persona card %r bound to conversation but not found; "
                "falling back (conversation unaffected)", pid)
            if not fallback:
                return None
        elif not fallback:
            return None
        try:
            return active_card(self.store, library=self.library)
        except Exception:  # 全局设置损坏也不阻塞对话
            self.fallbacks.append((pid, "global fallback failed; builtin default"))
            logger.exception("persona global fallback failed; using builtin default")
            return PersonaCardLibrary().require(DEFAULT_CARD_ID)

    # -- 极简渲染 ---------------------------------------------------------

    @staticmethod
    def render(card: PersonaCard | None) -> str:
        """把角色卡渲染为极简人格描述（纯自然语言，可为空串）。

        只取人格表达字段：name / summary / temperament / relationship_style /
        voice_hint；兼容旧卡时分别以 description / tone / timbre_hint 回补。
        **绝不**拼接权限、工具许可、安全规则、style_rules / external_prompt /
        taboos 等长文——那些属于旧全局快照路径或宿主策略层。
        """
        if card is None:
            return ""
        name = _clip(card.name)
        summary = _clip(card.summary or card.description)
        temperament = _clip(card.temperament or card.tone)
        relationship = _clip(card.relationship_style)
        voice = _clip(card.voice_hint or card.timbre_hint)
        parts: list[str] = []
        if name:
            parts.append(f"你是「{name}」。")
        if summary:
            parts.append(f"设定：{summary}")
        if temperament:
            parts.append(f"气质：{temperament}")
        if relationship:
            parts.append(f"相处方式：{relationship}")
        if voice:
            parts.append(f"声音气质：{voice}")
        return "\n".join(parts)

    # -- 第三方长卡提炼预览（只建议，不改写） ------------------------------

    @staticmethod
    def to_simple_card(text_or_card: str | PersonaCard) -> dict[str, Any]:
        """从第三方长卡原文（或已导入卡）提炼极简卡建议。

        返回 ``{"original_id", "suggested", "requires_confirmation": True}``；
        纯函数、启发式提取，**绝不**写存储、不改原卡——是否采纳由用户在
        审核界面确认（PRD §4.3）。
        """
        if isinstance(text_or_card, PersonaCard):
            original_id = text_or_card.card_id
            text = text_or_card.external_prompt or text_or_card.description or ""
            name = _clip(text_or_card.name)
            voice = _clip(text_or_card.voice_hint or text_or_card.timbre_hint)
        else:
            original_id = ""
            text = str(text_or_card or "")
            name = ""
            voice = ""
        lines = [
            ln.strip().lstrip("#-*＊· ").strip()
            for ln in text.splitlines()
        ]
        lines = [ln for ln in lines if ln]
        suggested = {
            "name": name or "鲸鱼",
            "summary": _clip(_first_line(lines)),
            "temperament": _clip(_pick_line(lines, ("性格", "气质", "temperament"))),
            "relationship_style": _clip(
                _pick_line(lines, ("相处", "关系", "relationship"))),
            "voice_hint": voice or "warm_neutral",
        }
        return {
            "original_id": original_id,
            "suggested": suggested,
            "requires_confirmation": True,
        }


def _first_line(lines: list[str]) -> str:
    return lines[0] if lines else ""


def _pick_line(lines: list[str], keywords: tuple[str, ...]) -> str:
    """挑第一条包含关键词的内容行（去掉「性格：」式前缀），用于预览建议。"""
    for ln in lines:
        low = ln.lower()
        if any(k in low for k in keywords):
            return re.split(r"[:：]", ln, maxsplit=1)[-1].strip() or ln
    return ""
