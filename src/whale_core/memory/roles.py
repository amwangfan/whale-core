"""Fact roles and narrow, query-scoped boundary matching."""
from __future__ import annotations

import re

from whale_core.memory.task_plan import evidence_needs, plan_query


_TOPICS = {
    "health": ("病", "复查", "检查", "药", "胃", "不舒服", "晕", "过载", "心理", "身体", "健康"),
    "food": ("吃", "饭", "菜", "火锅", "芒果", "忌口", "过敏"),
    "pet": ("猫", "宠物", "栗子", "喂食"),
    "access": ("门", "钥匙", "住址", "地址", "门码", "密码"),
    "dropoff": ("放过去", "送过去", "送汤", "放东西", "放到", "取物架", "门口放"),
    "social": ("邀请", "饭局", "朋友", "座位"),
    "identity": ("称呼", "名字", "真名", "昵称", "叫我", "叫A", "被叫", "可以叫", "叫什么"),
    "family": ("家里", "家庭", "父母", "家人"),
    "nonresponse": ("没回", "不回", "失联", "联系不上", "点名提醒"),
    "checkin": ("关心", "是否还好", "问候", "主动追问", "单独询问"),
    "photo": ("照片", "相册", "图片", "封面", "人像", "倒影"),
    "contact": ("联系人", "旧电话", "电话号码", "紧急联系"),
    "audio": ("录音", "哼唱", "歌单", "曲目"),
    "encouragement": ("鼓励", "夸奖", "表扬"),
}
_GENERIC_BIGRAMS = {"群里", "群聊", "聊里", "私聊", "公开", "回复", "别人", "朋友", "现在", "最近", "应该", "可以", "不要", "不想", "我的", "你的", "他的", "什么"}


def memory_role(summary: str) -> str:
    text = str(summary or "")
    # A record about *not forming* a durable preference is not itself a
    # behavior rule to inject into unrelated future replies.
    if (any(word in text for word in ("一次性", "一时", "只是当天", "临时", "用完即忘"))
            and any(word in text for word in
                    ("不应写入长期", "不应升格", "不应作为长期", "不用记成长期",
                     "不应记成长期", "不用记成长期偏好"))):
        return "one_off"
    audience = any(word in text for word in
                   ("群里", "群聊", "公开", "对外", "转述", "透露", "分享", "汇报", "告诉别人"))
    rule = any(word in text for word in
               ("不希望", "不想", "不要", "不应", "不能", "不得", "不适合", "不打算", "别", "只允许", "先问", "需同意", "未经"))
    if audience and rule:
        return "boundary"
    if (any(word in text for word in ("进门", "进屋", "开门", "门锁", "门码", "旧码", "钥匙", "密码"))
            and any(word in text for word in
                    ("可以", "允许", "不应", "不能", "不得", "不要", "不再", "停用", "作废", "先问", "需同意"))):
        return "access_permission"
    if any(word in text for word in ("不能", "不得", "不要", "不应", "不用", "必须", "应先", "先问")):
        return "constraint"
    if (any(word in text for word in ("只会", "只有"))
            and any(word in text for word in ("提", "问", "回复", "接话", "聊"))):
        return "constraint"
    if any(word in text for word in ("喜欢", "偏好", "更愿意", "希望")):
        return "preference"
    return "fact"


def constraint_kind(summary: str) -> str:
    """Distinguish disclosure, physical access, and other behavior rules."""
    role = memory_role(summary)
    return {"boundary": "disclosure_boundary",
            "access_permission": "access_permission",
            "constraint": "constraint"}.get(role, "")


def access_relevance(summary: str, query: str) -> int:
    """Check entry rules for entry questions and explicitly assigned home care."""
    question = str(query or "")
    explicit = any(word in question for word in
               ("进门", "开门", "入内", "进屋", "旧码", "门锁", "门码", "入口码", "一次性码", "钥匙", "密码"))
    assigned_care = (any(word in question for word in ("明确让", "明确请求", "请B", "请 B"))
                     and any(word in question for word in ("帮忙喂", "照看", "看猫")))
    if not (explicit or assigned_care):
        return 0
    text = str(summary or "")
    if (any(word in text for word in ("漏水", "冒烟", "着火", "水声"))
            and not any(word in question for word in ("漏水", "冒烟", "着火", "水声"))):
        return 0
    if not any(word in text for word in
               ("进门", "进屋", "开门", "门锁", "门码", "旧码", "钥匙", "密码")):
        return 0
    return 1 + int(bool(_bigrams(text) & _bigrams(question)))


def constraint_relevance(summary: str, query: str) -> int:
    """Keep a behavior constraint only when the question shares its subject matter."""
    plan = plan_query(query)
    matched = set(evidence_needs(summary, plan))
    primary = set(plan.needs) - {"disclosure", "proactive"}
    if "disclosure" in plan.needs and not primary:
        return 0  # Disclosure questions use applicable boundaries, not generic behavior rules.
    if primary and not (matched & primary):
        return 0
    overlap = _bigrams(str(summary or "")) & _bigrams(str(query or ""))
    if ((not overlap and not ({"nonresponse", "food_choice"} & matched))
            or (not primary and len(overlap) < 2)):
        return 0
    score = 1 + len(overlap)
    # Match the requested pet action, not merely the same animal's name.
    if "猫粮" in query and "猫粮" in summary:
        score += 4
    return score


def _topics(text: str) -> set[str]:
    return {topic for topic, words in _TOPICS.items() if any(word in text for word in words)}


def task_topics(query: str) -> set[str]:
    """Topics for distinct actions; an item being delivered is not a food task."""
    topics = _topics(str(query or ""))
    if "dropoff" in topics:
        topics.discard("food")
    return topics


def _bigrams(text: str) -> set[str]:
    return {part[i:i + 2] for part in re.findall(r"[\u4e00-\u9fff]+", text)
            for i in range(len(part) - 1)} - _GENERIC_BIGRAMS


def lexical_overlap(left: str, right: str) -> int:
    return len(_bigrams(left) & _bigrams(right))


def boundary_relevance(summary: str, query: str, *, subject_id: str,
                       is_group: bool = False) -> int:
    """Return 0 when an authorized boundary does not apply to this reply."""
    text = str(summary or "")
    question = str(query or "")
    group = is_group or any(word in question for word in ("群里", "群聊", "群中", "群友"))
    public = group or any(word in question for word in
                          ("公开", "分享", "转述", "告诉别人", "发给朋友",
                           "回复朋友", "对外", "前台", "邻居", "物业"))
    direct = any(word in question for word in
                 ("回复", "追问", "告诉", "怎么回答", "想问", "要问"))
    group_rule = any(word in text for word in ("群里", "群聊", "本群"))
    public_rule = group_rule or any(word in text for word in
                                   ("公开", "对外", "转述", "透露", "分享", "汇报", "告诉别人"))
    if group_rule and not group:
        return 0
    if public_rule and not public:
        return 0
    private_action = any(word in question for word in ("主动私聊", "主动私下", "私下追问", "私聊追问"))
    rule_about_private_action = any(word in text for word in
                                    ("主动追问", "私下确认", "私聊追问", "私下问"))
    if private_action and not rule_about_private_action:
        return 0
    if not public_rule and not direct:
        return 0
    subject = str(subject_id or "")
    subject_match = (subject in ("", "owner") or bool(subject and subject in question))
    overlap = _bigrams(text) & _bigrams(question)
    shared_entity = bool(overlap)
    quoted = set(re.findall(r"[“‘\"]([^”’\"]{2,20})[”’\"]", text))
    named_match = any(name in question for name in quoted)
    rule_topics = _topics(text)
    query_topics = _topics(question)
    if (group and "health" in rule_topics and "food" in query_topics
            and any(word in question for word in ("不能吃", "要避", "过敏", "忌口"))):
        query_topics.add("health")
    broad_group_question = (group and any(word in question for word in
                            ("有没有人", "有谁", "谁", "大家", "有人"))
                            and bool(rule_topics & query_topics))
    if not shared_entity and not named_match and not (rule_topics & query_topics) and not broad_group_question:
        return 0
    if not rule_topics and len(overlap) < 3 and not named_match:
        return 0
    if "identity" in rule_topics and "identity" not in query_topics:
        return 0
    if rule_topics and not (rule_topics & query_topics):
        # "How is X lately?" can be a request to disclose X's health state.
        if not (shared_entity and any(word in question for word in ("怎么了", "近况"))):
            return 0
    return 1 + int(subject_match) + 2 * int(bool(rule_topics & query_topics)) + int(shared_entity)
