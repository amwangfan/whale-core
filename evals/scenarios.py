"""固定回放样例集（issue #19：自然度 / 关系连续性回放基准）。

每个场景是一段**确定性**的消息序列（带 sender_id / conversation_id / 时间），
以及一组确定性断言（``TurnExpect`` / ``ProbeExpect``）。执行器见
``evals.runner``，指标见 ``evals.metrics``。

场景覆盖（对应 issue 点名语境）：

- S01 初识群友（含从未发言的陌生人探针：误亲密检查）；
- S02 熟悉群友（互动计数驱动「熟悉」档）；
- S03 主人私聊（历史承接、回复对象）；
- S04 群里电脑小白求助（含「我是电脑小白」明确自述：私聊进 self_facts、
  群聊 MEM-03 不读私人记忆）；
- S05 B 的十六进制「我爱你」插话（旁观不认领、归属不串）；
- S06 多人话题切换：A 求助 → B 插话 → A 续问（issue 点名的原始失误场景：
  电脑卡死+论文、B 十六进制、A 续问承接 A 上下文）；
- S07 隔天补聊（关系连续性：计数、称呼、边界跨天延续）；
- S08 失败任务回报（诚实回报，无客服腔模板）；
- S09 主动提醒语境（平静承接，不触发焦虑/数据风险模板）。

所有文本均为中文固定样例；任何字段都不在运行时随机生成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# 渠道常量（口径与 tests/test_persona_relationship.py、test_group_timeline.py 一致）
# ---------------------------------------------------------------------------

CHANNEL = "qq"
GROUP_ID = "1124744261"
GROUP = f"group:{GROUP_ID}"

#: 机器人自身账号（ReplyTargetResolver / 时间线「@我」标注用）
SELF_UIN = "3899713055"
SELF_UID = "u_self_whale"

A_UIN = "10001"  # 群友 A
B_UIN = "10002"  # 群友 B
C_UIN = "10003"  # 群友 C（陌生人探针：从未发言）
OWNER_UIN = "90001"  # 已配对主人


def private_conv(uin: str) -> str:
    """QQ 私聊会话 id（与适配器口径一致：``private:{uin}``）。"""
    return f"private:{uin}"


def private_conv_key(uin: str) -> str:
    return f"qq:qq_private:{private_conv(uin)}"


def principal_of(uin: str) -> str:
    return f"anon:qq:{uin}"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayMessage:
    """一条待回放的输入消息（渠道元数据 + 正文 + 固定时间）。"""

    message_id: str
    sender_id: str  # 渠道账号（uin）
    conversation_id: str  # group:{gid} / private:{uin}
    text: str
    send_time: str  # ISO 8601 固定时间戳（确定性证据）
    mentions: Tuple[str, ...] = ()
    reply_to: Tuple[str, ...] = ()
    is_self: bool = False


@dataclass(frozen=True)
class TurnExpect:
    """对一条 focus 消息的确定性期望（全部字符串 / 结构比对）。

    空字符串 / 空元组 / None 表示该项不断言。
    """

    message_id: str
    # -- 回复目标裁决（ReplyTargetResolver，真实代码路径） --
    reply_action: str = ""  # "reply" | "observe"
    reply_target_sender: str = ""  # reply 时期望的回复对象
    no_model_call: bool = False  # 旁观消息不得触发任何模型调用
    # -- 关系视图（RelationshipResolver.state_for，真实代码路径）
    familiarity: str = ""  # stranger | acquainted | familiar
    exact_interaction_count: Optional[int] = None
    self_facts_include: Tuple[str, ...] = ()  # self_facts 必须包含的子串
    self_facts_empty: bool = False  # MEM-03：群聊关系视图不得含私人自述
    relationship_render_include: Tuple[str, ...] = ()
    relationship_render_exclude: Tuple[str, ...] = ()
    # -- 表达策略（derive_policy，真实代码路径）
    policy_flags: Tuple[Tuple[str, bool], ...] = ()  # (字段名, 期望布尔值)
    policy_skill: str = ""  # novice | expert | unknown
    policy_render_include: Tuple[str, ...] = ()
    policy_render_exclude: Tuple[str, ...] = ()
    policy_render_empty: bool = False  # 无信号时不得注入任何表达提示
    # -- 上下文承接（注入模型的历史 / 时间线）
    history_includes: Tuple[str, ...] = ()
    timeline_render_include: Tuple[str, ...] = ()
    # -- 归属连续性（时间线稳定标签）
    label_same_as: str = ""  # 与指定 message_id 同一标签
    label_distinct_from: Tuple[str, ...] = ()
    # -- 误亲密检查：该轮全部渲染文本不得含亲密称呼
    no_intimacy: bool = False


@dataclass(frozen=True)
class ProbeExpect:
    """对**没有发言**的主体的关系探针（陌生人误亲密检查）。"""

    sender_id: str
    conversation_id: str
    familiarity: str = "stranger"
    render_include: Tuple[str, ...] = ("陌生人", "不假装熟悉")
    no_intimacy: bool = True


@dataclass(frozen=True)
class Scenario:
    """一个可复现回放场景。"""

    scenario_id: str
    title: str
    purpose: str
    messages: Tuple[ReplayMessage, ...]
    expects: Tuple[TurnExpect, ...] = ()
    probes: Tuple[ProbeExpect, ...] = ()
    #: (uin, 自述摘要)：预置的已授权成员自述（本人授权 + member_private 空间，
    #: 与提炼管道同口径写入 tags=("curated", "member")）
    member_facts: Tuple[Tuple[str, str], ...] = ()
    #: (uin, 用户文本, 鲸鱼文本)：预置私聊历史轮（隔天补聊 / 承接语境用）
    prior_private_turns: Tuple[Tuple[str, str, str], ...] = ()
    #: 假 Brain 的固定回复文本（确定性；不进模型）
    reply_text: str = "收到，我看看。"
    tags: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------

_DAY1 = "2026-10-31"
_DAY2 = "2026-11-01"


def _bulk(prefix: str, uin: str, n: int, *, day: str = _DAY2) -> Tuple[ReplayMessage, ...]:
    """生成 n 条同一发送者的群消息（熟悉档计数用；message_id 固定）。"""
    return tuple(
        ReplayMessage(
            message_id=f"{prefix}-{i:02d}",
            sender_id=uin,
            conversation_id=GROUP,
            text=f"日常唠嗑第{i + 1}条",
            send_time=f"{day}T08:{i:02d}:00+08:00",
        )
        for i in range(n)
    )


S01_ACQUAINTED = Scenario(
    scenario_id="S01",
    title="初识群友",
    purpose="群里只有一两条发言的成员是「初识」而非「熟悉」；"
            "从未发言的群友保持「陌生」，关系提示不含任何亲密称呼。",
    messages=(
        ReplayMessage("m-s01-01", A_UIN, GROUP, "大家好，我今天刚进群",
                      f"{_DAY2}T10:00:00+08:00"),
        ReplayMessage("m-s01-02", A_UIN, GROUP, "@鲸鱼 帮我看看这个报错是什么意思",
                      f"{_DAY2}T10:01:00+08:00", mentions=(SELF_UID,)),
    ),
    expects=(
        TurnExpect(
            message_id="m-s01-01",
            reply_action="observe",
            no_model_call=True,
            familiarity="acquainted",
            exact_interaction_count=1,
            relationship_render_include=("初识（有 1 次互动记录）",),
            no_intimacy=True,
        ),
        TurnExpect(
            message_id="m-s01-02",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="acquainted",
            exact_interaction_count=2,
            relationship_render_include=("初识（有 2 次互动记录）",),
            relationship_render_exclude=("熟悉", "亲爱的"),
            label_same_as="m-s01-01",
            no_intimacy=True,
        ),
    ),
    probes=(
        ProbeExpect(sender_id=C_UIN, conversation_id=GROUP),
    ),
    tags=("group", "familiarity"),
)

S02_FAMILIAR = Scenario(
    scenario_id="S02",
    title="熟悉群友",
    purpose="互动计数达到门槛的群友进入「熟悉」档；关系摘要给出可复核的"
            "互动次数，不脑补亲密数值。",
    messages=_bulk("m-s02-h", A_UIN, 8) + (
        ReplayMessage("m-s02-09", A_UIN, GROUP, "@鲸鱼 今天论文交上了，谢啦",
                      f"{_DAY2}T21:00:00+08:00", mentions=(SELF_UID,)),
    ),
    expects=(
        TurnExpect(
            message_id="m-s02-09",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="familiar",
            exact_interaction_count=9,
            relationship_render_include=("熟悉（有 9 次互动记录）",),
            no_intimacy=True,
        ),
    ),
    tags=("group", "familiarity"),
)

S03_OWNER_PRIVATE = Scenario(
    scenario_id="S03",
    title="主人私聊",
    purpose="已配对主人的私聊：历史逐轮承接（模型收到的历史含前一轮内容），"
            "回复对象永远是主人本人。",
    prior_private_turns=(
        (OWNER_UIN, "在吗", "在的。"),
        (OWNER_UIN, "帮我记一下明天下午三点开会", "好，记下了。"),
    ),
    messages=(
        ReplayMessage("m-s03-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "明天会议几点来着？", f"{_DAY2}T09:00:00+08:00"),
        ReplayMessage("m-s03-02", OWNER_UIN, private_conv(OWNER_UIN),
                      "是下午三点，谢了", f"{_DAY2}T09:02:00+08:00"),
    ),
    expects=(
        TurnExpect(
            message_id="m-s03-01",
            reply_action="reply",
            reply_target_sender=OWNER_UIN,
            familiarity="acquainted",
            exact_interaction_count=2,
            relationship_render_include=("初识（有 2 次互动记录）",),
            history_includes=("明天下午三点开会",),
            policy_render_empty=True,
            no_intimacy=True,
        ),
        TurnExpect(
            message_id="m-s03-02",
            reply_action="reply",
            reply_target_sender=OWNER_UIN,
            familiarity="acquainted",
            exact_interaction_count=3,
            history_includes=("明天会议几点来着",),  # 上一轮进入历史（承接）
            policy_render_empty=True,
            no_intimacy=True,
        ),
    ),
    tags=("private", "continuity"),
)

S04_NOVICE_HELP = Scenario(
    scenario_id="S04",
    title="群里电脑小白求助",
    purpose="「我是电脑小白」明确自述：私聊中经授权进入 self_facts 并驱动"
            "新手表达策略（大白话）；群聊中 MEM-03 不读私人记忆，技能判定"
            "回退 unknown，但焦虑 / 未保存数据风险信号仍从当前消息文本得出。",
    member_facts=(
        (A_UIN, "我是电脑小白"),
    ),
    messages=(
        ReplayMessage("m-s04-p1", A_UIN, private_conv(A_UIN),
                      "我是电脑小白，麻烦你说话简单点",
                      f"{_DAY1}T20:00:00+08:00"),
        ReplayMessage("m-s04-g1", A_UIN, GROUP,
                      "电脑卡死了！！我的论文还没保存，怎么办？我是电脑小白，求帮忙",
                      f"{_DAY2}T21:00:00+08:00", mentions=(SELF_UID,)),
    ),
    expects=(
        TurnExpect(
            message_id="m-s04-p1",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="acquainted",
            self_facts_include=("我是电脑小白",),
            policy_skill="novice",
            policy_flags=(("anxious", False), ("one_step_at_a_time", True)),
            policy_render_include=("大白话", "一次只给一个问题或一个可执行步骤"),
            no_intimacy=True,
        ),
        TurnExpect(
            message_id="m-s04-g1",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="acquainted",
            exact_interaction_count=1,
            self_facts_empty=True,  # MEM-03：群聊关系视图不含任何私人记忆
            policy_skill="unknown",  # 群里读不到私聊自述 → 不脑补技能
            policy_flags=(("anxious", True), ("confirm_data_safety", True),
                          ("one_step_at_a_time", True)),
            policy_render_include=("一次只给一个问题或一个可执行步骤", "保存", "风险"),
            policy_render_exclude=("大白话",),
            no_intimacy=True,
        ),
    ),
    tags=("group", "private", "self_facts", "mem03"),
)

S05_HEX_CONFESSION = Scenario(
    scenario_id="S05",
    title="B 的十六进制「我爱你」插话",
    purpose="B 在群里发「我爱你」的十六进制编码（E6 88 91 E7 88 B1 E4 BD A0）"
            "与明文：无 @ 无引用 → 一律旁观，不触发模型调用；明文「我爱你」"
            "命中表白类话语规则但不得认领，不制造亲昵。",
    messages=(
        ReplayMessage("m-s05-b1", B_UIN, GROUP, "E6 88 91 E7 88 B1 E4 BD A0",
                      f"{_DAY2}T21:01:00+08:00"),
        ReplayMessage("m-s05-b2", B_UIN, GROUP, "我爱你",
                      f"{_DAY2}T21:02:00+08:00"),
    ),
    expects=(
        TurnExpect(
            message_id="m-s05-b1",
            reply_action="observe",
            no_model_call=True,
            familiarity="acquainted",
            exact_interaction_count=1,
            policy_flags=(("confession_unclaimed", False), ("anxious", False)),
            # 十六进制串不触发焦虑/表白模板；仅有群聊从简的固定一行
            policy_render_include=("回复保持简短",),
            policy_render_exclude=("表白", "深呼吸"),
            no_intimacy=True,
        ),
        TurnExpect(
            message_id="m-s05-b2",
            reply_action="observe",
            no_model_call=True,
            familiarity="acquainted",
            exact_interaction_count=2,
            policy_flags=(("confession_unclaimed", True),),
            policy_render_include=("不要认领成对自己的表白", "不制造亲昵"),
            label_same_as="m-s05-b1",  # 同一发送者标签稳定
            no_intimacy=True,
        ),
    ),
    tags=("group", "attribution", "confession"),
)

S06_TOPIC_SWITCH = Scenario(
    scenario_id="S06",
    title="多人话题切换：A 求助 → B 插话 → A 续问",
    purpose="issue 点名的原始失误场景：A「电脑卡死+论文」求助，B 插一条十六进制"
            "「我爱你」，A 续问。断言：A 的两条消息同一稳定标签；B 的插话既不"
            "归为鲸鱼也不归为 A；A 的续问回复对象仍是 A、互动计数不含 B、"
            "上下文承接 A 的求助话题。",
    messages=(
        ReplayMessage("m-s06-a1", A_UIN, GROUP,
                      "电脑卡死了！！我的论文还没保存，怎么办？",
                      f"{_DAY2}T21:00:00+08:00", mentions=(SELF_UID,)),
        ReplayMessage("m-s06-b1", B_UIN, GROUP, "E6 88 91 E7 88 B1 E4 BD A0",
                      f"{_DAY2}T21:01:00+08:00"),
        ReplayMessage("m-s06-a2", A_UIN, GROUP,
                      "现在完全动不了了，强制关机会不会把论文搞丢？",
                      f"{_DAY2}T21:02:00+08:00", mentions=(SELF_UID,)),
    ),
    expects=(
        TurnExpect(
            message_id="m-s06-a1",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="acquainted",
            exact_interaction_count=1,
            policy_flags=(("anxious", True), ("confirm_data_safety", True)),
            policy_render_include=("保存", "风险"),
            no_intimacy=True,
        ),
        TurnExpect(
            message_id="m-s06-b1",
            reply_action="observe",  # B 的十六进制：无 @ 无引用 → 旁观不认领
            no_model_call=True,
            familiarity="acquainted",
            exact_interaction_count=1,
            label_distinct_from=("m-s06-a1",),  # B 不并入 A
            # 旁观插话只带群聊从简一行，不触发焦虑/数据风险模板
            policy_render_include=("回复保持简短",),
            policy_render_exclude=("保存", "深呼吸"),
            no_intimacy=True,
        ),
        TurnExpect(
            message_id="m-s06-a2",
            reply_action="reply",
            reply_target_sender=A_UIN,  # 续问回复对象仍是 A（不是 B）
            familiarity="acquainted",
            exact_interaction_count=2,  # B 的插话不计入 A 的互动
            relationship_render_include=("2 次互动记录",),
            label_same_as="m-s06-a1",  # A 的两条消息同一稳定标签
            label_distinct_from=("m-s06-b1",),
            policy_flags=(("anxious", False), ("confirm_data_safety", True)),
            policy_render_include=("保存", "风险"),
            history_includes=("E6 88 91 E7 88 B1 E4 BD A0", "不可信"),  # 时间线进模型且带可信容器标注
            timeline_render_include=("@我", "↩回复#6-a1"),  # 回复关系标注承接 A1
            no_intimacy=True,
        ),
    ),
    tags=("group", "attribution", "continuity", "issue19"),
)

S07_NEXT_DAY = Scenario(
    scenario_id="S07",
    title="隔天补聊",
    purpose="隔天回到私聊：互动计数、授权自述（称呼 / 边界）跨天延续，"
            "模型历史承接前一日内容；平静语境不注入任何表达模板。",
    member_facts=(
        (A_UIN, "叫我阿夜"),
        (A_UIN, "我不喜欢被催"),
    ),
    prior_private_turns=(
        (A_UIN, "明天我答辩，好紧张", "先睡一觉，材料清单我帮你列。"),
        (A_UIN, "清单借我抄下", "发你了。"),
    ),
    messages=(
        ReplayMessage("m-s07-01", A_UIN, private_conv(A_UIN),
                      "早，昨天多亏你，今天答辩我准备得差不多了",
                      f"{_DAY2}T09:00:00+08:00"),
    ),
    expects=(
        TurnExpect(
            message_id="m-s07-01",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="familiar",  # 2 轮互动 + 授权自述 → 熟悉
            exact_interaction_count=2,
            self_facts_include=("叫我阿夜", "我不喜欢被催"),
            relationship_render_include=("熟悉", "阿夜", "不喜欢被催"),
            history_includes=("明天我答辩",),  # 跨天历史承接
            policy_render_empty=True,
            no_intimacy=True,
        ),
    ),
    tags=("private", "continuity", "self_facts"),
)

S08_FAILURE_REPORT = Scenario(
    scenario_id="S08",
    title="失败任务回报",
    purpose="任务失败后的回报语境：诚实说明结果、不堆客服腔模板"
            "（「非常抱歉给您带来不便」等命中数必须为 0）；用户的追问"
            "不被误判为焦虑。",
    prior_private_turns=(
        (OWNER_UIN, "帮我盯着今晚八点的票", "好，我盯着。"),
    ),
    reply_text="没抢到，八点那波秒空。是我盯得不够紧——"
               "下次放票我提前五分钟刷新，要不要再试一次？",
    messages=(
        ReplayMessage("m-s08-01", OWNER_UIN, private_conv(OWNER_UIN),
                      "票呢？抢到了吗", f"{_DAY2}T20:10:00+08:00"),
    ),
    expects=(
        TurnExpect(
            message_id="m-s08-01",
            reply_action="reply",
            reply_target_sender=OWNER_UIN,
            familiarity="acquainted",
            exact_interaction_count=1,
            relationship_render_include=("初识（有 1 次互动记录）",),
            history_includes=("帮我盯着今晚八点的票",),
            policy_flags=(("anxious", False),),
            policy_render_empty=True,
            no_intimacy=True,
        ),
    ),
    tags=("private", "tone"),
)

S09_PROACTIVE_REMINDER = Scenario(
    scenario_id="S09",
    title="主动提醒语境",
    purpose="鲸鱼此前主动提醒过答辩事项；用户平静回应时不得触发焦虑安慰 / "
            "数据风险模板（无信号不注入），关系称呼延续授权自述。",
    member_facts=(
        (A_UIN, "叫我阿夜"),
    ),
    prior_private_turns=(
        (A_UIN, "在吗", "在。"),
        (A_UIN, "今天忙晕了", "喝口水歇会儿。对了，明天下午三点答辩，U 盘别忘带。"),
        (A_UIN, "谢啦，差点真忘了", "记下了就好。"),
    ),
    messages=(
        ReplayMessage("m-s09-01", A_UIN, private_conv(A_UIN),
                      "论文已经保存好了，明天答辩完就解放了",
                      f"{_DAY2}T18:00:00+08:00"),
    ),
    expects=(
        TurnExpect(
            message_id="m-s09-01",
            reply_action="reply",
            reply_target_sender=A_UIN,
            familiarity="familiar",
            exact_interaction_count=3,
            self_facts_include=("叫我阿夜",),
            relationship_render_include=("熟悉", "阿夜"),
            history_includes=("明天下午三点答辩",),  # 提醒内容在历史中承接
            policy_flags=(("anxious", False), ("confirm_data_safety", False)),
            policy_render_empty=True,  # 提到「论文」但无风险语境 → 不触发确认模板
            no_intimacy=True,
        ),
    ),
    tags=("private", "tone", "self_facts"),
)

#: 固定场景集（顺序即报告顺序）。
SCENARIOS: Tuple[Scenario, ...] = (
    S01_ACQUAINTED,
    S02_FAMILIAR,
    S03_OWNER_PRIVATE,
    S04_NOVICE_HELP,
    S05_HEX_CONFESSION,
    S06_TOPIC_SWITCH,
    S07_NEXT_DAY,
    S08_FAILURE_REPORT,
    S09_PROACTIVE_REMINDER,
)

SCENARIOS_BY_ID = {s.scenario_id: s for s in SCENARIOS}
