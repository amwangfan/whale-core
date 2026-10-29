"""内部处理型插件（PROCESSOR）：模型/计算委托，无对外 I/O。

典型场景（维护者提出的**上下文隔离**）：T 档陪伴模型（DeepSeek）只产
「操作意图」，具体执行——多步工具循环、大量命令/检索原始输出——委托给
更便宜的执行器模型（如 qwen3.8-flash）；执行完只把**压缩摘要 + 回执**
回流给委托方。DeepSeek 的上下文不再被 shell / 命令输出挤满。

安全约定：

- processor 插件自身**不做任何对外副作用**——循环里的每次工具调用都必须走
  宿主注入的受门控执行入口（ShellPolicy 白名单 / PluginGateway），本模块
  不绕过门控、也不自带执行能力；
- ``ProcessorRouter`` 对回流摘要做**强制截断**（上下文隔离的最后防线），
  回执只保留短文本（工具名:ok/fail 级别），原始工具输出不回流；
- 台账（audit）不记 task 全文与 constraints（可能含隐私/配置）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from whale_core.plugins.manifest import PluginKind
from whale_core.plugins.registry import PluginRegistry

PROCESS_STATUSES = frozenset({"done", "failed", "needs_approval", "denied"})
DEFAULT_MAX_SUMMARY_CHARS = 1200
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 2000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProcessingRequest:
    """一次内部处理委托（如：执行一个操作意图）。"""

    capability: str  # processor manifest 声明的能力名，如 "operation_execution"
    task: str  # 委托方产出的操作意图（短文本 / 结构化摘要，不是整段对话）
    constraints: Mapping[str, Any] = field(default_factory=dict)
    # constraints 约定键：allowed_tools（tuple）、budget、tier 等，由 processor 解释
    context_digest: str = ""  # 最小上下文摘要——不传完整对话，隔离是目的本身
    source: str = ""  # 触发来源（审计用，如 "qq:owner" / "call:session-1"）
    request_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class ProcessingResult:
    """处理结果：只有摘要与回执回流委托方上下文。"""

    status: str  # done | failed | needs_approval | denied
    summary: str = ""  # 给委托方上下文的压缩摘要（router 强制截断）
    receipts: tuple[str, ...] = ()  # 执行回执（工具名:ok/fail 级，不含原始输出）
    at: str = field(default_factory=_utc_now)


class IntentProcessor(Protocol):
    """内部处理插件的执行契约。"""

    def process(self, request: ProcessingRequest) -> ProcessingResult:
        ...


class NullIntentProcessor:
    """未接线 stub：manifest 已注册但 processor 尚未部署（契约先行）。"""

    def process(self, request: ProcessingRequest) -> ProcessingResult:
        return ProcessingResult("failed", "processor not connected")


class ProcessorRouter:
    """capability → processor 解析 + 派发 + 摘要截断 + 审计。

    - 未注册能力 → denied（不静默）；
    - processor 抛异常 → failed，不炸调用方；
    - 非法 status 一律折算 failed；
    - summary 强制截断至 ``max_summary_chars``——即使 processor 违约返回
      超长文本，委托方上下文也不会被挤爆（上下文隔离的最后防线）。
    """

    def __init__(self, registry: PluginRegistry, *, max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS) -> None:
        self.registry = registry
        self.max_summary_chars = max_summary_chars
        self.last_route: tuple[str, ProcessingResult] | None = None

    def route(self, request: ProcessingRequest) -> ProcessingResult:
        candidates = self.registry.processors_for(request.capability)
        if not candidates:
            result = ProcessingResult(
                "denied", f"no processor registered for capability {request.capability!r}"
            )
            self.last_route = ("", result)
            return result
        plugin_id, provider = candidates[0]  # 注册顺序即优先级（宿主控制）
        processor: IntentProcessor = provider if provider is not None else NullIntentProcessor()
        try:
            raw = processor.process(request)
        except Exception:
            raw = ProcessingResult("failed", "processor error")
        status = raw.status if raw.status in PROCESS_STATUSES else "failed"
        result = ProcessingResult(
            status=status,
            summary=str(raw.summary or "")[: self.max_summary_chars],
            receipts=tuple(str(r)[:120] for r in raw.receipts[:20]),
            at=raw.at,
        )
        self.last_route = (plugin_id, result)
        return result

    def audit(self, request: ProcessingRequest, result: ProcessingResult, processor_id: str) -> dict:
        """台账：不记 task 全文 / constraints / summary 全文（隐私面最小化）。"""
        return {
            "request_id": request.request_id,
            "capability": request.capability,
            "processor": processor_id,
            "source": request.source,
            "status": result.status,
            "task_digest": hashlib.sha256(request.task.encode("utf-8", errors="replace")).hexdigest(),
            "receipts": list(result.receipts),
            "at": _utc_now(),
        }


class ExecutorModelClient(Protocol):
    """执行器模型客户端（Chat Completions 形状；如 qwen3.8-flash 端点）。

    真实 HTTP 由宿主接线（模型端点属 MODEL 类插件或 env 配置）；
    本模块只在测试替身 / 注入客户端上工作，不自带网络。
    """

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        ...


ToolRunner = Callable[[str, Mapping[str, Any]], "tuple[bool, str]"]


def _first_json_object(text: str) -> dict | None:
    """宽松解析模型输出中的第一个 JSON 对象（容忍 ```json 围栏与前后缀文本）。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(s[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except ValueError:
                return None
    return None


class DelegatingIntentProcessor:
    """参考实现：操作意图 → 执行器模型工具循环 → 摘要回流（上下文隔离）。

    循环协议（执行器模型每轮只输出一个 JSON 对象）：

    - ``{"tool": "<名称>", "args": {...}}`` → 调宿主注入的 ``tool_runner``
      （**必须**是受门控入口：ShellPolicy 白名单 / PluginGateway 派发）；
      原始输出截断后只回喂执行器模型，不写进结果摘要；
    - ``{"final": "<摘要>"}`` → done；摘要即回流委托方上下文的全部内容。

    runner 回执以 ``plugin-hold`` / ``plugin-denied`` / ``needs_approval`` 开头时
    （PluginGateway / ShellPolicy 的门控回执），立即以 needs_approval 结束——
    审批权在人，执行器模型不能代批。
    """

    def __init__(
        self,
        model: ExecutorModelClient,
        *,
        tool_runner: ToolRunner,
        max_rounds: int = 4,
        max_tool_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    ) -> None:
        self.model = model
        self.tool_runner = tool_runner
        self.max_rounds = max_rounds
        self.max_tool_output_chars = max_tool_output_chars
        self.max_summary_chars = max_summary_chars

    def process(self, request: ProcessingRequest) -> ProcessingResult:
        raw_allowed = request.constraints.get("allowed_tools", ())
        if isinstance(raw_allowed, (list, tuple, set, frozenset)):
            allowed = tuple(
                item.strip() for item in raw_allowed
                if isinstance(item, str) and item.strip()
            )
        else:
            allowed = ()
        system = (
            "你是操作执行器。用允许的工具完成任务，每轮只输出一个 JSON 对象："
            '{"tool": "名称", "args": {...}} 或 {"final": "给委托方的摘要"}。'
            f"允许的工具：{'、'.join(allowed) if allowed else '（无——直接输出 final）'}。"
            "工具原始输出只供你参考，不要整段复述；final 摘要不超过 "
            f"{self.max_summary_chars} 字，只写结论、关键值与未完成项。"
        )
        user = f"任务：{request.task[:2000]}"
        if request.context_digest:
            user += f"\n上下文摘要：{request.context_digest[:1000]}"
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        receipts: list[str] = []

        for _ in range(self.max_rounds):
            try:
                raw = self.model.complete(messages)
            except Exception:
                return ProcessingResult("failed", "executor model error", tuple(receipts))
            parsed = _first_json_object(raw)
            if parsed is None:
                messages.append({"role": "assistant", "content": str(raw)[:500]})
                messages.append({"role": "user", "content": "只输出一个 JSON 对象（tool 或 final）。"})
                continue
            if "final" in parsed:
                summary = str(parsed.get("final") or "")[: self.max_summary_chars]
                return ProcessingResult("done", summary or "（空摘要）", tuple(receipts))
            tool = str(parsed.get("tool") or "").strip()
            if not tool:
                messages.append({"role": "assistant", "content": str(raw)[:500]})
                messages.append({"role": "user", "content": 'JSON 需要 "tool" 或 "final" 键。'})
                continue
            if tool not in allowed:
                receipts.append(f"{tool}:denied-not-allowed")
                messages.append({"role": "assistant", "content": str(raw)[:500]})
                messages.append({"role": "user", "content": f"工具 {tool} 不在允许清单，换一个或输出 final。"})
                continue
            args = parsed.get("args")
            args = dict(args) if isinstance(args, Mapping) else {}
            try:
                ok, out = self.tool_runner(tool, args)
            except Exception:
                ok, out = False, "runner error"
            text = str(out or "")
            receipts.append(f"{tool}:{'ok' if ok else 'fail'}")
            if text.startswith(("plugin-hold", "plugin-denied", "needs_approval")):
                return ProcessingResult(
                    "needs_approval",
                    f"工具 {tool} 被门控拦下，等待人工审批：{text[:200]}",
                    tuple(receipts),
                )
            messages.append({"role": "assistant", "content": str(raw)[:500]})
            messages.append(
                {
                    "role": "user",
                    "content": f"工具 {tool} {'ok' if ok else 'fail'}：{text[: self.max_tool_output_chars]}",
                }
            )
        return ProcessingResult("failed", "rounds exhausted without final", tuple(receipts))
