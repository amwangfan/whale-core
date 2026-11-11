"""2026-11-12：群聊语义审批（ParticipationJudge / group_judge 模块）测试。

覆盖：
- 时间线按当前消息截断（排除未来）；当前消息在 prompt 中只出现一次；
- 不同 sender 相同文本 → 标签不坍缩（不再统一「群友」）；
- 真实 @/回复关系标注；bot 自己 = 「我」；
- 裁定严格解析（固定枚举；非法/异常 fail-closed 为 observe）；
- 与 GroupTurnPolicy 的集成：候选审批、quiet/静默只允许 directed、
  valuable_join 不突破、judge 存在时 directed_classifier 不双调、
  复读链否决不被审批复活。
"""

from __future__ import annotations

from datetime import datetime, timezone

from whale_core.life.group_judge import (
    ParticipationJudge, build_participation_prompt, truncate_at_message)
from whale_core.life.group_turn import (
    GroupTurnPolicy, ParticipationQuery, parse_participation_verdict)
from whale_core.memory.store import InMemoryStore

GROUP = "group:g1"
T0 = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def _entry(mid, sender, text, **kw):
    e = {"message_id": mid, "sender_id": sender,
         "sender_uid": "", "text": text, "is_self": False,
         "send_time": "2026-11-12T11:59:00+00:00"}
    e.update(kw)
    return e


# -- 截断与 prompt 构建 -----------------------------------------------------


def test_truncate_at_message_excludes_future():
    entries = (
        {"message_id": "m1", "sender_id": "u1", "text": "a"},
        {"message_id": "m2", "sender_id": "u2", "text": "b"},
        {"message_id": "m3", "sender_id": "u1", "text": "c"},  # 未来（合批期间到达）
    )
    assert [e["message_id"] for e in truncate_at_message(entries, "m2")] == ["m1"]
    # 空 id / 未命中 → 原样
    assert len(truncate_at_message(entries, "")) == 3
    assert len(truncate_at_message(entries, "nope")) == 3


def test_prompt_current_message_appears_once_and_future_excluded():
    entries = (
        _entry("m1", "u1", "前面聊的"),
        _entry("m2", "u2", "当前消息本体"),   # 当前消息（已在时间线里）
        _entry("m3", "u1", "未来的消息"),     # 未来：不得进上下文
    )
    q = ParticipationQuery(text="当前消息本体", activity="lively",
                           conversation=GROUP, timeline_entries=entries,
                           sender_id="u2", message_id="m2")
    prompt = build_participation_prompt(q, bot_name="Enxi")
    assert prompt.count("当前消息本体") == 1     # 只出现在「最新一条」
    assert "未来的消息" not in prompt            # 未来被截断
    assert "前面聊的" in prompt
    assert "最新一条" in prompt


def test_prompt_same_text_different_senders_get_distinct_labels():
    entries = (
        {"message_id": "m1", "sender_id": "uin-aaa", "text": "哈哈哈"},
        {"message_id": "m2", "sender_id": "u2", "text": "哈哈哈"},
    )
    q = ParticipationQuery(text="哈哈哈", activity="lively",
                           conversation=GROUP, timeline_entries=entries,
                           sender_id="uin-bbb", message_id="")
    prompt = build_participation_prompt(q, bot_name="Enxi")
    # 三个 sender（含当前）标签两两不同——不再统一坍缩成「群友」
    ctx_lines = [l for l in prompt.splitlines() if "哈哈哈" in l]
    assert len(ctx_lines) == 3  # 2 条上下文 + 1 条最新

    def _label(line):
        if line.startswith("最新一条"):
            return line.split("（")[1].split("）")[0]
        return line.split("[", 1)[1].split("]")[0].split("|")[0]
    labels = [_label(l) for l in ctx_lines]
    assert len(set(labels)) == 3


def test_prompt_marks_mentions_and_reply_relations():
    entries = (
        {"message_id": "m1", "sender_id": "u1", "text": "旧话",
         "reply_to": ("123", "uid-x", "99887766", "55")},
        {"message_id": "m2", "sender_id": "u1", "text": "喊机器人",
         "mentions": ["self-uid"]},
        {"message_id": "m3", "sender_id": "me", "text": "我说的", "is_self": True},
    )
    q = ParticipationQuery(text="最新", activity="lively",
                           conversation=GROUP, timeline_entries=entries,
                           sender_id="u2", message_id="")
    prompt = build_participation_prompt(q, bot_name="Enxi", self_uid="self-uid")
    assert "回复#7766" in prompt          # 真实回复关系（uin/uid 解析不到→#尾号）
    assert "@Enxi" in prompt              # @机器人 标注
    assert "[我|#m3] 我说的" in prompt     # bot 自己的发言标签 + 可识别消息ID
    assert "不可信" in prompt             # 群聊内容不可信纪律
    assert "被谈论不等于被邀请" in prompt
    assert "接得上不等于值得说" in prompt
    assert "不代表后面的每条都在对她说" in prompt


def test_prompt_reply_and_third_party_mention_attribution():
    """真实 host 形状：回复关系按四元组 uin/uid 解析被回复者标签（不猜
    origMsgId 与上下文哪行匹配）；@第三方 与 @机器人 分开标注。"""
    entries = (
        {"message_id": "aaaa1111", "sender_id": "10001", "sender_uid": "uid-a",
         "text": "今晚开团吗"},
        {"message_id": "bbbb2222", "sender_id": "10002", "sender_uid": "uid-b",
         "text": "回复来了", "reply_to": ("10001", "uid-a", "99988877", "7"),
         "mentions": ["uid-a"]},
    )
    q = ParticipationQuery(
        text="那这个呢", activity="lively", conversation=GROUP,
        timeline_entries=entries, sender_id="10002", sender_uid="uid-b",
        message_id="cccc3333",
        mentions=("uid-a", "self-uid"),
        reply_to=("10001", "uid-a", "99988877", "7"))
    prompt = build_participation_prompt(q, bot_name="Enxi", self_uid="self-uid")
    # 上下文行：回复按 uid-a → 10001 的标签归属（不猜 origMsgId 匹配）
    assert "回复群友" in prompt and "(#8877)" in prompt
    # 当前消息行：@第三方（uid-a 解析为群友标签）与 @机器人 分开
    cur = [l for l in prompt.splitlines() if l.startswith("最新一条")][0]
    assert "@Enxi" in cur and "@群友" in cur


def test_prompt_carries_time_and_signals():
    q = ParticipationQuery(text="那你再画一张吧", activity="lively",
                           conversation=GROUP,
                           timeline_entries=(_entry("m1", "u1", "x"),),
                           signals=("follow_up",), bot_spoke_recently=True,
                           sender_id="u1", message_id="")
    prompt = build_participation_prompt(q, bot_name="Enxi")
    assert "11-12 11:59" in prompt        # 时间标注（近期≠持续认领的判断素材）
    assert "follow_up" in prompt
    assert "近期在群里发过言" in prompt


# -- 严格解析 ----------------------------------------------------------------


def test_parse_verdict_strict():
    assert parse_participation_verdict("directed") == "directed"
    assert parse_participation_verdict(" valuable_join ") == "valuable_join"
    assert parse_participation_verdict("OBSERVE") == "observe"
    assert parse_participation_verdict("observe。") == "observe"
    # 非法输出一律 fail-closed 为 observe（自由理由永不放行）
    for bad in ("", None, "我觉得可以参与", "directed,因为她在叫机器人", "join",
                "是", "valuable"):
        assert parse_participation_verdict(bad) == "observe", bad


def test_judge_exception_fails_closed():
    def boom(prompt):
        raise RuntimeError("model down")
    judge = ParticipationJudge(boom, bot_name="Enxi")
    q = ParticipationQuery(text=" Enxi 在吗", activity="lively")
    assert judge(q) == "observe"


# -- 与 GroupTurnPolicy 集成 -------------------------------------------------


def _policy(store, judge, *, quiet=False, activity_now=T0, **kw):
    # 默认关闭话题滚动概率，避免测例被随机插话干扰（与 test_group_turn 同纪律）
    kw.setdefault("topic_join_prob", 0.0)
    return GroupTurnPolicy(store, bot_names=("鲸鱼",), now_fn=lambda: activity_now,
                           quiet_now=lambda: quiet,
                           participation_judge=judge, **kw)


def test_judge_observe_suppresses_rule_candidates():
    """他人互聊的问句 / 只是在谈论 bot：规则候选被审批否决 → 旁观。"""
    # 互聊问句（lively 旧规则会接；judge 判 observe → 不接）
    store = InMemoryStore()
    p = _policy(store, lambda q: "observe")
    d = p.maybe_join(conversation=GROUP, text="这个报错什么意思？",
                     activity="lively")
    assert (d.action, d.reason_code) == ("observe", "judge_observe")
    # 谈论 bot 名字 ≠ 邀请（named 候选也被否决）
    store2 = InMemoryStore()
    p2 = _policy(store2, lambda q: "observe")
    d2 = p2.maybe_join(conversation=GROUP, text="鲸鱼昨天那个回复笑死我了",
                       activity="lively")
    assert (d2.action, d2.reason_code) == ("observe", "judge_observe")


def test_judge_directed_joins_even_in_quiet_hours_and_quiet_mode():
    """明显承接/点名请求：judge 判 directed → 静默时段与 quiet 档都放行。"""
    tl = ({"is_self": True, "text": "我刚录了句语音", "message_id": "m0"},)
    store = InMemoryStore()
    p = _policy(store, lambda q: "directed", quiet=True)
    d = p.maybe_join(conversation=GROUP, text="说出来各位的名字哦",
                     activity="lively", timeline_entries=tl)
    assert (d.action, d.reason_code) == ("join", "quiet_directed")
    # quiet 档：directed 也放行（quiet 只拦非 directed）
    # （时间线尾条 = 当前群友消息；连续发言限制与旧规则口径一致）
    store2 = InMemoryStore()
    p2 = _policy(store2, lambda q: "directed")
    tl2 = tl + ({"is_self": False, "text": "鲸鱼帮看下", "message_id": "m1"},)
    d2 = p2.maybe_join(conversation=GROUP, text="鲸鱼帮看下",
                       activity="quiet", timeline_entries=tl2)
    assert d2.action == "join"


def test_judge_valuable_join_allowed_but_never_breaks_quiet():
    """真正有价值的参与：balanced/lively 放行（valuable_join）；
    quiet 档与时间静默只允许 directed——valuable_join 不突破。"""
    store = InMemoryStore()
    p = _policy(store, lambda q: "valuable_join")
    d = p.maybe_join(conversation=GROUP, text="有人知道怎么配吗",
                     activity="lively")
    assert (d.action, d.reason_code) == ("join", "valuable_join")
    # balanced：question 不是候选（保持原候选范围），但机器人近期发言 →
    # 送审，valuable_join 放行
    store_b = InMemoryStore()
    p_b = _policy(store_b, lambda q: "valuable_join")
    tl_b = ({"is_self": True, "text": "我刚说过", "message_id": "m0"},
            {"is_self": False, "text": "嗯嗯", "message_id": "m1"})
    d_b = p_b.maybe_join(conversation=GROUP, text="有人知道怎么配吗",
                         activity="balanced", timeline_entries=tl_b)
    assert (d_b.action, d_b.reason_code) == ("join", "valuable_join")
    # balanced 无候选且无近期发言 → 不送审，维持旁观（候选范围未扩大）
    store_b2 = InMemoryStore()
    p_b2 = _policy(store_b2, lambda q: "valuable_join")
    d_b2 = p_b2.maybe_join(conversation=GROUP, text="有人知道怎么配吗",
                           activity="balanced")
    assert (d_b2.action, d_b2.reason_code) == ("observe", "not_directed")
    # quiet 档：不突破
    store2 = InMemoryStore()
    p2 = _policy(store2, lambda q: "valuable_join")
    d2 = p2.maybe_join(conversation=GROUP, text="有人知道怎么配吗",
                       activity="quiet")
    assert d2.action == "observe"
    # 时间静默：不突破
    store3 = InMemoryStore()
    p3 = _policy(store3, lambda q: "valuable_join", quiet=True)
    d3 = p3.maybe_join(conversation=GROUP, text="有人知道怎么配吗",
                       activity="lively")
    assert (d3.action, d3.reason_code) == ("observe", "quiet_hours")


def test_judge_observe_on_wrapup_and_stale_self_context():
    """寒暄收尾 / 机器人近期发过言但群友已转入互聊：判 observe → 旁观。
    （机器人近期出现过 ≠ 持续认领每一条。）"""
    tl = ({"is_self": True, "text": "画好了，发你啦", "message_id": "m0"},)
    store = InMemoryStore()
    p = _policy(store, lambda q: "observe")
    d = p.maybe_join(conversation=GROUP, text="好嘞谢谢啦",
                     activity="lively", timeline_entries=tl)
    assert (d.action, d.reason_code) == ("observe", "judge_observe")


def test_judge_exception_and_illegal_output_fail_closed():
    """模型异常 / 非法输出：一律旁观（fail-closed，不放大为参与）。"""
    def boom(q):
        raise RuntimeError("down")
    store = InMemoryStore()
    p = _policy(store, boom)
    d = p.maybe_join(conversation=GROUP, text="鲸鱼在吗", activity="lively")
    assert d.action == "observe"

    store2 = InMemoryStore()
    p2 = _policy(store2, lambda q: "我觉得可以参与因为……")
    d2 = p2.maybe_join(conversation=GROUP, text="鲸鱼在吗",
                       activity="lively")
    assert d2.action == "observe"


def test_judge_replaces_directed_classifier_no_double_call():
    """judge 存在时 directed_classifier 不再被调用（避免双调）。"""
    calls = []
    store = InMemoryStore()
    p = GroupTurnPolicy(
        store, bot_names=("鲸鱼",), now_fn=lambda: T0,
        quiet_now=lambda: False,
        participation_judge=lambda q: "directed",
        directed_classifier=lambda t, e: calls.append(1) or True)
    tl = ({"is_self": True, "text": "我在", "message_id": "m0"},
          {"is_self": False, "text": "随便一句", "message_id": "m1"})
    d = p.maybe_join(conversation=GROUP, text="随便一句", activity="lively",
                     timeline_entries=tl)
    assert d.action == "join"
    assert calls == []


def test_judge_not_called_without_candidates_or_self_context():
    """无字面候选且机器人近期未发言 → 不送审（调用有界），维持旁观。"""
    calls = []
    store = InMemoryStore()

    def spy(q):
        calls.append(q)
        return "valuable_join"
    p = _policy(store, spy)
    d = p.maybe_join(conversation=GROUP, text="今晚吃啥", activity="lively",
                     timeline_entries=({"is_self": False, "text": "别人聊的",
                                        "message_id": "m0"},))
    assert (d.action, d.reason_code) == ("observe", "not_directed")
    assert calls == []


def test_chain_judge_veto_not_resurrected_with_participation_judge():
    """复读链被 chain_judge 否决后：不送审复活（无候选无 self 上下文），
    复读跟风也不经过 participation_judge（chain_judge 是其唯一判定）。"""
    chain = tuple({"is_self": False, "text": "有毒吧",
                   "sender_id": f"u{i % 2}", "message_id": f"m{i}"}
                  for i in range(3))
    calls = []
    store = InMemoryStore()
    p = GroupTurnPolicy(
        store, bot_names=("鲸鱼",), now_fn=lambda: T0,
        quiet_now=lambda: False,
        participation_judge=lambda q: calls.append(q) or "valuable_join",
        chain_judge=lambda t, e: False,
        topic_join_prob=1.0, rng=lambda: 0.0)
    d = p.maybe_join(conversation=GROUP, text="有毒吧", activity="lively",
                     timeline_entries=chain)
    assert d.action == "observe"   # 否决不复活（topic_roll 已被 chain_rejected 拦）
    assert calls == []             # 无候选无 self 上下文：审批不调用


def test_snapshot_excludes_future_from_rules():
    """按当前消息截断：未来条目不参与连续发言/已被接住等规则判定。"""
    # 未来一条 is_self 不得触发连续发言限制
    store = InMemoryStore()
    p = _policy(store, lambda q: "directed")
    tl = ({"is_self": False, "text": "前一条", "message_id": "m0"},
          {"is_self": False, "text": "鲸鱼帮看下", "message_id": "m1"},
          {"is_self": True, "text": "未来自发言", "message_id": "m2"})
    d = p.maybe_join(conversation=GROUP, text="鲸鱼帮看下", activity="lively",
                     timeline_entries=tl, message_id="m1")
    assert d.action == "join"
    # 未来长回复不得触发 already_covered（当前之前的历史里上一条很短）
    store2 = InMemoryStore()
    p2 = _policy(store2, lambda q: "observe")
    tl2 = ({"is_self": False, "text": "短", "message_id": "m0"},
           {"is_self": False, "text": "嗯", "message_id": "m1"},
           {"is_self": False, "text": "这是一条足够完整的未来长回复，已把问题说清楚",
            "message_id": "m2"})
    d2 = p2.maybe_join(conversation=GROUP, text="嗯", activity="lively",
                       timeline_entries=tl2, message_id="m1")
    assert d2.reason_code != "already_covered"


def test_snapshot_missing_current_id_means_empty_history():
    """message_id 非空但未在时间线找到 → 历史按空处理：不据不可定位/未来
    条目做 bot_recent/承接/复读判定（message_id 为空才走 legacy 兼容）。"""
    store = InMemoryStore()
    calls = []
    p = _policy(store, lambda q: calls.append(q) or "directed")
    tl = ({"is_self": True, "text": "我刚画了张图", "message_id": "m0"},)
    d = p.maybe_join(conversation=GROUP, text="那你再画一张吧",
                     activity="lively", timeline_entries=tl,
                     message_id="not-in-timeline")
    # 历史按空 → bot_recent=False → 承接不成立、无候选 → 不送审 → 旁观
    assert (d.action, d.reason_code) == ("observe", "not_directed")
    assert calls == []


def test_snapshot_repeat_chain_uses_history_plus_current():
    """复读链 = 历史 + 当前（当前本就是链的一环）；未来条目不参与、
    命中后不送 participation_judge（chain_judge 唯一决策，不双调）。"""
    calls = []
    store = InMemoryStore()
    p = _policy(store, lambda q: calls.append(q) or "observe")
    tl = ({"is_self": False, "text": "有毒吧", "sender_id": "u1",
           "message_id": "m0"},
          {"is_self": False, "text": "有毒吧", "sender_id": "u2",
           "message_id": "m1"},
          {"is_self": False, "text": "有毒吧", "sender_id": "u1",
           "message_id": "m2"},
          {"is_self": True, "text": "未来的机器人发言", "message_id": "m3"})
    d = p.maybe_join(conversation=GROUP, text="有毒吧", activity="lively",
                     timeline_entries=tl, message_id="m2")
    # 未来 m3（is_self）若参与会把链打断——快照排除后链命中
    assert (d.action, d.reason_code) == ("join", "repeat_chain")
    assert d.text == "有毒吧"
    assert calls == []  # 复读命中不送语义审批


def test_consecutive_uses_snapshot_with_current():
    """前一条是机器人、当前是群友承接新消息 → 不算连发（judge-directed
    正常放行；2026-11-12 reviewer 决策）。"""
    store = InMemoryStore()
    p = _policy(store, lambda q: "directed")
    tl = ({"is_self": True, "text": "我画了张蜥蜴", "message_id": "m0"},
          {"is_self": False, "text": "那你再画一张吧", "message_id": "m1"})
    d = p.maybe_join(conversation=GROUP, text="那你再画一张吧",
                     activity="lively", timeline_entries=tl, message_id="m1")
    assert d.action == "join"


def test_already_covered_hard_gate_deferred_to_judge():
    """judge 路径：长他人回复 + 短当前消息不再硬拦——短句若是明确请求
    （点名/承接）由 judge 判 directed 放行；判 observe 则旁观。
    legacy 路径（无 judge）硬拦不变（test_group_turn 既有用例覆盖）。"""
    prev = "这是一段足够完整的长回复，已经把问题说清楚了"
    tl = ({"is_self": False, "text": prev, "message_id": "m0"},
          {"is_self": False, "text": "鲸鱼帮我", "message_id": "m1"})
    # 短明确请求 + judge directed → 参与（不被 already_covered 误杀）
    store = InMemoryStore()
    p = _policy(store, lambda q: "directed")
    d = p.maybe_join(conversation=GROUP, text="鲸鱼帮我", activity="lively",
                     timeline_entries=tl, message_id="m1")
    assert d.action == "join"
    # judge 判收尾/无需补充 → 旁观
    store2 = InMemoryStore()
    p2 = _policy(store2, lambda q: "observe")
    tl2 = ({"is_self": False, "text": prev, "message_id": "m0"},
           {"is_self": False, "text": "鲸鱼真好", "message_id": "m1"})
    d2 = p2.maybe_join(conversation=GROUP, text="鲸鱼真好", activity="lively",
                       timeline_entries=tl2, message_id="m1")
    assert (d2.action, d2.reason_code) == ("observe", "judge_observe")


def test_integration_prompt_to_verdict_roundtrip():
    """端到端（伪模型）：prompt 里能看到当前消息与归属，回答 directed → 参与。"""
    seen = []

    def fake_llm(prompt):
        seen.append(prompt)
        return "directed"
    judge = ParticipationJudge(fake_llm, bot_name="鲸鱼")
    store = InMemoryStore()
    p = _policy(store, judge)
    tl = ({"is_self": True, "text": "我画了张蜥蜴", "sender_id": "me",
           "message_id": "m0"},
          {"is_self": False, "text": "那你再画一张吧", "sender_id": "u9",
           "message_id": "m9"})
    d = p.maybe_join(conversation=GROUP, text="那你再画一张吧",
                     activity="lively", timeline_entries=tl,
                     sender_id="u9", message_id="m9")
    assert d.action == "join"
    assert len(seen) == 1
    assert "那你再画一张吧" in seen[0] and "[我|#m0]" in seen[0]
