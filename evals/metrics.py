"""确定性指标与断言求值（issue #19）。

指标定义（全部是字符串 / 结构比对，不用模型自评）：

1. **归属正确率**：时间线渲染中每条消息的归属标签须满足——同一发送者恒同
   标签、不同发送者标签互不相同、``is_self`` 消息标签为「我」且非自己
   的消息绝不为「我」；
2. **自述事实召回**：场景声明应进入 ``self_facts`` 的自述子串，实际进入的
   比例（私聊授权路径；群聊路径按 MEM-03 断言为空，不计入分子分母）；
3. **客服腔命中数**：内置小型中文客服腔词表，扫描每一轮的关系提示、表达
   提示、人格注入文本与回复文本，命中数必须为 0；
4. **误亲密检查**：陌生人（及声明 ``no_intimacy`` 的）场景，关系提示 /
   表达提示 / 人格注入文本不得含亲密称呼词表中的任何词。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Tuple

from evals.runner import ScenarioResult, TurnRecord
from evals.scenarios import Scenario, TurnExpect

# ---------------------------------------------------------------------------
# 内置词表（小而明确；只列无歧义短语，避免误伤正常中文表达）
# ---------------------------------------------------------------------------

#: 模板式安慰 / 客服腔短语。
CUSTOMER_SERVICE_PHRASES: Tuple[str, ...] = (
    "请问有什么可以帮您",
    "请问有什么可以帮你",
    "有什么可以帮您",
    "还有什么可以帮您",
    "非常抱歉给您带来不便",
    "抱歉给您带来不便",
    "抱歉给你带来不便",
    "感谢您的反馈",
    "感谢你的反馈",
    "感谢您的理解与支持",
    "祝您生活愉快",
    "祝您使用愉快",
    "很高兴为您服务",
    "竭诚为您服务",
    "请问还有什么需要",
    "敬请谅解",
)

#: 亲密称呼（误亲密检查）。注意不收「亲昵」——陌生人提示里有「不制造亲昵」
#: 这一否定式纪律句，不能误伤。
INTIMATE_ADDRESS_TERMS: Tuple[str, ...] = (
    "亲爱的",
    "宝贝",
    "宝宝",
    "宝子",
    "亲亲",
    "小可爱",
    "心肝",
    "老公",
    "老婆",
    "达令",
    "甜心",
)


def customer_service_hits(texts: Iterable[Tuple[str, str]]) -> Tuple[str, ...]:
    """扫描 (来源, 文本) 序列，返回命中描述「来源：短语」（确定性子串比对）。"""
    out: list[str] = []
    for source, text in texts:
        for phrase in CUSTOMER_SERVICE_PHRASES:
            if phrase in (text or ""):
                out.append(f"{source}：{phrase}")
    return tuple(out)


def intimacy_violations(texts: Iterable[Tuple[str, str]]) -> Tuple[str, ...]:
    """扫描 (来源, 文本) 序列，返回亲密称呼命中描述「来源：称呼」。"""
    out: list[str] = []
    for source, text in texts:
        for term in INTIMATE_ADDRESS_TERMS:
            if term in (text or ""):
                out.append(f"{source}：{term}")
    return tuple(out)


# ---------------------------------------------------------------------------
# 逐条断言求值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    description: str
    passed: bool
    detail: str = ""


def _check(check_id: str, description: str, passed: bool, detail: str = "") -> CheckResult:
    return CheckResult(check_id=check_id, description=description,
                       passed=passed, detail=detail)


def evaluate_turn(sid: str, turn: TurnRecord, expect: TurnExpect,
                  label_of) -> list[CheckResult]:
    """对一条 focus 消息求值全部确定性期望。"""
    checks: list[CheckResult] = []
    mid = expect.message_id

    def cid(name: str) -> str:
        return f"{sid}:{mid}:{name}"

    if expect.reply_action:
        checks.append(_check(
            cid("reply_action"), f"回复裁决应为 {expect.reply_action}",
            turn.reply_action == expect.reply_action,
            f"实际 {turn.reply_action}（{turn.reply_reason}）"))
    if expect.reply_target_sender:
        checks.append(_check(
            cid("reply_target"), f"回复对象应为 {expect.reply_target_sender}",
            turn.reply_target_sender == expect.reply_target_sender,
            f"实际 {turn.reply_target_sender or '（无）'}"))
    if expect.no_model_call:
        checks.append(_check(
            cid("no_model_call"), "旁观消息不得触发模型调用 / 回复",
            turn.persona_injected == "" and turn.reply_text == "",
            f"persona {len(turn.persona_injected)} 字 / reply {turn.reply_text!r}"))
    if expect.familiarity:
        checks.append(_check(
            cid("familiarity"), f"关系档应为 {expect.familiarity}",
            turn.familiarity == expect.familiarity,
            f"实际 {turn.familiarity}"))
    if expect.exact_interaction_count is not None:
        checks.append(_check(
            cid("interaction_count"),
            f"互动计数应为 {expect.exact_interaction_count}",
            turn.interaction_count == expect.exact_interaction_count,
            f"实际 {turn.interaction_count}"))
    for needle in expect.self_facts_include:
        checks.append(_check(
            cid(f"self_fact:{needle}"), f"self_facts 应含「{needle}」",
            any(needle in fact for fact in turn.self_facts),
            f"实际 self_facts={turn.self_facts!r}"))
    if expect.self_facts_empty:
        checks.append(_check(
            cid("self_facts_empty"), "群聊关系视图不得含私人自述（MEM-03）",
            turn.self_facts == (), f"实际 {turn.self_facts!r}"))
    for needle in expect.relationship_render_include:
        checks.append(_check(
            cid(f"rel_render+:{needle}"), f"关系提示应含「{needle}」",
            needle in turn.relationship_render, f"实际渲染：{turn.relationship_render!r}"))
    for needle in expect.relationship_render_exclude:
        checks.append(_check(
            cid(f"rel_render-:{needle}"), f"关系提示不得含「{needle}」",
            needle not in turn.relationship_render, f"实际渲染：{turn.relationship_render!r}"))
    for name, expected in expect.policy_flags:
        actual = getattr(turn.policy, name, None)
        checks.append(_check(
            cid(f"policy:{name}"), f"表达策略 {name} 应为 {expected}",
            actual is expected, f"实际 {actual!r}"))
    if expect.policy_skill:
        checks.append(_check(
            cid("policy_skill"), f"技能档应为 {expect.policy_skill}",
            turn.policy.skill == expect.policy_skill,
            f"实际 {turn.policy.skill}"))
    for needle in expect.policy_render_include:
        checks.append(_check(
            cid(f"policy_render+:{needle}"), f"表达提示应含「{needle}」",
            needle in turn.policy_render, f"实际渲染：{turn.policy_render!r}"))
    for needle in expect.policy_render_exclude:
        checks.append(_check(
            cid(f"policy_render-:{needle}"), f"表达提示不得含「{needle}」",
            needle not in turn.policy_render, f"实际渲染：{turn.policy_render!r}"))
    if expect.policy_render_empty:
        checks.append(_check(
            cid("policy_render_empty"), "无信号时不得注入表达提示（PER-02）",
            turn.policy_render == "", f"实际渲染：{turn.policy_render!r}"))
    history_blob = "\n".join(turn.history_texts)
    for needle in expect.history_includes:
        checks.append(_check(
            cid(f"history+:{needle}"), f"注入模型的历史应含「{needle}」",
            needle in history_blob, f"实际历史 {len(history_blob)} 字"))
    for needle in expect.timeline_render_include:
        checks.append(_check(
            cid(f"timeline+:{needle}"), f"时间线渲染应含「{needle}」",
            needle in turn.timeline_render, "实际渲染见报告明细"))
    if expect.label_same_as:
        mine, other = label_of(turn.message_id), label_of(expect.label_same_as)
        checks.append(_check(
            cid(f"label=:{expect.label_same_as}"),
            f"应与 {expect.label_same_as} 同一归属标签",
            bool(mine) and mine == other, f"{mine!r} vs {other!r}"))
    for other_id in expect.label_distinct_from:
        mine, other = label_of(turn.message_id), label_of(other_id)
        checks.append(_check(
            cid(f"label!=:{other_id}"), f"不得与 {other_id} 同一归属标签",
            bool(mine) and mine != other, f"{mine!r} vs {other!r}"))
    if expect.no_intimacy:
        hits = intimacy_violations((
            (f"{mid} 关系提示", turn.relationship_render),
            (f"{mid} 表达提示", turn.policy_render),
            (f"{mid} 人格注入", turn.persona_injected),
        ))
        checks.append(_check(
            cid("no_intimacy"), "渲染文本不得含亲密称呼（误亲密检查）",
            not hits, "；".join(hits)))
    return checks


# ---------------------------------------------------------------------------
# 场景级指标
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioMetrics:
    attribution_correct: int = 0
    attribution_total: int = 0
    self_facts_found: int = 0
    self_facts_expected: int = 0
    customer_service_hits: Tuple[str, ...] = ()
    intimacy_violations: Tuple[str, ...] = ()
    checks_passed: int = 0
    checks_total: int = 0

    @property
    def attribution_rate(self) -> float:
        return 1.0 if self.attribution_total == 0 else (
            self.attribution_correct / self.attribution_total)

    @property
    def self_facts_rate(self) -> float:
        return 1.0 if self.self_facts_expected == 0 else (
            self.self_facts_found / self.self_facts_expected)

    @property
    def passed(self) -> bool:
        return (self.attribution_correct == self.attribution_total
                and self.self_facts_found == self.self_facts_expected
                and not self.customer_service_hits
                and not self.intimacy_violations
                and self.checks_passed == self.checks_total)


def attribution_records_check(result: ScenarioResult) -> Tuple[int, int, list[CheckResult]]:
    """归属正确率：同发送者恒同标签、异发送者互异、「我」仅属于 is_self。"""
    records = result.attributions
    sid = result.scenario.scenario_id
    sender_labels: Dict[str, set] = {}
    label_owner: Dict[str, str] = {}
    for r in records:
        if r.is_self:
            continue
        sender_labels.setdefault(r.sender_id, set()).add(r.label)
        label_owner.setdefault(r.label, r.sender_id)

    correct = 0
    problems: list[str] = []
    for r in records:
        ok = bool(r.label)
        if r.is_self:
            ok = ok and r.label == "我"
        else:
            ok = (ok and r.label != "我"
                  and len(sender_labels.get(r.sender_id, ())) == 1
                  and label_owner.get(r.label) == r.sender_id)
        if ok:
            correct += 1
        else:
            problems.append(f"{r.message_id}({r.sender_id})→{r.label or '缺失'}")

    checks = [_check(
        f"{sid}:attribution",
        f"归属正确率 {correct}/{len(records)}",
        correct == len(records), "；".join(problems))]
    # 结构性检查：任何两个不同发送者不得共享标签
    seen: Dict[str, str] = {}
    for sender, labels in sorted(sender_labels.items()):
        for label in labels:
            other = seen.setdefault(label, sender)
            if other != sender:
                checks.append(_check(
                    f"{sid}:attribution:collision:{label}",
                    f"标签 {label} 被 {other} 与 {sender} 共享", False))
    return correct, len(records), checks


def scenario_metrics(result: ScenarioResult) -> ScenarioMetrics:
    """计算单场景四项指标（确定性）。"""
    correct, total, _ = attribution_records_check(result)
    expected = found = 0
    for expect in result.scenario.expects:
        turn = result.turn(expect.message_id)
        for needle in expect.self_facts_include:
            expected += 1
            if turn is not None and any(needle in f for f in turn.self_facts):
                found += 1
    cs_texts: list[Tuple[str, str]] = []
    for t in result.turns:
        cs_texts += [
            (f"{t.message_id} 关系提示", t.relationship_render),
            (f"{t.message_id} 表达提示", t.policy_render),
            (f"{t.message_id} 人格注入", t.persona_injected),
            (f"{t.message_id} 回复文本", t.reply_text),
        ]
    for p in result.probes:
        cs_texts.append((f"探针 {p.sender_id} 关系提示", p.relationship_render))
    cs_hits = customer_service_hits(cs_texts)

    intimacy: list[str] = []
    expect_by_id = {e.message_id: e for e in result.scenario.expects}
    for t in result.turns:
        expect = expect_by_id.get(t.message_id)
        flagged = (expect.no_intimacy if expect else False) or (
            t.familiarity == "stranger")
        if flagged:
            intimacy += intimacy_violations((
                (f"{t.message_id} 关系提示", t.relationship_render),
                (f"{t.message_id} 表达提示", t.policy_render),
                (f"{t.message_id} 人格注入", t.persona_injected),
            ))
    for p in result.probes:
        intimacy += intimacy_violations(
            ((f"探针 {p.sender_id} 关系提示", p.relationship_render),))

    checks = evaluate_scenario(result)
    return ScenarioMetrics(
        attribution_correct=correct, attribution_total=total,
        self_facts_found=found, self_facts_expected=expected,
        customer_service_hits=cs_hits, intimacy_violations=tuple(intimacy),
        checks_passed=sum(1 for c in checks if c.passed),
        checks_total=len(checks),
    )


def evaluate_scenario(result: ScenarioResult) -> list[CheckResult]:
    """求值一个场景的全部断言（focus 期望 + 探针 + 归属 + 词表扫描）。"""
    scenario: Scenario = result.scenario
    sid = scenario.scenario_id
    checks: list[CheckResult] = []

    label_of = {a.message_id: a.label for a in result.attributions}.get

    for expect in scenario.expects:
        turn = result.turn(expect.message_id)
        if turn is None:
            return [_check(f"{sid}:{expect.message_id}:missing",
                           "focus 消息未回放", False)]
        checks.extend(evaluate_turn(sid, turn, expect, label_of))

    for probe, expect in zip(result.probes, scenario.probes):
        checks.append(_check(
            f"{sid}:probe:{probe.sender_id}:familiarity",
            f"探针 {probe.sender_id} 应为 {expect.familiarity}",
            probe.familiarity == expect.familiarity,
            f"实际 {probe.familiarity}"))
        for needle in expect.render_include:
            checks.append(_check(
                f"{sid}:probe:{probe.sender_id}:render+:{needle}",
                f"探针关系提示应含「{needle}」",
                needle in probe.relationship_render,
                f"实际渲染：{probe.relationship_render!r}"))
        if expect.no_intimacy:
            hits = intimacy_violations(
                ((f"探针 {probe.sender_id}", probe.relationship_render),))
            checks.append(_check(
                f"{sid}:probe:{probe.sender_id}:no_intimacy",
                "陌生人关系提示不得含亲密称呼", not hits, "；".join(hits)))

    _, _, attr_checks = attribution_records_check(result)
    checks.extend(attr_checks)

    metrics_texts: list[Tuple[str, str]] = []
    for t in result.turns:
        metrics_texts += [
            (f"{t.message_id} 关系提示", t.relationship_render),
            (f"{t.message_id} 表达提示", t.policy_render),
            (f"{t.message_id} 人格注入", t.persona_injected),
            (f"{t.message_id} 回复文本", t.reply_text),
        ]
    cs_hits = customer_service_hits(metrics_texts)
    checks.append(_check(
        f"{sid}:customer_service", "客服腔命中数应为 0",
        not cs_hits, "；".join(cs_hits)))
    return checks


# ---------------------------------------------------------------------------
# 汇总指标
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkMetrics:
    scenarios: int
    attribution_correct: int
    attribution_total: int
    self_facts_found: int
    self_facts_expected: int
    customer_service_hits: Tuple[str, ...]
    intimacy_violations: Tuple[str, ...]
    checks_passed: int
    checks_total: int
    per_scenario: Tuple[Tuple[str, ScenarioMetrics], ...] = field(default_factory=tuple)

    @property
    def attribution_rate(self) -> float:
        return 1.0 if self.attribution_total == 0 else (
            self.attribution_correct / self.attribution_total)

    @property
    def self_facts_rate(self) -> float:
        return 1.0 if self.self_facts_expected == 0 else (
            self.self_facts_found / self.self_facts_expected)

    @property
    def all_passed(self) -> bool:
        return (self.attribution_correct == self.attribution_total
                and self.self_facts_found == self.self_facts_expected
                and not self.customer_service_hits
                and not self.intimacy_violations
                and self.checks_passed == self.checks_total)


def aggregate_metrics(results: Iterable[ScenarioResult]) -> BenchmarkMetrics:
    """汇总整个回放集的指标。"""
    results = tuple(results)
    per: list[Tuple[str, ScenarioMetrics]] = []
    attr_c = attr_t = fact_f = fact_e = chk_p = chk_t = 0
    cs: list[str] = []
    intim: list[str] = []
    for r in results:
        m = scenario_metrics(r)
        per.append((r.scenario.scenario_id, m))
        attr_c += m.attribution_correct
        attr_t += m.attribution_total
        fact_f += m.self_facts_found
        fact_e += m.self_facts_expected
        chk_p += m.checks_passed
        chk_t += m.checks_total
        cs += m.customer_service_hits
        intim += m.intimacy_violations
    return BenchmarkMetrics(
        scenarios=len(results),
        attribution_correct=attr_c, attribution_total=attr_t,
        self_facts_found=fact_f, self_facts_expected=fact_e,
        customer_service_hits=tuple(cs), intimacy_violations=tuple(intim),
        checks_passed=chk_p, checks_total=chk_t, per_scenario=tuple(per),
    )
