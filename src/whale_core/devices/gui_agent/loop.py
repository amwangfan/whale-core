"""GuiAgentLoop：observe → decide → act → verify 主循环。

设计出处（dsh-mobile-gui-agent）：
- 一回合一个动作；act 后的观察直接复用为下回合观察（src/agent/executor.ts）；
- stale_observation 不计失败、强制重新观察（src/agent/elements.ts）；
- 卡住检测只提示不终止（src/agent/stuck-detector.ts）；
- 高风险动作审批，未提供审批端口即拒绝（src/agent/approval.ts）；
- 侧效动作超时绝不自动重放（whale v1 agent_loop reconcile 纪律）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from whale_core.devices.transports.base import (
    ActionReceipt,
    ActionRequest,
    DeviceTransport,
    Observation,
    ObserveRequest,
    ReceiptStatus,
    new_id,
    now_ms,
)
from whale_core.devices.gui_agent.approval import ApprovalPort, classify_risk
from whale_core.devices.gui_agent.decision import (
    SIDE_EFFECT_ACTIONS,
    DecisionPort,
    GuiAction,
)
from whale_core.devices.gui_agent.stuck import StuckDetector, normalize_action
from whale_core.devices.gui_agent.trace import RunTraceWriter
from whale_core.devices.gui_agent.verify import verify_action

DEFAULT_MAX_STEPS = 50
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_TASK_TIMEOUT_MS = 600_000
DEFAULT_ACTION_TIMEOUT_MS = 10_000
# 观察比单动作慢得多（截图+uiautomator+多条 dumpsys；慢机/录制并发时尤甚），
# 独立于动作超时（nova 真机实测观察可达 15s+）
DEFAULT_OBSERVE_TIMEOUT_MS = 45_000


@dataclass(frozen=True)
class GuiStepRecord:
    step: int
    action_kind: str
    outcome: str                 # receipt.status 值 / "skipped" / "approval_denied"
    summary: str = ""
    error_code: str | None = None
    verification: str = ""       # verification.status 值
    observation_id: str = ""     # 步后观察


@dataclass
class GuiRunResult:
    status: str                  # succeeded | stopped | failed
    reason: str
    run_id: str
    steps: list[GuiStepRecord] = field(default_factory=list)
    last_observation_id: str = ""


def _state_sig(obs: Observation | None) -> str:
    if obs is None:
        return "none"
    return (f"{obs.foreground_package}|{obs.activity or ''}|"
            f"{obs.ui_signature}|{obs.screenshot_signature or ''}")


def _obs_summary(obs: Observation | None) -> str:
    if obs is None:
        return "(无观察)"
    return (f"前台 {obs.foreground_package} 元素 {len(obs.elements)} 个"
            f"{' 截屏' if obs.screenshot_ref else ''}")


class GuiAgentLoop:
    def __init__(
        self,
        transport: DeviceTransport,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        task_timeout_ms: int = DEFAULT_TASK_TIMEOUT_MS,
        action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
        observe_timeout_ms: int = DEFAULT_OBSERVE_TIMEOUT_MS,
        observe_request: ObserveRequest | None = None,
    ) -> None:
        self.transport = transport
        self.max_steps = max(1, int(max_steps))
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.task_timeout_ms = int(task_timeout_ms)
        self.action_timeout_ms = int(action_timeout_ms)
        self.observe_timeout_ms = int(observe_timeout_ms)
        self._observe_request = observe_request or ObserveRequest()

    async def run(
        self,
        device_id: str,
        *,
        goal: str,
        decision: DecisionPort,
        approval: ApprovalPort | None = None,
        trace: RunTraceWriter | None = None,
        task_id: str | None = None,
    ) -> GuiRunResult:
        trace = trace or RunTraceWriter(new_id("run"))
        run_id = trace.run_id
        steps: list[GuiStepRecord] = []
        act_steps = 0
        consecutive_failures = 0
        started_at = now_ms()
        stuck = StuckDetector()
        stuck_hint = ""

        trace.emit("task_started", task_summary=goal)
        observation = await self._observe(device_id, trace)
        if observation is None:
            trace.emit("task_failed", result="initial-observe-failed")
            return GuiRunResult("failed", "initial-observe-failed", run_id, steps)

        # 锁屏护栏：locked 状态下允许若干次解锁尝试（agent 可能滑开无密码锁屏），
        # 但持续 locked 的任务不可能推进——诚实快速失败，不烧步数
        #（nova GSI 实测：keyguard 无法远程解除时 12 步全部浪费在解锁上）
        locked_act_budget = 3

        while act_steps < self.max_steps:
            if now_ms() - started_at > self.task_timeout_ms:
                trace.emit("task_failed", result="task-timeout")
                return GuiRunResult("failed", "task-timeout", run_id, steps,
                                    observation.observation_id)

            action = await decision.decide(
                goal=goal, observation=observation,
                history=[{"step": s.step, "action": s.action_kind,
                          "outcome": s.outcome, "summary": s.summary}
                         for s in steps],
                stuck_hint=stuck_hint)

            if observation.locked and action.kind != "observe":
                if locked_act_budget <= 0:
                    trace.emit("task_failed", result="device-locked")
                    return GuiRunResult("failed", "device-locked", run_id, steps,
                                        observation.observation_id)
                locked_act_budget -= 1

            if action.done or action.kind == "finish":
                kind = "task_completed" if action.success else "task_failed"
                trace.emit(kind, result=action.reason)
                return GuiRunResult(
                    "succeeded" if action.success else "failed",
                    action.reason or "finish", run_id, steps,
                    observation.observation_id)

            if action.kind == "observe":
                observation = await self._observe(device_id, trace) or observation
                continue

            act_steps += 1
            step_no = len(steps) + 1

            # ---- 审批（高风险动作）----
            risk = classify_risk(action.kind, action.arguments, observation)
            if risk:
                if approval is None:
                    steps.append(GuiStepRecord(
                        step_no, action.kind, "approval_denied",
                        summary=risk, error_code="approval_denied",
                        observation_id=observation.observation_id))
                    trace.emit("action_failed", result="approval_denied:no-port",
                               action=self._trace_action(action))
                    consecutive_failures += 1
                    if consecutive_failures >= self.max_consecutive_failures:
                        trace.emit("task_failed", result="max-consecutive-failures")
                        return GuiRunResult("failed", "max-consecutive-failures",
                                            run_id, steps,
                                            observation.observation_id)
                    continue
                trace.emit("approval_requested", result=risk,
                           action=self._trace_action(action))
                ok = await approval.request(
                    action_kind=action.kind, arguments=dict(action.arguments),
                    reason=risk, observation_summary=_obs_summary(observation))
                trace.emit("approval_resolved",
                           result="approved" if ok else "denied")
                if not ok:
                    steps.append(GuiStepRecord(
                        step_no, action.kind, "approval_denied",
                        summary=risk, error_code="approval_denied",
                        observation_id=observation.observation_id))
                    trace.emit("action_failed", result="approval_denied",
                               action=self._trace_action(action))
                    continue

            # ---- 执行 ----
            args = dict(action.arguments)
            obs_id = None
            if "element_id" in args:
                obs_id = observation.observation_id  # §8 绑定
            req = ActionRequest(
                action_id=new_id("act"),
                capability=action.kind,
                arguments=args,
                idempotency_key=f"agent:{run_id}:step{step_no}",
                timeout_ms=self.action_timeout_ms,
                task_id=task_id,
                run_id=run_id,
                observation_id=obs_id,
            )
            before = observation
            trace.emit("action_started", action=self._trace_action(action))
            receipt = await self._execute(device_id, req)

            # ---- stale：不计失败，强制重新观察 ----
            if (receipt.status is ReceiptStatus.FAILED
                    and receipt.error_code == "stale_observation"):
                steps.append(GuiStepRecord(
                    step_no, action.kind, "stale_observation",
                    summary="元素已过期，重新观察", error_code="stale_observation",
                    observation_id=before.observation_id))
                trace.emit("action_failed", result="stale_observation",
                           action=self._trace_action(action))
                stuck_hint = self._stuck(stuck, action, before, None, False,
                                         "stale_observation", trace)
                observation = await self._observe(device_id, trace) or before
                continue

            # ---- 超时纪律：侧效动作绝不自动重放 ----
            if receipt.status is ReceiptStatus.TIMEOUT:
                side_effect = action.kind in SIDE_EFFECT_ACTIONS
                note = ("侧效动作超时：结果未知，已交决策层处置（不重放）"
                        if side_effect else "只读动作超时")
                steps.append(GuiStepRecord(
                    step_no, action.kind, "timeout",
                    summary=note, error_code="timeout",
                    observation_id=before.observation_id))
                trace.emit("action_failed", result=note,
                           action=self._trace_action(action))
                if side_effect:
                    consecutive_failures += 1
                    stuck_hint = self._stuck(stuck, action, before, None, False,
                                             "timeout", trace)
                    observation = await self._observe(device_id, trace) or before
                    if consecutive_failures >= self.max_consecutive_failures:
                        trace.emit("task_failed", result="max-consecutive-failures")
                        return GuiRunResult("failed", "max-consecutive-failures",
                                            run_id, steps,
                                            observation.observation_id)
                    continue
                # 只读动作（wait）：有界重试一次
                receipt = await self._execute(device_id, req)
                if receipt.status is ReceiptStatus.TIMEOUT:
                    consecutive_failures += 1
                    observation = await self._observe(device_id, trace) or before
                    continue

            # ---- 失败终态 ----
            if receipt.status is not ReceiptStatus.SUCCEEDED:
                steps.append(GuiStepRecord(
                    step_no, action.kind, receipt.status.value,
                    summary=receipt.summary, error_code=receipt.error_code,
                    observation_id=before.observation_id))
                trace.emit("action_failed", result=receipt.summary or receipt.status.value,
                           action=self._trace_action(action))
                consecutive_failures += 1
                stuck_hint = self._stuck(stuck, action, before, None, False,
                                         receipt.error_code, trace)
                observation = await self._observe(device_id, trace) or before
                if consecutive_failures >= self.max_consecutive_failures:
                    trace.emit("task_failed", result="max-consecutive-failures")
                    return GuiRunResult("failed", "max-consecutive-failures",
                                        run_id, steps, observation.observation_id)
                continue

            # ---- 成功：after 观察 + 确定性验证 ----
            after = await self._observe(device_id, trace) or before
            verification = verify_action(
                kind=action.kind, arguments=args, before=before, after=after)
            steps.append(GuiStepRecord(
                step_no, action.kind, "succeeded",
                summary=receipt.summary, verification=verification.status.value,
                observation_id=after.observation_id))
            trace.emit("action_completed", result=receipt.summary,
                       verification={"status": verification.status.value,
                                     "summary": verification.summary},
                       action=self._trace_action(action))
            consecutive_failures = 0
            stuck_hint = self._stuck(stuck, action, before, after, True,
                                     None, trace)
            observation = after  # 复用为下回合观察（省一次往返）

        trace.emit("task_failed", result="max-steps")
        return GuiRunResult("stopped", "max-steps", run_id, steps,
                            observation.observation_id if observation else "")

    # ---- internals -------------------------------------------------------

    async def _observe(self, device_id: str,
                       trace: RunTraceWriter) -> Observation | None:
        try:
            obs = await asyncio.wait_for(
                self.transport.observe(device_id, self._observe_request),
                timeout=self.observe_timeout_ms / 1000)
        except Exception as exc:  # noqa: BLE001 — 观察失败不炸循环，但原因必须可见
            trace.emit("note", result=f"observe-failed: {type(exc).__name__}: "
                                     f"{str(exc)[:150]}")
            return None
        trace.emit("observation",
                   result=f"前台 {obs.foreground_package} 元素 {len(obs.elements)} 个")
        return obs

    async def _execute(self, device_id: str, req: ActionRequest) -> ActionReceipt:
        try:
            return await self.transport.execute(device_id, req)
        except Exception as exc:  # noqa: BLE001 — transport 异常归一为 failed
            return ActionReceipt(
                action_id=req.action_id, status=ReceiptStatus.FAILED,
                summary=f"transport 异常: {type(exc).__name__}",
                error_code="internal", completed_at=now_ms())

    def _stuck(self, stuck: StuckDetector, action: GuiAction,
               before: Observation | None, after: Observation | None,
               success: bool, error_code: str | None,
               trace: RunTraceWriter) -> str:
        hint = stuck.record(
            action_key=normalize_action(action.kind, action.arguments),
            state_before=_state_sig(before), state_after=_state_sig(after),
            success=success, error_code=error_code)
        if hint:
            trace.emit("note", result=hint.render())
            return hint.render()
        return ""

    @staticmethod
    def _trace_action(action: GuiAction) -> dict:
        target = ""
        args = action.arguments
        if "element_id" in args:
            target = f"element:{args['element_id']}"
        elif "package" in args:
            target = str(args["package"])
        elif "x" in args and "y" in args:
            target = f"({args['x']},{args['y']})"
        return {"type": action.kind, "label": action.label or action.kind,
                "target": target}


def success_or(error_code: str | None) -> bool:
    return error_code is None
