"""IntentPlan：结构化意图契约（模型提议、核心裁决）。

本模块定义「模型提议、核心裁决」（model proposes, core decides）的边界：

- 模型（路由器 / 执行器）把自由文本输出整理为 :class:`IntentPlan`，但 plan
  **不含任何授权语义**：``risk_hint`` 只是模型自评的参考提示，**永不作为
  授权依据**；真正的风险级与审批由核心策略层
  （``whale_core.plugins.policy`` 的插件策略、
  ``whale_core.cloud.shell_policy`` 的 Shell 分档）根据工具清单计算。
- 模型不能通过输出 ``approved=true`` / ``admin=true`` 之类字段自我批准：
  :func:`parse_intent_plan` 只承认 :class:`IntentPlan` 声明的字段，
  未定义字段一律丢弃。
- ``intent_id`` 由核心侧在解析时生成，不采纳模型给出的值
  （身份属于核心，不属于提议方）。

``plan_from_model_text`` 的 JSON 提取与 ``whale_core.plugins.processing``
的私有函数 ``_first_json_object`` 是**镜像关系**：那边是私有实现，按约定
**不 import**；此处按括号配平独立实现（容忍前后散文与 ``` 代码围栏，
字符串内的括号与转义字符不参与配平）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

__all__ = [
    "IntentKind",
    "IntentPlan",
    "IntentPlanError",
    "parse_intent_plan",
    "plan_from_model_text",
]

GOAL_MAX_CHARS = 2000
TEXT_MAX_CHARS = 500  # expected_output / success_criteria / reason
RISK_HINT_MAX_CHARS = 200
TASK_ID_MAX_CHARS = 128
TIMEOUT_DEFAULT_S = 60.0
TIMEOUT_MIN_S = 1.0
TIMEOUT_MAX_S = 3600.0


class IntentKind(str, Enum):
    """意图类别。"""

    CHAT = "chat"  # 纯对话，无需工具
    TASK = "task"  # 需要能力/工具的任务
    CLARIFY = "clarify"  # 信息不足，需要向用户澄清


_KIND_VALUES = frozenset(kind.value for kind in IntentKind)


class IntentPlanError(ValueError):
    """意图契约解析 / 校验失败（错误消息必含字段名）。"""


@dataclass(frozen=True)
class IntentPlan:
    """一次意图的结构化提议（冻结、无授权语义）。

    ``risk_hint`` 是模型自评风险——**仅参考，永不作为授权依据**；
    风险级由核心策略层（plugins/policy、cloud/shell_policy）根据工具清单计算。
    """

    intent_id: str = field(default_factory=lambda: uuid4().hex)
    goal: str = ""  # 必填非空（parse 时校验），≤2000 字符
    kind: str = "task"  # IntentKind 值
    slots: Mapping[str, Any] = field(default_factory=dict)  # 已填槽位
    missing: tuple[str, ...] = ()  # 缺失槽位名
    capabilities: tuple[str, ...] = ()  # 需要的能力 ID（如 "web.search"）
    expected_output: str = ""
    success_criteria: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""
    reason: str = ""
    risk_hint: str = ""  # 模型自评风险——仅参考，永不作为授权依据
    task_id: str = ""
    timeout_s: float = 60.0


def _coerce_str(value: Any, *, limit: int) -> str:
    """宽松 str 化（None → ""）并按上限截断。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    """校验「序列且元素全 str」（bare str/bytes 不算序列），list 归一为 tuple。"""
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise IntentPlanError(f"{name} 必须是元素全为 str 的序列，收到 {type(value).__name__}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str):
            raise IntentPlanError(f"{name} 的元素必须全是 str，收到 {type(item).__name__}")
    return items


def _timeout(value: Any) -> float:
    """转 float，非法值回落 60.0，钳制到 [1, 3600]。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = TIMEOUT_DEFAULT_S
    if math.isnan(seconds):
        seconds = TIMEOUT_DEFAULT_S
    return min(TIMEOUT_MAX_S, max(TIMEOUT_MIN_S, seconds))


def parse_intent_plan(data: Mapping[str, Any]) -> IntentPlan:
    """把不可信的模型输出字典校验、归一为 :class:`IntentPlan`。

    只承认 IntentPlan 声明的字段，未定义字段（如 ``approved=true``）一律丢弃；
    ``intent_id`` 由核心侧生成，不采纳输入值。违反校验规则即抛
    :class:`IntentPlanError`（消息含字段名）。
    """
    if not isinstance(data, Mapping):
        raise IntentPlanError(f"data 必须是 Mapping，收到 {type(data).__name__}")

    if "goal" not in data:
        raise IntentPlanError("goal 缺失：必须提供非空 goal")
    goal_raw = data["goal"]
    if not isinstance(goal_raw, str):
        raise IntentPlanError(f"goal 必须是 str，收到 {type(goal_raw).__name__}")
    goal = goal_raw.strip()
    if not goal:
        raise IntentPlanError("goal 必须是非空字符串（strip 后为空）")
    goal = goal[:GOAL_MAX_CHARS]  # 超长截断，不报错

    kind_raw = data.get("kind", IntentKind.TASK.value)
    if not isinstance(kind_raw, str) or kind_raw not in _KIND_VALUES:
        raise IntentPlanError(f"kind 必须是 {sorted(_KIND_VALUES)} 之一，收到 {kind_raw!r}")
    kind = kind_raw

    slots_raw = data.get("slots", {})
    if not isinstance(slots_raw, Mapping):
        raise IntentPlanError(f"slots 必须是 Mapping，收到 {type(slots_raw).__name__}")
    slots = dict(slots_raw)  # 防御性拷贝：调用方之后改原 dict 不影响冻结 plan

    missing = _str_tuple(data.get("missing", ()), "missing")
    capabilities = _str_tuple(data.get("capabilities", ()), "capabilities")

    needs_clarification = bool(data.get("needs_clarification", False))
    if kind == IntentKind.CLARIFY.value:
        needs_clarification = True  # clarify 意图强制需要澄清

    clarification_question = _coerce_str(
        data.get("clarification_question", ""), limit=GOAL_MAX_CHARS
    )
    if kind == IntentKind.CLARIFY.value and not clarification_question.strip():
        raise IntentPlanError("kind=clarify 时 clarification_question 必须非空")

    return IntentPlan(
        goal=goal,
        kind=kind,
        slots=slots,
        missing=missing,
        capabilities=capabilities,
        expected_output=_coerce_str(data.get("expected_output", ""), limit=TEXT_MAX_CHARS),
        success_criteria=_coerce_str(data.get("success_criteria", ""), limit=TEXT_MAX_CHARS),
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        reason=_coerce_str(data.get("reason", ""), limit=TEXT_MAX_CHARS),
        risk_hint=_coerce_str(data.get("risk_hint", ""), limit=RISK_HINT_MAX_CHARS),
        task_id=_coerce_str(data.get("task_id", ""), limit=TASK_ID_MAX_CHARS),
        timeout_s=_timeout(data.get("timeout_s", TIMEOUT_DEFAULT_S)),
    )


def _json_object_spans(text: str) -> Iterator[str]:
    """产出 text 中每个 brace 配平的 ``{...}`` 片段（字符串/转义感知）。

    镜像 ``whale_core.plugins.processing._first_json_object`` 的意图
    （宽松提取模型输出中的 JSON 对象；那边是私有实现，故不 import，
    此处独立实现）：从每个 ``{`` 起做括号配平，字符串内的 ``{}`` 不计、
    ``\\`` 转义的下一个字符不参与判断。
    """
    start = 0
    while True:
        open_at = text.find("{", start)
        if open_at == -1:
            return
        depth = 0
        in_string = False
        escaped = False
        for at in range(open_at, len(text)):
            ch = text[at]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[open_at : at + 1]
                    break
        start = open_at + 1


def plan_from_model_text(text: str) -> IntentPlan:
    """从模型自由文本中提取首个完整 JSON 对象并解析为 :class:`IntentPlan`。

    容忍前后散文与 ``` 代码围栏；brace 配平但 JSON 非法的片段会被跳过，
    继续寻找下一个候选。找不到可用 JSON 对象时抛
    ``IntentPlanError("no JSON object in model output")``。
    """
    if not isinstance(text, str):
        raise IntentPlanError(f"model output 必须是 str，收到 {type(text).__name__}")
    for span in _json_object_spans(text):
        try:
            data = json.loads(span)
        except ValueError:
            continue  # 不是合法 JSON，尝试下一个 '{' 候选
        if isinstance(data, Mapping):
            return parse_intent_plan(data)
    raise IntentPlanError("no JSON object in model output")
