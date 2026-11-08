"""决策层：DecisionPort 协议 + 脚本化/LLM 实现。

动作词汇（capability + arguments，对齐 contracts/v2 §5.1）：
observe / device.tap{x,y} / device.tap{element_id}（须 observation_id）/
device.long_press / device.swipe / device.input_text / device.key /
device.wait / device.app.launch{package} / finish{success,message}（不上 wire）。

参考：dsh-mobile-gui-agent src/tools/schema.ts（oneOf 动作集）、
src/core/serialize.ts（元素行序列化）、src/agent/prompt.ts（规则）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from whale_core.devices.transports.base import Observation

# 决策层可产出的动作全集（finish/observe 为循环控制词，非设备能力）
ACTION_KINDS = frozenset({
    "observe",
    "device.tap", "device.long_press", "device.swipe", "device.input_text",
    "device.key", "device.wait", "device.app.launch",
    "finish",
})

# 副作用动作：超时后禁止自动重放（v1 reconcile 纪律）
SIDE_EFFECT_ACTIONS = frozenset({
    "device.tap", "device.long_press", "device.swipe", "device.input_text",
    "device.key", "device.app.launch",
})

KEY_WHITELIST = frozenset({
    "back", "home", "enter", "delete", "tab",
    "volume_up", "volume_down", "media_play_pause",
})


@dataclass(frozen=True)
class GuiAction:
    """一步决策产物。kind=finish 时循环终止。"""

    kind: str                       # ACTION_KINDS 之一
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    done: bool = False              # kind=finish 时恒 True
    success: bool = True            # finish 时的成败
    label: str = ""                 # 中文可理解短句，入 trace（无思维链）


class DecisionPort(Protocol):
    async def decide(
        self,
        *,
        goal: str,
        observation: Observation | None,
        history: Sequence[Mapping[str, Any]],
        stuck_hint: str,
    ) -> GuiAction:
        ...


class ScriptedDecision:
    """测试用：按队列吐 GuiAction；耗尽则 finish(success=True)。"""

    def __init__(self, actions: Sequence[GuiAction]) -> None:
        self._queue = list(actions)
        self.calls: list[dict] = []

    async def decide(self, *, goal: str, observation: Observation | None,
                     history: Sequence[Mapping[str, Any]],
                     stuck_hint: str = "") -> GuiAction:
        self.calls.append({"observation": observation, "stuck_hint": stuck_hint})
        if not self._queue:
            return GuiAction(kind="finish", done=True, reason="script-exhausted")
        return self._queue.pop(0)


_AGENT_RULES = """你是手机 GUI 自主体。规则：
1. 每个回合只输出一个动作，JSON 格式：{"action": "...", "arguments": {...}, "reason": "...", "label": "...", "done": false}
2. action 只能是：observe / device.tap / device.long_press / device.swipe / device.input_text / device.key / device.wait / device.app.launch / finish
3. 参数形状（严格，错名即失败）：
   - device.tap 点元素 {"element_id": N}；点坐标 {"x": 123, "y": 456}
   - device.long_press {"element_id": N} 或 {"x": 123, "y": 456, "duration_ms": 800}
   - device.swipe {"x1": 540, "y1": 1800, "x2": 540, "y2": 600, "duration_ms": 300}
   - device.input_text {"text": "要输入的文字"}；可选 "element_id" 先聚焦
   - device.key {"key": "back|home|enter|delete|tab"}
   - device.wait {"duration_ms": 1500}
   - device.app.launch {"package": "com.example.app"}
   - observe 无参数；finish {"success": true}
4. 点击元素用 element_id；元素序号只对最近一次观察有效，不确定就先 observe
5. 完成后 {"action":"finish","done":true,"success":true,"reason":"...","label":"任务完成的简述"}
6. label 用简短中文说明这一步做什么（如「点击搜索按钮」），不要解释思考过程
7. 只输出 JSON，不要输出其他文字"""


def serialize_elements(obs: Observation, *, max_items: int = 200) -> str:
    """元素表序列化（借鉴 src/core/serialize.ts 的行格式）。"""
    lines = []
    for el in obs.elements[:max_items]:
        flags = []
        if el.clickable:
            flags.append("clickable")
        if el.editable:
            flags.append("editable")
        if el.scrollable:
            flags.append("scrollable")
        label = el.text or el.content_desc or el.resource_id.rsplit("/", 1)[-1]
        role = el.class_name.rsplit(".", 1)[-1]
        l, t, r, b = el.bounds
        lines.append(f'[{el.element_id}] {role} "{label[:40]}" '
                     f'{" ".join(flags)} bounds=({l},{t},{r},{b})')
    return "\n".join(lines)


class LlmJsonDecision:
    """LLM JSON 决策端。complete: async callable(prompt: str) -> str。"""

    def __init__(self, complete, *, rules: str = _AGENT_RULES) -> None:
        self._complete = complete
        self._rules = rules

    async def decide(self, *, goal: str, observation: Observation | None,
                     history: Sequence[Mapping[str, Any]],
                     stuck_hint: str = "") -> GuiAction:
        obs_text = "(尚无观察)"
        if observation is not None:
            obs_text = (
                f"前台应用: {observation.foreground_package}\n"
                f"屏幕: {observation.screen_width}x{observation.screen_height}"
                f"{' 键盘弹出' if observation.keyboard_shown else ''}"
                f"{' 已锁屏' if observation.locked else ''}\n"
                f"元素表：\n{serialize_elements(observation)}"
            )
        hist = [
            {"step": h.get("step"), "action": h.get("action"),
             "outcome": h.get("outcome"), "summary": str(h.get("summary", ""))[:120]}
            for h in list(history)[-6:]
        ]
        prompt = (
            f"{self._rules}\n\n"
            f"任务目标：{goal}\n"
            f"最近步骤：{json.dumps(hist, ensure_ascii=False)}\n"
            + (f"卡住提示：{stuck_hint}\n" if stuck_hint else "")
            + f"当前观察：\n{obs_text}\n\n给出下一步 JSON。"
        )
        try:
            raw = await self._complete(prompt)
        except Exception as exc:  # noqa: BLE001 — 模型失败 → 终止，不瞎控
            return GuiAction(kind="finish", done=True, success=False,
                             reason=f"model-error:{type(exc).__name__}")
        action = parse_action_json(str(raw))
        if action.kind == "finish" and not action.success and action.reason.startswith(
                ("unparseable", "bad-action", "key-not-allowed")):
            # 格式错误给一次纠错机会（把错误回给模型），再失败才终止
            try:
                raw2 = await self._complete(
                    prompt + f"\n\n上一次输出有误（{action.reason}）。"
                             "请严格按动作词汇和 JSON 格式重新输出。")
                return parse_action_json(str(raw2))
            except Exception as exc:  # noqa: BLE001
                return GuiAction(kind="finish", done=True, success=False,
                                 reason=f"model-error:{type(exc).__name__}")
        return action


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_action_json(text: str) -> GuiAction:
    """解析模型输出为 GuiAction；失败 → finish(success=False)，不瞎控。"""
    blob = text.strip()
    m = _FENCE_RE.search(blob)
    if m:
        blob = m.group(1)
    else:
        start, end = blob.find("{"), blob.rfind("}")
        if start < 0 or end <= start:
            return GuiAction(kind="finish", done=True, success=False,
                             reason="unparseable-decision")
        blob = blob[start:end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return GuiAction(kind="finish", done=True, success=False,
                         reason="unparseable-decision")
    kind = str(data.get("action") or "").strip()
    if kind not in ACTION_KINDS:
        return GuiAction(kind="finish", done=True, success=False,
                         reason=f"bad-action:{kind[:40] or 'empty'}")
    args = data.get("arguments")
    if not isinstance(args, Mapping):
        args = {}
    args = dict(args)
    # device.key 白名单（防任意 keyevent）；模型常带 KEYCODE_ 前缀，归一化
    if kind == "device.key":
        key = str(args.get("key") or "").lower()
        if key.startswith("keycode_"):
            key = key[len("keycode_"):]
            args["key"] = key
        if key not in KEY_WHITELIST:
            return GuiAction(kind="finish", done=True, success=False,
                             reason=f"key-not-allowed:{key[:40]}")
    done = bool(data.get("done")) or kind == "finish"
    return GuiAction(
        kind=kind,
        arguments=args,
        reason=str(data.get("reason") or "")[:400],
        done=done,
        success=bool(data.get("success", True)),
        label=str(data.get("label") or "")[:200],
    )
