"""Deterministic offline model.

Two uses: (1) tests inject an exact script of completions; (2) a fresh
install without any configured provider still has a working agent ("demo
brain") so the console, memory and tools can be explored before a real model
is configured. It never pretends to be an LLM in the UI: model name is
``offline``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..kernel.ids import ulid
from .base import Completion, DeltaFn, Message, ToolCall

_FACT_PATTERNS = [
    (re.compile(r"我(?:的)?(?P<what>[^，。！？,.!?]{1,12}?)(?:叫|是)(?P<val>[^，。！？,.!?]{1,20})"), "fact"),
    (re.compile(r"我(?:很|最|特别)?(?:喜欢|爱|讨厌|不喜欢)(?P<val>[^，。！？,.!?]{1,20})"), "preference"),
    (re.compile(r"我(?:住在|在)(?P<val>[^，。！？,.!?]{2,20})(?:工作|上班|生活|住)?"), "fact"),
    (re.compile(r"我养了?(?P<val>[^，。！？,.!?]{1,20})"), "fact"),
]


class ScriptedModel:
    def __init__(self, script: list[Completion | str] | None = None, name: str = "offline") -> None:
        self.name = name
        self.script = list(script or [])
        self.offline = script is None  # a scripted test double is not the "offline brain"
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[Message], *, tools: list[dict[str, Any]] | None = None,
                       temperature: float = 0.7, max_tokens: int = 1024,
                       on_delta: DeltaFn | None = None, json_mode: bool = False,
                       reasoning_effort: str = "") -> Completion:
        self.calls.append({"messages": messages, "tools": tools, "json_mode": json_mode,
                           "max_tokens": max_tokens, "reasoning_effort": reasoning_effort})
        if self.script:
            item = self.script.pop(0)
            out = item if isinstance(item, Completion) else Completion(text=item)
        elif json_mode:
            out = Completion(text=self._json_task(messages))
        else:
            out = self._agent_turn(messages, tools or [])
        out.model = self.name
        if on_delta and out.text:
            for i in range(0, len(out.text), 6):
                r = on_delta(out.text[i : i + 6])
                if asyncio.iscoroutine(r):
                    await r
        return out

    # -- offline heuristics ---------------------------------------------------
    def _json_task(self, messages: list[Message]) -> str:
        system = messages[0].text if messages else ""
        user = messages[-1].text if messages else ""
        if "extract" in system.lower() or "提取" in system:
            items = []
            for line in user.splitlines():
                if not line.startswith("用户:"):
                    continue
                text = line[3:].strip()
                for pattern, kind in _FACT_PATTERNS:
                    m = pattern.search(text)
                    if m:
                        quote = m.group(0)
                        items.append({"kind": kind, "content": "用户" + quote[1:] if quote.startswith("我") else quote,
                                      "quote": quote, "importance": 0.6, "subject": "user"})
                        break
            return json.dumps({"memories": items}, ensure_ascii=False)
        if "plan" in system.lower():
            return json.dumps({"steps": ["理解目标", "收集信息", "给出结果"]}, ensure_ascii=False)
        return "{}"

    def _agent_turn(self, messages: list[Message], tools: list[dict[str, Any]]) -> Completion:
        names = {t["function"]["name"].replace("__", ".") for t in tools}
        last = messages[-1]
        if last.role == "tool":
            # summarize the most recent tool result
            results = []
            for m in reversed(messages):
                if m.role != "tool":
                    break
                results.append(m.text)
            body = results[0] if results else ""
            try:
                parsed = json.loads(body)
                body = parsed.get("summary") or parsed.get("text") or json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, AttributeError):
                pass
            return Completion(text=f"好的，处理完了：{str(body)[:300]}")
        user = last.text.strip()
        if (m := re.match(r"^(?:请)?记住[:：，, ]?(.+)", user)) and "memory.remember" in names:
            return self._call("memory.remember", {"content": m.group(1), "kind": "fact"})
        if (m := re.match(r"^(?:忘掉|忘记)[:：，, ]?(.+)", user)) and "memory.search" in names:
            return self._call("memory.search", {"query": m.group(1)})
        if (m := re.match(r"^(?:搜索|搜一下|查一下)[:：，, ]?(.+)", user)) and "web.search" in names:
            return self._call("web.search", {"query": m.group(1)})
        if re.search(r"几点|时间|日期|星期", user) and "time.now" in names:
            return self._call("time.now", {})
        recalled = ""
        system = messages[0].text if messages and messages[0].role == "system" else ""
        if "## 相关记忆" in system:
            block = system.split("## 相关记忆", 1)[1].strip().splitlines()
            first = next((l.lstrip("- ").strip() for l in block if l.strip().startswith("-")), "")
            if first:
                recalled = f"（我记得：{first[:60]}）"
        return Completion(text=f"我现在用的是离线大脑，还没接上真正的模型{recalled}。你说的是「{user[:80]}」——"
                               "到「设置 → 模型」里配置一个模型后，我就能真正思考和回答了。")

    @staticmethod
    def _call(name: str, args: dict[str, Any]) -> Completion:
        return Completion(tool_calls=[ToolCall(f"call_{ulid()}", name, args)], finish_reason="tool_calls")


class HashEmbedding:
    """Character-trigram hashing embedding: offline, deterministic, decent for zh/en near-duplicates."""

    name = "hash-embed"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib
        import math

        out = []
        for text in texts:
            vec = [0.0] * self.dim
            t = re.sub(r"\s+", "", text.lower())
            grams = [t[i : i + n] for n in (1, 2, 3) for i in range(max(0, len(t) - n + 1))]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=4).digest(), "big")
                vec[h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
