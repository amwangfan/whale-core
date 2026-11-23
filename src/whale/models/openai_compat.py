"""OpenAI-compatible chat + embeddings (DeepSeek, Qwen/DashScope, Ollama, vLLM…).

Streaming with incremental tool-call assembly. Models that reject the
``tools`` parameter fall back to a prompt-level protocol automatically, so
small local models still get a working agent loop.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from ..kernel.ids import ulid
from .base import Completion, DeltaFn, Message, ModelError, ToolCall, Usage

_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

PROMPT_TOOLS = """你可以调用工具。需要调用时，只输出一行或多行：
<tool_call>{"name": "工具名", "arguments": {...}}</tool_call>
不需要工具时直接回答。可用工具：
{tools}"""


class OpenAICompatChat:
    def __init__(self, base_url: str, model: str, api_key: str = "", *, timeout: float = 120.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = model
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0),
                                                   follow_redirects=False)
        self.native_tools = True

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def complete(self, messages: list[Message], *, tools: list[dict[str, Any]] | None = None,
                       temperature: float | None = 0.7, max_tokens: int = 1024,
                       on_delta: DeltaFn | None = None, json_mode: bool = False,
                       reasoning_effort: str = "") -> Completion:
        if tools and not self.native_tools:
            return await self._complete_prompt_tools(messages, tools, temperature, max_tokens, on_delta)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        try:
            return await self._stream(body, on_delta)
        except ModelError as exc:
            if reasoning_effort and exc.status == 400 and "reasoning" in str(exc).lower():
                # 服务商不识该 reasoning 档位：去掉重试（等价于不下发），其余 fallback 链照旧生效
                return await self.complete(messages, tools=tools, temperature=temperature,
                                           max_tokens=max_tokens, on_delta=on_delta,
                                           json_mode=json_mode, reasoning_effort="")
            if tools and exc.status == 400 and "tool" in str(exc).lower():
                self.native_tools = False
                return await self._complete_prompt_tools(messages, tools, temperature, max_tokens, on_delta)
            if json_mode and exc.status == 400:
                body.pop("response_format", None)
                return await self._stream(body, on_delta)
            raise

    async def _complete_prompt_tools(self, messages, tools, temperature, max_tokens, on_delta) -> Completion:
        desc = "\n".join(
            f"- {t['function']['name']}: {t['function'].get('description', '')} 参数: "
            f"{json.dumps(t['function'].get('parameters', {}), ensure_ascii=False)}"
            for t in tools
        )
        converted: list[Message] = []
        for m in messages:
            if m.role == "tool":
                converted.append(Message("user", f"[工具 {m.name or ''} 结果]\n{m.text}"))
            elif m.role == "assistant" and m.tool_calls:
                calls = "\n".join(
                    f"<tool_call>{json.dumps({'name': c.name, 'arguments': c.arguments}, ensure_ascii=False)}</tool_call>"
                    for c in m.tool_calls
                )
                converted.append(Message("assistant", (m.text + "\n" + calls).strip()))
            else:
                converted.append(m)
        sys_extra = Message("system", PROMPT_TOOLS.replace("{tools}", desc))
        if converted and converted[0].role == "system":
            converted.insert(1, sys_extra)
        else:
            converted.insert(0, sys_extra)
        body = {"model": self.model, "messages": [m.to_openai() for m in converted],
                "max_tokens": max_tokens, "stream": True}
        if temperature is not None:
            body["temperature"] = temperature
        result = await self._stream(body, None)
        calls = []
        for raw in _TOOL_RE.findall(result.text):
            try:
                obj = json.loads(raw)
                calls.append(ToolCall(f"call_{ulid()}", obj["name"], obj.get("arguments") or {}))
            except (json.JSONDecodeError, KeyError):
                continue
        text = _TOOL_RE.sub("", result.text).strip()
        if on_delta and text and not calls:
            r = on_delta(text)
            if asyncio.iscoroutine(r):
                await r
        result.text = "" if calls else text
        result.tool_calls = calls
        result.finish_reason = "tool_calls" if calls else result.finish_reason
        return result

    async def _stream(self, body: dict[str, Any], on_delta: DeltaFn | None) -> Completion:
        url = f"{self.base_url}/chat/completions"
        out = Completion(model=self.model)
        parts: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        try:
            async with self._client.stream("POST", url, headers=self._headers(), json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise ModelError(f"HTTP {resp.status_code}: {detail}",
                                     retryable=resp.status_code in (429, 500, 502, 503, 504),
                                     status=resp.status_code)
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" not in ctype:
                    data = json.loads(await resp.aread())
                    return self._from_full(data, on_delta)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        out.usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        if delta.get("content"):
                            parts.append(delta["content"])
                            if on_delta:
                                r = on_delta(delta["content"])
                                if asyncio.iscoroutine(r):
                                    await r
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            out.finish_reason = choice["finish_reason"]
        except httpx.TimeoutException as exc:
            raise ModelError(f"model timeout: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"model transport error: {exc}", retryable=True) from exc
        out.text = "".join(parts)
        out.reasoning = "".join(reasoning)
        for _, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": slot["args"]}
            out.tool_calls.append(ToolCall(slot["id"] or f"call_{ulid()}", slot["name"], args))
        return out

    def _from_full(self, data: dict[str, Any], on_delta: DeltaFn | None) -> Completion:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        out = Completion(text=msg.get("content") or "", model=data.get("model", self.model),
                         finish_reason=choice.get("finish_reason") or "stop",
                         reasoning=msg.get("reasoning_content") or "")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            out.tool_calls.append(ToolCall(tc.get("id") or f"call_{ulid()}", fn.get("name", ""), args))
        u = data.get("usage") or {}
        out.usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        if on_delta and out.text:
            r = on_delta(out.text)
            if asyncio.iscoroutine(r):
                asyncio.ensure_future(r)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAICompatEmbedding:
    def __init__(self, base_url: str, model: str, api_key: str = "",
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = model
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = await self._client.post(f"{self.base_url}/embeddings", headers=headers,
                                           json={"model": self.model, "input": texts})
        except httpx.HTTPError as exc:
            raise ModelError(f"embedding transport error: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise ModelError(f"embedding HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
        data = resp.json().get("data") or []
        return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
