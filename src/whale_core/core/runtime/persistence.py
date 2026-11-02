"""任务台账与记忆持久化语义（issue #30 拆分，G1-B）。

- ``_store_runtime_state``：按 MemoryStore 共享的运行期状态（task_lock /
  workers / active_tasks / transient_args），WeakKeyDictionary 键控；
- ``PersistenceMixin``：journal/台账读写（_persist_task/_save_task/_append_event）、
  相位状态机（终态不回头）、生命周期 API（cancel/decide/resume/recover）、
  送达回执（mark_delivered/mark_delivery_failed/redeliver）与待办查询。
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, replace
from uuid import uuid4
from weakref import WeakKeyDictionary

from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime.context_resolution import _authz_snapshot
from whale_core.core.runtime.envelope import _PHASE_STATUS, TASK_PHASES, TurnResult
from whale_core.memory.history import conversation_lock
from whale_core.memory.store import MemoryStore

_STORE_STATES_LOCK = threading.Lock()
_STORE_STATES: WeakKeyDictionary = WeakKeyDictionary()


def _store_runtime_state(store: MemoryStore) -> tuple[dict, bool]:
    with _STORE_STATES_LOCK:
        state = _STORE_STATES.get(store)
        first = state is None
        if first:
            state = {"task_lock": threading.RLock(),
                     "workers": threading.BoundedSemaphore(4), "active_tasks": set(), "transient_args": {}}
            _STORE_STATES[store] = state
        return state, first


class PersistenceMixin:
    """任务持久化职责（编排器 mixin；状态由 ConversationOrchestrator 提供）。"""

    def _persist_task(self, plan: IntentPlan, *, conversation_id: str, text: str,
                      source: str, channel: str = "", owner_id: str = "",
                      tool_name: str = "",
                      async_delivery: bool = False,
                      context=None) -> tuple[str, IntentPlan]:
        """G1-B：在任何外部效果（含 ack）之前持久化任务台账。

        返回 (task_id, 带 task_id 的 plan)。台账 phase=pending，
        journal 记录渠道/来源/原文供恢复与补发。
        S1：携带 ResolvedContext 时落一份**授权快照**（主体/会话/scope/
        策略版本），供恢复与敏感动作前重验（INV-09）。
        """
        import re
        task_id = plan.task_id or plan.intent_id
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            task_id = uuid4().hex
        authz = _authz_snapshot(context)
        with self._task_lock:
            if self.store.get(f"task:{task_id}") is not None:
                task_id = uuid4().hex
            plan = replace(plan, task_id=task_id)
            transient_keys = {k for k in plan.slots if k.lower() in {
                "audio_base64", "audio", "audio_bytes", "file_base64", "image_base64"}}
            persisted_plan = asdict(plan)
            if transient_keys:
                self._transient_args[task_id] = {k: plan.slots[k] for k in transient_keys}
                persisted_plan["slots"] = {k: v for k, v in plan.slots.items() if k not in transient_keys}
            self.store.put(f"runtime:task:{task_id}", {
                "plan": persisted_plan, "transient_keys": sorted(transient_keys),
                "conversation_id": conversation_id, "source": source,
                "channel": channel, "owner_id": owner_id,
                "text": text, "next_step": 0, "results": [],
                "receipts": [], "tool_name": tool_name, "async": async_delivery,
                **({"authz": authz} if authz else {}),
            })
            self._save_task(task_id, plan, "pending", (), owner_id=owner_id)
            self._append_event(task_id, "planned",
                               source=source, channel=channel)
        return task_id, plan

    def _update_journal_plan(self, task_id: str, plan: IntentPlan) -> None:
        """异步路径参考图改写后同步 journal 里的 plan（幂等键不变）。"""
        with self._task_lock:
            journal = self.store.get(f"runtime:task:{task_id}")
            if not isinstance(journal, dict):
                return
            persisted_plan = asdict(plan)
            transient_keys = set(journal.get("transient_keys") or [])
            if transient_keys:
                persisted_plan["slots"] = {
                    k: v for k, v in persisted_plan["slots"].items()
                    if k not in transient_keys}
            journal["plan"] = persisted_plan
            self.store.put(f"runtime:task:{task_id}", journal)

    def _start_plan(self, plan: IntentPlan, conversation_id: str, text: str, *,
                    source: str, tool_name: str = "", channel: str = "",
                    owner_id: str = "", context=None) -> TurnResult:
        task_id, _plan = self._persist_task(
            plan, conversation_id=conversation_id, text=text, source=source,
            channel=channel, owner_id=owner_id, tool_name=tool_name,
            context=context)
        return self._execute_task(task_id)

    # ---- G1-B：phase 状态机、事件流水与送达回执 ----

    def _append_event(self, task_id: str, kind: str, **fields) -> None:
        """任务事件流水（持久化、可审计）：每个生命周期转换一条。"""
        try:
            with self._task_lock:
                record = self.store.get(f"task:{task_id}") or {}
                events = list(record.get("events") or [])
                entry = {"at": time.time(), "kind": kind}
                entry.update({k: v for k, v in fields.items()
                              if isinstance(v, (str, int, float, bool))})
                events.append(entry)
                record["events"] = events[-200:]
                self.store.put(f"task:{task_id}", record)
        except Exception:
            pass  # 事件流水失败不影响主流程（台账本身仍在）

    def _save_task(self, task_id: str, plan: IntentPlan, status: str,
                   receipts, **extra) -> dict:
        with self._task_lock:
            return self._save_task_locked(task_id, plan, status, receipts, **extra)

    _LEGACY_PHASE = {"answered": "succeeded"}  # 旧调用方传 status 词表

    def _save_task_locked(self, task_id: str, plan: IntentPlan, status: str,
                          receipts, **extra) -> dict:
        record = self.store.get(f"task:{task_id}") or {}
        if record.get("cancel_requested"):
            status = "unknown" if record.get("in_flight") else "cancelled"
        phase = self._LEGACY_PHASE.get(status, status)
        if phase not in TASK_PHASES:
            phase = "unknown"
        # 不回头：终态之后再 _save_task 只允许同态或 delivery 侧写
        terminal = {"succeeded", "failed", "unknown", "cancelled", "denied",
                    "delivered", "delivery_failed"}
        old_phase = record.get("phase", "")
        if old_phase in terminal and phase not in terminal:
            phase = old_phase
        record.update({"task_id": task_id, "at": time.time(), "goal": plan.goal[:500],
                       "phase": phase,
                       "status": _PHASE_STATUS.get(phase, phase),
                       "capabilities": list(plan.capabilities),
                       "receipts": list(receipts), **extra})
        self.store.put(f"task:{task_id}", record)
        index = self.store.get("tasks:recent") or []
        self.store.put("tasks:recent", ([i for i in index if i != task_id] + [task_id])[-50:])
        if old_phase != phase:
            self._append_event(task_id, phase)
        return record

    def register_delivery_handler(self, channel: str, handler) -> None:
        """宿主登记渠道投递器：``callable(task_record) -> bool``（True=已送达）。

        用于 redeliver_task 人工重发与启动恢复后的补发；handler 必须真实
        发送后按结果返回，不允许「接口返回 ok 就视为已送达」之外的谎报——
        返回 False 会保持 delivery_failed 可见。
        """
        self._delivery_handlers[channel] = handler

    def mark_delivered(self, task_id: str, *, channel: str = "") -> None:
        """宿主确认真实送达后调用（G1-B 送达回执，与「已执行」分开）。"""
        with self._task_lock:
            record = self.store.get(f"task:{task_id}")
            if not isinstance(record, dict):
                raise KeyError(task_id)
            delivery = dict(record.get("delivery") or {})
            delivery.update(state="delivered", last_error="",
                            attempts=int(delivery.get("attempts", 0)) + 1,
                            channel=channel or delivery.get("channel", ""))
            record["delivery"] = delivery
            if record.get("phase") in ("succeeded", "delivery_failed"):
                record["phase"] = "delivered"
                record["status"] = "answered"
            self.store.put(f"task:{task_id}", record)
        self._append_event(task_id, "delivered", channel=channel)

    def mark_delivery_failed(self, task_id: str, *, error: str = "",
                             channel: str = "") -> None:
        """补发失败：保持可见（delivery_failed），可人工 redeliver_task。"""
        with self._task_lock:
            record = self.store.get(f"task:{task_id}")
            if not isinstance(record, dict):
                return
            delivery = dict(record.get("delivery") or {})
            delivery.update(state="failed", last_error=error[:200],
                            attempts=int(delivery.get("attempts", 0)) + 1,
                            channel=channel or delivery.get("channel", ""))
            record["delivery"] = delivery
            if record.get("phase") in ("succeeded", "delivered"):
                record["phase"] = "delivery_failed"
            self.store.put(f"task:{task_id}", record)
        self._append_event(task_id, "delivery_failed", error=error[:120])

    def undelivered_tasks(self) -> list[dict]:
        """已完成但结果未确认送达的任务（待补发可见列表）。"""
        out: list[dict] = []
        for key in list(self.store.keys()):
            if not key.startswith("task:"):
                continue
            record = self.store.get(key)
            if not isinstance(record, dict):
                continue
            delivery = record.get("delivery") or {}
            if (record.get("phase") in ("succeeded", "delivery_failed")
                    and delivery.get("state") in ("pending", "failed")):
                out.append(record)
        return out

    def tasks_for_owner(self, owner_id: str, *, limit: int = 20) -> list[dict]:
        """G2 跨设备连续性：同一逻辑 owner 在任何渠道都能查到自己任务的进度。

        按 journal 里的 owner_id 归并（信封由渠道适配器在宿主侧认证后填写）；
        返回记录含 phase/status/delivery，但**不含**正文全文——跨渠道只暴露
        进度与结局摘要，私有上下文不跨权限域泄漏。
        """
        owner_id = (owner_id or "").strip()
        if not owner_id:
            return []
        out: list[dict] = []
        recent = self.store.get("tasks:recent") or []
        for task_id in reversed(recent):
            record = self.store.get(f"task:{task_id}")
            journal = self.store.get(f"runtime:task:{task_id}")
            if not isinstance(record, dict) or not isinstance(journal, dict):
                continue
            if str(journal.get("owner_id") or "") != owner_id:
                continue
            out.append({
                "task_id": task_id,
                "goal": record.get("goal", ""),
                "phase": record.get("phase", record.get("status", "")),
                "status": record.get("status", ""),
                "channel": journal.get("channel", ""),
                "delivery": (record.get("delivery") or {}).get("state", ""),
                "at": record.get("at", 0),
            })
            if len(out) >= limit:
                break
        return out

    def redeliver_task(self, task_id: str) -> dict:
        """人工重发：仅允许已执行成功但未确认送达的任务；不重新执行工具。"""
        with self._task_lock:
            record = self.store.get(f"task:{task_id}")
            if not isinstance(record, dict):
                raise KeyError(task_id)
            if record.get("phase") not in ("succeeded", "delivery_failed"):
                raise ValueError("only succeeded-but-undelivered tasks can be redelivered")
            delivery = dict(record.get("delivery") or {})
            if delivery.get("state") == "delivered":
                raise ValueError("task already delivered")
            channel = str(delivery.get("channel") or record.get("channel") or "")
            reply = str(record.get("result_reply") or "")
            if not reply:
                raise ValueError("no stored result to redeliver")
        handler = self._delivery_handlers.get(channel)
        if handler is None:
            raise ValueError(f"no delivery handler for channel {channel!r}")
        try:
            ok = bool(handler(record))
        except Exception as exc:
            self.mark_delivery_failed(task_id, error=f"redeliver:{type(exc).__name__}",
                                      channel=channel)
            raise
        if ok:
            self.mark_delivered(task_id, channel=channel)
            self._append_event(task_id, "redelivered", channel=channel)
        else:
            self.mark_delivery_failed(task_id, error="redeliver:handler-false",
                                      channel=channel)
        return self.store.get(f"task:{task_id}") or {}

    def pending_tasks(self) -> list[dict]:
        return [r for task_id in self.store.get("tasks:recent") or []
                if isinstance(r := self.store.get(f"task:{task_id}"), dict)
                and r.get("status") == "pending_approval"]

    def cancel_task(self, task_id: str) -> dict:
        """Stop later steps; an already dispatched external action may finish.

        Synchronous Python handlers cannot be safely killed. In-flight work is
        reported as unknown rather than claiming cancellation rolled it back.
        """
        with self._task_lock:
            record = self.store.get(f"task:{task_id}")
            if not isinstance(record, dict):
                raise KeyError(task_id)
            if record.get("status") not in {"pending", "acknowledged", "running",
                                            "pending_approval"}:
                raise ValueError("task is already terminal")
            record["cancel_requested"] = True
            phase = "unknown" if record.get("in_flight") else "cancelled"
            record["phase"] = phase
            record["status"] = phase
            record["error"] = "cancel-requested-in-flight" if record.get("in_flight") else "cancelled"
            self.store.put(f"task:{task_id}", record)
            self._append_event(task_id, phase, detail="cancel-requested")
            if phase == "cancelled":
                self._transient_args.pop(task_id, None)
            return record

    def decide_task(self, task_id: str, decision: str) -> dict:
        """Trusted host entry point, called only after authenticating the owner.

        This records approval for the exact pending action; it does not execute.
        resume_task still passes through the original gateway and all its policy.
        """
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve|reject")
        with self._task_lock:
            record = self.store.get(f"task:{task_id}")
            if not isinstance(record, dict):
                raise KeyError(task_id)
            if record.get("status") != "pending_approval":
                raise ValueError("task is not awaiting approval")
            previous = record.get("decision")
            if previous and previous != decision:
                raise ValueError("decision already recorded")
            record.update(decision=decision, decision_at=time.time())
            if decision == "reject":
                record["status"] = "denied"
                record["phase"] = "denied"
                self._transient_args.pop(task_id, None)
            self.store.put(f"task:{task_id}", record)
            self._append_event(task_id, "approved" if decision == "approve" else "rejected")
            return record

    def resume_task(self, task_id: str) -> TurnResult:
        with self._task_lock:
            record = self.store.get(f"task:{task_id}")
            if not isinstance(record, dict):
                raise KeyError(task_id)
            if record.get("status") != "pending_approval" or record.get("decision") != "approve":
                raise ValueError("task requires an explicit approval before resume")
            if task_id in self._active_tasks:
                raise ValueError("task is already executing")
            record["status"] = "running"
            record["phase"] = "running"
            self.store.put(f"task:{task_id}", record)
            self._append_event(task_id, "resumed")
        journal = self.store.get(f"runtime:task:{task_id}") or {}
        with conversation_lock(self.store, str(journal.get("conversation_id", ""))):
            return self._execute_task(task_id)

    def recover_interrupted_tasks(self, *, on_complete=None) -> list[str]:
        """进程重启后恢复被掐断的在途任务（G1-B phase 感知）。

        - phase=pending/acknowledged：任务已持久化但从未派发（ack 可能已发），
          安全重新执行（未产生外部副作用），完成后走 on_complete；
        - phase=running 且 journal 尚未派发任何工具（next_step=0、无回执）
          → 同样安全重跑；
        - 已派发过工具的 → 标记 unknown（外部结果不可知，**不自动重放**），
          走 on_complete 告知用户「上次被重启打断」；
        - phase=succeeded 但 delivery 未 delivered 的：不重跑，仅保持
          「待补发」可见（宿主可用 redeliver_task 人工重发）。
        返回恢复处理的任务 id 列表。幂等：重复调用不会重复执行。
        """
        recovered: list[str] = []
        for key in list(self.store.keys()):
            if not key.startswith("task:"):
                continue
            record = self.store.get(key)
            if not isinstance(record, dict):
                continue
            phase = record.get("phase") or record.get("status")
            if phase not in ("pending", "acknowledged", "running"):
                continue
            task_id = key[5:]
            journal = self.store.get(f"runtime:task:{task_id}")
            if not isinstance(journal, dict):
                continue
            raw = journal.get("plan") or {}
            raw = {**raw, "capabilities": tuple(raw.get("capabilities") or ()),
                   "missing": tuple(raw.get("missing") or ())}
            try:
                plan = IntentPlan(**raw)
            except Exception:
                continue
            untouched = (journal.get("next_step", 0) == 0
                         and not journal.get("receipts")
                         and not journal.get("dispatch_started")
                         and not record.get("in_flight"))
            if untouched:
                self._append_event(task_id, "recovered", detail="restart-rerun")
                def _rerun(tid=task_id, jid=journal):
                    result = self._execute_task(tid)
                    if on_complete is not None:
                        try:
                            on_complete(jid.get("conversation_id", ""), result.reply,
                                        result.status)
                        except Exception:
                            self.mark_delivery_failed(tid, error="host-callback-raised")
                threading.Thread(target=_rerun, daemon=True,
                                 name=f"whale-recover-{task_id}").start()
            else:
                # 已派发过工具：标 unknown，告知用户被打断（由宿主补发口径）
                self._save_task(task_id, plan, "unknown",
                                (*journal.get("receipts", []), "recovered:interrupted-by-restart"),
                                in_flight=False)
                if on_complete is not None:
                    jauthz = journal.get("authz") or {}
                    notice = self._voiced(
                        journal.get("conversation_id", ""), journal.get("text", ""),
                        "上一个任务执行到一半进程被重启打断了（外部结果不可知）。"
                        "请如实告诉用户：上次的事被重启打断了，请重发一次；"
                        "不要假装完成了。",
                        allowed_spaces=(jauthz.get("memory_spaces") if jauthz else None),
                        write_space=(jauthz.get("write_space") if jauthz else None))
                    try:
                        on_complete(journal.get("conversation_id", ""), notice, "unknown")
                    except Exception:
                        pass
            recovered.append(task_id)
        return recovered
