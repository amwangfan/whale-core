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
# 气泡分隔：任何 ◆ 一律视为分隔（独占一行/行首内联/句中夹带都认）。
# ◆ 不是正常中文用字，模型只会从规划提示里学到它——出现即标记。
# 2026-11-01 两次修复：先放行首内联形态，再放开句中形态（生产实证
# 「我在呢。◆ 八点起」这类句中夹带也曾泄漏）。
_BUBBLE_SEP_RE = re.compile(r"\s*◆\s*")
_BLANK_LINES_RE = re.compile(r"\n{2,}")
# 2026-11-01 生产实证：模型写了两个 [[稍后补充:…]] 时只剥第一个，第二个
# 原样发进群里（owner 抓到现行）。纵深防御：气泡定稿前剥掉一切残留
# [[…]] 规划标记——[[…]] 不是正常聊天用字，出现即内部标记。
_RESIDUAL_MARK_RE = re.compile(r"\[\[[^\[\]\n]{1,60}\]\]")
# 媒体引用净化（2026-11-01 生产实证「恶意链接」事件）：模型把工具结果里的
# 签名 OSS 链接 /api/v1/artifacts/ 本地产物引用贴进回复正文，QQ 直接弹
# 恶意链接警告且外部根本打不开。图片由系统作为真实图片消息发出，正文里的
# 媒体引用一律转成 QQ 风格的 [图片] 占位（历史/时间线里也能据此知道发过图）。
_MD_IMG_RE = re.compile(r"!\[[^\]\n]{0,60}\]\([^)\n]{1,500}\)")
_OSS_URL_RE = re.compile(
    r"https?://[^\s)\]）>\"']*(?:oss[-\w]*\.aliyuncs\.com|dashscope)[^\s)\]）>\"']*")
_ARTIFACT_REF_RE = re.compile(r"/api/v1/artifacts/[A-Za-z0-9_-]+")
_IMAGE_RUN_RE = re.compile(r"(?:\s*\[图片\]){2,}")


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_media_refs(text: str) -> str:
    b = _MD_IMG_RE.sub("[图片]", text)
    b = _OSS_URL_RE.sub("[图片]", b)
    b = _ARTIFACT_REF_RE.sub("[图片]", b)
    return _IMAGE_RUN_RE.sub("[图片]", b)


def _map_outside_fences(text: str, fn) -> str:
    """对 ``` 围栏**之外**的片段应用 fn；围栏块内容原样保留。"""
    parts = _FENCE_RE.split(text)
    fences = _FENCE_RE.findall(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        out.append(fn(part))
        if i < len(fences):
            out.append(fences[i])
    return "".join(out)


def _scrub(text: str) -> str:
    """剥内部标记 + 媒体引用（非围栏片段）。"""
    return _strip_media_refs(_RESIDUAL_MARK_RE.sub("", text))


def _tidy_bubble(text: str) -> str:
    """气泡定稿：折叠连续空行（聊天气泡里的空行读着 AI 味，owner 2026-11-01）；
    剥残留 [[…]] 规划标记与媒体引用（转 [图片]）。
    含代码围栏的气泡：围栏块内原样保留（空行/[[…]]/链接都是代码内容），
    块外照常净化；空行折叠整体跳过（无法区分空行是否在围栏内）。"""
    b = str(text or "").strip()
    if "```" in b:
        return _map_outside_fences(b, _scrub).strip()
    b = _scrub(b)
    # 剥标记可能留下的行尾空白/空行一并收拾
    b = "\n".join(line.rstrip() for line in b.split("\n"))
    return _BLANK_LINES_RE.sub("\n", b).strip()


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
    # 稍后补充：剥离**所有**标记文本（模型可能写多个，search 只剥一个会漏），
    # 第一个原因带回给调用方（调用方决定是否真有后续事件）。
    # 围栏代码块内的 [[…]] 是代码内容，不当标记处理。
    follow_up = ""
    outside = _FENCE_RE.split(raw)
    first = next((m for p in outside if (m := _FOLLOW_RE.search(p))), None)
    if first is not None:
        follow_up = first.group(1).strip()
        raw = _map_outside_fences(raw, lambda p: _FOLLOW_RE.sub("", p)).strip()
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
