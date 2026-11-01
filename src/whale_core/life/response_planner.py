"""issue #23：ResponsePlanner —— 模型规划的自然多气泡/沉默/后续补充。

边界（与 #17/#18/#20 的分工）：
- 本模块只做**本回合回复结构规划**：一条、多气泡、沉默、稍后补充标记；
  是否该在群里开口是 #18（GroupTurnPolicy），送达/打断/幂等是 #17
  （batching/epoch/发送管线），任务完成后的续聊是 #20（Supervisor 回执）。
- 模型用轻量标记表达意图（见 PLANNING_HINT）；解析是**确定性**的，
  无标记时回落既有 ``segments.split_reply``（fallback，不是规划）。
- 「稍后补充」只是意图声明：只有调用方用服务端可信的未完成任务/事件
  关联验证后才会真的续聊；模型自己写标记不会产生任何后台行为
  （防注入：正文标记不改变权限/不创建任务）。

标记约定（注入到人格层提示，见 PLANNING_HINT）：
- 气泡分隔：单独一行 ``◆``；
- 沉默：回复以 ``[[沉默]]`` 开头（正文其余内容丢弃）；
- 稍后补充：结尾 ``[[稍后补充:原因]`` 或 ``[[稍后补充：原因]]``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

from whale_core.life.segments import split_reply

#: 人格层提示（由宿主在 QQ 会话拼入 persona 文本）。
PLANNING_HINT = (
    "回复规划：像聊天一样自然组织回复，默认一条说完；内容真是几个独立"
    "短句/动作时，可用单独一行的 ◆ 分成最多 2–3 个气泡（不要硬拆）。"
    "不想回复时，整轮只输出 [[沉默]]。确有后续事件（如在跑的任务出结果）"
    "需要晚点再补一句时，在结尾写 [[稍后补充:原因]]。"
    "不要每轮都提问、撒娇、称呼对方名字或总结对方的话——有需要才做，"
    "没有就自然结束（issue #41 反模板纪律）。"
)

SILENCE_MARK = "[[沉默]]"
_FOLLOW_RE = re.compile(r"\[\[稍后补充[:：]([^\]\n]{1,60})\]\]")
# 气泡分隔：◆ 出现在行首即视为新气泡起点——独占一行（\n◆\n）或行首后接文本
# （\n◆ 接着说）都算数，文首的 ◆ 同样生效。2026-11-01 修复：旧正则只认
# 独占一行形态，模型实际常写「◆ 接着说」，导致标记与多余空行泄漏进气泡。
_BUBBLE_SEP_RE = re.compile(r"(?:\A|\n)[ \t]*◆[ \t]*(?:\n|$)?")
_BLANK_LINES_RE = re.compile(r"\n{2,}")


def _tidy_bubble(text: str) -> str:
    """气泡定稿：折叠连续空行（聊天气泡里的空行读着 AI 味，owner 2026-11-01）；
    含代码围栏的气泡保留原始空行（``` 块内空行是代码格式的一部分）。"""
    b = str(text or "").strip()
    if "```" in b:
        return b
    return _BLANK_LINES_RE.sub("\n", b)


@dataclass(frozen=True)
class ReplyPlan:
    """一回合的发言计划。``bubbles`` 为最终要逐条发送的气泡文本。

    issue #41 结构化计划：``action``/``tone_target``/``length_target``/
    ``delay_ms`` 与文本内容分离——文本标记只表达「说/不说/稍后补充」的
    意图；语气与长度目标由 ``derive_plan_guidance`` 从关系/情绪/场合的
    确定性信号推导，模型不能自报目标（防模板化与越权）。
    """

    bubbles: Tuple[str, ...]
    silent: bool = False
    follow_up: str = ""            # 「稍后补充」的原因声明（需服务端验证才生效）
    planner: str = "fallback"      # model（◆ 规划）| fallback（规则拆分）| silent
    action: str = "reply"          # reply | observe | follow_up
    tone_target: str = ""          # warm | neutral | calm | ""（无目标）
    length_target: str = ""        # short | normal | ""
    delay_ms: int = 0              # 可选延迟（0 = 立即；由宿主节奏决定，不由模型）


def derive_plan_guidance(*, familiarity: str = "stranger", anxious: bool = False,
                         is_group: bool = False) -> dict:
    """从确定性信号推导语气/长度目标（issue #41；无权限语义）。

    - 焦虑信号 → calm + short（先安抚与降载，与 ExpressionPolicy 一致）；
    - 陌生人 → neutral + 默认长度；初识/熟悉 → warm；
    - 群聊 → short 倾向（不刷屏）。
    """
    if anxious:
        return {"tone_target": "calm", "length_target": "short"}
    tone = "neutral" if familiarity == "stranger" else "warm"
    length = "short" if is_group else ""
    return {"tone_target": tone, "length_target": length}


def plan_reply(text: str, *, is_group: bool, max_segments: int,
               tone_target: str = "", length_target: str = "") -> ReplyPlan:
    """把模型原始输出解析为发言计划（确定性；无标记走既有规则拆分）。"""
    def _mk(bubbles, *, silent=False, follow_up="", planner, delay_ms=0):
        action = "observe" if silent else ("follow_up" if follow_up else "reply")
        return ReplyPlan(
            tuple(b for b in (_tidy_bubble(t) for t in bubbles) if b),
            silent=silent, follow_up=follow_up, planner=planner,
            action=action, tone_target=tone_target,
            length_target=length_target, delay_ms=delay_ms)

    raw = str(text or "").strip()
    if not raw:
        return _mk((), silent=True, planner="silent")
    # 沉默标记只认开头（防正文注入伪造沉默/归属）
    if raw.startswith(SILENCE_MARK):
        return _mk((), silent=True, planner="silent")
    # 稍后补充：剥离标记文本，原因带回给调用方（调用方决定是否真有后续事件）
    follow_up = ""
    m = _FOLLOW_RE.search(raw)
    if m is not None:
        follow_up = m.group(1).strip()
        raw = (raw[:m.start()] + raw[m.end():]).strip()
    if not raw:
        return _mk((), silent=True, follow_up=follow_up, planner="silent")

    if _BUBBLE_SEP_RE.search(raw):
        parts = [p.strip() for p in _BUBBLE_SEP_RE.split(raw) if p.strip()]
        cap = max(1, int(max_segments))
        if len(parts) > cap:
            # 超出上限的尾部合并进最后一个气泡（内容不丢，不静默截断）
            parts = [*parts[:cap - 1], "\n".join(parts[cap - 1:])]
        return _mk(parts, follow_up=follow_up, planner="model")

    # 无显式规划标记：回落既有语义分段（fallback，不是模型规划）
    return _mk(split_reply(raw, is_group=is_group, max_segments=max_segments),
               follow_up=follow_up, planner="fallback")


def clean_reply_text(text: str) -> str:
    """模型原始回复 → 可落库/可展示的净文本（2026-11-01 记忆污染修复）。

    剥离 ◆ 气泡标记（转分段拼接）、[[沉默]]/[[稍后补充:…]] 规划标记；
    沉默轮返回空串（调用方据此跳过记忆/历史写入——原始标记落库会反过来
    教会模型继续内联输出标记）。
    """
    plan = plan_reply(text, is_group=False, max_segments=10)
    if plan.silent:
        return ""
    return "\n".join(plan.bubbles)
