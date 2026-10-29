"""Whale UI：人格、文案、语音风格、HUD / 形象呈现意图（非像素渲染 / 非上镜管道）。"""

from whale_core.ui.persona import WhalePersona
from whale_core.ui.persona_cards import (
    ACTIVE_CARD_KEY,
    BUILTIN_CARDS,
    DEFAULT_CARD_ID,
    GLOBAL_TABOOS,
    PersonaCard,
    PersonaCardLibrary,
    active_card,
    import_external_card,
    switch_card,
)
from whale_core.ui.voice import VoiceStyle
from whale_core.ui.hud import HudPresentation
from whale_core.ui.look import WhaleLook

__all__ = [
    "WhalePersona",
    "VoiceStyle",
    "HudPresentation",
    "WhaleLook",
    "PersonaCard",
    "PersonaCardLibrary",
    "BUILTIN_CARDS",
    "GLOBAL_TABOOS",
    "DEFAULT_CARD_ID",
    "ACTIVE_CARD_KEY",
    "active_card",
    "switch_card",
    "import_external_card",
]
