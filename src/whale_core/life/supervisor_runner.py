"""issue #20：SupervisedTaskRunner —— AgentSupervisor 与既有任务/外发链路的薄适配。

定位：**整合而非第二套任务运行时**。

- 派生/预算/退避/状态机由 ``AgentSupervisor`` 记账与门控（不直接派发工具）；
- 子任务执行使用**服务端解析**的身份/会话/记忆 ACL/能力快照（spawn 时落
  盘，执行前经 ``recheck_fn`` 复核——权限撤销/会话变更即失败，不沿用旧权）；
- 完成必须携带真实回执（execute_fn 返回值）；LLM 文本自称成功不算数；
- 完成后的「回来继续聊」只走 ProactiveOutbox 草稿，派发侧必须显式
  ``response_origin=reactive_completion``（issue #22：任务续答属于原请求
  的延续，深夜不拦，但仍是独立审计 lane）；重复 completion callback
  不会发两次（supervisor 状态机拒绝非 running 的 complete）。
- 外部结果不可知 → ``mark_unknown`` 终态，绝不自动重试。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Optional

from whale_core.life.initiative import (
    ORIGIN_REACTIVE_COMPLETION,
    ProactiveDraft,
    ProactiveOutbox,
    ProactivePrefs,
)
from whale_core.life.supervisor import (
    PHASE_QUEUED,
    AgentSupervisor,
    SupervisorError,
)

#: spawn 时落盘的授权快照键（执行前复核的基线）。
_AUTHZ_KEYS = ("conversation", "sender_id", "principal_id", "scopes",
               "allowed_spaces")


class SupervisedTaskRunner:
    """监督任务的执行泵：due → start → recheck → execute → complete/unknown。"""

    def __init__(
        self,
        supervisor: AgentSupervisor,
        *,
        outbox: Optional[ProactiveOutbox] = None,
        recheck_fn: Optional[Callable[[dict[str, Any]], bool]] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.supervisor = supervisor
        self.outbox = outbox
        # 执行前复核：True=快照仍有效；False/异常=权限已失效（任务失败）
        self._recheck = recheck_fn
        self._now = now_fn or time.time

    # ---- 派生（能力快照收窄） ---------------------------------------------

    def submit_goal(
        self,
        goal: str,
        *,
        context,
        scope: Iterable[str],
        steps_budget: int,
        time_budget_s: float,
        parent_task_id: str | None = None,
        channel: str = "direct_message",
    ) -> dict[str, Any]:
        """从（自然语言）目标派生有界任务；scope 只许是上下文能力的子集。"""
        want = frozenset(str(s) for s in scope)
        have = frozenset(getattr(context, "capability_scopes", ()) or ())
        if not want <= have:
            raise SupervisorError(
                "scope-widening-denied",
                f"任务 scope 超出当前会话授权：{sorted(want - have)}")
        record = self.supervisor.spawn(
            goal=goal, scope=sorted(want), steps_budget=steps_budget,
            time_budget_s=time_budget_s, parent_task_id=parent_task_id)
        # 授权快照：执行时复核的基线（服务端解析值，不取自模型输出）
        record["authz"] = {
            "conversation": str(getattr(
                getattr(context, "conversation", None), "conversation_id", "")
                            or getattr(context, "conversation_id", "") or ""),
            "sender_id": str(getattr(getattr(context, "principal", None),
                                     "principal_id", "") or ""),
            "principal_id": str(getattr(getattr(context, "principal", None),
                                        "principal_id", "") or ""),
            "scopes": sorted(have),
            "allowed_spaces": sorted(
                str(getattr(s, "space_id", "") or "")
                for s in (getattr(context, "allowed_memory_spaces", ()) or ())),
        }
        record["notify_channel"] = channel
        self.supervisor._put(record)
        return record

    # ---- 执行泵 ------------------------------------------------------------

    def run_pending(
        self,
        execute_fn: Callable[[dict[str, Any]], tuple[str, str]],
        *,
        max_tasks: int = 4,
    ) -> list[dict[str, Any]]:
        """执行到期任务。``execute_fn(record)`` 必须返回 ``(receipt, summary)``；
        抛错 → 失败（retryable 由异常类型不推断，显式由调用方 fail(retryable=…)）。

        返回本轮处理的任务台账列表（含未变的）。
        """
        handled: list[dict[str, Any]] = []
        for record in self.supervisor.list_tasks():
            if len(handled) >= max_tasks:
                break
            if record["phase"] != PHASE_QUEUED:
                continue
            backoff = record.get("backoff_until")
            if backoff and self._now() < float(backoff):
                continue
            task_id = record["task_id"]
            try:
                self.supervisor.start(task_id)
            except SupervisorError:
                continue
            # 执行前复核授权快照（撤销/会话变更/owner 变更 → 失败，不提权执行）
            if self._recheck is not None:
                try:
                    still_valid = bool(self._recheck(record))
                except Exception:
                    still_valid = False
                if not still_valid:
                    self.supervisor.fail(task_id, "authz-recheck-failed",
                                         retryable=False)
                    handled.append(self.supervisor.get(task_id) or record)
                    continue
            try:
                receipt, summary = execute_fn(record)
            except Exception as exc:
                self.supervisor.fail(task_id, f"execute:{type(exc).__name__}",
                                     retryable=False)
                handled.append(self.supervisor.get(task_id) or record)
                continue
            try:
                final, draft_id = self.supervisor.complete(
                    task_id, receipt=receipt, summary=summary,
                    channel=str(record.get("notify_channel")
                                or "direct_message"))
            except SupervisorError:
                # 并发/重复 completion：状态机拒绝，不产生第二条 follow-up
                handled.append(self.supervisor.get(task_id) or record)
                continue
            final = dict(final)
            final["followup_draft_id"] = draft_id
            handled.append(final)
        return handled

    # ---- 完成通知的派发（走既有 Outbox 裁决 + 续答来源标记） ---------------

    def dispatch_ready_followups(
        self,
        prefs: ProactivePrefs,
        *,
        port,
        now,
        max_dispatch: int = 4,
        explicitly_approved: bool = False,
    ) -> list[str]:
        """explicitly_approved：宿主已完成安全审核/人工批准时置 True
        （direct_message/call 渠道的策略要求）；否则该渠道草稿只会得到
        needs_approval 而不发送（不伪造审批）。"""
        """派发就绪的任务完成草稿（reactive_completion：深夜不拦，静默只拦
        主动打扰——issue #20/#22）。返回已接受派发的 draft_id。"""
        if self.outbox is None:
            return []
        accepted: list[str] = []
        for record in self.supervisor.list_tasks():
            if len(accepted) >= max_dispatch:
                break
            draft_id = f"task-followup:{record['task_id']}"
            draft = self.outbox.get(draft_id)
            if draft is None:
                continue
            verdict = self.outbox.inspect(
                draft_id, prefs, now=now, explicitly_approved=explicitly_approved,
                response_origin=ORIGIN_REACTIVE_COMPLETION)
            if verdict.status != "ready":
                continue
            result = self.outbox.dispatch(
                draft_id, prefs, now=now, port=port,
                explicitly_approved=explicitly_approved,
                response_origin=ORIGIN_REACTIVE_COMPLETION)
            if result.status == "accepted":
                accepted.append(draft_id)
        return accepted
