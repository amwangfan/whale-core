"""issue #20：SupervisedTaskRunner——监督任务接入正式执行/回执/续聊链路。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from whale_core.life.initiative import ProactiveOutbox, ProactivePrefs
from whale_core.life.supervisor import (
    PHASE_FAILED,
    PHASE_SUCCEEDED,
    PHASE_UNKNOWN,
    AgentSupervisor,
    SupervisorError,
)
from whale_core.life.supervisor_runner import SupervisedTaskRunner
from whale_core.memory.store import InMemoryStore


class _Ctx:
    class conversation:
        conversation_id = "private:10001"
    class principal:
        principal_id = "owner"
    capability_scopes = ("tool.web.search", "task.read:self")
    allowed_memory_spaces = ()


class TruePort:
    def __init__(self):
        self.sent = []

    def send(self, draft) -> bool:
        self.sent.append(draft.draft_id)
        return True


def _runner(store, **kw):
    outbox = ProactiveOutbox(store)
    sup = AgentSupervisor(store, outbox=outbox)
    return sup, outbox, SupervisedTaskRunner(sup, outbox=outbox, **kw)


def test_full_loop_goal_to_receipt_to_followup():
    """自然语言目标 → 有界任务 → 真实回执 → 汇总 → Outbox 续聊草稿。"""
    store = InMemoryStore()
    sup, outbox, runner = _runner(store)
    rec = runner.submit_goal("帮我盯着下载任务", context=_Ctx(),
                             scope=("tool.web.search",), steps_budget=3,
                             time_budget_s=60)
    assert rec["phase"] == "queued"
    assert rec["authz"]["conversation"] == "private:10001"
    handled = runner.run_pending(lambda r: ("receipt:dl-1", "下载完成了"))
    final = handled[0]
    assert final["phase"] == PHASE_SUCCEEDED
    assert final["receipt"] == "receipt:dl-1"
    # 完成 → Outbox 草稿（续聊只走既有 Outbox）
    draft = outbox.get(f"task-followup:{rec['task_id']}")
    assert draft is not None and "下载完成" in draft.text


def test_scope_widening_denied_at_submit():
    store = InMemoryStore()
    _, _, runner = _runner(store)
    with pytest.raises(SupervisorError):
        runner.submit_goal("越权", context=_Ctx(), scope=("tool.shell.safe",),
                           steps_budget=1, time_budget_s=10)


def test_authz_recheck_failure_blocks_execution():
    store = InMemoryStore()
    sup, outbox, runner = _runner(store, recheck_fn=lambda rec: False)
    runner.submit_goal("执行任务", context=_Ctx(), scope=("tool.web.search",),
                       steps_budget=1, time_budget_s=10)
    called = []
    handled = runner.run_pending(lambda r: called.append(r) or ("r", "s"))
    assert called == []                       # 复核失败不执行
    assert handled[0]["phase"] == PHASE_FAILED
    assert handled[0]["last_error"] == "authz-recheck-failed"


def test_repeated_completion_callback_no_duplicate_followup():
    store = InMemoryStore()
    sup, outbox, runner = _runner(store)
    rec = runner.submit_goal("t", context=_Ctx(), scope=("tool.web.search",),
                             steps_budget=1, time_budget_s=10)
    runner.run_pending(lambda r: ("rcpt", "done"))
    # 第二次 run_pending：任务已终态，不重复执行/不重复草稿
    called = []
    runner.run_pending(lambda r: called.append(r) or ("x", "y"))
    assert called == []
    assert sup.get(rec["task_id"])["phase"] == PHASE_SUCCEEDED
    # complete 幂等拒绝：非 running 直接 SupervisorError（不会产生第二条草稿）
    with pytest.raises(SupervisorError):
        sup.complete(rec["task_id"], receipt="again", summary="dup")


def test_unknown_never_retried():
    store = InMemoryStore()
    sup, _, runner = _runner(store)
    rec = runner.submit_goal("t", context=_Ctx(), scope=("tool.web.search",),
                             steps_budget=1, time_budget_s=10)
    sup.start(rec["task_id"])
    sup.mark_unknown(rec["task_id"], "重启时外部副作用结果不可知")
    called = []
    runner.run_pending(lambda r: called.append(r) or ("r", "s"))
    assert called == []
    assert sup.get(rec["task_id"])["phase"] == PHASE_UNKNOWN


def test_followup_dispatch_is_reactive_completion_quiet_exempt():
    """任务续答深夜可送达（reactive_completion）；主动草稿同刻仍 deferred。"""
    from whale_core.life.initiative import ProactiveDraft
    store = InMemoryStore()
    sup, outbox, runner = _runner(store)
    night = datetime(2026, 10, 31, 19, 30, tzinfo=timezone.utc)  # 03:30 CST
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}),
                           auto_send=True, daily_limit=10)
    runner.submit_goal("t", context=_Ctx(), scope=("tool.web.search",),
                       steps_budget=1, time_budget_s=10)
    runner.run_pending(lambda r: ("rcpt", "任务结果"))
    port = TruePort()
    # direct_message 渠道需宿主显式批准（安全审核通过后才发）——不伪造审批：
    assert runner.dispatch_ready_followups(prefs, port=port, now=night) == []
    accepted = runner.dispatch_ready_followups(prefs, port=port, now=night,
                                               explicitly_approved=True)
    assert accepted and port.sent == accepted  # 深夜续答送达
    # 对照：主动草稿同刻被静默拦下
    outbox.propose(ProactiveDraft("u1", "主动问候", "r", ("e",),
                                  channel="direct_message"))
    assert outbox.inspect("u1", prefs, now=night).status == "deferred"
