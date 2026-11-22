"""Activation acts only on candidates already approved by their source."""

from whale_core.memory.activation import MemoryCandidate, activate


def _candidate(key: str, summary: str, score: float = 0.5):
    return MemoryCandidate(key, summary, score, "owner_fact")


def test_multi_part_request_keeps_delivery_and_access_rule():
    query = "冷藏件到了，能进门吗，签收先联系谁？"
    rows = activate(query, (
        _candidate("noise", "今天猫粮在窗边", 0.99),
        _candidate("access", "旧门码不能再开门", 0.4),
        _candidate("delivery", "冷藏件放保温袋；签收先打电话", 0.3),
    ), limit=2)
    assert {row.candidate.key for row in rows} == {"access", "delivery"}
    assert next(row for row in rows if row.candidate.key == "access").decision == "CONSTRAIN"


def test_boundary_is_scoped_to_audience_and_topic():
    rule = _candidate("privacy", "不要在群里透露我的健康细节")
    assert activate("群里有人问我的身体怎么样", (rule,), is_group=True)[0].decision == "CONSTRAIN"
    assert activate("群里讨论门锁", (rule,), is_group=True) == ()
    assert activate("私聊里我的身体怎么样", (rule,)) == ()


def test_plain_fact_uses_retrieval_order_without_special_task():
    rows = activate("我的猫叫什么名字", (
        _candidate("weak", "我喜欢蓝色", 0.2),
        _candidate("name", "我养的猫叫年糕", 0.9),
    ), limit=1)
    assert [row.candidate.key for row in rows] == ["name"]


def test_duplicate_candidate_key_is_selected_once():
    fact = _candidate("name", "我养的猫叫年糕")
    assert len(activate("我的猫叫什么", (fact, fact))) == 1


def test_explicit_recall_request_keeps_the_named_fact():
    rows = activate("香菜的事还记得吗", (
        _candidate("food", "用户对香菜过敏", 0.45),
        _candidate("noise", "用户喜欢登山", 0.9),
    ))
    assert [row.candidate.key for row in rows] == ["food"]


def test_group_disclosure_does_not_pull_unrelated_pet_care_rule():
    rows = activate("群里有人问栗子最近怎么了，想回复", (
        _candidate("privacy", "不要在群里透露栗子的病情"),
        _candidate("treat", "栗子最近不要吃鱼味零食"),
    ), is_group=True)
    assert [row.candidate.key for row in rows] == ["privacy"]


def test_group_food_choice_keeps_meal_rule_and_privacy_boundary():
    rows = activate("群聊里大家定火锅锅底，想照顾 A 又不暴露健康信息", (
        _candidate("meal", "聚餐不要默认点辣油，应留清汤选项"),
        _candidate("privacy", "不要在群里说 A 胃怎么了"),
        _candidate("pet", "栗子不要吃鱼味零食"),
    ), is_group=True)
    assert {row.candidate.key for row in rows} == {"meal", "privacy"}


def test_sharing_question_keeps_subject_fact_and_applicable_boundary():
    rows = activate("可以告诉群友我的猫叫什么吗", (
        _candidate("name", "我养的猫叫年糕"),
        _candidate("boundary", "不要在群里透露我的猫的名字"),
    ), is_group=True)
    assert {row.candidate.key for row in rows} == {"name", "boundary"}
    assert next(row for row in rows if row.candidate.key == "boundary").decision == "CONSTRAIN"


def test_emergency_access_rule_requires_emergency_context():
    rows = activate("旧码还能进门吗", (
        _candidate("normal", "旧码已作废，不能进门"),
        _candidate("emergency", "漏水时也不能用旧码进门，应联系物业"),
    ))
    assert [row.candidate.key for row in rows] == ["normal"]
    emergency = activate("门口有水声，能用旧码进门吗", (
        _candidate("normal", "旧码已作废，不能进门"),
        _candidate("emergency", "漏水或冒烟时也不能用旧码进门，应联系物业"),
    ))
    assert {row.candidate.key for row in emergency} == {"normal", "emergency"}


def test_dinner_constraint_does_not_pull_pet_food_rule():
    rows = activate("今晚做海鲜粥，能用共用海鲜汤底吗", (
        _candidate("person", "最近按过敏处理，避开虾蟹贝和共用海鲜汤底"),
        _candidate("pet", "猫吃三文鱼会挠脖子，不要买三文鱼罐头"),
    ))
    assert [row.candidate.key for row in rows] == ["person"]


def test_one_off_choice_is_not_reused_as_durable_preference():
    rows = activate("下次聚会给 A 买什么饮料？", (
        _candidate("once", "A 只是当天想喝气泡水，不应作为长期饮品偏好"),
        _candidate("usual", "A 平时喜欢燕麦奶"),
    ))
    assert all(row.candidate.key != "once" for row in rows)


def test_one_off_code_rule_applies_to_record_retention_question():
    rule = _candidate("code", "报名费页面的一次性核销码不应写入长期记忆")
    rows = activate("整理报名费凭证时该保留什么？", (rule,))
    assert [row.candidate.key for row in rows] == ["code"]
    assert rows[0].decision == "CONSTRAIN"
