"""Execution journals verify real dispatch counts, approval scope and failure state."""
import json
import threading
import time

import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec, runtime_approval_verifier
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.gateway import PluginGateway
from whale_core.plugins.manifest import PluginManifest, PluginKind, RiskClass
from whale_core.plugins.policy import PluginActionPolicy
from whale_core.plugins.registry import PluginRegistry

class Brain:
    def reply(self, text, **kwargs):
        return "收到"

class Planner:
    def __init__(self, capabilities): self.capabilities = capabilities
    def plan(self, text, **kwargs):
        return json.dumps({"goal": "发送", "kind": "task", "capabilities": self.capabilities,
                           "slots": {"target": "owner", "message": "hello"}})

def make(store=None, capabilities=("read",), gateway=None):
    store = store or InMemoryStore()
    tools = ToolRegistry()
    return ConversationOrchestrator(store, WhaleCompanion(store, Brain()),
                                    intent_model=Planner(capabilities), tools=tools, gateway=gateway)

def test_partial_success_then_hold_is_pending_and_resume_skips_completed_tool():
    calls = []
    approved = [False]
    class Gateway:
        def dispatch(self, intent):
            if approved[0]:
                calls.append("send")
                return "plugin:mail:send:ok:sent"
            return "plugin-hold:mail:send"
    orch = make(capabilities=("read", "mail"), gateway=Gateway())
    orch.tools.register(ToolSpec("read", "read", handler=lambda _: calls.append("read") or "read"))
    orch.tools.register(ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="send"))
    first = orch.handle_message("c", "发送")
    assert first.status == "pending_approval"
    assert calls == ["read"]
    with pytest.raises(ValueError): orch.resume_task(first.task_id)
    orch.decide_task(first.task_id, "approve")
    approved[0] = True
    second = orch.resume_task(first.task_id)
    assert second.status == "answered"
    assert calls == ["read", "send"]
    with pytest.raises(ValueError): orch.resume_task(first.task_id)

def test_pending_task_survives_runtime_recreation_and_reject_is_terminal():
    class Gateway:
        def dispatch(self, intent): return "plugin-hold:mail:send"
    orch = make(capabilities=("mail",), gateway=Gateway())
    orch.tools.register(ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="send"))
    task = orch.handle_message("c", "发送")
    restarted = make(store=orch.store)
    assert restarted.pending_tasks()[0]["task_id"] == task.task_id
    assert restarted.decide_task(task.task_id, "reject")["status"] == "denied"
    with pytest.raises(ValueError): restarted.resume_task(task.task_id)

def test_cancel_pending_never_executes():
    class Gateway:
        def dispatch(self, intent): return "plugin-hold:mail:send"
    orch = make(capabilities=("mail",), gateway=Gateway())
    orch.tools.register(ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="send"))
    task = orch.handle_message("c", "发送")
    assert orch.cancel_task(task.task_id)["status"] == "cancelled"
    with pytest.raises(ValueError): orch.decide_task(task.task_id, "approve")

def test_timeout_is_unknown_no_replay_and_worker_capacity_bounded(monkeypatch):
    release = threading.Event()
    calls = []
    orch = make()
    orch.tools.register(ToolSpec("slow", "read", handler=lambda _: (calls.append(1), release.wait())[0]))
    monkeypatch.setattr("whale_core.core.runtime.plan_from_model_text", lambda _: IntentPlan(
        goal="slow", capabilities=("read",), timeout_s=.03))
    try:
        results = [orch.handle_message("c", "等待") for _ in range(5)]
        assert [r.status for r in results[:4]] == ["unknown"] * 4
        assert results[4].status == "failed"
        assert len(calls) == 4
        with pytest.raises(ValueError): orch.resume_task(results[0].task_id)
    finally:
        release.set()

def test_cancel_in_flight_blocks_following_steps(monkeypatch):
    started, release = threading.Event(), threading.Event()
    orch = make(capabilities=("read", "after"))
    orch.tools.register(ToolSpec("slow", "read", handler=lambda _: (started.set(), release.wait(), "done")[-1]))
    called = []
    orch.tools.register(ToolSpec("after", "after", handler=lambda _: called.append(1) or "bad"))
    box = []
    thread = threading.Thread(target=lambda: box.append(orch.handle_message("c", "等待")))
    thread.start()
    assert started.wait(1)
    task_id = orch.store.get("tasks:recent")[0]
    assert orch.cancel_task(task_id)["status"] == "unknown"
    thread.join(1)
    release.set()
    assert box[0].status == "unknown"
    assert called == []

def test_round_limit_not_just_documentation():
    orch = make(capabilities=("read", "after"))
    orch.max_tool_rounds = 1
    called = []
    for cap in ("read", "after"):
        orch.tools.register(ToolSpec(cap, cap, handler=lambda _, c=cap: called.append(c) or c))
    assert orch.handle_message("c", "读取").status == "failed"
    assert called == ["read"]

def test_action_approval_is_exact_and_one_use():
    from whale_core.core.runtime import _action_fingerprint
    from whale_core.core.types import ActionIntent
    store = InMemoryStore()
    action = ActionIntent("plugin_action", {"args": {"value": 1}}, reason="runtime-task:t:0")
    store.put("task:t", {"status": "running", "decision": "approve",
                         "pending_fingerprint": _action_fingerprint(action), "approval_token": action.reason})
    verifier = runtime_approval_verifier(store)
    changed = ActionIntent("plugin_action", {"args": {"value": 2}}, reason=action.reason)
    assert not verifier(changed)
    assert verifier(action)
    assert not verifier(action)

def test_running_task_becomes_unknown_after_restart():
    store = InMemoryStore()
    store.put("tasks:recent", ["t"])
    store.put("task:t", {"status": "running", "task_id": "t"})
    make(store=store)
    assert store.get("task:t")["status"] == "unknown"

def test_model_task_id_collision_cannot_overwrite_existing(monkeypatch):
    orch = make()
    orch.store.put("task:existing", {"status": "answered", "goal": "original"})
    monkeypatch.setattr("whale_core.core.runtime.plan_from_model_text", lambda _: IntentPlan(
        goal="new", task_id="existing", capabilities=()))
    assert orch.handle_message("c", "做事").task_id != "existing"
    assert orch.store.get("task:existing")["goal"] == "original"

def test_hot_rebuild_shares_workers_and_does_not_interrupt_active_journal():
    store = InMemoryStore()
    orch = make(store=store)
    store.put("tasks:recent", ["t"])
    store.put("task:t", {"status": "running", "task_id": "t"})
    rebuilt = make(store=store)
    assert store.get("task:t")["status"] == "running"
    assert rebuilt._workers is orch._workers
    assert rebuilt._task_lock is orch._task_lock
    assert rebuilt._active_tasks is orch._active_tasks

def test_real_gateway_approval_uses_owner_journal_and_does_not_trust_model_flags():
    from whale_core.core.runtime import runtime_owner_verifier
    store, registry, calls = InMemoryStore(), PluginRegistry(), []
    class Executor:
        def execute(self, action, target, args):
            calls.append((action, target, args["message"]))
            return True, "sent"
    registry.register(PluginManifest(plugin_id="mail", name="mail", version="1.0.0", kind="output",
                      actions=("send",), action_risk={"send": "communicate"}), executor=Executor())
    gateway = PluginGateway(registry, PluginActionPolicy(registry),
                            owner_verifier=runtime_owner_verifier(store),
                            approval_verifier=runtime_approval_verifier(store))
    orch = make(store=store, capabilities=("mail",), gateway=gateway)
    orch.tools.register(ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="send"))
    task = orch.handle_message("c", "发送", source="api")
    assert task.status == "pending_approval" and calls == []
    orch.decide_task(task.task_id, "approve")
    assert orch.resume_task(task.task_id).status == "answered"
    assert calls == [("send", "owner", "hello")]


def test_manifest_risk_mismatch_does_not_run_readonly_handler():
    store, registry, calls = InMemoryStore(), PluginRegistry(), []
    registry.register(PluginManifest(plugin_id="mail", name="mail", version="1.0.0", kind="output",
                      actions=("send",), action_risk={"send": "communicate"}))
    gateway = PluginGateway(registry, PluginActionPolicy(registry))
    orch = make(store=store, capabilities=("mail",), gateway=gateway)
    orch.tools.register(ToolSpec("send", "mail", plugin_id="mail", action="send",
                                handler=lambda _: calls.append("bypass") or "sent"))
    result = orch.handle_message("c", "发送")
    assert result.status == "denied" and calls == []
    assert "manifest-risk-mismatch" in result.receipts[0]

def test_approved_task_cannot_silently_change_plugin_action_before_resume():
    class Gateway:
        def dispatch(self, intent):
            return "plugin-hold:mail:send"
    orch = make(capabilities=("mail",), gateway=Gateway())
    orch.tools.register(ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="send"))
    task = orch.handle_message("c", "发送")
    orch.decide_task(task.task_id, "approve")
    # Simulate a host plugin upgrade changing the tool's action after review.
    orch.tools._tools["send"] = ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="different")
    result = orch.resume_task(task.task_id)
    assert result.status == "denied"
    assert result.receipts[-1].endswith("approval-scope-changed")

def test_audio_input_is_transient_not_in_sqlite_backup_or_memory_export(tmp_path):
    import io
    from whale_core.memory.sqlite_store import SqliteMemoryStore
    from whale_core.memory.backup import create_backup
    from whale_core.memory.export import export_jsonl
    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    orch = make(store=store, capabilities=("asr.transcribe",))
    encoded = "U0VDUkVUX0FVRElPX0JZVEVTX01VU1RfTk9UX1BFUlNJU1Q="
    seen = []
    orch.tools.register(ToolSpec("asr", "asr.transcribe", handler=lambda args: seen.append(args["audio_base64"]) or "转写结果"))
    result = orch.execute_tool("asr", {"audio_base64": encoded, "format": "wav"})
    assert result.status == "answered" and seen == [encoded]
    journal = store.get(f"runtime:task:{result.task_id}")
    assert "audio_base64" not in journal["plan"]["slots"]
    assert result.task_id not in orch._transient_args
    snapshot = create_backup(store, tmp_path / "backup.sqlite")
    assert encoded.encode() not in snapshot.read_bytes()
    buf = io.StringIO(); export_jsonl(store, buf)
    assert encoded not in buf.getvalue()
    assert encoded not in str([store.get(key) for key in store.keys()])
    store.close()

def test_pending_media_cannot_be_replayed_when_transient_input_was_lost():
    class Gateway:
        def dispatch(self, intent): return "plugin-hold:mail:send"
    orch = make(gateway=Gateway())
    orch.tools.register(ToolSpec("send_audio", "audio.send", risk_class="communicate", plugin_id="mail", action="send"))
    task = orch.execute_tool("send_audio", {"audio_base64": "U0VDUkVU"})
    assert task.status == "pending_approval"
    orch.decide_task(task.task_id, "approve")
    orch._transient_args.clear()  # process-local media lost across restart
    result = orch.resume_task(task.task_id)
    assert result.status == "failed"
    assert "transient-input-unavailable" in result.receipts[-1]
