"""issue #53 抽检预设：companion / glasses 关键路径的最小场景子集。

完整 14 场景仍是回归主集；预设只为冒烟与门禁抽检，不替代盲评全量。
场景全部合成，无私聊原文。
"""
from __future__ import annotations

from typing import Dict, Tuple

# companion 主路径：闲聊 / 话题切换 / 情绪 / 接梗 / 技术 / 收尾
COMPANION_SMOKE: Tuple[str, ...] = ("L01", "L04", "L06", "L11", "L12", "L14")

# glasses/轻量 API 更关心短回合与工具失败体面：日常 + 焦虑 + 工具失败 + 收尾
GLASSES_SMOKE: Tuple[str, ...] = ("L01", "L06", "L10", "L14")

PRESETS: Dict[str, Tuple[str, ...]] = {
    "companion": COMPANION_SMOKE,
    "glasses": GLASSES_SMOKE,
    "all": (),  # 空 = 调用方用 LIVE_SCENARIOS 全集
}


def resolve_preset(name: str) -> Tuple[str, ...] | None:
    """返回场景 id 元组；未知名 → None；all → 空元组表示全集。"""
    key = (name or "").strip().lower()
    if key not in PRESETS:
        return None
    return PRESETS[key]
