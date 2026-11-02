"""有界工具执行（issue #30 拆分）：固定能力链与 G1-C 模型执行器共用门控。

每一步的参数校验、授权、审批、额度、幂等与结果截断都在核心
（``_dispatch_step``）；执行器只看到允许的工具目录，不能自批。
结局回复组装在 ``response_assembly``。
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any, Mapping

from whale_core.core import budget
from whale_core.core.deepseek import ModelError
from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime.envelope import TurnResult
from whale_core.core.runtime.policy import _action_fingerprint, validate_tool_manifest
from whale_core.core.runtime.tools import ToolSpec
from whale_core.core.types import ActionIntent
from whale_core.memory.history import conversation_lock
from whale_core.plugins.intents import PluginAction
from whale_core.plugins.manifest import RiskClass


class ToolExecutionMixin:
    """工具执行职责（编排器 mixin；状态由 ConversationOrchestrator 提供）。"""

    def execute_tool(self, name: str, args: Mapping[str, Any], *,
                     conversation_id: str = "workflow", source: str = "api",
                     timeout_s: float = 60.0, task_id: str = "") -> TurnResult:
        """Explicit trusted-host tool invocation using the same bounded journal.

        Used by workflows; name is resolved against the installed registry and
        never interpreted as code. Tool risk and gateway policy remain active.
        """
        spec = self.tools.get(name)
        if spec is None:
            raise KeyError(name)
        import math
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 3600:
            raise ValueError("timeout_s must be within (0,3600]")
        plan = IntentPlan(goal=f"工具：{spec.name}", capabilities=(spec.capability,),
                          slots=dict(args), timeout_s=timeout_s, task_id=task_id)
        with conversation_lock(self.store, conversation_id):
            return self._start_plan(plan, conversation_id, f"执行工具：{spec.name}",
                                    source=source, tool_name=name)

    def _execute_task(self, task_id: str) -> TurnResult:
        with self._task_lock:
            if task_id in self._active_tasks:
                raise ValueError("task is already executing")
            self._active_tasks.add(task_id)
            self._append_event(task_id, "leased")
        try:
            return self._execute_task_inner(task_id)
        finally:
            with self._task_lock:
                self._active_tasks.discard(task_id)
                record = self.store.get(f"task:{task_id}") or {}
                if record.get("status") != "pending_approval":
                    self._transient_args.pop(task_id, None)

    def _execute_task_inner(self, task_id: str) -> TurnResult:
        journal = self.store.get(f"runtime:task:{task_id}")
        if not isinstance(journal, dict):
            raise ValueError("task execution journal unavailable")
        raw = journal["plan"]
        transient = self._transient_args.get(task_id, {})
        if any(key not in transient for key in journal.get("transient_keys", [])):
            plan = IntentPlan(**raw)
            receipts = [*journal.get("receipts", []), "tool-fail:transient-input-unavailable"]
            self._save_task(task_id, plan, "failed", receipts, in_flight=False)
            return TurnResult(reply="临时媒体输入已释放，请重新提供；没有重放工具。", status="failed",
                              plan=plan, receipts=tuple(receipts), task_id=task_id)
        raw = {**raw, "slots": {**raw["slots"], **transient}}
        plan = IntentPlan(**{**raw, "capabilities": tuple(raw["capabilities"]),
                             "missing": tuple(raw["missing"])})
        text, cid = journal["text"], journal["conversation_id"]
        receipts, results = list(journal["receipts"]), list(journal["results"])
        self._save_task(task_id, plan, "running", receipts)
        started = time.monotonic()
        status = "answered"
        if not plan.capabilities and self.executor_model is not None:
            # G1-C：模型执行器模式（可选）——固定链无法声明能力时由执行模型
            # 逐轮提议工具；每步仍经 _dispatch_step 的审批/网关/预算门控。
            status = self._execute_with_executor(
                task_id, plan, journal, receipts, results, started)
        elif not plan.capabilities:
            receipts.append("capability-unavailable:(none-declared)")
            status = "failed"
        for step, capability in enumerate(plan.capabilities):
            if step < journal["next_step"]:
                continue
            record = self.store.get(f"task:{task_id}") or {}
            if record.get("cancel_requested"):
                status = "cancelled" if not record.get("in_flight") else "unknown"
                break
            if step >= self.max_tool_rounds:
                receipts.append("task-limit:max-tool-rounds")
                status = "failed"
                break
            remaining = plan.timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                receipts.append("task-timeout:before-dispatch")
                status = "failed"
                break
            selected = self.tools.get(journal.get("tool_name", "")) if journal.get("tool_name") else None
            specs = ([selected] if selected is not None and selected.capability == capability else
                     [] if journal.get("tool_name") else self.tools.for_capability(capability))
            if not specs:
                receipts.append(f"capability-unavailable:{capability}")
                status = "failed"
                break
            spec = specs[0]
            outcome, note = self._dispatch_step(
                task_id, spec, plan, dict(plan.slots), step, remaining)
            if outcome == "cancelled":
                if note:
                    receipts.append(note)
                status = "cancelled"
                break
            receipts.append(note)
            if outcome == "timeout":
                status = "unknown"
                break
            if outcome == "scope_denied":
                status = "denied"
                break
            if outcome != "ok":
                status = ("pending_approval" if note.startswith("plugin-hold:") else
                          "denied" if note.startswith(("plugin-denied:", "tool-denied:")) else "failed")
                break
            payload = note.split(":ok:", 1)[-1]
            results.append(f"[{spec.name}] {payload}")
            journal = self.store.get(f"runtime:task:{task_id}") or journal
            journal.update(next_step=step + 1, results=results, receipts=receipts)
            self.store.put(f"runtime:task:{task_id}", journal)
            # The approval applies only to one action, never later steps.
            with self._task_lock:
                record = self.store.get(f"task:{task_id}") or {}
                record.pop("decision", None)
                record.pop("approval_used", None)
                self.store.put(f"task:{task_id}", record)
        record = self.store.get(f"task:{task_id}") or {}
        if record.get("cancel_requested"):
            status = "unknown" if record.get("in_flight") else "cancelled"
        journal = self.store.get(f"runtime:task:{task_id}") or journal
        journal.update(receipts=receipts, results=results)
        self.store.put(f"runtime:task:{task_id}", journal)
        saved = self._save_task(task_id, plan, status, receipts)
        status = saved["status"]
        return self._assemble_turn_reply(
            task_id, plan, journal, text, cid, status, receipts, results, saved)

    def _dispatch_step(self, task_id: str, spec: ToolSpec, plan: IntentPlan,
                       args: Mapping[str, Any], step: int,
                       remaining: float) -> tuple[str, str]:
        """单个工具步骤的授权绑定与限时派发（固定链与模型执行器共用）。

        审批绑定核心生成的任务身份 + 精确工具与参数指纹；非只读必经插件网关。
        返回 ``(outcome, note)``：outcome ∈
        ok | failed | timeout | cancelled | scope_denied；note 为回执文本。
        """
        # Bind approval to a core-generated task identity, exact tool and args.
        action = PluginAction(plugin_id=spec.plugin_id, action=spec.action,
                              target=str(args.get("target", "")),
                              args=dict(args)).to_intent()
        token = f"runtime-task:{task_id}:{step}"
        registry = getattr(self.gateway, "registry", None)
        manifest = registry.manifest(spec.plugin_id) if registry is not None else None
        action = replace(action, reason=token, payload={**action.payload,
            "runtime_manifest_version": manifest.version if manifest is not None else ""})
        fingerprint = _action_fingerprint(action)
        # F-01（红队）：capability scope 在执行端强制——任务携带授权快照时，
        # 工具能力与快照 scope 必须匹配（陌生人/群上限/会话收窄在此生效）。
        # 快照缺失时（S4 租约）可尝试用租约补 scope：租约须覆盖该能力并
        # 原子消耗操作次数；过期/撤销/用尽/参数指纹不符即拒（INV-06/09）。
        authz = (self.store.get(f"runtime:task:{task_id}") or {}).get("authz") or {}
        if authz:
            required = f"tool.{spec.capability}"
            base_scopes = set(authz.get("scopes") or ())
            grant_scopes = set(authz.get("grant_scopes") or ())
            grant_ids = tuple(authz.get("grant_ids") or ())
            if required not in base_scopes:
                # 租约贡献的能力：每次执行都要经 grant_gate 原子消耗次数
                if (required not in grant_scopes
                        or not grant_ids
                        or self.grant_gate is None
                        or not self.grant_gate(grant_ids, fingerprint, required)):
                    return "denied", f"tool-denied:{spec.name}:scope-{required}"
        with self._task_lock:
            record = self.store.get(f"task:{task_id}") or {}
            if record.get("cancel_requested"):
                return "cancelled", ""
            if record.get("decision") == "approve" and (
                    record.get("pending_fingerprint") != fingerprint or
                    record.get("approval_token") != token):
                return "scope_denied", f"tool-denied:{spec.name}:approval-scope-changed"
            # Durable pre-dispatch marker. Unknown external effects after a
            # crash must not be replayed as untouched tasks.
            journal = self.store.get(f"runtime:task:{task_id}")
            if not isinstance(journal, dict):
                raise ValueError("task journal unavailable before dispatch")
            journal["dispatch_started"] = {
                "step": step, "tool": spec.name, "at": time.time(),
                "fingerprint": fingerprint,
            }
            self.store.put(f"runtime:task:{task_id}", journal)
            record.update(status="running", phase="running", in_flight=True,
                          step=step,
                          pending_fingerprint=fingerprint, approval_token=token)
            self.store.put(f"task:{task_id}", record)
        step_plan = plan if dict(args) == dict(plan.slots) else replace(plan, slots=dict(args))
        outcome = self._bounded_tool(spec, step_plan, action, remaining, task_id)
        if outcome is None:
            return "timeout", f"tool-timeout:{spec.name}:outcome-unknown"
        ok, note = outcome
        with self._task_lock:
            record = self.store.get(f"task:{task_id}") or {}
            record["in_flight"] = False
            cancelled = bool(record.get("cancel_requested"))
            self.store.put(f"task:{task_id}", record)
        if cancelled:
            return "cancelled", note
        return ("ok" if ok else "failed"), note

    def _execute_with_executor(self, task_id: str, plan: IntentPlan,
                               journal: dict, receipts: list, results: list,
                               started: float) -> str:
        """G1-C 多轮工具执行模型：模型提议下一步，核心裁决并执行每一步。

        执行器只看到允许的工具目录（名称/能力/描述/风险）与有限轮预算；
        每一步的参数校验、授权、审批、额度、幂等与结果截断都在核心
        （``_dispatch_step`` 与固定链同一条门控路径）；每轮产生可回放的
        结构化回执。固定 Skill 链（plan.capabilities 非空）仍是稳定保底
        模式，本模式只在未声明能力且配置了 ``llm.tool_executor`` 时启用。
        """
        from whale_core.core.tool_executor import ExecutorStepError, ModelToolExecutor

        executor = ModelToolExecutor(self.executor_model)
        catalog = self.tools.specs()
        step = int(journal.get("next_step", 0))
        bad_proposals = 0
        while step < self.max_tool_rounds:
            record = self.store.get(f"task:{task_id}") or {}
            if record.get("cancel_requested"):
                return "cancelled" if not record.get("in_flight") else "unknown"
            remaining = plan.timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                receipts.append("task-timeout:before-dispatch")
                return "failed"
            try:
                proposal = executor.propose(goal=plan.goal, catalog=catalog,
                                            results=results, receipts=receipts)
            except Exception as exc:
                receipts.append(f"executor:invalid-proposal:{type(exc).__name__}")
                bad_proposals += 1
                if bad_proposals >= 2:
                    return "failed"
                continue
            if proposal["action"] == "finish":
                receipts.append("executor:finish")
                return "answered"
            if proposal["action"] == "fail":
                receipts.append(f"executor:gave-up:{proposal['rationale'][:80]}")
                return "failed"
            spec = self.tools.get(proposal["tool"])
            if spec is None:
                receipts.append(f"executor:unknown-tool:{proposal['tool'][:40]}")
                bad_proposals += 1
                if bad_proposals >= 2:
                    return "failed"
                continue
            bad_proposals = 0
            outcome, note = self._dispatch_step(
                task_id, spec, plan, proposal["args"], step, remaining)
            if outcome == "cancelled":
                if note:
                    receipts.append(note)
                return "cancelled"
            receipts.append(note)
            if outcome == "timeout":
                return "unknown"
            if outcome == "scope_denied":
                return "denied"
            if outcome != "ok":
                return ("pending_approval" if note.startswith("plugin-hold:") else
                        "denied" if note.startswith(("plugin-denied:", "tool-denied:"))
                        else "failed")
            payload = note.split(":ok:", 1)[-1]
            results.append(f"[{spec.name}] {payload}")
            step += 1
            journal = self.store.get(f"runtime:task:{task_id}") or journal
            journal.update(next_step=step, results=results, receipts=receipts)
            self.store.put(f"runtime:task:{task_id}", journal)
            # The approval applies only to one action, never later steps.
            with self._task_lock:
                record = self.store.get(f"task:{task_id}") or {}
                record.pop("decision", None)
                record.pop("approval_used", None)
                self.store.put(f"task:{task_id}", record)
        receipts.append("task-limit:max-tool-rounds")
        return "failed"

    def _bounded_tool(self, spec: ToolSpec, plan: IntentPlan, action: ActionIntent,
                      timeout: float, task_id: str) -> tuple[bool, str] | None:
        if not self._workers.acquire(blocking=False):
            return False, f"tool-fail:{spec.name}:worker-capacity-exhausted"
        done = threading.Event()
        box: list[tuple[bool, str]] = []
        def run() -> None:
            try:
                box.append(self._run_tool(spec, plan, action=action))
            except Exception as exc:
                box.append((False, f"tool-fail:{spec.name}:{type(exc).__name__}"))
            finally:
                try:
                    with self._task_lock:
                        record = self.store.get(f"task:{task_id}")
                        if isinstance(record, dict):
                            record["in_flight"] = False
                            self.store.put(f"task:{task_id}", record)
                except Exception:
                    # A closed/failing store must not leak worker capacity.
                    pass
                finally:
                    self._workers.release()
                    done.set()
        threading.Thread(target=run, daemon=True, name="whale-tool").start()
        deadline = time.monotonic() + timeout
        while not done.wait(min(0.05, max(0, deadline - time.monotonic()))):
            record = self.store.get(f"task:{task_id}") or {}
            if record.get("cancel_requested") or time.monotonic() >= deadline:
                return None
        return box[0]

    def _run_tool(self, spec: ToolSpec, plan: IntentPlan, *, action: ActionIntent | None = None) -> tuple[bool, str]:
        """执行单个工具，返回 (ok, receipt)。风险门控：非只读必经插件网关。"""
        if risk_error := validate_tool_manifest(spec, self.gateway):
            return False, f"tool-denied:{spec.name}:{risk_error}"
        if spec.capability.startswith("media."):
            # 媒体生成按次计 media_jobs 预算：预留先于真实派发，失败仍占额度。
            try:
                budget.reserve(self.store, "media_jobs")
            except ModelError:
                return False, f"tool-fail:{spec.name}:budget-exhausted"
        if spec.risk_class != RiskClass.READ_ONLY.value:
            if not (spec.plugin_id and spec.action):
                return False, f"tool-denied:{spec.name}:risk-without-plugin-mapping"
            if self.gateway is None:
                return False, f"tool-denied:{spec.name}:no-gateway"
            intent = action or PluginAction(
                plugin_id=spec.plugin_id, action=spec.action,
                target=str(plan.slots.get("target", "")), args=dict(plan.slots),
            ).to_intent()
            receipt = self.gateway.dispatch(intent)
            # 精确前缀匹配判定成功（安全审查修正：":ok:" 子串可被回执文本注入伪造）
            ok_prefix = f"plugin:{spec.plugin_id}:{spec.action}:ok:"
            if receipt.startswith(ok_prefix):
                return True, receipt[: self.tool_output_chars]
            return False, receipt[:200]
        if spec.handler is None:
            return False, f"tool-unwired:{spec.name}"
        try:
            output = str(spec.handler(dict(plan.slots)))
        except Exception as exc:
            return False, f"tool-fail:{spec.name}:{type(exc).__name__}"
        return True, f"tool:{spec.name}:ok:{output[: self.tool_output_chars]}"
