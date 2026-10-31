"""G4：ExpressionPolicy —— 表达深度/长度/确认策略（下一阶段计划 §6.1/§6.2）。

输入（全部是有来源的信号，不脑补）：

- 用户自述技能水平：只从已授权记忆的本人自述摘要判定（novice/expert），
  没有自述 = unknown（按普通用户对待）；
- 当前消息情绪信号：简单规则——焦虑词命中 + 感叹/问号密度；
- 群/私聊场合。

输出一小段自然语言表达指导，注入 persona 上下文。它**不是**替代人格的
第二套模型：人格仍由 PersonaCard 承担；本策略只调表达深浅与节奏，不含任何
权限语义（PER-01），且全部规则按需出现——默认情形不注入任何额外提示
（PER-02：不把安慰/亲昵/长度强制为固定模板）。

客服式表达修复（§6.2）内建为条件规则：先判断焦虑/未保存数据、一次一个
问题或步骤、不机械复读安慰、不冒认表白、危及数据的操作先确认风险。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Tuple

#: 焦虑/情绪化词汇（命中即焦虑信号）。
_ANXIETY_WORDS = (
    "怎么办", "完蛋", "救命", "急死", "焦虑", "害怕", "来不及", "崩溃",
    "哭了", "难受", "担心", "慌了", "糟了", "惨了",
)

#: 自述技能水平（只匹配本人自述句式，不匹配对他人的描述）。
_SKILL_NOVICE_RE = re.compile(
    r"我(?:是|算)?[^，。,.；;]{0,6}(?:新手|小白|门外汉|刚入门|刚学)")
_SKILL_EXPERT_RE = re.compile(
    r"我(?:是|做|干|搞)[^，。,.；;]{0,8}(?:程序员|开发者|工程师|运维|技术|开发)")

#: 数据风险语境：未保存的工作成果 + 可能危及它的设备状况。
_DATA_WORDS = ("论文", "没保存", "未保存", "文档", "稿子", "方案", "文件", "数据")
_DEVICE_TROUBLE_WORDS = (
    "卡死", "死机", "动不了", "蓝屏", "崩溃", "很卡", "卡顿", "强制关机", "重启")

#: 表白类话语（无明确发给鲸鱼的证据时不得认领——§6.2）。
_CONFESSION_RE = re.compile(r"我爱你|我喜欢你|爱你哟|表白|嫁给我|做我(?:男|女)朋友")

_PUNCT = "！!？?"


def _emotion_marks(text: str) -> int:
    return sum(text.count(c) for c in _PUNCT)


def detect_skill(self_facts: Tuple[str, ...]) -> str:
    """从已授权自述判定技能水平；无自述 = ``unknown``（不脑补）。"""
    for fact in self_facts:
        if _SKILL_EXPERT_RE.search(fact):
            return "expert"
    for fact in self_facts:
        if _SKILL_NOVICE_RE.search(fact):
            return "novice"
    return "unknown"


@dataclass(frozen=True)
class ExpressionPolicy:
    """一条输入的表达指导（全部字段都是表达层语义，无权限含义）。"""

    depth: str = "normal"                  # deep | normal | simple
    length: str = "normal"                 # short | normal
    skill: str = "unknown"                 # novice | expert | unknown
    anxious: bool = False
    one_step_at_a_time: bool = False       # 一次只给一个问题/步骤
    confirm_data_safety: bool = False      # 危及数据的操作前先确认
    confession_unclaimed: bool = False     # 表白类话语无归属证据
    is_group: bool = False
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """渲染为一小段自然语言提示；全部默认且无信号时返回空串。"""
        lines: list[str] = []
        if self.anxious:
            lines.append(
                "对方现在比较着急：先回应当下的处境（而不是直接进完整排查流程）；"
                "安慰要贴合具体处境，不要机械复读「别慌」「深呼吸」。")
        if self.one_step_at_a_time:
            lines.append(
                "一次只给一个问题或一个可执行步骤，确认对方跟上再继续。")
        if self.confirm_data_safety:
            lines.append(
                "涉及可能丢失数据的操作（如强制关机/重启）前，先确认对方是否已"
                "保存重要文件，并说明数据丢失风险。")
        if self.skill == "novice":
            lines.append("对方自述是新手：少用术语，多用大白话。")
        elif self.skill == "expert":
            lines.append("对方有专业背景：可以给出完整技术解释，不必刻意简化。")
        if self.confession_unclaimed:
            lines.append(
                "收到「我爱你」类话语但没有明确发给你的证据（@或引用）时，"
                "不要认领成对自己的表白，不制造亲昵。")
        if self.length == "short" and not self.anxious and not self.one_step_at_a_time:
            lines.append("回复保持简短。")
        if not lines:
            return ""
        return "表达提示：\n" + "\n".join(lines)


def derive_policy(
    text: str,
    *,
    is_group: bool = False,
    self_facts: Tuple[str, ...] = (),
) -> ExpressionPolicy:
    """从当前消息信号 + 已授权自述推导表达策略（纯函数，便于单测）。"""
    text = text or ""
    reasons: list[str] = []

    # 情绪信号：焦虑词命中；或感叹/问号密度高（短消息 ≥2 个，或任意 ≥4 个）。
    marks = _emotion_marks(text)
    anxious = any(w in text for w in _ANXIETY_WORDS) \
        or marks >= 4 or (marks >= 2 and len(text) <= 60)
    if anxious:
        reasons.append("anxiety_signal")

    skill = detect_skill(self_facts)
    if skill != "unknown":
        reasons.append(f"skill:{skill}")

    # 数据安全风险：提到未保存的工作成果，且处于设备故障/焦虑语境。
    confirm_data_safety = any(w in text for w in _DATA_WORDS) and (
        anxious or any(w in text for w in _DEVICE_TROUBLE_WORDS))
    if confirm_data_safety:
        reasons.append("unsaved_data_risk")

    confession = bool(_CONFESSION_RE.search(text))
    if confession:
        reasons.append("confession_like_text")

    # 深度：专家放开；新手或焦虑者从简。
    if skill == "expert":
        depth = "deep"
    elif skill == "novice" or anxious:
        depth = "simple"
    else:
        depth = "normal"

    # 一次一个步骤：新手或焦虑者（客服式修复 §6.2）。
    one_step = skill == "novice" or anxious
    if one_step:
        reasons.append("one_step")

    # 长度：焦虑 / 新手 / 群聊从简。
    if anxious or skill == "novice" or (is_group and depth != "deep"):
        length = "short"
    else:
        length = "normal"

    return ExpressionPolicy(
        depth=depth, length=length, skill=skill, anxious=anxious,
        one_step_at_a_time=one_step,
        confirm_data_safety=confirm_data_safety,
        confession_unclaimed=confession, is_group=is_group,
        reasons=tuple(reasons),
    )
