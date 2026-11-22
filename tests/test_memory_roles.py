"""Pure classification does not read storage or grant permissions."""

from whale_core.memory.relations import query_attributes, relation_hints
from whale_core.memory.roles import (
    access_relevance, boundary_relevance, memory_role,
)
from whale_core.memory.task_plan import evidence_needs, plan_query


def test_disclosure_and_access_are_different_roles():
    assert memory_role("不要在群里透露我的健康细节") == "boundary"
    assert memory_role("旧门码不能再开门") == "access_permission"
    assert memory_role("我喜欢燕麦奶") == "preference"


def test_boundary_needs_matching_audience_and_topic():
    rule = "不要在群里透露我的健康细节"
    assert boundary_relevance(rule, "群里有人问我的身体怎么样", subject_id="owner") > 0
    assert boundary_relevance(rule, "私聊里我的身体怎么样", subject_id="owner") == 0
    assert boundary_relevance(rule, "群里讨论门锁", subject_id="owner") == 0


def test_group_context_alone_does_not_activate_unrelated_boundary():
    rule = "在画室群里 A 没回时，不要公开点名提醒，应私聊提醒"
    assert boundary_relevance(rule, "画室群里有人问 A 的真实姓名",
                              subject_id="owner", is_group=True) == 0
    assert boundary_relevance(rule, "画室群里 A 没回，要不要公开点名提醒",
                              subject_id="owner", is_group=True) > 0


def test_private_followup_does_not_use_public_explanation_rule():
    public_rule = "群聊里不能透露 A 的检查和病史"
    private_rule = "当 A 群里晚到时，B 不应主动追问 A 的检查和病史"
    query = "A 群里说晚到，B 想主动私聊追问是否和检查有关"
    assert boundary_relevance(public_rule, query, subject_id="owner", is_group=True) == 0
    assert boundary_relevance(private_rule, query, subject_id="owner", is_group=True) > 0


def test_quoted_private_alias_can_anchor_a_group_boundary():
    rule = "A 私下用“河灯”报名，不得在群里公开或帮别人猜"
    assert boundary_relevance(rule, "群里有人问“河灯”是谁",
                              subject_id="owner", is_group=True) > 0
    assert boundary_relevance(rule, "群里有人问门码是什么",
                              subject_id="owner", is_group=True) == 0


def test_access_rule_does_not_apply_to_unrelated_delivery():
    rule = "旧门码不能再开门"
    assert access_relevance(rule, "可以用旧门码进门吗") > 0
    assert access_relevance(rule, "包裹放在哪里") == 0


def test_task_plan_and_relation_hints_are_pure():
    query = "冷藏件到了，能进门吗，签收先联系谁？"
    plan = plan_query(query)
    assert {"delivery", "access"} <= set(plan.needs)
    assert "delivery" in evidence_needs("冷藏件放保温袋；签收先打电话", plan)
    hints = relation_hints("冷藏件放保温袋；签收先打电话")
    assert any(hint["entity"] == "delivery" for hint in hints)
    assert query_attributes(query)["access"] == {"permission"}


def test_tell_me_is_not_third_party_disclosure():
    assert "disclosure" not in plan_query("告诉我我的猫叫什么").needs
    assert "disclosure" in plan_query("可以告诉群友我的猫叫什么吗").needs
