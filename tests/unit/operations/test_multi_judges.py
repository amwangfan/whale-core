"""多模型裁判（evals/live/judges.py）离线测试：探测/跳过/一致性/CLI 退出码。"""

from __future__ import annotations

import json

from evals.live.judges import (
    JUDGE_PROVIDERS,
    consistency_summary,
    main,
    probe_judges,
    run_multi_judge,
)

PAIRS = [
    {"pair_id": "p1", "scenario_id": "L01", "turn_index": 0,
     "input_text": "你好", "A_output": "你好呀", "B_output": "你好。"},
    {"pair_id": "p2", "scenario_id": "L01", "turn_index": 1,
     "input_text": "吃了吗", "A_output": "吃了", "B_output": "没呢"},
]

GOOD_JSON = json.dumps({
    "自然度": {"A": 4, "B": 3}, "角色一致性": {"A": 4, "B": 4},
    "话题承接": {"A": 4, "B": 3}, "重复/客服腔": {"A": 1, "B": 1},
    "情绪适配": {"A": 4, "B": 3}, "过度亲密": {"A": 1, "B": 1},
    "打扰感": {"A": 1, "B": 1}, "偏好": "A", "理由": "更自然",
}, ensure_ascii=False)


class TestProbe:
    def test_no_keys_all_unavailable(self):
        probe = probe_judges({})
        assert all(not p["available"] for p in probe)
        assert {p["name"] for p in probe} == {"deepseek", "kimi", "qwen", "glm"}
        # 不回显任何 key 值
        assert all("key" not in str(p).lower() or p["env_key"] in str(p)
                   for p in probe)

    def test_key_present_marks_available(self):
        probe = probe_judges({"MOONSHOT_API_KEY": "sk-x",
                              "WHALE_JUDGE_KIMI_MODEL": "kimi-k3-pro"})
        kimi = next(p for p in probe if p["name"] == "kimi")
        assert kimi["available"] and kimi["model"] == "kimi-k3-pro"  # env 覆盖模型 id


class TestRunMultiJudge:
    def _factory(self, replies):
        def make(provider, env):
            return lambda prompt: replies[provider.name]
        return make

    def test_only_available_providers_scored(self):
        # 专属 key 优先；无专属时回落共享网关 key（DEEPSEEK_API_KEY）
        env = {"MOONSHOT_API_KEY": "k"}  # 只有 kimi 专属 key
        summary = run_multi_judge(
            env, PAIRS,
            callable_factory=self._factory({"kimi": GOOD_JSON}))
        assert set(summary["judged_providers"]) == {"kimi"}
        assert summary["providers"]["qwen"]["status"] == "skipped_no_key"
        assert summary["providers"]["glm"]["status"] == "skipped_no_key"
        assert summary["providers"]["deepseek"]["status"] == "skipped_no_key"

    def test_shared_gateway_key_enables_all(self):
        """owner 决策：所有模型走统一平台（共享 key+端点）。"""
        env = {"DEEPSEEK_API_KEY": "k", "DEEPSEEK_BASE_URL": "https://gw.local/v1"}
        summary = run_multi_judge(
            env, PAIRS,
            callable_factory=self._factory(
                {n: GOOD_JSON for n in ("deepseek", "kimi", "qwen", "glm")}))
        assert set(summary["judged_providers"]) == {
            "deepseek", "kimi", "qwen", "glm"}
        deepseek = summary["providers"]["deepseek"]
        assert len(deepseek["scores"]) == 2
        assert summary["disclaimer"]

    def test_provider_failure_isolated(self):
        def factory(provider, env):
            if provider.name == "deepseek":
                def bad(_p):
                    raise RuntimeError("quota")
                return bad
            return lambda prompt: GOOD_JSON
        env = {"DEEPSEEK_API_KEY": "k", "MOONSHOT_API_KEY": "k"}
        summary = run_multi_judge(env, PAIRS, callable_factory=factory)
        assert summary["providers"]["deepseek"]["status"] == "failed"
        assert summary["providers"]["deepseek"]["error_kind"] == "RuntimeError"
        assert summary["providers"]["kimi"]["status"] == "ok"

    def test_consistency_agreement(self):
        env = {"DEEPSEEK_API_KEY": "k", "MOONSHOT_API_KEY": "k"}
        summary = run_multi_judge(
            env, PAIRS,
            callable_factory=self._factory(
                {"deepseek": GOOD_JSON, "kimi": GOOD_JSON}))
        cons = consistency_summary(summary)
        assert cons["preference_agreement"] == 2
        assert cons["preference_compared"] == 2

    def test_cli_no_provider_exit_2(self, tmp_path, capsys):
        pairs = tmp_path / "pairs.jsonl"
        pairs.write_text("\n".join(json.dumps(p) for p in PAIRS),
                         encoding="utf-8")
        rc = main(["--pairs", str(pairs), "--out", str(tmp_path / "out")])
        # 环境里恰好有 DEEPSEEK_API_KEY 时不走 2（取决于测试环境）——
        # 这里只验证：无 key 时输出明确且非零
        import os
        all_key_envs = {n for p in JUDGE_PROVIDERS for n in p.key_envs}
        if not any(os.environ.get(n) for n in all_key_envs):
            assert rc == 2
