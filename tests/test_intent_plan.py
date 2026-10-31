"""IntentPlan 结构化意图契约测试（全离线：无网络 / 无子进程）。"""

from __future__ import annotations

import dataclasses

import pytest

from whale_core.core.intent_plan import (
    IntentKind,
    IntentPlan,
    IntentPlanError,
    parse_intent_plan,
    plan_from_model_text,
)

# ---------------------------------------------------------------------------
# 合法路径
# ---------------------------------------------------------------------------


def test_minimal_goal_only_plan_uses_defaults() -> None:
    plan = parse_intent_plan({"goal": "写一首关于鲸鱼的诗"})
    assert plan.goal == "写一首关于鲸鱼的诗"
    assert plan.kind == IntentKind.TASK.value == "task"
    assert plan.slots == {}
    assert plan.missing == ()
    assert plan.capabilities == ()
    assert plan.expected_output == ""
    assert plan.success_criteria == ""
    assert plan.needs_clarification is False
    assert plan.clarification_question == ""
    assert plan.reason == ""
    assert plan.risk_hint == ""
    assert plan.task_id == ""
    assert plan.timeout_s == 60.0
    assert len(plan.intent_id) == 32  # uuid4().hex


def test_full_fields_parse() -> None:
    data = {
        "goal": "把会议纪要发到邮箱",
        "kind": "task",
        "slots": {"to": "a@b.c", "detail": {"x": 1}},
        "missing": ["when"],
        "capabilities": ["mail.send", "web.search"],
        "expected_output": "发送回执",
        "success_criteria": "对方收到",
        "needs_clarification": 0,
        "clarification_question": "",
        "reason": "用户明确要求",
        "risk_hint": "low",
        "task_id": "t-1",
        "timeout_s": 30,
    }
    plan = parse_intent_plan(data)
    assert plan.kind == "task"
    assert plan.slots == {"to": "a@b.c", "detail": {"x": 1}}
    assert plan.missing == ("when",) and isinstance(plan.missing, tuple)
    assert plan.capabilities == ("mail.send", "web.search") and isinstance(plan.capabilities, tuple)
    assert plan.expected_output == "发送回执"
    assert plan.success_criteria == "对方收到"
    assert plan.needs_clarification is False  # bool(0)
    assert plan.reason == "用户明确要求"
    assert plan.risk_hint == "low"
    assert plan.task_id == "t-1"
    assert plan.timeout_s == 30.0
    # slots 防御性拷贝：调用方之后改原 dict 不影响冻结 plan
    data["slots"]["to"] = "evil@example.com"
    assert plan.slots["to"] == "a@b.c"


def test_kind_chat_roundtrip() -> None:
    plan = parse_intent_plan({"goal": "随便聊聊", "kind": "chat"})
    assert plan.kind == IntentKind.CHAT.value
    assert plan.needs_clarification is False


# ---------------------------------------------------------------------------
# 校验失败路径（消息必含字段名）
# ---------------------------------------------------------------------------


def test_data_must_be_mapping() -> None:
    for bad in (None, 42, "goal", [{"goal": "x"}]):
        with pytest.raises(IntentPlanError, match="data"):
            parse_intent_plan(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):  # IntentPlanError 是 ValueError 子类
        parse_intent_plan(None)  # type: ignore[arg-type]


def test_goal_missing() -> None:
    with pytest.raises(IntentPlanError, match="goal"):
        parse_intent_plan({"kind": "task"})


def test_goal_blank_after_strip() -> None:
    with pytest.raises(IntentPlanError, match="goal"):
        parse_intent_plan({"goal": "   \n\t "})


def test_goal_non_str() -> None:
    with pytest.raises(IntentPlanError, match="goal"):
        parse_intent_plan({"goal": 123})


def test_goal_truncated_at_2000_not_error() -> None:
    plan = parse_intent_plan({"goal": "x" * 2500})
    assert len(plan.goal) == 2000


def test_kind_invalid_value() -> None:
    with pytest.raises(IntentPlanError, match="kind"):
        parse_intent_plan({"goal": "g", "kind": "auto-approve"})


def test_kind_non_str() -> None:
    with pytest.raises(IntentPlanError, match="kind"):
        parse_intent_plan({"goal": "g", "kind": 7})


def test_slots_must_be_mapping() -> None:
    with pytest.raises(IntentPlanError, match="slots"):
        parse_intent_plan({"goal": "g", "slots": [("a", 1)]})
    with pytest.raises(IntentPlanError, match="slots"):
        parse_intent_plan({"goal": "g", "slots": "a=1"})


def test_missing_rejects_non_str_element() -> None:
    with pytest.raises(IntentPlanError, match="missing"):
        parse_intent_plan({"goal": "g", "missing": ["ok", 1]})


def test_missing_rejects_bare_string() -> None:
    with pytest.raises(IntentPlanError, match="missing"):
        parse_intent_plan({"goal": "g", "missing": "when"})


def test_capabilities_rejects_non_str_element() -> None:
    with pytest.raises(IntentPlanError, match="capabilities"):
        parse_intent_plan({"goal": "g", "capabilities": ["web.search", None]})


def test_clarify_requires_clarification_question() -> None:
    with pytest.raises(IntentPlanError, match="clarification_question"):
        parse_intent_plan({"goal": "g", "kind": "clarify"})
    with pytest.raises(IntentPlanError, match="clarification_question"):
        parse_intent_plan({"goal": "g", "kind": "clarify", "clarification_question": "  "})


# ---------------------------------------------------------------------------
# 归一化语义
# ---------------------------------------------------------------------------


def test_clarify_forces_needs_clarification() -> None:
    plan = parse_intent_plan(
        {
            "goal": "订票",
            "kind": "clarify",
            "clarification_question": "请问哪天出发？",
            "needs_clarification": False,
        }
    )
    assert plan.kind == IntentKind.CLARIFY.value
    assert plan.needs_clarification is True
    assert plan.clarification_question == "请问哪天出发？"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (0, 1.0),
        (-10, 1.0),
        (9999, 3600.0),
        ("abc", 60.0),
        (None, 60.0),
        ("30", 30.0),
        (2.5, 2.5),
    ],
)
def test_timeout_clamped_and_fallback(raw: object, want: float) -> None:
    plan = parse_intent_plan({"goal": "g", "timeout_s": raw})
    assert plan.timeout_s == want


def test_text_fields_str_coerced_and_truncated() -> None:
    plan = parse_intent_plan(
        {
            "goal": "g",
            "expected_output": 123,
            "success_criteria": "s" * 600,
            "reason": None,
            "risk_hint": "r" * 300,
            "task_id": "t" * 200,
        }
    )
    assert plan.expected_output == "123"
    assert len(plan.success_criteria) == 500
    assert plan.reason == ""
    assert len(plan.risk_hint) == 200
    assert len(plan.task_id) == 128


def test_unknown_fields_dropped_no_self_approval() -> None:
    plan = parse_intent_plan(
        {
            "goal": "g",
            "approved": True,
            "admin": True,
            "permissions": ["*"],
            "risk_hint": "definitely-safe",
        }
    )
    field_names = {f.name for f in dataclasses.fields(plan)}
    assert "approved" not in field_names
    assert not hasattr(plan, "approved")
    assert not hasattr(plan, "admin")
    assert not hasattr(plan, "permissions")
    assert plan.risk_hint == "definitely-safe"  # 仅参考，无授权语义


def test_intent_id_core_generated_not_from_model() -> None:
    forged = parse_intent_plan({"goal": "g", "intent_id": "forged-id"})
    assert forged.intent_id != "forged-id"
    other = parse_intent_plan({"goal": "g"})
    assert forged.intent_id != other.intent_id


# ---------------------------------------------------------------------------
# plan_from_model_text
# ---------------------------------------------------------------------------


def test_model_text_pure_json() -> None:
    plan = plan_from_model_text('{"goal": "查天气", "kind": "task", "capabilities": ["web.search"]}')
    assert plan.goal == "查天气"
    assert plan.capabilities == ("web.search",)


def test_model_text_prose_wrapped() -> None:
    text = '好的，这是我的计划：\n{"goal": "写诗", "kind": "chat"}\n以上，请查收。'
    plan = plan_from_model_text(text)
    assert plan.goal == "写诗"
    assert plan.kind == "chat"


def test_model_text_code_fence() -> None:
    text = '```json\n{\n  "goal": "总结网页",\n  "kind": "task"\n}\n```'
    plan = plan_from_model_text(text)
    assert plan.goal == "总结网页"


def test_model_text_no_json_raises() -> None:
    with pytest.raises(IntentPlanError, match="no JSON object in model output"):
        plan_from_model_text("这里没有 JSON，只有散文。")
    with pytest.raises(IntentPlanError, match="no JSON object in model output"):
        plan_from_model_text("{")


def test_model_text_nested_braces_and_braces_in_strings() -> None:
    text = (
        '计划如下：{"goal": "用 {template} 渲染并发送", '
        '"slots": {"a": {"b": [1, {"c": 2}]}}, "kind": "task"} 完。'
    )
    plan = plan_from_model_text(text)
    assert plan.goal == "用 {template} 渲染并发送"
    assert plan.slots == {"a": {"b": [1, {"c": 2}]}}


def test_model_text_skips_invalid_brace_block() -> None:
    plan = plan_from_model_text('备注 {这不是 JSON} 计划：{"goal": "g"}')
    assert plan.goal == "g"


def test_model_text_validation_error_propagates() -> None:
    with pytest.raises(IntentPlanError, match="kind"):
        plan_from_model_text('{"goal": "g", "kind": "bogus"}')


def test_model_text_clarify_flow() -> None:
    text = '需要澄清：```json\n{"goal": "订票", "kind": "clarify", "clarification_question": "哪天出发？"}\n```'
    plan = plan_from_model_text(text)
    assert plan.kind == "clarify"
    assert plan.needs_clarification is True
    assert plan.clarification_question == "哪天出发？"


# ---------------------------------------------------------------------------
# 不可变性
# ---------------------------------------------------------------------------


def test_plan_is_frozen() -> None:
    plan = parse_intent_plan({"goal": "g"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.goal = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.timeout_s = 5.0  # type: ignore[misc]
