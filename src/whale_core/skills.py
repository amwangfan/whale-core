"""Skill 层（roadmap §4.3）：Markdown 驱动的工具组合工作流。

三层语义：

- Capability = 系统能做什么（如 ``web.search``）；
- Tool = 模型可按 Schema 调用的具体操作（``core.runtime.ToolSpec``）；
- **Skill = 说明如何组合工具完成一个目标的可复用工作流**，用 Markdown +
  极简结构块描述，**不是新的 Python 插件**。能用现有工具组合完成的流程，
  优先写成 Skill 文本。

**安全约定：Skill 文本是数据，不是指令。** 模板渲染只产出工具参数字符串；
每一步仍经 ``ToolRegistry`` 照常门控——read_only 直接执行，非 read_only
必须经插件网关（ActionGateway）按 ``ToolSpec.plugin_id``/``action`` 映射派发，
无网关或缺映射一律 denied。Skill 无法借此提权，也不能自批。

文件格式（手搓解析，不引第三方 yaml）::

    # skill: search-and-summarize
    description: 搜索并总结
    ## step: search
    tool: search_web
    args:
      query: "{input}"
    save_as: results
    ## step: read
    tool: fetch_url
    args:
      url: "{steps.search.first_url}"
    save_as: page

规则：首个非空行必须是 ``# skill: <name>``；其后 ``description:`` 行（可选）；
每个 ``## step: <step_name>`` 起一步；step 块内 ``tool:`` 必填、``args:``
下挂缩进的 ``- key: value`` 或 ``key: value`` 行、``save_as:`` 可选。
args 值两端成对的单/双引号会剥掉一层。畸形输入抛 ``SkillError``（含行号）。

模板：args 值里 ``{input}`` 替换为本次输入；``{steps.<step>.text}`` 为该步
（截断后的）输出全文；``{steps.<step>.first_url}`` 为从该步输出提取的首个
http(s) URL。引用不存在的步骤/字段在执行期发现即失败（status=failed +
receipt 说明）。step 只能引用**已执行完**的前序步骤。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from whale_core.core.runtime import ActionGatewayLike, ToolRegistry
from whale_core.plugins.intents import PluginAction
from whale_core.plugins.manifest import RiskClass

SKILL_OUTPUT_CHARS = 1200

_SKILL_HEADER = "# skill:"
_STEP_HEADER = "## step:"
_DESCRIPTION_KEY = "description:"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_URL_RE = re.compile(r"https?://[^\s\"'<>\]\)]+")
_URL_TRAILING = ".,;:!?。，；：！？"


class SkillError(ValueError):
    """Skill 解析或模板渲染错误（消息含行号或步骤上下文）。"""


@dataclass(frozen=True)
class SkillStep:
    """工作流中的一步：调用哪个工具、传什么模板参数、输出存到哪。"""

    name: str
    tool: str
    args: Mapping[str, str] = field(default_factory=dict)  # 模板字符串
    save_as: str = ""


@dataclass(frozen=True)
class Skill:
    """一个解析完毕的 Skill：名字、描述、按序执行的步骤。"""

    name: str
    description: str
    steps: tuple[SkillStep, ...]


@dataclass(frozen=True)
class SkillResult:
    """一次 Skill 执行的如实结局：

    - status：``succeeded``（全部步骤成功）/ ``failed``（模板、工具、执行错误）/
      ``denied``（风险门控拒绝，含网关 hold——Skill 不等待审批，如实报告）；
    - outputs：``save_as`` → 该步截断后的输出（只含有 save_as 的步骤）；
    - receipts：逐步审计回执 ``skill:<名>:step:<步>:tool:<工具>:ok/fail/denied[:原因]``。
    """

    status: str
    outputs: Mapping[str, str] = field(default_factory=dict)
    receipts: tuple[str, ...] = ()


# ---------- 解析 ----------


def _strip_quotes(value: str) -> str:
    """剥掉值两端成对的一层引号（手写格式的最小 YAML 化便利）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_arg_entry(text: str, line_no: int) -> tuple[str, str]:
    """解析一条 args 条目：``- key: value`` 或 ``key: value``。"""
    entry = text[2:].strip() if text.startswith("- ") else text
    if ":" not in entry:
        raise SkillError(f"line {line_no}: malformed args entry (expected 'key: value'): {text!r}")
    key, _, value = entry.partition(":")
    key = key.strip()
    if not key:
        raise SkillError(f"line {line_no}: args entry with empty key: {text!r}")
    return key, _strip_quotes(value.strip())


def parse_skill(text: str) -> Skill:
    """把 Markdown Skill 文本解析为 ``Skill``；畸形输入抛 ``SkillError``（含行号）。"""
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        raise SkillError("line 1: empty skill text, expected '# skill: <name>'")
    first = lines[i].strip()
    if not first.startswith(_SKILL_HEADER):
        raise SkillError(f"line {i + 1}: skill must start with '# skill: <name>', got {first!r}")
    name = first[len(_SKILL_HEADER):].strip()
    if not name:
        raise SkillError(f"line {i + 1}: skill name missing after '# skill:'")

    description = ""
    steps: list[SkillStep] = []
    seen_steps: set[str] = set()
    # 当前步的暂存：None 表示还没进入任何 step 块
    cur: dict[str, Any] | None = None
    in_args = False  # 刚见过 `args:`，后续缩进行都是条目

    def finalize() -> None:
        nonlocal cur
        if cur is None:
            return
        if not cur["tool"]:
            raise SkillError(
                f"line {cur['line']}: step {cur['name']!r} missing required 'tool:'")
        steps.append(SkillStep(
            name=cur["name"], tool=cur["tool"],
            args=dict(cur["args"]), save_as=cur["save_as"],
        ))
        cur = None

    for i in range(i + 1, len(lines)):
        line_no = i + 1
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            continue  # 空行任何位置都允许
        if stripped.startswith(_STEP_HEADER):
            finalize()
            step_name = stripped[len(_STEP_HEADER):].strip()
            if not step_name:
                raise SkillError(f"line {line_no}: step name missing after '## step:'")
            if step_name in seen_steps:
                raise SkillError(f"line {line_no}: duplicate step name {step_name!r}")
            seen_steps.add(step_name)
            cur = {"name": step_name, "line": line_no, "tool": "", "args": {}, "save_as": ""}
            in_args = False
            continue
        if stripped.startswith(_SKILL_HEADER):
            raise SkillError(f"line {line_no}: duplicate skill header")
        if in_args and raw[:1] in (" ", "\t"):
            key, value = _parse_arg_entry(stripped, line_no)
            if key in cur["args"]:
                raise SkillError(f"line {line_no}: duplicate args key {key!r} in step {cur['name']!r}")
            cur["args"][key] = value
            continue
        in_args = False
        if cur is None:
            if stripped.startswith(_DESCRIPTION_KEY):
                if description:
                    raise SkillError(f"line {line_no}: duplicate description")
                description = stripped[len(_DESCRIPTION_KEY):].strip()
                continue
            raise SkillError(
                f"line {line_no}: unexpected content before first step: {stripped!r}")
        # step 块内的键行
        if stripped.startswith("tool:"):
            if cur["tool"]:
                raise SkillError(f"line {line_no}: duplicate 'tool:' in step {cur['name']!r}")
            cur["tool"] = stripped[len("tool:"):].strip()
            if not cur["tool"]:
                raise SkillError(f"line {line_no}: empty 'tool:' in step {cur['name']!r}")
        elif stripped == "args:" or stripped.startswith("args:"):
            if stripped != "args:":
                raise SkillError(
                    f"line {line_no}: 'args:' must be a bare block header, got {stripped!r}")
            if cur["args"]:
                raise SkillError(f"line {line_no}: duplicate 'args:' in step {cur['name']!r}")
            in_args = True
        elif stripped.startswith("save_as:"):
            if cur["save_as"]:
                raise SkillError(f"line {line_no}: duplicate 'save_as:' in step {cur['name']!r}")
            cur["save_as"] = stripped[len("save_as:"):].strip()
            if not cur["save_as"]:
                raise SkillError(f"line {line_no}: empty 'save_as:' in step {cur['name']!r}")
        else:
            raise SkillError(
                f"line {line_no}: unrecognized line in step {cur['name']!r}: {stripped!r}")

    finalize()
    if not steps:
        raise SkillError(f"line 1: skill {name!r} has no steps ('## step: <name>' blocks)")
    return Skill(name=name, description=description, steps=tuple(steps))


# ---------- 模板 ----------


def _first_url(text: str) -> str | None:
    """从文本中提取首个 http(s) URL（剥掉常见尾随标点）。"""
    match = _URL_RE.search(text or "")
    if match is None:
        return None
    return match.group(0).rstrip(_URL_TRAILING)


def _render_template(template: str, *, input_text: str,
                     step_outputs: Mapping[str, str]) -> str:
    """渲染 ``{input}`` 与 ``{steps.<step>.text|first_url}``；未知引用抛 SkillError。"""

    def repl(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if expr == "input":
            return input_text
        if expr.startswith("steps."):
            parts = expr[len("steps."):].split(".")
            if len(parts) != 2 or not all(parts):
                raise SkillError(f"malformed step reference {{{expr}}}")
            step_name, field_name = parts
            if step_name not in step_outputs:
                raise SkillError(f"unknown or not-yet-executed step reference {step_name!r}")
            output = step_outputs[step_name]
            if field_name == "text":
                return output
            if field_name == "first_url":
                url = _first_url(output)
                if url is None:
                    raise SkillError(f"step {step_name!r} output contains no http(s) URL")
                return url
            raise SkillError(f"unknown field {field_name!r} in {{{expr}}}")
        raise SkillError(f"unknown placeholder {{{expr}}}")

    return _PLACEHOLDER_RE.sub(repl, template)


# ---------- 执行 ----------


def run_skill(skill: Skill, tools: ToolRegistry, *, gateway: ActionGatewayLike | None = None,
              input_text: str = "", output_chars: int = SKILL_OUTPUT_CHARS) -> SkillResult:
    """逐步串行执行 Skill，返回如实结局（任何异常都不炸，落在 status/receipts）。

    - 每步先把 args 模板渲染成纯字符串，再按 ``ToolRegistry`` 门控：
      read_only 直接调 handler；非 read_only 必须经 ``gateway`` 以
      ``PluginAction``（plugin_id/action 取自 ``ToolSpec``）派发——无网关或
      缺映射 → denied；
    - 网关回执 ``plugin-hold:`` / ``plugin-denied:`` 一律记为 denied
      （Skill 不等待审批，pending 语义如实归入 denied）；
    - 每步输出截断到 ``output_chars``；``save_as`` 非空的步骤进入 outputs；
      所有已执行步骤的输出都可被后续步骤以 ``{steps.<名>.text|first_url}`` 引用；
    - 首个失败/拒绝即终止整个 Skill（后续步骤不再执行），已完成步骤的
      outputs/receipts 照常保留。
    """
    limit = max(0, int(output_chars))
    receipts: list[str] = []
    outputs: dict[str, str] = {}
    step_outputs: dict[str, str] = {}

    def finish(status: str) -> SkillResult:
        return SkillResult(status=status, outputs=dict(outputs), receipts=tuple(receipts))

    for step in skill.steps:
        tag = f"skill:{skill.name}:step:{step.name}:tool:{step.tool}"
        # 1. 模板渲染（执行期才发现的引用错误 → failed）
        try:
            rendered = {
                key: _render_template(value, input_text=input_text, step_outputs=step_outputs)
                for key, value in step.args.items()
            }
        except SkillError as exc:
            receipts.append(f"{tag}:fail:template:{exc}")
            return finish("failed")
        # 2. 工具解析
        spec = tools.get(step.tool)
        if spec is None:
            receipts.append(f"{tag}:fail:unknown-tool")
            return finish("failed")
        # 3. 风险门控（与 ConversationOrchestrator 同款语义，执行器不能自批）
        if spec.risk_class != RiskClass.READ_ONLY.value:
            if not (spec.plugin_id and spec.action):
                receipts.append(f"{tag}:denied:risk-without-plugin-mapping")
                return finish("denied")
            if gateway is None:
                receipts.append(f"{tag}:denied:no-gateway")
                return finish("denied")
            intent = PluginAction(
                plugin_id=spec.plugin_id, action=spec.action,
                target=rendered.get("target", ""), args=dict(rendered),
            ).to_intent()
            try:
                receipt = str(gateway.dispatch(intent))
            except Exception as exc:
                receipts.append(f"{tag}:fail:gateway-{type(exc).__name__}")
                return finish("failed")
            if receipt.startswith("plugin:") and ":ok:" in receipt:
                output = receipt.split(":ok:", 1)[-1]
            elif receipt.startswith(("plugin-hold:", "plugin-denied:")):
                receipts.append(f"{tag}:denied:{receipt[:200]}")
                return finish("denied")
            else:
                receipts.append(f"{tag}:fail:{receipt[:200]}")
                return finish("failed")
        else:
            if spec.handler is None:
                receipts.append(f"{tag}:fail:unwired")
                return finish("failed")
            try:
                output = str(spec.handler(dict(rendered)))
            except Exception as exc:
                receipts.append(f"{tag}:fail:{type(exc).__name__}")
                return finish("failed")
        # 4. 成功：截断、入账、供后续步骤引用
        output = output[:limit]
        receipts.append(f"{tag}:ok")
        step_outputs[step.name] = output
        if step.save_as:
            outputs[step.save_as] = output
    return finish("succeeded")
