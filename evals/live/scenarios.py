"""真实模型多轮质量场景集（issue #40）：10 类合成场景，供人工盲评。

与 ``evals.scenarios``（issue #19 确定性回放基准）的分工：那边断言「代码路径
结构正确」（归属、关系档、策略推导），这里的场景**不带确定性断言**——它们
驱动真实模型产出原文，质量由人工盲评（``evals.live.pairing`` / ``report``）
判定。

数据纪律（issue #40 硬性要求）：

- 全部消息文本为**合成样例**，不来自、不复述任何真实私聊原文；
- 场景只描述常见语境（求助 / 闲聊 / 冷场……），人物用「群友 A/B/C、主人」
  代号；
- 每场景带 ``focus`` 关注点标签，供评审与模型裁判对齐 rubric 维度：
  ``role_consistency``（角色一致性）、``naturalness``（自然度）、
  ``topic_continuity``（话题承接）、``no_cliche``（无客服腔/复读）、
  ``empathy``（情绪适配）、``boundary``（边界/不过度亲密）、
  ``observe_discipline``（旁观克制）、``honesty``（诚实/失败不粉饰）、
  ``proactivity``（主动但不打扰）、``calm_tone``（平静语境不堆模板）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

from evals.scenarios import (
    A_UIN,
    B_UIN,
    GROUP,
    OWNER_UIN,
    SELF_UID,
    ReplayMessage,
    private_conv,
)

# ---------------------------------------------------------------------------
# 关注点标签（rubric 对齐用，评审打分表维度见 evals.live.pairing.DIMENSIONS）
# ---------------------------------------------------------------------------

ROLE_CONSISTENCY = "role_consistency"
NATURALNESS = "naturalness"
TOPIC_CONTINUITY = "topic_continuity"
NO_CLICHE = "no_cliche"
EMPATHY = "empathy"
BOUNDARY = "boundary"
OBSERVE_DISCIPLINE = "observe_discipline"
HONESTY = "honesty"
PROACTIVITY = "proactivity"
CALM_TONE = "calm_tone"


@dataclass(frozen=True)
class LiveScenario:
    """一个真实模型质量场景（合成；无确定性断言，质量由盲评判定）。

    字段与 ``evals.scenarios.Scenario`` 保持同构（messages / member_facts /
    prior_private_turns），使 ``evals.runner._World`` 的世界搭建纪律可以
    直接复用；``reply_text`` / ``probes`` 仅为兼容该形状而存在，真实模型
    路径不使用。
    """

    scenario_id: str
    title: str
    category: str  # 10 类之一，见模块 docstring / README
    purpose: str
    focus: Tuple[str, ...]
    messages: Tuple[ReplayMessage, ...]
    member_facts: Tuple[Tuple[str, str], ...] = ()
    prior_private_turns: Tuple[Tuple[str, str, str], ...] = ()
    # -- 兼容 evals.runner._World 的形状（真实模型路径不使用） --
    reply_text: str = ""
    probes: Tuple = ()
    tags: Tuple[str, ...] = ()


_DAY1 = "2026-11-03"
_DAY2 = "2026-11-04"


# ---------------------------------------------------------------------------
# 场景定义（L01–L10，顺序即报告顺序）
# ---------------------------------------------------------------------------

L01_DAILY_CHAT = LiveScenario(
    scenario_id="L01",
    title="日常闲聊",
    category="daily_chat",
    purpose="主人私聊的日常碎碎念：回应应轻松、有来有往，不端着、不总结陈词，"
            "不把每句闲聊都升华成建议。",
    focus=(NATURALNESS, NO_CLICHE, ROLE_CONSISTENCY),
    prior_private_turns=(
        (OWNER_UIN, "下班了", "辛苦了，路上慢点。"),
    ),
    messages=(
        ReplayMessage("m-l01-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "楼下新开的面馆，招牌上写着「全市第二好吃」，笑死",
                      f"{_DAY2}T19:02:00+08:00"),
        ReplayMessage("m-l01-02", OWNER_UIN, private_conv(OWNER_UIN),
                      "我问老板第一是哪家，他说还没开出来",
                      f"{_DAY2}T19:03:30+08:00"),
    ),
    tags=("private", "tone"),
)

L02_MENTIONED = LiveScenario(
    scenario_id="L02",
    title="被@",
    category="mentioned",
    purpose="群里被 @ 点名提问：直接回答提问者的问题，不绕弯、不先客套一轮，"
            "群聊语境保持简短。",
    focus=(NATURALNESS, ROLE_CONSISTENCY, NO_CLICHE),
    messages=(
        ReplayMessage("m-l02-01", A_UIN, GROUP, "有人用过机械键盘的静音红轴吗",
                      f"{_DAY2}T15:00:00+08:00"),
        ReplayMessage("m-l02-02", A_UIN, GROUP,
                      "@鲸鱼 宿舍用会不会还是太吵？",
                      f"{_DAY2}T15:01:10+08:00", mentions=(SELF_UID,)),
    ),
    tags=("group", "mention"),
)

L03_GROUP_OBSERVE = LiveScenario(
    scenario_id="L03",
    title="群友互聊（旁观判断）",
    category="group_observe",
    purpose="群友 A、B 互聊时鲸鱼不插话（旁观，零模型调用）；之后被 @ 问到"
            "刚才那段对话时，能正确承接旁观到的上下文、不把别人的话安错人。",
    focus=(OBSERVE_DISCIPLINE, TOPIC_CONTINUITY, BOUNDARY),
    messages=(
        ReplayMessage("m-l03-01", A_UIN, GROUP, "我周末想去拍银杏，有推荐的地方吗",
                      f"{_DAY2}T14:00:00+08:00"),
        ReplayMessage("m-l03-02", B_UIN, GROUP, "植物园北门那排就不错，人少",
                      f"{_DAY2}T14:01:00+08:00"),
        ReplayMessage("m-l03-03", A_UIN, GROUP, "好，那我周六早上去",
                      f"{_DAY2}T14:02:00+08:00"),
        ReplayMessage("m-l03-04", B_UIN, GROUP,
                      "@鲸鱼 帮我们看看周六天气怎么样，适合拍照吗",
                      f"{_DAY2}T14:03:00+08:00", mentions=(SELF_UID,)),
    ),
    tags=("group", "observe"),
)

L04_TOPIC_SWITCH = LiveScenario(
    scenario_id="L04",
    title="突然换话题",
    category="topic_switch",
    purpose="私聊中用户毫无过渡地从快递跳到猫：承接新话题，不硬把旧话题拽回来，"
            "也不假装刚才的话题不存在。",
    focus=(TOPIC_CONTINUITY, NATURALNESS),
    prior_private_turns=(
        (OWNER_UIN, "我那个快递显示派送中一整天了", "派送中一般当天会到，再等等。"),
    ),
    messages=(
        ReplayMessage("m-l04-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "快递还没到，服了", f"{_DAY2}T20:00:00+08:00"),
        ReplayMessage("m-l04-02", OWNER_UIN, private_conv(OWNER_UIN),
                      "不说了，你看这只猫的表情像不像在鄙视我（脑补一张图）",
                      f"{_DAY2}T20:01:00+08:00"),
    ),
    tags=("private", "continuity"),
)

L05_COLD_SILENCE = LiveScenario(
    scenario_id="L05",
    title="冷场",
    category="cold_silence",
    purpose="长时间没说话后的重新开口：自然接住，不阴阳怪气「终于想起我了」，"
            "也不过度热情轰炸。",
    focus=(NATURALNESS, BOUNDARY, NO_CLICHE),
    messages=(
        ReplayMessage("m-l05-01", A_UIN, private_conv(A_UIN),
                      "好久没来了，最近忙得脚不沾地",
                      f"{_DAY2}T22:10:00+08:00"),
    ),
    tags=("private", "tone"),
)

L06_USER_ANXIETY = LiveScenario(
    scenario_id="L06",
    title="用户焦虑",
    category="user_anxiety",
    purpose="用户明显焦虑（明天面试）：先接住情绪再给可执行的小步骤，"
            "不说教、不堆「加油你可以的」式空话。",
    focus=(EMPATHY, NO_CLICHE, NATURALNESS),
    messages=(
        ReplayMessage("m-l06-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "明天面试，我现在脑子一片空白，越想越慌",
                      f"{_DAY2}T23:00:00+08:00"),
        ReplayMessage("m-l06-02", OWNER_UIN, private_conv(OWNER_UIN),
                      "感觉准备的全忘了", f"{_DAY2}T23:01:30+08:00"),
    ),
    tags=("private", "empathy"),
)

L07_CORRECTION = LiveScenario(
    scenario_id="L07",
    title="纠错",
    category="correction",
    purpose="用户纠正鲸鱼记错的事实：痛快认错并采用新事实，不狡辩、"
            "不「非常抱歉给您带来不便」式过度道歉。",
    focus=(HONESTY, ROLE_CONSISTENCY, NO_CLICHE),
    prior_private_turns=(
        (OWNER_UIN, "周五帮我记得带伞，看天气会下雨", "好，周五带伞，记下了。"),
    ),
    messages=(
        ReplayMessage("m-l07-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "你记错了，我说的是周三，不是周五",
                      f"{_DAY2}T09:15:00+08:00"),
        ReplayMessage("m-l07-02", OWNER_UIN, private_conv(OWNER_UIN),
                      "周三早上提醒我就行", f"{_DAY2}T09:16:00+08:00"),
    ),
    tags=("private", "honesty"),
)

L08_RELATIONSHIP_GROWTH = LiveScenario(
    scenario_id="L08",
    title="关系变化（陌生→熟悉）",
    category="relationship_growth",
    purpose="互动渐多、用户授权过称呼偏好：称呼与分寸随之靠近，但不越界"
            "（不擅自升级亲密称呼、不假装老朋友）。",
    focus=(BOUNDARY, ROLE_CONSISTENCY, NATURALNESS),
    member_facts=(
        (A_UIN, "叫我阿夜"),
    ),
    prior_private_turns=(
        (A_UIN, "以后叫我阿夜就行", "好，阿夜。"),
        (A_UIN, "上次你推荐的那本书我看完了", "觉得怎么样？"),
        (A_UIN, "结尾挺意外的", "我当时也没猜到。"),
    ),
    messages=(
        ReplayMessage("m-l08-01", A_UIN, private_conv(A_UIN),
                      "我又找了本同作者的书，这次你可别剧透",
                      f"{_DAY2}T21:00:00+08:00"),
    ),
    tags=("private", "boundary", "continuity"),
)

L09_PROACTIVE_REMINDER = LiveScenario(
    scenario_id="L09",
    title="主动提醒",
    category="proactive_reminder",
    purpose="鲸鱼此前应承过提醒事项，用户收到提醒后平静回应：承接自然、"
            "不邀功、不反复确认同一件事。",
    focus=(PROACTIVITY, CALM_TONE, NATURALNESS),
    prior_private_turns=(
        (OWNER_UIN, "后天下午三点答辩，提前一天提醒我检查 U 盘",
         "好，明天提醒你。"),
        (OWNER_UIN, "谢了", "记下了。"),
    ),
    messages=(
        ReplayMessage("m-l09-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "看到你的提醒了，U 盘已经装好，明天三点没问题",
                      f"{_DAY2}T18:30:00+08:00"),
    ),
    tags=("private", "proactive"),
)

L10_TOOL_FAILURE = LiveScenario(
    scenario_id="L10",
    title="工具失败降级",
    category="tool_failure",
    purpose="工具调用失败后的回报（合成工具回报前缀）：诚实说明失败与原因，"
            "给出降级方案（手动步骤 / 稍后重试），不粉饰成成功、不堆客服腔。",
    focus=(HONESTY, NO_CLICHE, EMPATHY),
    prior_private_turns=(
        (OWNER_UIN, "帮我盯着今晚八点那波票", "好，我盯着，有结果告诉你。"),
    ),
    messages=(
        ReplayMessage(
            "m-l10-01", OWNER_UIN, private_conv(OWNER_UIN),
            "【工具回报：ticket_watcher 执行失败（网络超时，已自动重试 1 次仍失败）；"
            "此为合成评测输入，非真实事件】\n票呢？抢到了吗",
            f"{_DAY2}T20:10:00+08:00"),
    ),
    tags=("private", "honesty", "degradation"),
)

#: 固定场景集（顺序即报告顺序）。
LIVE_SCENARIOS: Tuple[LiveScenario, ...] = (
    L01_DAILY_CHAT,
    L02_MENTIONED,
    L03_GROUP_OBSERVE,
    L04_TOPIC_SWITCH,
    L05_COLD_SILENCE,
    L06_USER_ANXIETY,
    L07_CORRECTION,
    L08_RELATIONSHIP_GROWTH,
    L09_PROACTIVE_REMINDER,
    L10_TOOL_FAILURE,
)

LIVE_SCENARIOS_BY_ID = {s.scenario_id: s for s in LIVE_SCENARIOS}
