"""A/B 盲评配对（issue #40）：两个 run JSON → 匿名 pairs + 打分表模板。

纪律：

- **匿名化**：pairs.jsonl 里只有 ``A_output`` / ``B_output`` 两个展示位，
  绝不出现 run 的身份信息（模型名、版本、文件路径）；真实身份只存于
  单独的 ``mapping.json``（评审结束后才允许回收对照）；
- **可复现**：A/B 展示顺序按固定种子随机翻转（默认
  ``DEFAULT_SEED``），同一对 run JSON + 同一种子 → 同一份 pairs；
- 配对键为 ``(scenario_id, turn_index)``，并要求 ``input_digest`` 一致
  （同一输入条件才可比）；只有双方都产生了模型输出的轮次才进入配对
  （旁观轮天然无输出，跳过）。

手动运行::

    PYTHONPATH=src python3 -m evals.live.pairing \
        --a evals/live/out/baseline/run.json \
        --b evals/live/out/candidate/run.json \
        --out evals/live/out/review-01
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from evals.live.live_runner import load_run

#: A/B 展示顺序的默认随机种子（固定 → 同一输入可复现同一份 pairs）。
DEFAULT_SEED = 20260401

#: 评审维度（1-5 分）。负向维度分高 = 差（见 NEGATIVE_DIMENSIONS）。
DIMENSIONS: Tuple[str, ...] = (
    "自然度",
    "角色一致性",
    "话题承接",
    "重复/客服腔",
    "情绪适配",
    "过度亲密",
    "打扰感",
)

#: 负向维度：分越高越差，回归判定方向相反（见 evals.live.report）。
NEGATIVE_DIMENSIONS: Tuple[str, ...] = ("重复/客服腔", "过度亲密", "打扰感")

#: 打分表 CSV 表头（issue #40 指定列；维度列填 ``A分/B分``，如 ``4/3``）。
SCORES_HEADER: Tuple[str, ...] = (
    ("pair_id",) + DIMENSIONS + ("偏好", "备注")
)

PAIRS_FILENAME = "pairs.jsonl"
MAPPING_FILENAME = "mapping.json"
SCORES_TEMPLATE_FILENAME = "scores_template.csv"


class PairingError(ValueError):
    """两个 run 不可配对（输入条件不一致等）。"""


def _reply_turns(run: Mapping) -> dict:
    """``(scenario_id, turn_index) -> turn``，只收有模型输出的轮次。"""
    out: dict = {}
    for scenario in run.get("scenarios", ()):  # type: ignore[union-attr]
        sid = scenario.get("scenario_id")
        for turn in scenario.get("turns", ()):
            if turn.get("output_text"):
                out[(sid, turn.get("turn_index"))] = turn
    return out


def build_pairs(run_a: Mapping, run_b: Mapping, *,
                seed: int = DEFAULT_SEED) -> Tuple[list, dict]:
    """把 baseline（A 来源）与 candidate（B 来源）配成匿名 pairs。

    返回 ``(pairs, mapping)``：pairs 面向评审（无身份）；mapping 记录每个
    pair_id 的 A/B 展示位各来自哪一侧（``"baseline"`` / ``"candidate"``），
    并附两侧 run 的元数据（模型、参数、人格 sha、代码版本）供评审后回收。
    """
    turns_a = _reply_turns(run_a)
    turns_b = _reply_turns(run_b)
    rng = random.Random(seed)
    pairs: list = []
    pair_map: dict = {}
    unmatched: list = []
    for n, key in enumerate(sorted(turns_a), start=1):
        ta = turns_a[key]
        tb = turns_b.get(key)
        if tb is None:
            unmatched.append({"scenario_id": key[0], "turn_index": key[1],
                              "reason": "candidate 侧无对应回复轮"})
            continue
        if ta.get("input_digest") != tb.get("input_digest"):
            raise PairingError(
                f"输入条件不一致，拒绝配对：{key[0]} turn {key[1]} "
                f"（input_digest {ta.get('input_digest')} != "
                f"{tb.get('input_digest')}）——同一盲评要求同一输入")
        order = rng.choice(("AB", "BA"))
        if order == "AB":
            a_out, b_out = ta["output_text"], tb["output_text"]
            side_a, side_b = "baseline", "candidate"
        else:
            a_out, b_out = tb["output_text"], ta["output_text"]
            side_a, side_b = "candidate", "baseline"
        pair_id = f"p-{n:03d}"
        pairs.append({
            "pair_id": pair_id,
            "scenario_id": key[0],
            "turn_index": key[1],
            "input_digest": ta["input_digest"],
            # 场景全部合成（issue #40 数据纪律），输入原文可随 pairs 给评审，
            # 否则「话题承接」等维度无法评判。
            "input_text": ta.get("input_text", ""),
            "A_output": a_out,
            "B_output": b_out,
            "order": order,
        })
        pair_map[pair_id] = {"A": side_a, "B": side_b}
    mapping = {
        "seed": seed,
        "pair_count": len(pairs),
        "sides": {
            "baseline": dict(run_a.get("meta") or {}),
            "candidate": dict(run_b.get("meta") or {}),
        },
        "pairs": pair_map,
        "unmatched": unmatched,
    }
    return pairs, mapping


def write_pairs(pairs: Sequence[Mapping], mapping: Mapping, out_dir) -> dict:
    """写出 pairs.jsonl / mapping.json / scores_template.csv，返回路径表。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pairs_path = out / PAIRS_FILENAME
    with pairs_path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    mapping_path = out / MAPPING_FILENAME
    mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    template_path = out / SCORES_TEMPLATE_FILENAME
    with template_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SCORES_HEADER)
        for pair in pairs:
            writer.writerow([pair["pair_id"]] + [""] * (len(SCORES_HEADER) - 1))
    return {"pairs": pairs_path, "mapping": mapping_path,
            "scores_template": template_path}


def load_pairs(path) -> list:
    """读取 pairs.jsonl。"""
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def load_mapping(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parse_dim_cell(raw: str, *, pair_id: str, dim: str) -> Optional[tuple]:
    """解析维度格 ``A分/B分``（各 1-5）；空格返回 None（该维度未评）。"""
    text = (raw or "").strip()
    if not text:
        return None
    parts = text.split("/")
    if len(parts) != 2:
        raise PairingError(
            f"{pair_id} 维度「{dim}」格式应为 A分/B分（如 4/3），得到 {text!r}")
    try:
        a_score, b_score = int(parts[0]), int(parts[1])
    except ValueError:
        raise PairingError(
            f"{pair_id} 维度「{dim}」分数必须是整数，得到 {text!r}") from None
    for score in (a_score, b_score):
        if not 1 <= score <= 5:
            raise PairingError(
                f"{pair_id} 维度「{dim}」分数须在 1-5，得到 {score}")
    return a_score, b_score


def load_scores(path) -> list:
    """读取人工打分表（CSV）。整行全空的维度跳过；偏好列取 ``A``/``B``/``平``。

    返回每行 ``{pair_id, dims: {维度: (A分, B分)}, preference, note}``；
    未填写任何维度的行被忽略（模板原样回收时不报错）。
    """
    rows: list = []
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        for record in csv.DictReader(fh):
            pair_id = (record.get("pair_id") or "").strip()
            if not pair_id:
                continue
            dims = {}
            for dim in DIMENSIONS:
                parsed = _parse_dim_cell(record.get(dim) or "",
                                         pair_id=pair_id, dim=dim)
                if parsed is not None:
                    dims[dim] = parsed
            preference = (record.get("偏好") or "").strip()
            if preference and preference not in ("A", "B", "平"):
                raise PairingError(
                    f"{pair_id} 偏好列只接受 A/B/平，得到 {preference!r}")
            if not dims and not preference:
                continue
            rows.append({
                "pair_id": pair_id,
                "dims": dims,
                "preference": preference,
                "note": (record.get("备注") or "").strip(),
            })
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    """手动入口：两个 run JSON → pairs.jsonl / mapping.json / 打分表模板。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python3 -m evals.live.pairing",
        description="A/B 盲评配对（issue #40）：匿名化 + 固定种子可复现。")
    parser.add_argument("--a", required=True, help="baseline run JSON")
    parser.add_argument("--b", required=True, help="candidate run JSON")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"翻转种子（默认 {DEFAULT_SEED}）")
    args = parser.parse_args(argv)
    try:
        run_a, run_b = load_run(args.a), load_run(args.b)
        pairs, mapping = build_pairs(run_a, run_b, seed=args.seed)
        paths = write_pairs(pairs, mapping, args.out)
    except (PairingError, ValueError, OSError) as exc:
        print(f"配对失败：{exc}", file=sys.stderr)
        return 1
    print(f"pairs：{paths['pairs']}（{len(pairs)} 对，种子 {args.seed}）")
    print(f"mapping：{paths['mapping']}（评审结束前勿对照）")
    print(f"打分表模板：{paths['scores_template']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
