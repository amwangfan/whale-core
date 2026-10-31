"""issue #19 回放基准的 pytest 回归：每个场景一个 test，断言指标达标。

达标线（全部确定性字符串 / 结构比对）：

- 归属正确率 100%（时间线标签不串人，「我」仅属于鲸鱼自己的发言）；
- 自述事实 100% 进入 self_facts（私聊授权路径；群聊按 MEM-03 为空）；
- 客服腔命中数 0（内置中文客服腔词表扫描关系提示 / 表达提示 / 人格注入 /
  回复文本）；
- 误亲密命中数 0（陌生人及声明 no_intimacy 的轮次不得含亲密称呼）；
- 场景内全部确定性断言通过（evaluate_scenario 全绿）。
"""

from __future__ import annotations

import pytest

from evals import SCENARIOS, SCENARIOS_BY_ID, build_report, run_scenario
from evals.metrics import (
    CUSTOMER_SERVICE_PHRASES,
    INTIMATE_ADDRESS_TERMS,
    aggregate_metrics,
    customer_service_hits,
    evaluate_scenario,
    intimacy_violations,
    scenario_metrics,
)


def _run(sid: str):
    return run_scenario(SCENARIOS_BY_ID[sid])


def _assert_scenario_ok(result):
    """场景通用达标线：断言全绿 + 四项指标满分。"""
    checks = evaluate_scenario(result)
    failed = [c for c in checks if not c.passed]
    assert not failed, "失败断言：\n" + "\n".join(
        f"  [{c.check_id}] {c.description} —— {c.detail}" for c in failed)
    m = scenario_metrics(result)
    assert m.attribution_rate == 1.0, "归属正确率必须 100%"
    assert m.self_facts_rate == 1.0, "自述事实必须全部进入 self_facts"
    assert m.customer_service_hits == (), f"客服腔命中：{m.customer_service_hits}"
    assert m.intimacy_violations == (), f"误亲密命中：{m.intimacy_violations}"
    return m


def _label_of(result, message_id: str) -> str:
    for a in result.attributions:
        if a.message_id == message_id:
            return a.label
    return ""


# ---------------------------------------------------------------------------
# 逐场景回归
# ---------------------------------------------------------------------------


def test_s01_acquainted_stranger_probe():
    """初识群友：一两句发言 = 初识；未发言群友 = 陌生且无误亲密。"""
    result = _run("S01")
    _assert_scenario_ok(result)
    # 只有 @鲸鱼 的那条触发模型调用
    assert len(result.brain_calls) == 1
    assert result.brain_calls[0].text == "@鲸鱼 帮我看看这个报错是什么意思"
    # 陌生人探针：C 从未发言 → 陌生 + 分寸提示
    probe = result.probes[0]
    assert probe.familiarity == "stranger"
    assert probe.interaction_count == 0
    assert "陌生人" in probe.relationship_render
    assert "不假装熟悉" in probe.relationship_render


def test_s02_familiar_member():
    """熟悉群友：9 次互动 → 熟悉档，计数可复核。"""
    result = _run("S02")
    m = _assert_scenario_ok(result)
    assert m.attribution_total == 10  # 9 条群友发言 + 1 条鲸鱼回复
    turn = result.turn("m-s02-09")
    assert turn.familiarity == "familiar"
    assert "熟悉（有 9 次互动记录）" in turn.relationship_render


def test_s03_owner_private_continuity():
    """主人私聊：历史逐轮承接，回复对象恒为主人。"""
    result = _run("S03")
    _assert_scenario_ok(result)
    assert len(result.brain_calls) == 2
    # 第二轮的模型历史含第一轮内容（承接）
    second_history = "\n".join(h["content"] for h in result.brain_calls[1].history)
    assert "明天会议几点来着" in second_history
    for turn in result.turns:
        assert turn.reply_action == "reply"
        assert turn.reply_target_sender == "90001"


def test_s04_novice_help_self_facts_and_mem03():
    """电脑小白求助：私聊自述进 self_facts 驱动新手策略；群聊 MEM-03 为空。"""
    result = _run("S04")
    _assert_scenario_ok(result)
    private_turn = result.turn("m-s04-p1")
    assert any("我是电脑小白" in f for f in private_turn.self_facts)
    assert private_turn.policy.skill == "novice"
    assert "大白话" in private_turn.policy_render
    group_turn = result.turn("m-s04-g1")
    # MEM-03：群聊关系视图读不到任何私人自述
    assert group_turn.self_facts == ()
    assert group_turn.policy.skill == "unknown"
    # 但当前消息的焦虑 / 未保存数据风险信号仍然生效
    assert group_turn.policy.anxious is True
    assert group_turn.policy.confirm_data_safety is True
    assert "大白话" not in group_turn.policy_render


def test_s05_hex_confession_observe_only():
    """B 的十六进制 / 明文「我爱你」：无 @ 无引用 → 旁观，不认领、不亲昵。"""
    result = _run("S05")
    _assert_scenario_ok(result)
    # 全程旁观：零模型调用
    assert result.brain_calls == ()
    for turn in result.turns:
        assert turn.reply_action == "observe"
    # 两条 B 的消息同一稳定标签，且不是「我」
    label_b1 = _label_of(result, "m-s05-b1")
    assert label_b1 and label_b1 == _label_of(result, "m-s05-b2")
    assert label_b1 != "我"
    # 明文「我爱你」命中表白规则但不认领
    assert result.turn("m-s05-b2").policy.confession_unclaimed is True
    assert "不要认领成对自己的表白" in result.turn("m-s05-b2").policy_render


def test_s06_topic_switch_attribution():
    """issue 点名场景：A 求助 → B 十六进制插话 → A 续问，归属与承接。"""
    result = _run("S06")
    _assert_scenario_ok(result)
    label_a1 = _label_of(result, "m-s06-a1")
    label_a2 = _label_of(result, "m-s06-a2")
    label_b1 = _label_of(result, "m-s06-b1")
    # A 的两条消息同一稳定标签；B 独立；B 不被归为鲸鱼（「我」）或 A
    assert label_a1 and label_a1 == label_a2
    assert label_b1 and label_b1 != label_a1 and label_b1 != "我"
    # 两条鲸鱼回复都标注为「我」，且各自回复 A 的消息
    assert _label_of(result, "m-s06-a1-r") == "我"
    assert _label_of(result, "m-s06-a2-r") == "我"
    # B 的插话不触发回复；A 的两条都回给 A
    assert [t.reply_target_sender for t in result.turns
            if t.reply_action == "reply"] == ["10001", "10001"]
    assert result.turn("m-s06-b1").reply_action == "observe"
    # A 的续问仍承接 A 上下文：互动计数为 2（B 不串入），关系提示注入
    a2 = result.turn("m-s06-a2")
    assert a2.interaction_count == 2
    assert "2 次互动记录" in a2.persona_injected
    # 注入模型的时间线里，十六进制行挂在 B 的标签下
    timeline_block = a2.history_texts[0]
    hex_line = next(l for l in timeline_block.splitlines()
                    if "E6 88 91" in l)
    assert f"|{label_b1}]" in hex_line
    assert f"|{label_a1}]" not in hex_line and "|我]" not in hex_line


def test_s07_next_day_continuity():
    """隔天补聊：计数、称呼、边界跨天延续。"""
    result = _run("S07")
    _assert_scenario_ok(result)
    turn = result.turn("m-s07-01")
    assert turn.familiarity == "familiar"
    assert any("叫我阿夜" in f for f in turn.self_facts)
    assert any("我不喜欢被催" in f for f in turn.self_facts)
    assert "阿夜" in turn.relationship_render
    assert "不喜欢被催" in turn.relationship_render
    # 注入人格含关系提示，且历史承接前一日
    assert "关系：熟悉" in turn.persona_injected
    assert any("明天我答辩" in h for h in turn.history_texts)


def test_s08_failure_report_tone():
    """失败任务回报：诚实说明、零客服腔、用户追问不被误判为焦虑。"""
    result = _run("S08")
    _assert_scenario_ok(result)
    turn = result.turn("m-s08-01")
    assert turn.policy_render == ""  # 追问不含焦虑信号 → 不注入表达模板
    assert "没抢到" in turn.reply_text
    # 回复文本过客服腔词表（指标内已扫，这里显式锚定场景语义）
    assert customer_service_hits((("回复", turn.reply_text),)) == ()


def test_s09_proactive_reminder_context():
    """主动提醒语境：平静回应不触发焦虑 / 数据风险模板，称呼延续。"""
    result = _run("S09")
    _assert_scenario_ok(result)
    turn = result.turn("m-s09-01")
    assert turn.familiarity == "familiar"
    assert turn.policy.anxious is False
    assert turn.policy.confirm_data_safety is False  # 提到论文但无风险语境
    assert turn.policy_render == ""
    assert any("明天下午三点答辩" in h for h in turn.history_texts)


# ---------------------------------------------------------------------------
# 汇总指标 / 报告 / 确定性
# ---------------------------------------------------------------------------


def test_aggregate_metrics_all_green():
    """整个回放集：归属 100%、自述 100%、客服腔 0、误亲密 0、断言全绿。"""
    results = tuple(run_scenario(s) for s in SCENARIOS)
    metrics = aggregate_metrics(results)
    assert metrics.scenarios == 9
    assert metrics.attribution_total == 22  # 固定样例集的消息总数（含鲸鱼回写）
    assert metrics.attribution_rate == 1.0
    assert metrics.self_facts_expected == 4
    assert metrics.self_facts_rate == 1.0
    assert metrics.customer_service_hits == ()
    assert metrics.intimacy_violations == ()
    assert metrics.checks_total > 50
    assert metrics.all_passed


def test_metrics_positive_controls():
    """词表指标自身的确定性：正例必命中、负例不命中（防词表失效）。"""
    hits = customer_service_hits((("t", "请问有什么可以帮您？祝您生活愉快"),))
    assert "t：请问有什么可以帮您" in hits
    assert "t：祝您生活愉快" in hits
    assert customer_service_hits((("t", "先别强制关机，论文可能还没保存"),)) == ()
    bad = intimacy_violations((("t", "亲爱的宝贝，亲亲"),))
    assert "t：亲爱的" in bad and "t：宝贝" in bad
    # 「不制造亲昵」这类纪律句不得误伤
    assert intimacy_violations((("t", "保持礼貌与分寸，不假装熟悉、不制造亲昵。"),)) == ()
    # 词表非空（防空列表退化导致指标恒绿）
    assert len(CUSTOMER_SERVICE_PHRASES) >= 10
    assert len(INTIMATE_ADDRESS_TERMS) >= 8


def test_report_renders_markdown():
    """报告模板：指标总览 + 场景×指标表 + 失败明细（全绿时为「无」）。"""
    results = tuple(run_scenario(s) for s in SCENARIOS)
    report = build_report(results)
    assert report.startswith("# 人格自然度 / 关系连续性回放评测报告")
    assert "## 指标总览" in report and "## 场景 × 指标" in report
    for sid in SCENARIOS_BY_ID:
        assert sid in report
    assert "归属正确率" in report and "自述事实进入 self_facts" in report
    assert "客服腔命中数" in report and "误亲密命中数" in report
    assert "## 失败断言明细\n\n（无）" in report


def test_replay_is_deterministic():
    """同一场景回放两遍，观测完全一致（无时间 / 随机泄漏到断言面）。"""
    r1 = _run("S06")
    r2 = _run("S06")
    assert r1.attributions == r2.attributions
    assert [t.relationship_render for t in r1.turns] == \
           [t.relationship_render for t in r2.turns]
    assert [t.policy_render for t in r1.turns] == \
           [t.policy_render for t in r2.turns]
    assert [t.timeline_render for t in r1.turns] == \
           [t.timeline_render for t in r2.turns]
    assert [c.persona for c in r1.brain_calls] == [c.persona for c in r2.brain_calls]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
