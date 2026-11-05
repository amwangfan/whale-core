"""安卓自主体编排循环（core#58）：observe → 决策 → control/shell。

复用 ``DeviceActionPort`` / gateway 下发 ``action.request``，读
``receipt.result.summary``；大输出经 ``device.agent.output.read`` +
``output_ref`` 拉取。失败语义见 ``agent_failures``；停止条件：端侧 estop
（表现为 unavailable）之外，core 侧 **吊销设备** 或 **收窄 scope** 后本循环
在下一步前中止且不重放。

屏幕观察写入设备私有记忆命名空间 ``device-agent:{device_id}``，与 QQ 群/
主人私聊空间隔离。决策端口可注入：测试用脚本化；生产可接 DeepSeek
（``DeepSeekDecisionPort``）——意图/审操仍可走 AgentScope 端点配置。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from whale_core.devices.agent_failures import (
    FAILED_SCOPE_DENIED,
    FAILED_TIMEOUT,
    FAILED_UNAVAILABLE,
    UNKNOWN,
    classify_exception,
    classify_receipt,
    should_abort_loop,
    should_auto_retry,
)
from whale_core.devices.registry import DeviceRegistry
from whale_core.life.autonomy import DeviceUnavailable
from whale_core.memory.continuity import remember_episode

CAP_OBSERVE = "device.agent.observe"
CAP_CONTROL = "device.agent.control"
CAP_SHELL = "device.agent.shell"
CAP_OUTPUT_READ = "device.agent.output.read"

AGENT_ACTION_CAPS = frozenset({CAP_OBSERVE, CAP_CONTROL, CAP_SHELL})
DEFAULT_MAX_STEPS = 8
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_TIMEOUT_RETRIES = 1
_SUMMARY_MAX = 2000
_OUTPUT_REF_RE = re.compile(
    r"^(agent:ui:latest|agent:out:[A-Za-z0-9._-]{1,128}"
    r"|agent:shot:shot-\d+\.jpg)$"
)


def device_agent_space_id(device_id: str) -> str:
    """设备私有记忆空间 id（与群/主人私聊隔离）。"""
    return f"device-agent:{device_id}"


@dataclass(frozen=True)
class AgentDecision:
    """模型/规则给出的一步决策。"""

    kind: str  # observe | control | shell | stop
    args: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    done: bool = False


@dataclass(frozen=True)
class AgentStepRecord:
    """单步执行结局（供测试与审计）。"""

    step: int
    capability: str
    outcome: str
    detail: str
    summary: str = ""
    output_ref: str = ""
    idempotency_key: str = ""


@dataclass
class AgentLoopResult:
    status: str  # succeeded | stopped | failed
    reason: str
    steps: list[AgentStepRecord] = field(default_factory=list)
    last_summary: str = ""


class DecisionPort(Protocol):
    def decide(
        self,
        *,
        goal: str,
        observation: Mapping[str, Any] | None,
        history: Sequence[AgentStepRecord],
    ) -> AgentDecision:
        ...


class OutputReadPort(Protocol):
    def read_output(
        self,
        device_id: str,
        *,
        output_ref: str,
        timeout_s: float,
    ) -> Mapping[str, Any]:
        """拉取大输出；不可用抛 DeviceUnavailable。"""
        ...


class ScriptedDecisionPort:
    """测试用：按队列吐决策；耗尽则 stop。"""

    def __init__(self, decisions: Sequence[AgentDecision]) -> None:
        self._queue = list(decisions)

    def decide(self, *, goal: str, observation: Mapping[str, Any] | None,
               history: Sequence[AgentStepRecord]) -> AgentDecision:
        if not self._queue:
            return AgentDecision(kind="stop", reason="script-exhausted", done=True)
        return self._queue.pop(0)


class DeepSeekDecisionPort:
    """用 DeepSeek（或兼容端点）产出一步 JSON 决策。

    期望模型返回 JSON：
    ``{"kind":"observe|control|shell|stop","args":{},"reason":"...","done":false}``
    解析失败 → stop（不明不重放）。
    """

    def __init__(self, companion, *, system: str = "") -> None:
        self._companion = companion
        self._system = system or (
            "你是安卓设备自主体决策器。根据观察摘要决定下一步。"
            "只输出 JSON 对象，字段 kind/args/reason/done；"
            "kind 为 observe|control|shell|stop。"
            "不要扮演「像真人」的闲聊。"
        )

    def decide(self, *, goal: str, observation: Mapping[str, Any] | None,
               history: Sequence[AgentStepRecord]) -> AgentDecision:
        obs_text = ""
        if observation:
            obs_text = str(observation.get("summary") or "")[:_SUMMARY_MAX]
            if observation.get("output_preview"):
                obs_text += "\n" + str(observation["output_preview"])[:1500]
        hist = [
            {"step": s.step, "capability": s.capability, "outcome": s.outcome,
             "summary": s.summary[:200]}
            for s in history[-6:]
        ]
        prompt = (
            f"目标：{goal}\n"
            f"最近步骤：{json.dumps(hist, ensure_ascii=False)}\n"
            f"当前观察：{obs_text or '(无)'}\n"
            "给出下一步 JSON。"
        )
        try:
            raw = self._companion.reply(prompt, persona=self._system)
        except Exception as exc:  # noqa: BLE001 — 模型失败 → 停止，不瞎控
            return AgentDecision(kind="stop", reason=f"model-error:{type(exc).__name__}",
                                 done=True)
        text = raw if isinstance(raw, str) else str(getattr(raw, "text", raw) or "")
        return _parse_decision_json(text)


def _parse_decision_json(text: str) -> AgentDecision:
    blob = text.strip()
    if "```" in blob:
        parts = blob.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                blob = part
                break
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end <= start:
        return AgentDecision(kind="stop", reason="unparseable-decision", done=True)
    try:
        data = json.loads(blob[start:end + 1])
    except json.JSONDecodeError:
        return AgentDecision(kind="stop", reason="unparseable-decision", done=True)
    kind = str(data.get("kind") or "stop").strip()
    if kind not in ("observe", "control", "shell", "stop"):
        return AgentDecision(kind="stop", reason="bad-decision-kind", done=True)
    args = data.get("args") if isinstance(data.get("args"), Mapping) else {}
    return AgentDecision(
        kind=kind,
        args=dict(args),
        reason=str(data.get("reason") or "")[:400],
        done=bool(data.get("done")) or kind == "stop",
    )


class GatewayOutputReadPort:
    """生产：经 DeviceGateway.send_query 拉 output_ref。"""

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    def read_output(self, device_id: str, *, output_ref: str,
                    timeout_s: float) -> Mapping[str, Any]:
        from whale_core.devices.gateway import GatewayError
        if not _OUTPUT_REF_RE.match(output_ref or ""):
            raise DeviceUnavailable("invalid_message", f"非法 output_ref: {output_ref!r}")
        try:
            return self._gateway.send_query(
                device_id, operation=CAP_OUTPUT_READ,
                filters={"output_ref": output_ref}, max_items=1,
                timeout_s=timeout_s)
        except GatewayError as exc:
            raise DeviceUnavailable(exc.code, str(exc)) from exc


class SimulatedOutputReadPort:
    """模拟：按 output_ref 回脚本化内容。"""

    def __init__(self, payloads: Mapping[str, Any] | None = None) -> None:
        self._payloads = dict(payloads or {})

    def read_output(self, device_id: str, *, output_ref: str,
                    timeout_s: float) -> Mapping[str, Any]:
        if not _OUTPUT_REF_RE.match(output_ref or ""):
            raise DeviceUnavailable("invalid_message", f"非法 output_ref: {output_ref!r}")
        scripted = self._payloads.get(output_ref)
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is None:
            return {"status": "ok", "data": [{"text": f"sim:{output_ref}"}]}
        if isinstance(scripted, Mapping):
            return dict(scripted)
        return {"status": "ok", "data": [{"text": str(scripted)}]}


class DeviceAgentLoop:
    """observe → 决策 → control/shell 自主循环（可模拟设备验收）。"""

    def __init__(
        self,
        registry: DeviceRegistry,
        device_port,
        *,
        decision: DecisionPort,
        output_port: OutputReadPort | None = None,
        store=None,
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        timeout_retries: int = DEFAULT_TIMEOUT_RETRIES,
        remember: bool = True,
        stop_check: Callable[[], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.device_port = device_port
        self.decision = decision
        self.output_port = output_port or SimulatedOutputReadPort()
        self.store = store
        self.max_steps = max(1, int(max_steps))
        self.timeout_s = float(timeout_s)
        self.timeout_retries = max(0, int(timeout_retries))
        self.remember = remember
        self._stop_check = stop_check

    def run(self, device_id: str, *, goal: str) -> AgentLoopResult:
        steps: list[AgentStepRecord] = []
        observation: dict[str, Any] | None = None
        last_summary = ""

        gate = self._preflight(device_id, required=(CAP_OBSERVE,))
        if gate is not None:
            return AgentLoopResult(status="stopped", reason=gate, steps=steps)

        for step_i in range(1, self.max_steps + 1):
            if self._stop_check and self._stop_check():
                return AgentLoopResult(
                    status="stopped", reason="external-stop", steps=steps,
                    last_summary=last_summary)

            # 每步前复查吊销 / scope（core 侧停止等价）
            gate = self._preflight(device_id, required=(CAP_OBSERVE,))
            if gate is not None:
                return AgentLoopResult(
                    status="stopped", reason=gate, steps=steps,
                    last_summary=last_summary)

            decision = self.decision.decide(
                goal=goal, observation=observation, history=steps)
            if decision.kind == "stop" or decision.done:
                return AgentLoopResult(
                    status="succeeded", reason=decision.reason or "decision-stop",
                    steps=steps, last_summary=last_summary)

            capability = {
                "observe": CAP_OBSERVE,
                "control": CAP_CONTROL,
                "shell": CAP_SHELL,
            }.get(decision.kind, "")
            if not capability:
                return AgentLoopResult(
                    status="failed", reason="bad-decision-kind", steps=steps,
                    last_summary=last_summary)

            gate = self._preflight(device_id, required=(capability,))
            if gate is not None:
                return AgentLoopResult(
                    status="stopped", reason=gate, steps=steps,
                    last_summary=last_summary)

            record, observation, last_summary = self._dispatch_action(
                device_id, capability=capability, args=dict(decision.args),
                step=step_i, observation=observation, last_summary=last_summary)
            steps.append(record)

            if record.outcome == "executed":
                continue
            if should_auto_retry(record.outcome, capability=capability):
                # 仅 observe（只读）超时：有界重试，可用新幂等键
                retried = False
                for _ in range(self.timeout_retries):
                    gate = self._preflight(device_id, required=(capability,))
                    if gate is not None:
                        return AgentLoopResult(
                            status="stopped", reason=gate, steps=steps,
                            last_summary=last_summary)
                    record, observation, last_summary = self._dispatch_action(
                        device_id, capability=capability,
                        args=dict(decision.args), step=step_i,
                        observation=observation, last_summary=last_summary)
                    steps.append(record)
                    retried = True
                    if record.outcome == "executed":
                        break
                    if not should_auto_retry(record.outcome, capability=capability):
                        break
                if record.outcome == "executed":
                    continue
                if should_abort_loop(record.outcome) or not retried:
                    return AgentLoopResult(
                        status="failed", reason=record.outcome, steps=steps,
                        last_summary=last_summary)
            elif record.outcome == FAILED_TIMEOUT:
                # control/shell：超时后先用原幂等键 reconcile，禁止盲目重放
                reconciled, detail = self._reconcile_after_timeout(
                    device_id, idempotency_key=record.idempotency_key,
                    capability=capability)
                if reconciled == "retry_safe":
                    # 仅 not_found：证明未落地，才允许有界新键重试
                    retried = False
                    for _ in range(self.timeout_retries):
                        gate = self._preflight(device_id, required=(capability,))
                        if gate is not None:
                            return AgentLoopResult(
                                status="stopped", reason=gate, steps=steps,
                                last_summary=last_summary)
                        record, observation, last_summary = self._dispatch_action(
                            device_id, capability=capability,
                            args=dict(decision.args), step=step_i,
                            observation=observation, last_summary=last_summary)
                        steps.append(record)
                        retried = True
                        if record.outcome == "executed":
                            break
                        if record.outcome != FAILED_TIMEOUT:
                            break
                        # 再次超时仍须 reconcile；非 not_found 则升 unknown
                        again, detail = self._reconcile_after_timeout(
                            device_id, idempotency_key=record.idempotency_key,
                            capability=capability)
                        if again != "retry_safe":
                            record = AgentStepRecord(
                                step=record.step, capability=record.capability,
                                outcome=UNKNOWN, detail=detail or again,
                                summary=record.summary,
                                output_ref=record.output_ref,
                                idempotency_key=record.idempotency_key)
                            steps[-1] = record
                            break
                    if record.outcome == "executed":
                        continue
                    if should_abort_loop(record.outcome) or not retried:
                        return AgentLoopResult(
                            status="failed", reason=record.outcome, steps=steps,
                            last_summary=last_summary)
                elif reconciled == "executed":
                    # 对账发现已执行：记为成功，不再下发
                    record = AgentStepRecord(
                        step=record.step, capability=record.capability,
                        outcome="executed", detail=detail or "timeout-reconcile:executed",
                        summary=record.summary, output_ref=record.output_ref,
                        idempotency_key=record.idempotency_key)
                    steps[-1] = record
                    continue
                else:
                    # pending / unknown / 终态 failed / 无 status API：
                    # 升编排码（默认 unknown），禁止自动重放
                    final = reconciled if reconciled in {
                        FAILED_UNAVAILABLE, FAILED_SCOPE_DENIED, UNKNOWN,
                        FAILED_TIMEOUT,
                    } or str(reconciled).startswith("failed/") else UNKNOWN
                    record = AgentStepRecord(
                        step=record.step, capability=record.capability,
                        outcome=final, detail=detail or reconciled,
                        summary=record.summary, output_ref=record.output_ref,
                        idempotency_key=record.idempotency_key)
                    steps[-1] = record
                    return AgentLoopResult(
                        status="failed", reason=record.outcome, steps=steps,
                        last_summary=last_summary)
            if should_abort_loop(record.outcome):
                return AgentLoopResult(
                    status="failed", reason=record.outcome, steps=steps,
                    last_summary=last_summary)
            # deferred 等：停止，不明不重放
            return AgentLoopResult(
                status="failed", reason=record.outcome, steps=steps,
                last_summary=last_summary)

        return AgentLoopResult(
            status="stopped", reason="max-steps", steps=steps,
            last_summary=last_summary)

    # ---- internals -------------------------------------------------------

    def _reconcile_after_timeout(
        self,
        device_id: str,
        *,
        idempotency_key: str,
        capability: str,
    ) -> tuple[str, str]:
        """超时后用原幂等键查 action_status，决定能否安全重试。

        返回 ``(decision, detail)``：
        - ``retry_safe``：status ``not_found``（未在设备侧落地）
        - ``executed`` / ``failed/...`` / ``unknown``：终态或不明，禁止新键重放
        - 无 ``action_status``：对有副作用能力一律 ``unknown``
        """
        status_fn = getattr(self.device_port, "action_status", None)
        if not callable(status_fn):
            return UNKNOWN, "timeout-reconcile:no-status-api"
        try:
            status = status_fn(device_id, idempotency_key)
        except DeviceUnavailable as exc:
            if exc.code == "not_found":
                return "retry_safe", "timeout-reconcile:not_found"
            # 查状态本身超时/不可用：结果不明，禁止重放
            return UNKNOWN, f"timeout-reconcile:status-error:{exc.code}"
        except Exception as exc:  # noqa: BLE001
            return UNKNOWN, f"timeout-reconcile:status-error:{type(exc).__name__}"

        if not isinstance(status, Mapping):
            return UNKNOWN, "timeout-reconcile:invalid-status"
        state = str(status.get("state") or "")
        if state in ("pending", "received", "accepted"):
            # 仍可能在设备侧执行中：升 unknown，禁止自动重放
            return UNKNOWN, f"timeout-reconcile:{state}"
        if state == "unknown":
            return UNKNOWN, "timeout-reconcile:unknown"
        if state == "executed":
            return "executed", "timeout-reconcile:executed"
        if state == "failed":
            # 保守：终态 failed 仍可能已有部分副作用，不自动重试
            err = str(status.get("error_code") or "internal")
            orch = classify_exception(DeviceUnavailable(err, err))
            if orch == FAILED_TIMEOUT:
                orch = FAILED_UNAVAILABLE
            return orch, f"timeout-reconcile:failed:{err}"
        # 未识别状态：不明
        return UNKNOWN, f"timeout-reconcile:unrecognized:{state or 'empty'}"

    def _preflight(self, device_id: str, *, required: Sequence[str]) -> str | None:
        status = self.registry.status_of(device_id)
        if status == "revoked":
            return FAILED_UNAVAILABLE
        if status != "active":
            return FAILED_UNAVAILABLE
        have = set(self.registry.scopes_of(device_id))
        missing = [c for c in required if c not in have]
        if missing:
            return FAILED_SCOPE_DENIED
        return None

    def _dispatch_action(
        self,
        device_id: str,
        *,
        capability: str,
        args: dict[str, Any],
        step: int,
        observation: dict[str, Any] | None,
        last_summary: str,
    ) -> tuple[AgentStepRecord, dict[str, Any] | None, str]:
        idem = f"agent-{uuid4().hex[:24]}"
        try:
            receipt = self.device_port.send_action(
                device_id, capability=capability, args=args,
                idempotency_key=idem, timeout_s=self.timeout_s)
        except DeviceUnavailable as exc:
            code = classify_exception(exc)
            return (
                AgentStepRecord(step=step, capability=capability, outcome=code,
                                detail=exc.code, idempotency_key=idem),
                observation, last_summary,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                AgentStepRecord(step=step, capability=capability, outcome=UNKNOWN,
                                detail=type(exc).__name__, idempotency_key=idem),
                observation, last_summary,
            )

        outcome, detail = classify_receipt(receipt)
        summary = ""
        output_ref = ""
        result = receipt.get("result") if isinstance(receipt, Mapping) else None
        if isinstance(result, Mapping):
            summary = str(result.get("summary") or "")[:_SUMMARY_MAX]
            output_ref = str(result.get("output_ref") or "")

        new_obs = observation
        if outcome == "executed" and capability == CAP_OBSERVE:
            new_obs = {
                "summary": summary,
                "output_ref": output_ref,
                "raw_result": dict(result) if isinstance(result, Mapping) else {},
            }
            if output_ref and CAP_OUTPUT_READ in self.registry.scopes_of(device_id):
                preview = self._safe_pull_output(device_id, output_ref)
                if preview:
                    new_obs["output_preview"] = preview
            if self.remember and self.store is not None and summary:
                remember_episode(
                    self.store,
                    summary=summary,
                    key=f"agent-obs:{device_id}:{idem}",
                    tags=("device.agent.observe", f"device:{device_id}"),
                    extra={"device_id": device_id, "output_ref": output_ref,
                           "modality": str(args.get("modality") or args.get("mode") or "")},
                    space_id=device_agent_space_id(device_id),
                )
            last_summary = summary or last_summary
        elif outcome == "executed" and summary:
            last_summary = summary

        return (
            AgentStepRecord(
                step=step, capability=capability, outcome=outcome, detail=detail,
                summary=summary, output_ref=output_ref, idempotency_key=idem),
            new_obs, last_summary,
        )

    def _safe_pull_output(self, device_id: str, output_ref: str) -> str:
        try:
            payload = self.output_port.read_output(
                device_id, output_ref=output_ref, timeout_s=self.timeout_s)
        except DeviceUnavailable:
            return ""
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, Mapping):
                for key in ("text", "ui_tree", "content"):
                    if first.get(key):
                        return str(first[key])[:1500]
                if first.get("jpeg_base64"):
                    return f"[jpeg_base64 len={len(str(first['jpeg_base64']))}]"
            return str(first)[:1500]
        return ""
