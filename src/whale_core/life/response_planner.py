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
)

SILENCE_MARK = "[[沉默]]"
_FOLLOW_RE = re.compile(r"\[\[稍后补充[:：]([^\]\n]{1,60})\]\]")
_BUBBLE_SEP_RE = re.compile(r"\n\s*◆\s*\n")


@dataclass(frozen=True)
class ReplyPlan:
    """一回合的发言计划。``bubbles`` 为最终要逐条发送的气泡文本。"""

    bubbles: Tuple[str, ...]
    silent: bool = False
    follow_up: str = ""            # 「稍后补充」的原因声明（需服务端验证才生效）
    planner: str = "fallback"      # model（◆ 规划）| fallback（规则拆分）| silent


def plan_reply(text: str, *, is_group: bool, max_segments: int) -> ReplyPlan:
    """把模型原始输出解析为发言计划（确定性；无标记走既有规则拆分）。"""
    raw = str(text or "").strip()
    if not raw:
        return ReplyPlan((), silent=True, planner="silent")
    # 沉默标记只认开头（防正文注入伪造沉默/归属）
    if raw.startswith(SILENCE_MARK):
        return ReplyPlan((), silent=True, planner="silent")
    # 稍后补充：剥离标记文本，原因带回给调用方（调用方决定是否真有后续事件）
    follow_up = ""
    m = _FOLLOW_RE.search(raw)
    if m is not None:
        follow_up = m.group(1).strip()
        raw = (raw[:m.start()] + raw[m.end():]).strip()
    if not raw:
        return ReplyPlan((), silent=True, follow_up=follow_up, planner="silent")

    if _BUBBLE_SEP_RE.search(raw):
        parts = [p.strip() for p in _BUBBLE_SEP_RE.split(raw) if p.strip()]
        cap = max(1, int(max_segments))
        if len(parts) > cap:
            # 超出上限的尾部合并进最后一个气泡（内容不丢，不静默截断）
            parts = [*parts[:cap - 1], "\n".join(parts[cap - 1:])]
        return ReplyPlan(tuple(parts), follow_up=follow_up, planner="model")

    # 无显式规划标记：回落既有语义分段（fallback，不是模型规划）
    return ReplyPlan(
        tuple(split_reply(raw, is_group=is_group, max_segments=max_segments)),
        follow_up=follow_up, planner="fallback")
