"""issue #18：GroupTurnPolicy 群聊参与决策测试。

覆盖：
- quiet/balanced/lively 三档对同一段群聊的可预期不同参与行为；
- 直呼其名 / 提问信号 / 无指向消息的判定；
- 冷却、日配额、连续发言限制、免打扰、时间静默（非定向自主插话压制）；
- 诊断 reason_code 落 kv（无私人内容）；
- 群友间私事不认领（无信号即 observe）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_core.life.group_turn import GroupTurnPolicy
from whale_core.memory.store import InMemoryStore

GROUP = "group:g1"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _policy(store, *, quiet=False, now=T0):
    return GroupTurnPolicy(store, bot_names=("鲸鱼",), now_fn=lambda: now,
                           quiet_now=lambda: False if not quiet else True)


def test_same_scene_three_modes_differ():
    """同一段群聊：quiet 不参与、balanced 对直呼其名参与、lively 对提问参与。"""
    store = InMemoryStore()
    msg_named = "鲸鱼，这个报错什么意思"
    msg_question = "这个报错什么意思？"
    msg_chat = "今晚吃啥"

    # quiet：直呼其名才参与
    p = _policy(store)
    assert p.maybe_join(conversation=GROUP, text=msg_named,
                        activity="quiet").action == "join"
    store2 = InMemoryStore()
    p2 = _policy(store2)
    assert p2.maybe_join(conversation=GROUP, text=msg_question,
                         activity="quiet").reason_code == "quiet_mode"

    # balanced：提问但不点名 → 不参与；点名 → 参与
    store3 = InMemoryStore()
    p3 = _policy(store3)
    assert p3.maybe_join(conversation=GROUP, text=msg_question,
                         activity="balanced").reason_code == "not_directed"
    assert p3.maybe_join(conversation=GROUP, text=msg_named,
                         activity="balanced").action == "join"

    # lively：一般提问即可参与
    store4 = InMemoryStore()
    p4 = _policy(store4)
    assert p4.maybe_join(conversation=GROUP, text=msg_question,
                         activity="lively").action == "join"

    # 闲聊无信号：三档都不参与（群友私事不认领）
    for mode in ("quiet", "balanced", "lively"):
        s = InMemoryStore()
        assert _policy(s).maybe_join(
            conversation=GROUP, text=msg_chat, activity=mode
        ).action == "observe"


def test_cooldown_and_daily_budget():
    store = InMemoryStore()
    p = _policy(store)
    named = "鲸鱼帮看下"
    assert p.maybe_join(conversation=GROUP, text=named,
                        activity="balanced").action == "join"
    # 冷却期内 → observe
    d = p.maybe_join(conversation=GROUP, text=named, activity="balanced")
    assert (d.action, d.reason_code) == ("observe", "cooldown")
    # 过冷却后再参与直到配额尽
    now = T0 + timedelta(minutes=25)
    joins = 0
    for i in range(10):
        p2 = GroupTurnPolicy(store, bot_names=("鲸鱼",), now_fn=lambda: now,
                             quiet_now=lambda: False)
        d = p2.maybe_join(conversation=GROUP, text=named, activity="balanced")
        now += timedelta(minutes=25)
        if d.action == "join":
            joins += 1
        else:
            assert d.reason_code == "budget_exceeded"
            break
    assert joins == 5  # balanced 配额 6，首次已用 1


def test_consecutive_limit_no_double_join():
    """时间线最新一条已是鲸鱼发言 → 不连续主动插话。"""
    store = InMemoryStore()
    p = _policy(store)
    d = p.maybe_join(conversation=GROUP, text="鲸鱼这个怎么弄",
                     activity="lively",
                     timeline_entries=({"is_self": True, "text": "上一条是鲸鱼说的"},))
    assert (d.action, d.reason_code) == ("observe", "consecutive_limit")


def test_dnd_and_quiet_hours_suppress_proactive_join():
    """免打扰压制一切；时间静默只压制「没人找我」的非定向插话。"""
    store = InMemoryStore()
    p = _policy(store)
    d = p.maybe_join(conversation=GROUP, text="鲸鱼这个怎么弄",
                     activity="lively", dnd=True)
    assert (d.action, d.reason_code) == ("observe", "dnd")

    # 时间静默 + 无指向闲聊 → 压制
    store2 = InMemoryStore()
    p2 = GroupTurnPolicy(store2, now_fn=lambda: T0, quiet_now=lambda: True)
    d2 = p2.maybe_join(conversation=GROUP, text="今晚吃啥",
                       activity="lively")
    assert (d2.action, d2.reason_code) == ("observe", "quiet_hours")


def test_quiet_hours_directed_still_answers():
    """2026-11-01 owner 决策：静默时段里明确找鲸鱼的（直呼其名/承接请求）
    仍要回应——静默只压制主动插话；且不耗主动配额/冷却。"""
    # 直呼其名
    store = InMemoryStore()
    p = _policy(store, quiet=True)
    d = p.maybe_join(conversation=GROUP, text="鲸鱼这个怎么弄",
                     activity="lively")
    assert (d.action, d.reason_code) == ("join", "quiet_directed")
    # 不耗主动配额
    assert store.get(f"life:turnbudget:2026-11-01:{GROUP}") is None
    # 承接请求（机器人刚发言 + 「你+请求动词」）
    store2 = InMemoryStore()
    p2 = _policy(store2, quiet=True)
    d2 = p2.maybe_join(conversation=GROUP, text="那你再画一张吧",
                       activity="lively",
                       timeline_entries=({"is_self": True, "text": "刚画完"},))
    assert (d2.action, d2.reason_code) == ("join", "quiet_directed")
    # 静默时段的 bare question（没点名）仍压制——不算明确找它
    store3 = InMemoryStore()
    p3 = _policy(store3, quiet=True)
    d3 = p3.maybe_join(conversation=GROUP, text="这个报错什么意思？",
                       activity="lively")
    assert (d3.action, d3.reason_code) == ("observe", "quiet_hours")


def test_reason_code_recorded_without_private_content():
    """诊断落 kv：reason_code + 时间，不含消息正文。"""
    store = InMemoryStore()
    p = _policy(store)
    p.maybe_join(conversation=GROUP, text="今晚聚餐 privately", activity="quiet")
    last = p.last_decision(GROUP)
    assert last is not None
    assert last["reason_code"] == "quiet_mode" and last["action"] == "observe"
    assert "聚餐" not in str(last) and "privately" not in str(last)


def test_unknown_activity_falls_back_to_balanced():
    store = InMemoryStore()
    p = _policy(store)
    d = p.maybe_join(conversation=GROUP, text="鲸鱼看下", activity="bogus")
    assert d.action == "join"  # 按 balanced 判定（fail 到中间档）



def test_no_bundled_aliases_and_no_empty_regex_match():
    store = InMemoryStore()
    policy = GroupTurnPolicy(store, now_fn=lambda: T0)
    assert policy.maybe_join(
        conversation=GROUP, text="鲸鱼帮我看下？", activity="balanced"
    ).reason_code == "not_directed"
    assert GroupTurnPolicy(InMemoryStore(), bot_names=("阿澜",),
                           now_fn=lambda: T0).maybe_join(
        conversation=GROUP, text="阿澜帮我看下？",
        activity="balanced"
    ).action == "join"


def test_followup_request_after_bot_speaks_joins_lively():
    """2026-11-01 生产实证：「那你画个蜥蜴爬宠的图吧」——无 @ 无名无问号，
    但机器人刚在群里说过话（时间线近窗有 is_self）且是「你+请求动词」
    承接句式 → lively 档应主动接话。"""
    store = InMemoryStore()
    p = _policy(store)
    tl = [{"is_self": False, "text": "群里有人喜欢爬宠吗"},
          {"is_self": True, "text": "好，我这就搜搜，稍等"},
          {"is_self": False, "text": "搜索是付费功能"}]
    d = p.maybe_join(conversation=GROUP, text="那你画个蜥蜴爬宠的图吧",
                     activity="lively", timeline_entries=tl)
    assert d.action == "join"


def test_followup_requires_recent_self_and_lively():
    store = InMemoryStore()
    # 机器人近期没说话 → 不接（群友互相喊话不认领）
    p = _policy(store)
    tl = [{"is_self": False, "text": "你画个看看"}]
    d = p.maybe_join(conversation=GROUP, text="你画个蜥蜴看看",
                     activity="lively", timeline_entries=tl)
    assert d.reason_code == "not_directed"
    # balanced 档无此信号
    p2 = _policy(InMemoryStore())
    d2 = p2.maybe_join(conversation=GROUP, text="你画个蜥蜴看看",
                       activity="balanced",
                       timeline_entries=[{"is_self": True, "text": "我在"}])
    assert d2.reason_code == "not_directed"
    # 普通闲聊句式不命中（无承接动词）
    p3 = _policy(InMemoryStore())
    d3 = p3.maybe_join(conversation=GROUP, text="明天天气真不错",
                       activity="lively",
                       timeline_entries=[{"is_self": True, "text": "我在"}])
    assert d3.reason_code == "not_directed"
