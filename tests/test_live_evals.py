"""issue #40 真实模型评测基线的 pytest 回归：**全离线**。

- ``run_live`` 用假 ``model_reply`` 驱动（录制调用、返回固定文本，零网络），
  断言 run JSON 结构与「旁观不发模型」纪律；
- pairing 断言匿名化（pairs JSONL 不出现任何 run 身份信息）与固定种子
  可复现翻转；
- report 断言回归阈值判定（触发 / 不触发 / 安全维度零容忍）；
- judge 断言评审 prompt 不含 A/B 真实身份。

真实模型调用只允许发生在手动入口（``python3 -m evals.live.live_runner`` /
``evals.live.judge``），本文件不发起任何网络请求。
"""

from __future__ import annotations

import csv
import json

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.ui.effective_persona import build_effective_persona
from whale_core.ui.persona_cards import import_external_card

from evals.live.judge import (
    JUDGE_DISCLAIMER,
    build_judge_prompt,
    judge_pairs,
    parse_judge_output,
)
from evals.live.live_runner import (
    LiveEvalConfigError,
    LiveRunError,
    build_deepseek_reply,
    load_env_file,
    load_run,
    main as runner_main,
    run_live,
)
from evals.live.pairing import (
    DEFAULT_SEED,
    DIMENSIONS,
    PairingError,
    build_pairs,
    load_scores,
    write_pairs,
)
from evals.live.report import (
    build_comparison_report,
    check_regression,
    dimension_means,
    find_failure_pairs,
    resolve_human_scores,
)
from evals.live.scenarios import (
    LIVE_SCENARIOS,
    LIVE_SCENARIOS_BY_ID,
)


def _fake_reply(text: str, *, persona: str = "", memories=(), history=()) -> str:
    """确定性假模型：录制式 callable（本测试用闭包收集调用）。"""
    return f"假回复：{text[:12]}"


def _make_run(model_name: str, outputs: dict, *,
              persona_sha: str = "sha-x") -> dict:
    """手工构造最小 run dict（配对 / 报告测试用，不跑 run_live）。"""
    scenarios = []
    for sid, turns in outputs.items():
        scenarios.append({
            "scenario_id": sid,
            "title": sid,
            "category": "test",
            "focus": [],
            "turns": [
                {"turn_index": i, "message_id": f"{sid}-{i}",
                 "sender_id": "u", "conversation_id": "private:u",
                 "is_group": False, "action": "reply",
                 "input_text": f"{sid} 输入{i}",
                 "input_digest": f"dg-{sid}-{i}",
                 "output_text": text}
                for i, text in enumerate(turns)
            ],
        })
    return {
        "format": "whale-live-run/1",
        "meta": {"model_name": model_name, "sampling": {"temperature": 1.0},
                 "persona_id": "default", "persona_content_sha": persona_sha,
                 "persona_source": "conversation_bound",
                 "code_revision": "abc1234",
                 "generated_at": "2026-11-04T00:00:00+00:00"},
        "scenarios": scenarios,
    }


# ---------------------------------------------------------------------------
# run_live 产物结构（假模型驱动）
# ---------------------------------------------------------------------------


def test_run_live_structure_and_observe_discipline(tmp_path):
    calls = []

    def recording(text, *, persona="", memories=(), history=()):
        calls.append({"text": text, "persona": persona,
                      "history": tuple(history)})
        return _fake_reply(text)

    store = InMemoryStore()
    # PR#48 起无内置 default 卡：安装一张自定义卡作为评测人格
    import_external_card(store, card_id="eval-card", title="评测卡",
                         prompt_text="冷静客观的评测助手")
    run = run_live(recording,
                   [LIVE_SCENARIOS_BY_ID["L03"], LIVE_SCENARIOS_BY_ID["L06"]],
                   persona_store=store, persona_id="eval-card",
                   model_name="fake-model",
                   sampling={"temperature": 1.0},
                   out_dir=tmp_path,
                   revision="abc1234",
                   now="2026-11-04T00:00:00+00:00")

    meta = run["meta"]
    assert meta["model_name"] == "fake-model"
    assert meta["sampling"] == {"temperature": 1.0}
    assert meta["persona_id"] == "eval-card"
    assert meta["code_revision"] == "abc1234"
    assert meta["generated_at"] == "2026-11-04T00:00:00+00:00"
    # 人格快照 sha 与 build_effective_persona 直接计算一致（issue #36 链路）
    assert meta["persona_content_sha"] == build_effective_persona(
        store, "eval-card").content_sha

    l03, l06 = run["scenarios"]
    # L03 群友互聊：前三条旁观不发模型，仅被@的第四条触发一次调用
    assert [t["action"] for t in l03["turns"]] == [
        "observe", "observe", "observe", "reply"]
    assert all(t["output_text"] == "" for t in l03["turns"][:3])
    assert l03["turns"][3]["output_text"].startswith("假回复：")
    # L06 私聊两条都回复 → 全场景共 3 次模型调用
    assert [t["action"] for t in l06["turns"]] == ["reply", "reply"]
    assert len(calls) == 3 == meta["model_calls"]
    # 回复轮注入人格非空且带 sha；旁观轮没有
    assert all(t["persona_injected_sha"] for t in l06["turns"])
    assert all(t["persona_injected_sha"] == "" for t in l03["turns"][:3])
    # 注入人格含真实代码路径追加的关系提示（G4 链路工作正常）
    assert "互动记录" in calls[-1]["persona"]
    # 群回复轮的 history 是时间线渲染（含归属标签），不是无身份历史
    group_call = calls[0]
    assert "时间线" in group_call["history"][0]["content"]
    # 输入 digest 稳定（sha1 前 12 位）
    assert all(len(t["input_digest"]) == 12
               for s in run["scenarios"] for t in s["turns"])

    on_disk = load_run(tmp_path / "run.json")
    assert on_disk["meta"] == meta
    assert len(on_disk["scenarios"]) == 2


def test_run_live_rejects_empty_model_output(tmp_path):
    def empty(text, *, persona="", memories=(), history=()):
        return "   "

    with pytest.raises(LiveRunError):
        run_live(empty, [LIVE_SCENARIOS_BY_ID["L05"]],
                 persona_store=InMemoryStore(), persona_id="default",
                 model_name="fake", sampling={}, out_dir=tmp_path)


def test_scenario_set_covers_ten_categories():
    categories = {s.category for s in LIVE_SCENARIOS}
    assert categories == {
        "daily_chat", "mentioned", "group_observe", "topic_switch",
        "cold_silence", "user_anxiety", "correction", "relationship_growth",
        "proactive_reminder", "tool_failure",
    }
    assert all(s.focus for s in LIVE_SCENARIOS)


# ---------------------------------------------------------------------------
# 真实模型装配：密钥纪律（离线断言，不发请求）
# ---------------------------------------------------------------------------


def test_build_deepseek_reply_missing_key_raises():
    with pytest.raises(LiveEvalConfigError, match="DEEPSEEK_API_KEY"):
        build_deepseek_reply({})


def test_runner_cli_missing_key_exit_code_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    env_file = tmp_path / "empty.env"
    env_file.write_text("# 只有注释\nOTHER_KEY=abc\n", encoding="utf-8")
    code = runner_main(["--env", str(env_file), "--out", str(tmp_path / "o")])
    assert code == 2
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_load_env_file_parsing(tmp_path):
    env_file = tmp_path / "x.env"
    env_file.write_text(
        '# 注释\nDEEPSEEK_API_KEY="sk-test-123"\n\n'
        "export DEEPSEEK_MODEL='deepseek-chat'\n无效行\n",
        encoding="utf-8")
    env = load_env_file(env_file)
    assert env == {"DEEPSEEK_API_KEY": "sk-test-123",
                   "DEEPSEEK_MODEL": "deepseek-chat"}


# ---------------------------------------------------------------------------
# pairing：匿名化 + 可复现翻转
# ---------------------------------------------------------------------------


def _two_runs():
    outs = {f"S{i:02d}": [f"输出{j}" for j in range(3)] for i in range(8)
            for _ in [0]}  # 8 场景 × 3 轮
    run_a = _make_run("baseline-model-xyz", {
        sid: [f"基线-{sid}-{j}" for j in range(3)] for sid in outs})
    run_b = _make_run("candidate-model-uvw", {
        sid: [f"候选-{sid}-{j}" for j in range(3)] for sid in outs})
    return run_a, run_b


def test_pairing_reproducible_with_fixed_seed():
    run_a, run_b = _two_runs()
    pairs1, mapping1 = build_pairs(run_a, run_b)
    pairs2, mapping2 = build_pairs(run_a, run_b)
    assert pairs1 == pairs2 and mapping1 == mapping2
    assert len(pairs1) == 24
    # 默认种子下两种翻转顺序都出现（不是单边恒常）
    assert {p["order"] for p in pairs1} == {"AB", "BA"}
    assert mapping1["seed"] == DEFAULT_SEED


def test_pairing_anonymization(tmp_path):
    run_a, run_b = _two_runs()
    pairs, mapping = build_pairs(run_a, run_b)
    paths = write_pairs(pairs, mapping, tmp_path)
    pairs_text = paths["pairs"].read_text(encoding="utf-8")
    # pairs.jsonl 绝不出现两侧身份信息
    assert "baseline-model-xyz" not in pairs_text
    assert "candidate-model-uvw" not in pairs_text
    assert "baseline" not in pairs_text and "candidate" not in pairs_text
    # 每个 pair 字段齐全
    sample = pairs[0]
    assert set(sample) == {"pair_id", "scenario_id", "turn_index",
                           "input_digest", "input_text",
                           "A_output", "B_output", "order"}
    # mapping.json 单独保存真实身份（评审后回收）
    mapping_text = paths["mapping"].read_text(encoding="utf-8")
    assert "baseline-model-xyz" in mapping_text
    assert "candidate-model-uvw" in mapping_text
    p0 = mapping["pairs"][sample["pair_id"]]
    assert set(p0) == {"A", "B"}
    assert {p0["A"], p0["B"]} == {"baseline", "candidate"}
    # 打分表模板：表头 + 每对一行空行
    with paths["scores_template"].open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][:1] == ["pair_id"]
    assert tuple(rows[0][1:5]) == DIMENSIONS[:4]
    assert len(rows) == len(pairs) + 1
    assert all(r[0].startswith("p-") for r in rows[1:])


def test_pairing_rejects_mismatched_inputs():
    run_a, run_b = _two_runs()
    run_b["scenarios"][0]["turns"][0]["input_digest"] = "tampered"
    with pytest.raises(PairingError, match="输入条件不一致"):
        build_pairs(run_a, run_b)


def test_load_scores_roundtrip(tmp_path):
    path = tmp_path / "scores.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["pair_id", *DIMENSIONS, "偏好", "备注"])
        writer.writerow(["p-001", "4/3", "5/4", "", "", "", "1/2", "", "A",
                         "候选更自然"])
        writer.writerow(["p-002"] + [""] * 9)  # 模板空行：跳过
    rows = load_scores(path)
    assert len(rows) == 1
    assert rows[0]["dims"]["自然度"] == (4, 3)
    assert rows[0]["dims"]["过度亲密"] == (1, 2)
    assert rows[0]["preference"] == "A"


def test_load_scores_rejects_bad_cell(tmp_path):
    path = tmp_path / "bad.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["pair_id", *DIMENSIONS, "偏好", "备注"])
        writer.writerow(["p-001", "9/3"] + [""] * 8)
    with pytest.raises(PairingError, match="1-5"):
        load_scores(path)


# ---------------------------------------------------------------------------
# report：回归阈值判定
# ---------------------------------------------------------------------------


def _scored_rows(dims_ab: dict, *, pair_id="p-001", preference=""):
    """构造一行 resolve 前的人工分（A/B 展示位分数）。"""
    return [{"pair_id": pair_id, "dims": dims_ab,
             "preference": preference, "note": ""}]


def _mapping_ab():
    """A=baseline / B=candidate 的固定 mapping。"""
    return {"seed": DEFAULT_SEED, "pair_count": 1,
            "pairs": {"p-001": {"A": "baseline", "B": "candidate"}},
            "sides": {"baseline": {}, "candidate": {}}, "unmatched": []}


def test_regression_not_triggered_within_threshold():
    checks = check_regression({"自然度": 4.2}, {"自然度": 3.9})
    assert checks[0]["regression"] is False
    assert checks[0]["delta"] == pytest.approx(-0.3)


def test_regression_triggered_by_positive_drop():
    checks = check_regression({"自然度": 4.5}, {"自然度": 3.8})
    assert checks[0]["regression"] is True  # 下降 0.7 > 0.5


def test_regression_triggered_by_negative_rise():
    checks = check_regression({"重复/客服腔": 1.5}, {"重复/客服腔": 2.3})
    assert checks[0]["regression"] is True  # 负向上升 0.8 > 0.5


def test_regression_safety_dim_zero_tolerance():
    # 安全维度（过度亲密）：任何上升即 regression，不看阈值
    assert check_regression({"过度亲密": 1.0}, {"过度亲密": 1.2}
                            )[0]["regression"] is True
    assert check_regression({"过度亲密": 2.0}, {"过度亲密": 2.0}
                            )[0]["regression"] is False
    assert check_regression({"过度亲密": 2.0}, {"过度亲密": 1.0}
                            )[0]["regression"] is False


def test_report_end_to_end(tmp_path):
    run_a, run_b = _two_runs()
    pairs, mapping = build_pairs(run_a, run_b)
    # 构造人工分：自然度候选明显下降（触发回归），其余持平
    scored = []
    for pair in pairs:
        sides = mapping["pairs"][pair["pair_id"]]
        # 展示位分数：无论翻转到哪一侧，候选侧自然度都低 1 分
        nat = (4, 3) if sides["A"] == "baseline" else (3, 4)
        scored.append({"pair_id": pair["pair_id"], "dims": {"自然度": nat},
                       "preference": "", "note": ""})
    resolved = resolve_human_scores(scored, mapping)
    means = dimension_means(resolved)
    assert means["baseline"]["自然度"] == pytest.approx(4.0)
    assert means["candidate"]["自然度"] == pytest.approx(3.0)

    text = build_comparison_report(run_a, run_b, mapping=mapping,
                                   human_scores=scored)
    assert "❌ **regression**" in text and "自然度" in text
    assert "baseline-model-xyz" in text  # 报告（评审后）含身份
    assert "sha-x" in text  # 人格 content_sha 上报告
    assert "## 局限性" in text

    # 候选与基线持平 → 不回归
    flat = [{"pair_id": p["pair_id"],
             "dims": {"自然度": (4, 4), "过度亲密": (1, 1)},
             "preference": "", "note": ""} for p in pairs]
    text_ok = build_comparison_report(run_a, run_b, mapping=mapping,
                                      human_scores=flat)
    assert "✅ 无 regression" in text_ok


def test_report_failure_pairs_and_preference():
    mapping = _mapping_ab()
    rows = _scored_rows({"自然度": (5, 2), "打扰感": (1, 4)}, preference="A")
    resolved = resolve_human_scores(rows, mapping)
    failures = find_failure_pairs(resolved)
    assert len(failures) == 1
    joined = "；".join(failures[0]["reasons"])
    assert "自然度" in joined and "打扰感" in joined and "baseline" in joined


def test_report_judge_appendix_disclaimer():
    run_a, run_b = _two_runs()
    pairs, mapping = build_pairs(run_a, run_b)
    judge_payload = {
        "disclaimer": JUDGE_DISCLAIMER,
        "judge_model": "judge-model",
        "results": [{"pair_id": pairs[0]["pair_id"],
                     "scenario_id": pairs[0]["scenario_id"],
                     "turn_index": 0,
                     "scores": {"自然度": {"A": 4, "B": 4}, "偏好": "平"}}],
    }
    text = build_comparison_report(run_a, run_b, mapping=mapping,
                                   human_scores=(), judge_scores=judge_payload)
    assert JUDGE_DISCLAIMER in text
    assert "模型裁判" in text
    assert "暂无人工评分" in text


# ---------------------------------------------------------------------------
# judge：prompt 匿名 + 输出解析（假裁判，零网络）
# ---------------------------------------------------------------------------


def test_judge_prompt_contains_no_identity():
    run_a, run_b = _two_runs()
    pairs, _ = build_pairs(run_a, run_b)
    for pair in pairs:
        prompt = build_judge_prompt(pair, focus=("naturalness",))
        assert "baseline-model-xyz" not in prompt
        assert "candidate-model-uvw" not in prompt
        assert "baseline" not in prompt and "candidate" not in prompt
        # 匿名展示位存在
        assert "回复 A" in prompt and "回复 B" in prompt
        assert pair["input_text"] in prompt


def test_judge_pairs_with_fake_callable():
    run_a, run_b = _two_runs()
    pairs, _ = build_pairs(run_a, run_b)
    seen_prompts = []

    def fake_judge(prompt: str) -> str:
        seen_prompts.append(prompt)
        return ('说明文字 {"自然度": {"A": 4, "B": 3}, "偏好": "A",'
                ' "理由": "更自然"} 尾巴')

    results = judge_pairs(fake_judge, pairs[:3])
    assert len(results) == 3 == len(seen_prompts)
    assert results[0]["scores"]["自然度"] == {"A": 4, "B": 3}
    with pytest.raises(ValueError):
        parse_judge_output("没有 JSON 的输出")
