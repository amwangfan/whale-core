from datetime import datetime, timezone, time
from whale_core.life.initiative import ProactiveOutbox, ProactiveDraft, ProactivePrefs
from whale_core.memory.store import InMemoryStore

def test_shared_global_pause_blocks_separate_runner_but_keeps_explicit_passive_reply():
    store = InMemoryStore()
    ui_outbox, runner_outbox = ProactiveOutbox(store), ProactiveOutbox(store)
    now = datetime(2026, 10, 30, 4, tzinfo=timezone.utc)
    ui_outbox.propose(ProactiveDraft("d", "message", "reason", ("source",)))
    store.put("workflows:settings", {"paused": True})
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"hud"}),
                           auto_send=True, quiet_start=time(0), quiet_end=time(0))
    assert runner_outbox.inspect("d", prefs, now=now).status == "paused"
    assert runner_outbox.inspect("d", prefs, now=now, passive_reply=True).status == "ready"
    store.put("workflows:settings", {"paused": False})
    assert runner_outbox.inspect("d", prefs, now=now).status == "ready"
