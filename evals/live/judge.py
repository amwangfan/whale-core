"""模型裁判辅助（issue #40）：按 rubric 给匿名 A/B 对打分。

**定位：仅供参考，不能单独作为通过依据。** 模型裁判与人工盲评的关系：

- 评审 prompt 只含匿名的「回复 A / 回复 B」与合成输入，**不含任何 A/B
  真实身份**（模型名、版本、baseline/candidate 标签一律不出现）；
- 产物 ``judge_scores.json`` 自带免责声明，报告（``evals.live.report``）
  必须把 judge 分放在附录并标注「模型裁判仅供参考」；
- 真实模型调用只允许发生在手动入口（``python3 -m evals.live.judge``），
  测试注入假 callable（见 tests/test_live_evals.py）。

手动运行::

    PYTHONPATH=src python3 -m evals.live.judge \
        --env /opt/whale/secrets/qq_workflow.env \
        --pairs evals/live/out/review-01/pairs.jsonl \
        --out evals/live/out/review-01/judge_scores.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from evals.live.pairing import DIMENSIONS, load_pairs

#: 免责声明：产物与报告中必须原样标注。
JUDGE_DISCLAIMER = "模型裁判仅供参考：分数由模型按 rubric 生成，不能单独作为通过依据。"

JUDGE_SCORES_FILENAME = "judge_scores.json"

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


def build_judge_prompt(pair: Mapping, *, focus: Sequence[str] = ()) -> str:
    """构造匿名评审 prompt（只含「回复 A / 回复 B」，绝无真实身份信息）。"""
    focus_line = ("、".join(focus)) if focus else "综合质量"
    dims = "、".join(DIMENSIONS)
    return (
        "你是对话质量评审。下面是一段合成评测场景中的用户输入，以及两个匿名"
        "回复（回复 A、回复 B，顺序已随机化，不代表任何版本身份）。\n"
        "请按 rubric 逐维度给两个回复各打 1-5 分（1 最差，5 最好；"
        "「重复/客服腔」「过度亲密」「打扰感」为负向维度，1 = 完全没有该问题，"
        "5 = 问题严重），并给出整体偏好。\n"
        f"评审维度：{dims}\n"
        f"本场景关注点：{focus_line}\n"
        "评分要点：自然口语、不端着；前后话题承接一致；不堆砌客服腔套话；"
        "情绪回应贴切；保持边界、不擅自亲密；不打扰（旁观语境不乱接话）。\n"
        "只输出一个 JSON 对象，形如：\n"
        '{"自然度": {"A": 4, "B": 3}, "角色一致性": {"A": 4, "B": 4}, ..., '
        '"偏好": "A"|"B"|"平", "理由": "一句话"}\n'
        "不要输出 JSON 以外的任何内容。\n\n"
        f"【用户输入】\n{pair.get('input_text', '')}\n\n"
        f"【回复 A】\n{pair.get('A_output', '')}\n\n"
        f"【回复 B】\n{pair.get('B_output', '')}\n"
    )


def parse_judge_output(text: str) -> dict:
    """从模型输出中提取 JSON 分数块；提取失败抛 ValueError。"""
    match = _JSON_BLOCK_RE.search(text or "")
    if not match:
        raise ValueError(f"裁判输出不含 JSON 块：{(text or '')[:80]!r}")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("裁判输出 JSON 不是对象")
    return data


def judge_pairs(judge_reply: Callable[[str], str], pairs: Sequence[Mapping],
                *, focus_by_scenario: Optional[Mapping[str, Sequence[str]]] = None
                ) -> list:
    """逐对调用裁判 callable，返回每对的结构化分数（含原始输出摘要）。"""
    focus_by_scenario = focus_by_scenario or {}
    out: list = []
    for pair in pairs:
        prompt = build_judge_prompt(
            pair, focus=focus_by_scenario.get(pair.get("scenario_id"), ()))
        raw = judge_reply(prompt)
        scores = parse_judge_output(raw)
        out.append({
            "pair_id": pair.get("pair_id"),
            "scenario_id": pair.get("scenario_id"),
            "turn_index": pair.get("turn_index"),
            "scores": scores,
        })
    return out


def write_judge_scores(results: Sequence[Mapping], out_path,
                       *, model_name: str = "") -> Path:
    """写 judge_scores.json（自带免责声明）。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "disclaimer": JUDGE_DISCLAIMER,
        "judge_model": model_name,
        "pair_count": len(results),
        "results": list(results),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def load_judge_scores(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """手动入口（真实调用裁判模型）。配置缺失退出码 2，运行失败退出码 1。"""
    import argparse
    import os
    import sys

    from evals.live.live_runner import LiveEvalConfigError, build_deepseek_reply
    from evals.live.scenarios import LIVE_SCENARIOS

    parser = argparse.ArgumentParser(
        prog="python3 -m evals.live.judge",
        description=f"模型裁判（issue #40）。{JUDGE_DISCLAIMER}")
    parser.add_argument("--env", default=None, help="含 DEEPSEEK_API_KEY 的 env 文件")
    parser.add_argument("--pairs", required=True, help="pairs.jsonl 路径")
    parser.add_argument("--out", required=True, help="judge_scores.json 输出路径")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.env:
        from evals.live.live_runner import load_env_file

        try:
            env.update(load_env_file(args.env))
        except OSError as exc:
            print(f"读取 env 文件失败：{exc}", file=sys.stderr)
            return 2
    try:
        reply = build_deepseek_reply(env)
    except LiveEvalConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    pairs = load_pairs(args.pairs)
    focus_by_scenario = {s.scenario_id: s.focus for s in LIVE_SCENARIOS}
    try:
        results = judge_pairs(lambda prompt: reply(prompt), pairs,
                              focus_by_scenario=focus_by_scenario)
    except (ValueError, RuntimeError) as exc:
        print(f"裁判运行失败：{exc}", file=sys.stderr)
        return 1
    path = write_judge_scores(
        results, args.out,
        model_name=getattr(reply, "model_name", "") or env.get(
            "DEEPSEEK_MODEL", ""))
    print(f"{JUDGE_DISCLAIMER}")
    print(f"judge 分数已写入：{path}（{len(results)} 对）")
    return 0


if __name__ == "__main__":  # pragma: no cover —— 手动运行，真实调用模型
    raise SystemExit(main())
