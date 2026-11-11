"""Server-owned reviewed group shell approval through the real plugin gateway."""
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from test_shell_acl import _Brain, _shell_registry_and_gateway
from whale_core.core.companion import WhaleCompanion
from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime import ConversationOrchestrator, InputEnvelope, ToolSpec
from whale_core.memory.store import InMemoryStore


def setup(store=None, *, allowed=True, answer="OK", personal=True, gate=None):
    store = store or InMemoryStore()
    _, gateway, tools, calls = _shell_registry_and_gateway(store, personal=personal)
    review = SimpleNamespace(reply=lambda *a, **k: answer)
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()), tools=tools, gateway=gateway,
        intent_model=SimpleNamespace(model=review),
        task_auth_gate=lambda *a: allowed, grant_gate=gate)
    return orch, calls


def plan(**kw):
    return IntentPlan(goal="safe shell", auth_level="owner",
                      capabilities=("shell.safe",), slots={"command": "echo reviewed"}, **kw)


def test_reviewed_group_runs_once_without_sudo():
    orch, calls = setup()
    result = orch._start_plan(plan(), "group:2", "echo", source="qq")
    assert len(calls) == 1, result.receipts
    assert calls[0]["_whale_shell_grant"] is True
    assert not calls[0].get("_whale_is_owner")
    journal = orch.store.get(f"runtime:task:{result.task_id}")
    assert journal["review_approval"]["scope"] == "tool.shell.safe"
    assert journal["review_approval"]["task_id"] == result.task_id


@pytest.mark.parametrize("allowed,answer", [(False, "OK"), (True, "NO")])
def test_gate_and_review_declines_never_execute(allowed, answer):
    orch, calls = setup(allowed=allowed, answer=answer)
    orch._start_plan(plan(), "group:2", "echo", source="qq")
    assert calls == []


def test_forged_slots_do_not_approve():
    orch, calls = setup()
    forged = replace(plan(), auth_level="", slots={"command": "echo x", "review_approved": True})
    orch._start_plan(forged, "group:2", "echo", source="qq")
    assert calls == []


def test_review_does_not_expand_other_scopes():
    orch, calls = setup()
    task_id, saved = orch._persist_task(plan(), conversation_id="group:2", text="x",
                                       source="qq", review_approved=True)
    other = ToolSpec(name="fetch", capability="web.fetch", plugin_id="shell_sandbox",
                     action="run_command", risk_class="communicate")
    outcome, note = orch._dispatch_step(task_id, other, saved, {"command": "echo x"}, 0, 5)
    assert outcome == "denied" and "tool.web.fetch" in note
    assert calls == []


def test_real_grant_still_consumed_and_failure_not_bypassed():
    consumed = []
    orch, calls = setup(gate=lambda *a: consumed.append(a) or False)
    task_id, saved = orch._persist_task(plan(), conversation_id="group:2", text="x",
                                       source="qq", review_approved=True)
    journal = orch.store.get(f"runtime:task:{task_id}")
    journal["authz"].update(grant_ids=["real-grant"], grant_scopes=["tool.shell.safe"])
    orch.store.put(f"runtime:task:{task_id}", journal)
    result = orch._execute_task(task_id)
    assert len(consumed) == 1
    assert calls == [], result.receipts


def test_review_does_not_override_managed_plugin_policy():
    orch, calls = setup(personal=False)
    orch._start_plan(plan(), "group:2", "echo", source="qq")
    assert calls == []


def test_owner_direct_execution():
    orch, calls = setup(allowed=False, answer="NO")
    result = orch._start_plan(plan(), "local", "echo", source="cli", owner_id="owner")
    assert len(calls) == 1, result.receipts
    assert calls[0]["_whale_is_owner"] is True


def test_sqlite_restore_keeps_task_bound_review(tmp_path):
    from whale_core.memory.sqlite_store import SqliteMemoryStore
    store = SqliteMemoryStore(tmp_path / "tasks.sqlite3")
    orch, _ = setup(store)
    task_id, _ = orch._persist_task(plan(), conversation_id="group:2", text="x",
                                   source="qq", review_approved=True)
    store.db.close()
    restored = SqliteMemoryStore(tmp_path / "tasks.sqlite3")
    orch, calls = setup(restored)
    result = orch._execute_task(task_id)
    assert len(calls) == 1, result.receipts
    assert calls[0]["_whale_shell_grant"] is True
    restored.db.close()


def test_review_cannot_be_copied_to_another_task():
    orch, calls = setup()
    first, _ = orch._persist_task(plan(), conversation_id="group:2", text="x",
                                 source="qq", review_approved=True)
    second, saved = orch._persist_task(plan(), conversation_id="group:2", text="x",
                                      source="qq")
    original = orch.store.get(f"runtime:task:{first}")
    journal = orch.store.get(f"runtime:task:{second}")
    journal.update(authz=original["authz"], review_approval=original["review_approval"])
    orch.store.put(f"runtime:task:{second}", journal)
    outcome, _ = orch._dispatch_step(second, orch.tools.get("run_shell"), saved,
                                    saved.slots, 0, 5)
    assert outcome == "denied"
    assert calls == []


def test_unrelated_web_lease_does_not_block_reviewed_shell():
    orch, calls = setup(gate=lambda *a: pytest.fail("unrelated lease must not be consumed"))
    task_id, _ = orch._persist_task(plan(), conversation_id="group:2", text="x",
                                   source="qq", review_approved=True)
    journal = orch.store.get(f"runtime:task:{task_id}")
    journal["authz"].update(grant_ids=["web-only"], grant_scopes=["tool.web.fetch"])
    orch.store.put(f"runtime:task:{task_id}", journal)
    result = orch._execute_task(task_id)
    assert len(calls) == 1, result.receipts


def test_reviewed_group_with_real_resolved_context():
    from whale_core.life.context_resolver import (
        ConversationContextResolver, pairing_lookup_from_mapping)
    from whale_core.memory.bindings import BindingStore
    from whale_core.memory.spaces import SpaceStore

    orch, calls = setup()
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({}),
        bindings=BindingStore(orch.store, SpaceStore(orch.store)), owner_id="owner")
    context = resolver.resolve(channel="qq", sender_id="member", conversation_id="group:2")
    assert not context.principal.is_owner
    assert "tool.shell.safe" not in context.capability_scopes
    result = orch._start_plan(plan(), "group:2", "echo", source="qq", context=context)
    assert len(calls) == 1, result.receipts
    assert calls[0]["_whale_shell_grant"] is True
    assert not calls[0].get("_whale_is_owner")


@pytest.mark.parametrize("entry", ["async", "parallel"])
@pytest.mark.parametrize("case", ["approved", "unauthorized", "declined", "owner"])
def test_public_background_entry_review_authorization(entry, case):
    """Real planners, journals and gateway; only model replies/executor are stubs."""
    reviews = []
    orch, calls = setup(allowed=case != "unauthorized")

    class ReviewModel:
        def reply(self, *a, **kw):
            reviews.append(a)
            return "NO" if case == "declined" else "OK"

    class Planner:
        model = ReviewModel()

        def plan(self, *a, **kw):
            return json.dumps({"goal": "run safe shell", "kind": "task",
                               "auth_level": "owner", "capabilities": ["shell.safe"],
                               "slots": {"command": "echo reviewed"}})

    orch.intent_model = Planner()
    # Exercise the real async slow branch with a shell-only test tool catalog.
    orch.SLOW_CAPABILITY_PREFIXES = ("shell.",)
    env = InputEnvelope(conversation_id="group:2", text="执行安全命令",
                        source="cli" if case == "owner" else "qq", channel="qq",
                        owner_id="owner" if case == "owner" else "",
                        sender_id="owner" if case == "owner" else "member")
    done = threading.Event()
    outputs = []
    if entry == "async":
        def completed(result):
            outputs.append(result)
            done.set()
        first = orch.handle_envelope_async(env, on_complete=completed)
        if case in {"approved", "owner"}:
            assert first.status == "ack"
            assert done.wait(5), "async worker did not complete"
        else:
            assert first.status != "ack"
    else:
        def settled(route, result):
            outputs.append(result)
            done.set()
        orch.handle_envelope_parallel(env, on_followup=lambda result: None,
                                      on_settled=settled)
        assert done.wait(5), "parallel worker did not settle"
    if case in {"approved", "owner"}:
        assert len(calls) == 1, outputs
        assert len(reviews) == (0 if case == "owner" else 1)
        assert bool(calls[0].get("_whale_is_owner")) == (case == "owner")
        assert bool(calls[0].get("_whale_shell_grant")) == (case == "approved")
    else:
        assert calls == []
        assert orch.store.get("tasks:recent") in (None, [])
        assert len(reviews) == (1 if case == "declined" else 0)
