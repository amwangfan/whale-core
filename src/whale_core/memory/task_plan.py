"""Small task plan for selecting evidence, separate from action authorization."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskPlan:
    needs: tuple[str, ...]
    group: bool = False
    history: bool = False


def plan_query(query: str, *, group: bool = False) -> TaskPlan:
    """Find distinct questions in the current turn without a model call."""
    text = str(query or "")
    needs: list[str] = []

    def add(name: str, condition: bool) -> None:
        if condition and name not in needs:
            needs.append(name)

    add("access", any(w in text for w in
        ("进门", "开门", "进去", "进屋", "入内", "旧码", "门码", "入口码", "院门码", "钥匙", "门锁")))
    add("delivery", any(w in text for w in
        ("冷藏件", "冷藏药", "药件", "包裹", "快递", "送汤", "放过去", "送过去", "签收", "柜机")))
    add("medicine", any(w in text for w in ("药片", "猫药", "用药")))
    add("pet_snack", any(w in text for w in ("鱼味罐头", "冻干虾", "零食")))
    add("pet_feeding", "喂" in text)
    add("pet_food", "猫粮" in text)
    add("social_invite", any(w in text for w in
        ("邀请", "饭局", "座位", "见人", "聚餐")))
    add("checkin", any(w in text for w in
        ("关心", "留饭", "有没有吃饭", "吃了没", "过度追踪", "打卡")))
    add("nonresponse", any(w in text for w in
        ("没回", "不回", "未回", "联系不上", "失联", "没消息", "关机", "安静")))
    add("drink", any(w in text for w in ("饮料", "燕麦奶", "气泡水")))
    add("food_choice", any(w in text for w in
        ("点餐", "火锅", "锅底", "清汤", "聚餐")))
    add("absence", any(w in text for w in ("为什么突然", "少参加", "临时走开", "没来活动")))
    add("frontdesk", any(w in text for w in ("前台", "邻居", "对外安排")))
    add("disclosure", any(w in text for w in
        ("转述", "公开", "告诉别人", "告诉朋友", "告诉群友", "群聊", "群里", "对外", "回复朋友")))
    add("proactive", any(w in text for w in ("主动帮", "主动去", "主动安排")))
    return TaskPlan(tuple(needs), group or any(w in text for w in ("群聊", "群里")),
                    any(w in text for w in ("以前", "过去", "历史", "为什么认为", "来源")))


_EVIDENCE = {
    "access": ("进门", "进屋", "开门", "门码", "旧码", "钥匙", "门锁", "锁芯", "入口码", "自己进去"),
    "delivery": ("冷藏件", "冷藏药", "药件", "保温袋", "包裹", "快递", "签收", "柜机", "驿站", "取物架", "放东西"),
    "medicine": ("药片", "猫药", "用药", "药盒"),
    "pet_snack": ("鱼味", "冻干虾", "零食", "罐头先", "罐头不要"),
    "pet_feeding": ("喂", "每次半罐", "罐头开半", "添水"),
    "pet_food": ("猫粮",),
    "social_invite": ("邀请", "饭局", "座位", "见人", "围观", "近况", "一桌人"),
    "checkin": ("留饭", "吃了没", "打卡", "追问", "身体状况", "每日提醒"),
    "nonresponse": ("没回", "不回", "未回", "联系不上", "失联", "没消息",
                    "关机", "安静", "默认我是在睡", "不要脑补出事"),
    "drink": ("饮料", "燕麦奶", "气泡水"),
    "food_choice": ("点餐", "火锅", "锅底", "清汤", "辣油", "聚餐",
                    "不加辣", "清淡"),
    "absence": ("临时走开", "私事", "日程冲突", "少参加", "问起A为什么"),
    "frontdesk": ("前台", "邻居", "对外安排", "物资"),
    "disclosure": ("群里", "群聊", "对外", "转述", "透露", "分享", "告诉别人"),
    "proactive": ("主动", "替A", "未经", "先问"),
}


def evidence_needs(summary: str, plan: TaskPlan) -> tuple[str, ...]:
    """Return the task needs a memory actually covers, not just shared nouns."""
    text = str(summary or "")
    matched = [need for need in plan.needs
               if any(cue in text for cue in _EVIDENCE[need])]
    # A meal invitation is not a request to select food; a drop-off of soup is
    # not a dietary question. Likewise a group question need not invoke every
    # disclosure rule: applicable boundaries are selected in their own stage.
    if "social_invite" in plan.needs and "social_invite" not in matched:
        matched = [n for n in matched if n not in {"disclosure", "drink"}]
    if "absence" in plan.needs and "absence" not in matched:
        matched = [n for n in matched if n != "disclosure"]
    if "frontdesk" in plan.needs and "frontdesk" not in matched:
        matched = [n for n in matched if n not in {"disclosure", "proactive"}]
    return tuple(matched)
