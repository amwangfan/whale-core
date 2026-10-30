"""默认只读工具意图路由：常见显式搜索指令本地识别，可选 LLM 规划。

路由器只建议 capabilities，不能自行授权。对不匹配的消息返回 chat；
LLM 规划只在宿主显式开启时使用，避免所有闲聊多一次模型请求。
"""
from __future__ import annotations

import json
import re
from typing import Protocol

from whale_core.core.intent_plan import plan_from_model_text, IntentPlanError

_SEARCH = re.compile(
    r"^\s*(?:请|麻烦|能不能|可以|帮我|帮忙|给我|你能|能否|请你|请帮我|帮我查一下|麻烦你)?\s*"
    r"(?:搜索|搜一下|搜搜|查一下|查查|查找|检索|上网搜|上网查|search|look\s+up)\s*"
    r"(?:一下|一下子|关于|有关|下|：|:|，|,|\s)*",
    re.IGNORECASE,
)


class ReplyModel(Protocol):
    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str: ...


class SearchIntentRouter:
    """规则优先；可选模型识别其它任务，但模型不能批准动作。"""

    def __init__(self, *, model: ReplyModel | None = None, enable_llm: bool = False):
        self.model = model
        self.enable_llm = enable_llm and model is not None

    def plan(self, text: str, *, context: str = "") -> str:
        match = _SEARCH.match(text)
        if match:
            query = text[match.end():].strip(" ：:，,。\n\t")
            if query:
                return json.dumps({
                    "goal": "搜索并回答：" + query[:300],
                    "kind": "task",
                    "slots": {"query": query[:500], "limit": 5},
                    "capabilities": ["web.search"],
                    "reason": "用户明确要求搜索公开信息",
                }, ensure_ascii=False)
        if self.enable_llm:
            prompt = (
                "仅返回一个 JSON 对象，不要代码围栏。你负责建议意图，不能授予权限。"
                '格式：{"goal":"目标","kind":"chat|task|clarify",'
                '"slots":{},"capabilities":[],"clarification_question":""}。'
                "只有确实需要工具且能力已明确时才用 task；普通谈话用 chat。"
                "不得自行声明审批通过。可用只读能力：web.search（参数 query）。"
                "澄清要克制：能从上下文/记忆合理推断的缺省值就直接默认（如地点类请求"
                "没明说地点时，默认用户所在地；用户有出行信息时，用行程目的地或出发地）；"
                "确实无法推断才 clarify。"
            )
            if context:
                prompt += f"\n上下文（记忆与近期对话，供推断缺省值）：\n{context[:1500]}"
            prompt += f"\n用户原话：{text[:1500]}"
            raw = self.model.reply(prompt, persona="", memories=(), history=())
            try:
                plan = plan_from_model_text(raw)
            except IntentPlanError:
                return json.dumps({"goal": text[:500] or "对话", "kind": "chat"},
                                  ensure_ascii=False)
            if plan.kind == "task" and any(cap != "web.search" for cap in plan.capabilities):
                return json.dumps({"goal": text[:500] or "对话", "kind": "chat"},
                                  ensure_ascii=False)
            return raw
        return json.dumps({"goal": text[:500] or "对话", "kind": "chat"}, ensure_ascii=False)
