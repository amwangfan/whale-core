"""Small, explainable relation hints for facts and reply-time queries.

These hints are intentionally incomplete. An unknown relation stays unknown;
it must never be interpreted as permission or as a reason to discard a fact.
"""
from __future__ import annotations

import re


def relation_hints(text: str) -> tuple[dict[str, str], ...]:
    """Describe the topics and attributes explicitly mentioned in a sentence."""
    hints: list[dict[str, str]] = []
    for clause in re.split(r"[；;。\n]", str(text or "")):
        if not clause.strip():
            continue
        topics: list[str] = []
        if any(word in clause for word in ("进门", "开门", "钥匙", "门码", "锁芯", "门锁", "旧码")):
            topics.append("access")
        if any(word in clause for word in ("快递", "包裹", "冷藏件", "送货", "签收", "柜机", "驿站")):
            topics.append("delivery")
        if any(word in clause for word in ("转述", "告诉", "保密", "分享", "公开", "提及")):
            topics.append("disclosure")
        if not topics:
            continue
        attributes: list[str] = []
        if any(word in clause for word in ("允许", "可以", "不能", "不得", "不再", "作废", "停用", "能否", "还能", "能开")):
            attributes.append("permission")
        if any(word in clause for word in ("放", "保温袋", "柜机", "驿站")):
            attributes.append("placement")
        if any(word in clause for word in ("电话", "确认", "先问", "联系")):
            attributes.append("contact")
        if any(word in clause for word in ("换了", "更换", "现在", "目前")):
            attributes.append("status")
        condition = ""
        match = re.search(r"(?:如果|若|如需|需要时)([^，,；;。]+?)(?:先|就|则|应|要|$)", clause)
        if match:
            condition = match.group(1).strip()[:80]
        action = ("deny" if any(word in clause for word in
                 ("不能", "不得", "不要", "不再", "作废", "停用"))
                  else "allow" if any(word in clause for word in
                 ("允许", "可以"))
                  else "procedure" if any(word in clause for word in
                 ("先", "应", "需", "放", "联系", "打电话"))
                  else "state")
        for topic in topics:
            hints.append({"entity": topic, "attribute": ",".join(attributes),
                          "condition": condition, "action": action})
    return tuple(hints)


def query_attributes(text: str) -> dict[str, set[str]]:
    """A query may ask different attributes of different entities at once."""
    text = str(text or "")
    requested: dict[str, set[str]] = {}
    if any(word in text for word in ("进门", "开门", "钥匙", "门码", "锁芯", "门锁", "旧码")):
        requested["access"] = {"permission"} if any(word in text for word in (
            "进门", "开门", "能", "可", "该", "权限")) else {"status"}
    if any(word in text for word in ("快递", "包裹", "冷藏件", "送货", "签收", "柜机", "驿站")):
        attrs: set[str] = set()
        if any(word in text for word in ("放", "送", "到了", "到达", "冷藏件", "包裹")):
            attrs.add("placement")
        if any(word in text for word in ("签收", "联系", "电话", "确认")):
            attrs.add("contact")
        requested["delivery"] = attrs
    if any(word in text for word in ("转述", "告诉", "保密", "分享", "公开", "提及")):
        requested["disclosure"] = {"permission"}
    return requested
