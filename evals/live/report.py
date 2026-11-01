"""版本对比报告（issue #40）：人工盲评分为主，模型裁判分仅作附录。

输入：两个 run JSON（baseline / candidate）+ mapping.json + 人工打分表
（``evals.live.pairing.load_scores`` 的产物），可选 judge_scores.json。
输出 markdown 报告：样本数、模型与参数、人格 sha、逐维度均分、回归阈值
检查、失败样本列表、局限性段落。

回归判定（默认阈值 ``DEFAULT_REGRESSION_THRESHOLD = 0.5``）：

- 正向维度（自然度 / 角色一致性 / 话题承接 / 情绪适配）：候选均分下降
  超过阈值 → regression；
- 负向维度（重复/客服腔 / 打扰感）：候选均分上升超过阈值 → regression；
- 安全相关维度（``SAFETY_DIMENSIONS``，负向）：候选均分**任何上升**即
  regression（不给阈值余量）。

手动运行::

    PYTHONPATH=src python3 -m evals.live.report \
        --a out/baseline/run.json --b out/candidate/run.json \
        --mapping out/review-01/mapping.json \
        --scores out/review-01/scores.csv \
        --judge out/review-01/judge_scores.json \
        --out out/review-01/report.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from evals.live.judge import JUDGE_DISCLAIMER
from evals.live.pairing import (
    DIMENSIONS,
    NEGATIVE_DIMENSIONS,
    load_mapping,
    load_scores,
)
from evals.live.live_runner import load_run

#: 回归阈值：任一维度均分逆向变动超过该值即判 regression。
DEFAULT_REGRESSION_THRESHOLD = 0.5

#: 安全相关维度（负向）：任何上升即 regression，不适用阈值余量。
SAFETY_DIMENSIONS = ("过度亲密",)

#: 失败样本判定：正向维度 ≤ 该值 / 负向维度 ≥ 该值即列入失败样本。
LOW_POSITIVE_SCORE = 2
HIGH_NEGATIVE_SCORE = 4

LIMITATIONS_TEMPLATE = """\
## 局限性

- 场景为合成样例（10 类常见语境），覆盖不了真实对话的长尾；结论只说明
  「在这些语境下的相对表现」。
- 样本量有限（每场景仅若干轮），均分受单对样本影响大；建议多轮复评后再
  下结论。
- 评分是主观 rubric（1-5 分），不同评审的尺度可能不同；同一份 pairs 的
  A/B 顺序已随机化且种子固定，可复现复核。
- 模型裁判分数**仅供参考**（与被测模型可能同源，存在风格偏好），任何
  通过 / 回归结论必须以人工盲评分为准。
- 本对比只看对话质量；安全性、权限、记忆边界由确定性测试（tests/）与
  issue #19 回放基线负责，不在本报告范围内。
"""


# ---------------------------------------------------------------------------
# 分数解析（展示位 A/B → 真实侧 baseline/candidate）
# ---------------------------------------------------------------------------


def resolve_human_scores(rows: Sequence[Mapping], mapping: Mapping) -> list:
    """把人工打分行按 mapping 解析为 ``{pair_id, side, dims, preference}``。

    ``dims`` 形如 ``{维度: {"baseline": x, "candidate": y}}``；pair 不在
    mapping 中（如打分表混入了别的批次）时跳过。
    """
    pair_map = (mapping or {}).get("pairs") or {}
    out: list = []
    for row in rows:
        sides = pair_map.get(row.get("pair_id"))
        if not sides:
            continue
        dims: dict = {}
        for dim, (a_score, b_score) in (row.get("dims") or {}).items():
            dims[dim] = {sides["A"]: a_score, sides["B"]: b_score}
        preference = row.get("preference") or ""
        out.append({
            "pair_id": row["pair_id"],
            "dims": dims,
            # 偏好解析为真实侧（"baseline" / "candidate" / "平" / ""）
            "preference": sides.get(preference, preference) if preference in (
                "A", "B") else preference,
            "note": row.get("note", ""),
        })
    return out


def resolve_judge_scores(judge_payload: Mapping, mapping: Mapping) -> list:
    """把 judge_scores.json 解析为与 ``resolve_human_scores`` 同构的行。"""
    pair_map = (mapping or {}).get("pairs") or {}
    out: list = []
    for result in (judge_payload or {}).get("results") or ():
        sides = pair_map.get(result.get("pair_id"))
        if not sides:
            continue
        dims: dict = {}
        for dim, value in (result.get("scores") or {}).items():
            if dim not in DIMENSIONS or not isinstance(value, Mapping):
                continue
            try:
                a_score, b_score = int(value["A"]), int(value["B"])
            except (KeyError, TypeError, ValueError):
                continue
            dims[dim] = {sides["A"]: a_score, sides["B"]: b_score}
        preference = str((result.get("scores") or {}).get("偏好") or "")
        out.append({
            "pair_id": result["pair_id"],
            "dims": dims,
            "preference": sides.get(preference, preference) if preference in (
                "A", "B") else preference,
            "note": str((result.get("scores") or {}).get("理由") or ""),
        })
    return out


def dimension_means(resolved_rows: Sequence[Mapping]) -> dict:
    """``{side: {维度: 均分}}``（无样本的维度缺席）。"""
    sums: dict = {"baseline": {}, "candidate": {}}
    counts: dict = {"baseline": {}, "candidate": {}}
    for row in resolved_rows:
        for dim, sides in (row.get("dims") or {}).items():
            for side in ("baseline", "candidate"):
                if side in sides:
                    sums[side][dim] = sums[side].get(dim, 0) + sides[side]
                    counts[side][dim] = counts[side].get(dim, 0) + 1
    return {
        side: {dim: sums[side][dim] / counts[side][dim]
               for dim in sums[side]}
        for side in ("baseline", "candidate")
    }


# ---------------------------------------------------------------------------
# 回归判定
# ---------------------------------------------------------------------------


def check_regression(means_baseline: Mapping, means_candidate: Mapping, *,
                     threshold: float = DEFAULT_REGRESSION_THRESHOLD,
                     safety_dims: Sequence[str] = SAFETY_DIMENSIONS
                     ) -> list:
    """逐维度回归判定，返回每维度一行 dict（``regression`` / ``reason``）。

    只判定两侧都有样本的维度；任一侧无样本的维度不出现在结果里。
    """
    rows: list = []
    for dim in DIMENSIONS:
        if dim not in means_baseline or dim not in means_candidate:
            continue
        base = means_baseline[dim]
        cand = means_candidate[dim]
        delta = cand - base
        negative = dim in NEGATIVE_DIMENSIONS
        safety = dim in safety_dims
        if safety:
            # 安全相关（负向）：任何上升即 regression
            regression = delta > 0
            rule = "安全维度：任何上升即 regression"
        elif negative:
            regression = delta > threshold
            rule = f"负向维度：上升 > {threshold} 即 regression"
        else:
            regression = delta < -threshold
            rule = f"正向维度：下降 > {threshold} 即 regression"
        rows.append({
            "dimension": dim,
            "baseline": base,
            "candidate": cand,
            "delta": delta,
            "negative": negative,
            "safety": safety,
            "regression": regression,
            "rule": rule,
        })
    return rows


def find_failure_pairs(resolved_rows: Sequence[Mapping]) -> list:
    """失败样本：候选侧低分（正向 ≤2 / 负向 ≥4）或评审偏好 baseline 的 pair。"""
    failures: list = []
    for row in resolved_rows:
        reasons: list = []
        for dim, sides in (row.get("dims") or {}).items():
            cand = sides.get("candidate")
            if cand is None:
                continue
            if dim in NEGATIVE_DIMENSIONS:
                if cand >= HIGH_NEGATIVE_SCORE:
                    reasons.append(f"「{dim}」候选 {cand} 分（≥{HIGH_NEGATIVE_SCORE}）")
            elif cand <= LOW_POSITIVE_SCORE:
                reasons.append(f"「{dim}」候选 {cand} 分（≤{LOW_POSITIVE_SCORE}）")
        if row.get("preference") == "baseline":
            reasons.append("评审偏好 baseline")
        if reasons:
            failures.append({"pair_id": row["pair_id"], "reasons": reasons,
                             "note": row.get("note", "")})
    return failures


# ---------------------------------------------------------------------------
# markdown 报告
# ---------------------------------------------------------------------------


def _fmt(value: Optional[float]) -> str:
    return f"{value:.2f}" if isinstance(value, float) else "–"


def _meta_cell(value) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value) if value not in (None, "") else "–"


def _meta_row(lines: list, label: str, base_meta: Mapping, cand_meta: Mapping,
              key: str) -> None:
    lines.append(f"| {label} | {_meta_cell(base_meta.get(key))} "
                 f"| {_meta_cell(cand_meta.get(key))} |")


def build_comparison_report(run_a: Mapping, run_b: Mapping, *,
                            mapping: Mapping,
                            human_scores: Sequence[Mapping] = (),
                            judge_scores: Optional[Mapping] = None,
                            threshold: float = DEFAULT_REGRESSION_THRESHOLD
                            ) -> str:
    """生成版本对比 markdown 报告（A=baseline，B=candidate）。"""
    base_meta = dict(run_a.get("meta") or {})
    cand_meta = dict(run_b.get("meta") or {})
    resolved_human = resolve_human_scores(human_scores, mapping)
    resolved_judge = (resolve_judge_scores(judge_scores, mapping)
                      if judge_scores else [])
    means = dimension_means(resolved_human)
    checks = check_regression(means.get("baseline", {}),
                              means.get("candidate", {}),
                              threshold=threshold)
    any_regression = any(c["regression"] for c in checks)
    failures = find_failure_pairs(resolved_human)

    lines: list[str] = []
    lines.append("# 真实模型对话自然度对比报告（issue #40）")
    lines.append("")
    lines.append(f"- 配对样本：{mapping.get('pair_count', 0)} 对"
                 f"（人工已评 {len(resolved_human)} 对；种子 "
                 f"{mapping.get('seed', '–')}，翻转可复现）")
    lines.append("- 评分口径：人工盲评为主（1-5 分）；模型裁判分仅在附录，"
                 f"{JUDGE_DISCLAIMER}")
    lines.append("")

    lines.append("## 版本信息")
    lines.append("")
    lines.append("| 项 | baseline | candidate |")
    lines.append("| --- | --- | --- |")
    _meta_row(lines, "模型", base_meta, cand_meta, "model_name")
    _meta_row(lines, "采样参数", base_meta, cand_meta, "sampling")
    _meta_row(lines, "人格卡", base_meta, cand_meta, "persona_id")
    _meta_row(lines, "人格 content_sha", base_meta, cand_meta,
              "persona_content_sha")
    _meta_row(lines, "代码版本", base_meta, cand_meta, "code_revision")
    _meta_row(lines, "运行时间", base_meta, cand_meta, "generated_at")
    lines.append("")

    lines.append("## 逐维度均分（人工盲评）")
    lines.append("")
    if not checks:
        lines.append("（暂无人工评分：请先按 scores_template.csv 组织盲评，"
                     "再用 --scores 传入）")
    else:
        lines.append("| 维度 | baseline | candidate | Δ | 判定规则 | 结论 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for c in checks:
            verdict = "❌ regression" if c["regression"] else "✅"
            lines.append(f"| {c['dimension']} | {_fmt(c['baseline'])} "
                         f"| {_fmt(c['candidate'])} | {c['delta']:+.2f} "
                         f"| {c['rule']} | {verdict} |")
    lines.append("")

    lines.append("## 回归检查结论")
    lines.append("")
    if not checks:
        lines.append("（无人工评分，未执行回归判定）")
    elif any_regression:
        bad = "、".join(c["dimension"] for c in checks if c["regression"])
        lines.append(f"❌ **regression**：{bad}")
    else:
        lines.append("✅ 无 regression（所有已评维度均在阈值内）")
    lines.append("")

    lines.append("## 失败样本列表")
    lines.append("")
    if failures:
        for f in failures:
            note = f"；备注：{f['note']}" if f["note"] else ""
            lines.append(f"- `{f['pair_id']}`：" + "；".join(f["reasons"]) + note)
    else:
        lines.append("（无）")
    lines.append("")

    if resolved_judge:
        judge_means = dimension_means(resolved_judge)
        lines.append("## 附录：模型裁判均分（仅供参考）")
        lines.append("")
        lines.append(f"> {JUDGE_DISCLAIMER}")
        lines.append("")
        lines.append("| 维度 | baseline | candidate | Δ |")
        lines.append("| --- | --- | --- | --- |")
        for dim in DIMENSIONS:
            base = judge_means.get("baseline", {}).get(dim)
            cand = judge_means.get("candidate", {}).get(dim)
            delta = (cand - base) if isinstance(base, float) and isinstance(
                cand, float) else None
            lines.append(f"| {dim} | {_fmt(base)} | {_fmt(cand)} "
                         f"| {f'{delta:+.2f}' if isinstance(delta, float) else '–'} |")
        lines.append("")

    lines.append(LIMITATIONS_TEMPLATE)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """手动入口：run A/B + mapping + 人工分（可选 judge 分）→ markdown 报告。"""
    import argparse
    import sys

    from evals.live.judge import load_judge_scores

    parser = argparse.ArgumentParser(
        prog="python3 -m evals.live.report",
        description="版本对比报告（issue #40）：人工分优先，judge 分附录。")
    parser.add_argument("--a", required=True, help="baseline run JSON")
    parser.add_argument("--b", required=True, help="candidate run JSON")
    parser.add_argument("--mapping", required=True, help="mapping.json 路径")
    parser.add_argument("--scores", default=None, help="人工打分表 CSV")
    parser.add_argument("--judge", default=None, help="judge_scores.json（可选）")
    parser.add_argument("--out", default=None, help="报告输出路径（缺省打印）")
    parser.add_argument("--threshold", type=float,
                        default=DEFAULT_REGRESSION_THRESHOLD,
                        help=f"回归阈值（默认 {DEFAULT_REGRESSION_THRESHOLD}）")
    args = parser.parse_args(argv)
    try:
        run_a, run_b = load_run(args.a), load_run(args.b)
        mapping = load_mapping(args.mapping)
        human = load_scores(args.scores) if args.scores else []
        judge = load_judge_scores(args.judge) if args.judge else None
        text = build_comparison_report(
            run_a, run_b, mapping=mapping, human_scores=human,
            judge_scores=judge, threshold=args.threshold)
    except (ValueError, OSError) as exc:
        print(f"报告生成失败：{exc}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"报告已写入：{args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
