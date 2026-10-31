"""markdown 评测报告（issue #19）：场景 × 指标表格 + 通过 / 失败。

报告全部来自确定性回放结果（``evals.runner`` / ``evals.metrics``），
不含任何模型自评内容。手动生成：

    PYTHONPATH=src python3 -m evals.report
"""

from __future__ import annotations

from typing import Iterable, Sequence, Tuple

from evals.metrics import (
    BenchmarkMetrics,
    ScenarioMetrics,
    aggregate_metrics,
    evaluate_scenario,
)
from evals.runner import ScenarioResult


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "–（无样本）"
    return f"{part}/{total}（{part / total:.0%}）"


def _ok(cond: bool) -> str:
    return "✅ 通过" if cond else "❌ 失败"


def build_report(results: Sequence[ScenarioResult]) -> str:
    """生成完整 markdown 评测报告。"""
    results = tuple(results)
    metrics = aggregate_metrics(results)
    lines: list[str] = []
    lines.append("# 人格自然度 / 关系连续性回放评测报告")
    lines.append("")
    lines.append("- 基准：`evals/scenarios.py` 固定样例集 "
                 f"（{metrics.scenarios} 个场景，{metrics.checks_total} 条确定性断言）")
    lines.append("- 执行：`evals/runner.py` —— InMemoryStore + 假 Brain，"
                 "驱动真实 RelationshipResolver / derive_policy / "
                 "GroupTimelineStore / WhaleCompanion 代码路径，不调真实模型 API")
    lines.append("- 指标：`evals/metrics.py` —— 全部为确定性字符串 / 结构比对")
    lines.append("")

    # ---- 指标总览 ----
    lines.append("## 指标总览")
    lines.append("")
    lines.append("| 指标 | 结果 | 目标 | 结论 |")
    lines.append("| --- | --- | --- | --- |")
    attr_ok = metrics.attribution_correct == metrics.attribution_total
    lines.append(f"| 归属正确率（标签不串人） | "
                 f"{_pct(metrics.attribution_correct, metrics.attribution_total)} "
                 f"| 100% | {_ok(attr_ok)} |")
    fact_ok = metrics.self_facts_found == metrics.self_facts_expected
    lines.append(f"| 自述事实进入 self_facts | "
                 f"{_pct(metrics.self_facts_found, metrics.self_facts_expected)} "
                 f"| 100% | {_ok(fact_ok)} |")
    lines.append(f"| 客服腔命中数 | {len(metrics.customer_service_hits)} "
                 f"| 0 | {_ok(not metrics.customer_service_hits)} |")
    lines.append(f"| 误亲密命中数 | {len(metrics.intimacy_violations)} "
                 f"| 0 | {_ok(not metrics.intimacy_violations)} |")
    lines.append("")

    # ---- 场景 × 指标 ----
    lines.append("## 场景 × 指标")
    lines.append("")
    lines.append("| 场景 | 归属 | 自述召回 | 客服腔 | 误亲密 | 断言 | 结论 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    titles = {r.scenario.scenario_id: r.scenario.title for r in results}
    for sid, m in metrics.per_scenario:
        row_ok = m.passed
        lines.append(
            f"| {sid} {titles.get(sid, '')} "
            f"| {_pct(m.attribution_correct, m.attribution_total)} "
            f"| {_pct(m.self_facts_found, m.self_facts_expected)} "
            f"| {len(m.customer_service_hits)} "
            f"| {len(m.intimacy_violations)} "
            f"| {m.checks_passed}/{m.checks_total} "
            f"| {_ok(row_ok)} |")
    lines.append("")

    # ---- 场景目的速览 ----
    lines.append("## 场景目的")
    lines.append("")
    for r in results:
        lines.append(f"- **{r.scenario.scenario_id} {r.scenario.title}**："
                     f"{r.scenario.purpose}")
    lines.append("")

    # ---- 失败断言明细 ----
    lines.append("## 失败断言明细")
    lines.append("")
    failures: list[str] = []
    for r in results:
        for c in evaluate_scenario(r):
            if not c.passed:
                failures.append(
                    f"- `{c.check_id}` {c.description}"
                    + (f" —— {c.detail}" if c.detail else ""))
    if failures:
        lines.extend(failures)
    else:
        lines.append("（无）")
    lines.append("")

    # ---- 词表附录 ----
    lines.append("## 附录：内置词表")
    lines.append("")
    from evals.metrics import CUSTOMER_SERVICE_PHRASES, INTIMATE_ADDRESS_TERMS
    lines.append("- 客服腔词表：" + "、".join(f"「{p}」" for p in CUSTOMER_SERVICE_PHRASES))
    lines.append("- 亲密称呼词表：" + "、".join(f"「{t}」" for t in INTIMATE_ADDRESS_TERMS))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """回放全部场景并打印报告；有失败断言时退出码 1。"""
    from evals.runner import run_scenario
    from evals.scenarios import SCENARIOS

    results = tuple(run_scenario(s) for s in SCENARIOS)
    print(build_report(results))
    return 0 if aggregate_metrics(results).all_passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
