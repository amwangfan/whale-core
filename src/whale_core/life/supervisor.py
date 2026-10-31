"""G5.3：AgentSupervisor —— 父子任务、独立预算、状态机、失败退避、真实回执。

与 ``core/runtime.py`` 任务台账的关系：runtime 管「一次对话请求的有界工具
执行」（phase 状态机 + 审批 + 送达回执）；本模块管**自主循环里的长任务树**
（父子分解、跨任务预算、退避重试、完成后续聊）。两者共用 MemoryStore kv，
不平行重写任务引擎；本模块不直接派发工具——执行仍由 runtime/网关完成，
Supervisor 只记账与门控。

硬规则（计划 §7.3、§11 LIFE-02/MSG-03）：

- 子任务 scope 只许是父任务子集（widening 拒绝）；子任务 deadline 不得越过
  父任务剩余时限；每个子任务有**独立** steps/time 预算（父预算不代付）。
- 状态机：``queued → running → waiting_approval → succeeded / failed /
  cancelled / unknown``；``unknown`` 终态**不自动重试**（外部结果不可知）。
- 失败退避：仅显式 ``retryable=True`` 的失败可重入队，指数退避 + 次数上限。
- 「已完成」必须有真实回执：``complete`` 缺 receipt 直接拒绝。
- 任务完成后的续聊**只走 ProactiveOutbox**（复用预算/静默/审批策略），
  不另开主动消息通道。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox

#: 任务阶段（§7.3 状态机）
PHASE_QUEUED = "queued"
PHASE_RUNNING = "running"
PHASE_WAITING_APPROVAL = "waiting_approval"
PHASE_SUCCEEDED = "succeeded"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"
PHASE_UNKNOWN = "unknown"

TASK_PHASES = frozenset({
    PHASE_QUEUED, PHASE_RUNNING, PHASE_WAITING_APPROVAL,
    PHASE_SUCCEEDED, PHASE_FAILED, PHASE_CANCELLED, PHASE_UNKNOWN,
})
TERMINAL_PHASES = frozenset(
    {PHASE_SUCCEEDED, PHASE_FAILED, PHASE_CANCELLED, PHASE_UNKNOWN})

_INDEX_KEY = "supervisor:tasks"
_MAX_INDEX = 200


class SupervisorError(ValueError):
    """任务树操作被拒绝（携带稳定 code）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentSupervisor:
    """自主任务树监督器：只记账与门控，不直接派发工具。"""

    def __init__(
        self,
        store,
        *,
        outbox: ProactiveOutbox | None = None,
        max_children: int = 8,
        max_attempts: int = 3,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 300.0,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.outbox = outbox
        self.max_children = max(1, int(max_children))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base_s = max(0.0, float(backoff_base_s))
        self.backoff_cap_s = max(self.backoff_base_s, float(backoff_cap_s))
        self._now = now_fn or time.time

    # ---- 记录读写 --------------------------------------------------------

    def _key(self, task_id: str) -> str:
        return f"supervisor:task:{task_id}"

    def get(self, task_id: str) -> dict[str, Any] | None:
        record = self.store.get(self._key(task_id))
        return dict(record) if isinstance(record, Mapping) else None

    def _require(self, task_id: str) -> dict[str, Any]:
        record = self.get(task_id)
        if record is None:
            raise SupervisorError("not-found", f"未知任务 {task_id!r}")
        return record

    def _put(self, record: dict[str, Any]) -> dict[str, Any]:
        self.store.put(self._key(record["task_id"]), record)
        return record

    def list_tasks(self) -> list[dict[str, Any]]:
        out = []
        for task_id in self.store.get(_INDEX_KEY) or []:
            record = self.get(str(task_id))
            if record is not None:
                out.append(record)
        return out

    def children(self, parent_task_id: str) -> list[dict[str, Any]]:
        out = []
        for task_id in self.store.get(f"supervisor:children:{parent_task_id}") or []:
            record = self.get(str(task_id))
            if record is not None:
                out.append(record)
        return out

    # ---- 派生（父子 + scope 子集 + 独立预算） ------------------------------

    def spawn(
        self,
        *,
        goal: str,
        scope: Iterable[str],
        steps_budget: int,
        time_budget_s: float,
        parent_task_id: str | None = None,
        task_id: str = "",
    ) -> dict[str, Any]:
        if not goal.strip():
            raise SupervisorError("bad-request", "goal 必填")
        if int(steps_budget) <= 0:
            raise SupervisorError("bad-request", "steps_budget 须为正整数")
        if float(time_budget_s) <= 0:
            raise SupervisorError("bad-request", "time_budget_s 须为正数")
        now = self._now()
        child_scope = frozenset(str(s) for s in scope)
        parent_id = ""
        deadline = now + float(time_budget_s)
        if parent_task_id:
            parent = self._require(parent_task_id)
            if parent["phase"] in TERMINAL_PHASES:
                raise SupervisorError("parent-terminal", "父任务已终结，不能派生子任务")
            parent_scope = frozenset(parent.get("scope") or ())
            if not child_scope <= parent_scope:
                # scope 只许收窄：子任务要求父任务没有的能力一律拒绝
                raise SupervisorError(
                    "scope-widening-denied",
                    f"子任务 scope 超出父任务：{sorted(child_scope - parent_scope)}")
            running = [c for c in children_of(self, parent_task_id)
                       if c["phase"] not in TERMINAL_PHASES]
            if len(running) >= self.max_children:
                raise SupervisorError("children-cap", "父任务并发子任务已达上限")
            # 子任务不得比父任务活得更久
            deadline = min(deadline, float(parent.get("deadline") or deadline))
            parent_id = parent_task_id
        task_id = task_id or f"sup-{uuid4().hex[:20]}"
        if self.get(task_id) is not None:
            raise SupervisorError("conflict", f"任务 {task_id!r} 已存在")
        record = {
            "task_id": task_id,
            "parent_task_id": parent_id,
            "goal": goal[:500],
            "phase": PHASE_QUEUED,
            "scope": sorted(child_scope),
            "steps_budget": int(steps_budget),
            "steps_used": 0,
            "time_budget_s": float(time_budget_s),
            "created_at": now,
            "deadline": deadline,
            "attempts": 0,
            "backoff_until": None,
            "last_error": "",
            "receipt": "",
            "result_summary": "",
            "completed_at": None,
        }
        self._put(record)
        index = self.store.get(_INDEX_KEY) or []
        self.store.put(_INDEX_KEY, ([i for i in index if i != task_id] + [task_id])[-_MAX_INDEX:])
        if parent_id:
            ckey = f"supervisor:children:{parent_id}"
            kids = self.store.get(ckey) or []
            self.store.put(ckey, list(kids) + [task_id])
        return record

    # ---- 状态机 ------------------------------------------------------------

    def start(self, task_id: str) -> dict[str, Any]:
        record = self._require(task_id)
        if record["phase"] != PHASE_QUEUED:
            raise SupervisorError("bad-phase", f"仅 queued 可启动（当前 {record['phase']}）")
        now = self._now()
        if record.get("backoff_until") and now < float(record["backoff_until"]):
            raise SupervisorError("backoff", "退避中，未到重试时间")
        if now >= float(record["deadline"]):
            return self._finish(record, PHASE_FAILED, error="time-budget-exhausted")
        record["phase"] = PHASE_RUNNING
        record["backoff_until"] = None
        return self._put(record)

    def record_step(self, task_id: str, n: int = 1) -> dict[str, Any]:
        """计一步并复核预算/时限；超支即失败（预算独立，互不代付）。"""
        record = self._require(task_id)
        if record["phase"] != PHASE_RUNNING:
            raise SupervisorError("bad-phase", f"仅 running 可计步（当前 {record['phase']}）")
        record["steps_used"] = int(record.get("steps_used", 0)) + int(n)
        if record["steps_used"] > int(record["steps_budget"]):
            return self._finish(record, PHASE_FAILED, error="steps-budget-exhausted")
        if self._now() >= float(record["deadline"]):
            return self._finish(record, PHASE_FAILED, error="time-budget-exhausted")
        return self._put(record)

    def request_approval(self, task_id: str) -> dict[str, Any]:
        record = self._require(task_id)
        if record["phase"] != PHASE_RUNNING:
            raise SupervisorError("bad-phase", "仅 running 可转入待审批")
        record["phase"] = PHASE_WAITING_APPROVAL
        return self._put(record)

    def resume(self, task_id: str) -> dict[str, Any]:
        record = self._require(task_id)
        if record["phase"] != PHASE_WAITING_APPROVAL:
            raise SupervisorError("bad-phase", "仅 waiting_approval 可恢复")
        if self._now() >= float(record["deadline"]):
            return self._finish(record, PHASE_FAILED, error="time-budget-exhausted")
        record["phase"] = PHASE_RUNNING
        return self._put(record)

    def _finish(self, record: dict[str, Any], phase: str, *,
                error: str = "") -> dict[str, Any]:
        record["phase"] = phase
        if error:
            record["last_error"] = error[:200]
        self._put(record)
        return record

    def fail(self, task_id: str, error: str, *, retryable: bool = False) -> dict[str, Any]:
        """失败处理：仅显式 retryable 的失败按指数退避重入队；否则终结。"""
        record = self._require(task_id)
        if record["phase"] in TERMINAL_PHASES:
            # unknown 终态不重试；succeeded/cancelled/failed 同理不可退
            raise SupervisorError("terminal", f"任务已终结（{record['phase']}），不重试")
        if retryable and int(record.get("attempts", 0)) + 1 <= self.max_attempts:
            record["attempts"] = int(record.get("attempts", 0)) + 1
            delay = min(self.backoff_base_s * (2 ** (record["attempts"] - 1)),
                        self.backoff_cap_s)
            record["backoff_until"] = self._now() + delay
            record["phase"] = PHASE_QUEUED
            record["last_error"] = error[:200]
            return self._put(record)
        return self._finish(record, PHASE_FAILED, error=error)

    def mark_unknown(self, task_id: str, note: str) -> dict[str, Any]:
        """外部结果不可知（如重启/超时中断）：终态 unknown，**不自动重试**。"""
        record = self._require(task_id)
        if record["phase"] in TERMINAL_PHASES:
            raise SupervisorError("terminal", f"任务已终结（{record['phase']}）")
        return self._finish(record, PHASE_UNKNOWN, error=note)

    def cancel(self, task_id: str) -> dict[str, Any]:
        """取消并级联取消全部后代；终态任务保持不变。"""
        record = self._require(task_id)
        if record["phase"] not in TERMINAL_PHASES:
            record = self._finish(record, PHASE_CANCELLED, error="cancel-requested")
        for child in self.children(task_id):
            self.cancel(child["task_id"])
        return record

    def complete(
        self,
        task_id: str,
        *,
        receipt: str,
        summary: str,
        channel: str = "hud",
        importance: float = 0.6,
        topic: str = "",
    ) -> tuple[dict[str, Any], str | None]:
        """完成任务：必须有真实回执；续聊只走 ProactiveOutbox。

        返回 (record, draft_id)；未配置 outbox 时 draft_id 为 None（结果仍在
        台账可见，宿主可按既有补发路径处理）。
        """
        if not (receipt or "").strip():
            # 「已完成」必须有真实回执——发起任务/模型声称都不算完成
            raise SupervisorError("receipt-required", "完成任务必须携带真实执行回执")
        record = self._require(task_id)
        if record["phase"] != PHASE_RUNNING:
            raise SupervisorError("bad-phase", f"仅 running 可完成（当前 {record['phase']}）")
        record["phase"] = PHASE_SUCCEEDED
        record["receipt"] = receipt[:500]
        record["result_summary"] = (summary or "")[:800]
        record["completed_at"] = self._now()
        self._put(record)
        draft_id = None
        if self.outbox is not None and (summary or "").strip():
            draft = ProactiveDraft(
                draft_id=f"task-followup:{task_id}",
                text=summary[:800],
                reason="task-completed-followup",
                evidence=(f"supervisor:{task_id}", receipt[:200]),
                channel=channel,
                importance=importance,
                topic=topic,
            )
            self.outbox.propose(draft)  # 幂等；预算/审批由 dispatch 侧裁决
            draft_id = draft.draft_id
        return record, draft_id


def children_of(supervisor: AgentSupervisor, parent_task_id: str) -> list[dict[str, Any]]:
    return supervisor.children(parent_task_id)
