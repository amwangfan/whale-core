"""多模型裁判注册表（owner 决策 2026-11-01）：kimi / qwen / glm / deepseek
多裁判交叉打分，降低单一裁判与被测模型同源带来的风格偏好风险。

纪律（与 judge.py 相同）：

- **仅供参考，不能单独作为通过依据**；通过/回归结论以人工盲评为准；
- 评审 prompt 只含匿名「回复 A / 回复 B」，不含任何 A/B 身份信息；
- 密钥只从 env 读取（``--env`` 文件或进程环境），绝不落盘、不进产物；
- 缺 key 的平台**如实跳过**（status=skipped_no_key），不伪造分数；
- 真实调用只发生在手动入口（CLI / ``whale eval judge``）；测试注入假
  callable，零网络。

手动运行::

    PYTHONPATH=src python3 -m evals.live.judges \\
        --env /opt/whale/secrets/qq_workflow.env \\
        --pairs evals/live/out/pairs-01/pairs.jsonl \\
        --out  evals/live/out/pairs-01/judges/
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from evals.live.judge import (
    JUDGE_DISCLAIMER,
    judge_pairs,
    parse_judge_output,
)
from evals.live.live_runner import load_env_file
from evals.live.pairing import DIMENSIONS, load_pairs

#: 裁判产物文件名（按 provider 分文件 + 汇总）。
JUDGES_SUMMARY_FILENAME = "judges_scores.json"


@dataclass(frozen=True)
class JudgeProvider:
    """一个裁判模型（全部 OpenAI 兼容 chat completions 协议）。

    owner 决策 2026-11-01：生产所有模型都走当前已接入的统一平台
    （``DEEPSEEK_BASE_URL`` 端点 + ``DEEPSEEK_API_KEY``），kimi/qwen/glm
    只是该平台上的模型 id；配置了平台专属 key/端点 env 时优先用专属。
    """

    name: str            # 注册名（报告/文件名用）
    default_model: str   # 默认裁判模型 id
    model_env: str       # 可用该 env 覆盖模型 id
    key_envs: tuple      # 按优先级读取 API key 的 env 名
    base_envs: tuple     # 按优先级读取端点的 env 名
    fallback_base: str   # 无任何端点 env 时的官方默认


#: 平台注册表。模型 id 默认按 owner 指定；各平台实际模型 id 以官方文档为准，
#: 可用对应 *_MODEL env 覆盖（不改代码）。
JUDGE_PROVIDERS: tuple[JudgeProvider, ...] = (
    JudgeProvider("deepseek", "deepseek-chat",
                  "WHALE_JUDGE_DEEPSEEK_MODEL",
                  ("DEEPSEEK_API_KEY",), ("DEEPSEEK_BASE_URL",),
                  "https://api.deepseek.com/v1"),
    JudgeProvider("kimi", "kimi-k3",
                  "WHALE_JUDGE_KIMI_MODEL",
                  ("MOONSHOT_API_KEY", "DEEPSEEK_API_KEY"),
                  ("MOONSHOT_BASE_URL", "DEEPSEEK_BASE_URL"),
                  "https://api.moonshot.cn/v1"),
    JudgeProvider("qwen", "qwen3.8-max",
                  "WHALE_JUDGE_QWEN_MODEL",
                  ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"),
                  ("DASHSCOPE_BASE_URL", "DEEPSEEK_BASE_URL"),
                  "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    JudgeProvider("glm", "glm-5.2",
                  "WHALE_JUDGE_GLM_MODEL",
                  ("ZHIPU_API_KEY", "DEEPSEEK_API_KEY"),
                  ("ZHIPU_BASE_URL", "DEEPSEEK_BASE_URL"),
                  "https://open.bigmodel.cn/api/paas/v4"),
)


def _first_env(env: Mapping[str, str], names) -> str:
    for n in names:
        value = (env.get(n) or "").strip()
        if value:
            return value
    return ""


def effective_key_env(p: JudgeProvider, env: Mapping[str, str]) -> str:
    """实际提供 key 的 env 名（仅用于展示来源；不回显值）。"""
    for n in p.key_envs:
        if (env.get(n) or "").strip():
            return n
    return p.key_envs[0]


def effective_base_url(p: JudgeProvider, env: Mapping[str, str]) -> str:
    return _first_env(env, p.base_envs) or p.fallback_base


def effective_model(p: JudgeProvider, env: Mapping[str, str]) -> str:
    """裁判模型 id：专用覆盖 env > deepseek 沿用生产 DEEPSEEK_MODEL > 默认。"""
    override = (env.get(p.model_env) or "").strip()
    if override:
        return override
    if p.name == "deepseek" and (env.get("DEEPSEEK_MODEL") or "").strip():
        return env["DEEPSEEK_MODEL"].strip()
    return p.default_model


def probe_judges(env: Mapping[str, str]) -> list[dict]:
    """返回各平台可用性（含模型 id；**绝不含 key**）。"""
    out = []
    for p in JUDGE_PROVIDERS:
        key = _first_env(env, p.key_envs)
        out.append({
            "name": p.name,
            "available": bool(key),
            "model": effective_model(p, env),
            "env_key": effective_key_env(p, env),  # 只提示变量名，不回显值
        })
    return out


def make_judge_callable(provider: JudgeProvider, env: Mapping[str, str],
                        *, timeout: float = 60.0) -> Callable[[str], str]:
    """按平台装配真实裁判 callable（OpenAI 兼容；复用 DeepSeekCompanion）。"""
    api_key = _first_env(env, provider.key_envs)
    if not api_key:
        raise ValueError(f"{'/'.join(provider.key_envs)} 未配置")
    model = effective_model(provider, env)
    base_url = effective_base_url(provider, env)
    from whale_core.core.deepseek import DeepSeekCompanion

    client = DeepSeekCompanion(api_key=api_key, base_url=base_url,
                               model=model, timeout=timeout)

    def _judge(prompt: str) -> str:
        return client.reply(prompt)

    return _judge


def run_multi_judge(env: Mapping[str, str], pairs: Sequence[Mapping], *,
                    only: Sequence[str] = (),
                    focus_by_scenario: Optional[Mapping[str, Sequence[str]]] = None,
                    callable_factory=None) -> dict:
    """对每个可用平台逐对打分；返回汇总 dict（可JSON序列化）。

    ``only`` 限定平台子集；``make_judge_callable`` 可注入（测试）。
    任一平台失败不影响其他平台（逐平台 try，失败如实记录 error_kind）。
    """
    results: dict[str, dict] = {}
    names = [p.name for p in JUDGE_PROVIDERS]
    for provider in JUDGE_PROVIDERS:
        if only and provider.name not in only:
            continue
        model_id = effective_model(provider, env)
        if not _first_env(env, provider.key_envs):
            results[provider.name] = {
                "status": "skipped_no_key", "model": model_id,
                "env_key": "/".join(provider.key_envs),
            }
            continue
        try:
            judge = (callable_factory or make_judge_callable)(
                provider, env)
            scores = judge_pairs(judge, pairs,
                                 focus_by_scenario=focus_by_scenario)
            results[provider.name] = {
                "status": "ok", "model": model_id, "scores": scores,
            }
        except Exception as exc:  # 网络/配额/解析失败如实记录
            results[provider.name] = {
                "status": "failed", "model": model_id,
                "error_kind": type(exc).__name__,
            }
    return {
        "disclaimer": JUDGE_DISCLAIMER,
        "providers": results,
        "pair_count": len(pairs),
        "judged_providers": [n for n, r in results.items()
                             if r.get("status") == "ok"],
    }


def consistency_summary(summary: Mapping) -> dict:
    """裁判间一致性：偏好一致的 pair 比例 + 各维度跨裁判均分差。"""
    ok = {n: r for n, r in (summary.get("providers") or {}).items()
          if r.get("status") == "ok" and r.get("scores")}
    if len(ok) < 2:
        return {"note": "可用裁判不足 2 个，无一致性统计"}
    names = sorted(ok)
    pair_ids = sorted({s.get("pair_id") for r in ok.values()
                       for s in r["scores"]})
    agree = 0
    compared = 0
    for pid in pair_ids:
        prefs = set()
        for name in names:
            entry = next((s for s in ok[name]["scores"]
                          if s.get("pair_id") == pid), None)
            pref = (entry or {}).get("scores", {}).get("偏好")
            if pref in ("A", "B", "平"):
                prefs.add(pref)
        if prefs:
            compared += 1
            if len(prefs) == 1:
                agree += 1
    return {
        "preference_agreement": agree,
        "preference_compared": compared,
        "judges": names,
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="多模型裁判打分（issue #40；owner 决策 2026-11-01）"
                    "——手动运行，真实调用各平台模型；密钥只从 env 读取。")
    parser.add_argument("--env", default="", help="env 文件路径")
    parser.add_argument("--pairs", required=True, help="pairs.jsonl 路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--only", default="",
                        help="只用这些平台（逗号分隔，如 kimi,qwen）")
    args = parser.parse_args(argv)

    env = dict(**__import__("os").environ)
    if args.env:
        env.update(load_env_file(args.env))
    probe = probe_judges(env)
    for p in probe:
        state = "可用" if p["available"] else f"缺 {p['env_key']}"
        print(f"裁判 {p['name']:<10} {p['model']:<20} {state}")
    if not any(p["available"] for p in probe):
        print("错误：没有任何可用裁判平台（至少配置一个平台的 API key）",
              file=sys.stderr)
        return 2

    pairs = load_pairs(args.pairs)
    only = tuple(s.strip() for s in args.only.split(",") if s.strip())
    summary = run_multi_judge(env, pairs, only=only)
    summary["consistency"] = consistency_summary(summary)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, result in summary["providers"].items():
        if result.get("status") == "ok":
            (out_dir / f"judge_scores.{name}.json").write_text(
                json.dumps({"disclaimer": JUDGE_DISCLAIMER,
                            "provider": name, "model": result["model"],
                            "scores": result["scores"]},
                           ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / JUDGES_SUMMARY_FILENAME).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = summary["judged_providers"]
    print(f"完成：{len(ok)} 个平台打分（{', '.join(ok) or '无'}）"
          f"→ {out_dir / JUDGES_SUMMARY_FILENAME}")
    cons = summary["consistency"]
    if "preference_agreement" in cons:
        print(f"裁判偏好一致：{cons['preference_agreement']}/"
              f"{cons['preference_compared']} 对")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
