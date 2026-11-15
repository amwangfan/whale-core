from threading import Event, Thread
from types import SimpleNamespace

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.service.worker import WorkflowsWorker


class Service:
    def __init__(self):
        self.store = InMemoryStore()
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self.cancel = None
        self.fail = False

    def get_settings(self):
        return {"schedule_enabled": True, "paused": False, "interval_s": 60}

    def explore(self, *, topics=None, stop_event=None):
        self.calls += 1
        self.cancel = stop_event
        self.entered.set()
        assert self.release.wait(5)
        if self.fail:
            raise ValueError("private provider response")
        return {"status": "old-completed"}


@pytest.mark.parametrize("fail", [False, True])
def test_service_swap_cancels_old_generation_and_rejects_late_stats(fail):
    old, new = Service(), Service()
    old.fail = fail
    worker = WorkflowsWorker(old)
    worker._tick(0)
    thread = Thread(target=lambda: worker._tick(60))
    thread.start()
    try:
        assert old.entered.wait(3)
        assert worker.status()["in_flight"] is True
        worker.set_service(new)
        assert old.cancel.is_set()
        assert worker.status()["last_run_at"] is None
        assert worker.status()["next_run_at"] is None
    finally:
        old.release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert old.store.get("workflows:worker:last") is None
    assert worker.status()["last_result"] is None
    assert worker.status()["last_error"] is None
    worker._tick(100)
    worker._tick(159)
    assert new.calls == 0
    new.release.set()
    worker._tick(160)
    assert new.calls == 1
    assert new.store.get("workflows:worker:last")["last_result"] == "old-completed"


def test_simultaneous_ticks_do_not_run_duplicate_explorations():
    service = Service()
    worker = WorkflowsWorker(service)
    worker._tick(0)
    thread = Thread(target=lambda: worker._tick(60))
    thread.start()
    try:
        assert service.entered.wait(3)
        worker._tick(60)
        assert service.calls == 1
    finally:
        service.release.set()
        thread.join(3)


def test_stop_fences_late_completion_and_status_then_restart_gets_fresh_cancel_signal():
    service = Service()
    worker = WorkflowsWorker(service)
    worker._tick(0)
    thread = Thread(target=lambda: worker._tick(60))
    thread.start()
    try:
        assert service.entered.wait(3)
        assert worker.stop() is False
        assert service.cancel.is_set()
        assert worker.status()["next_run_at"] is None
    finally:
        service.release.set()
        thread.join(3)
    assert worker.status()["status"] == "stopped"
    assert service.store.get("workflows:worker:last") is None
    worker.start()
    try:
        assert not worker._cancel.is_set()
        assert worker.status()["in_flight"] is False
    finally:
        assert worker.stop()


def test_diagnostic_write_failure_is_not_reclassified_as_exploration_failure():
    service = Service()
    service.release.set()
    writes = []
    def fail(*args):
        writes.append(args[0])
        raise OSError("private store failure")
    service.store.put = fail
    worker = WorkflowsWorker(service)
    worker._tick(0)
    worker._tick(60)
    status = worker.status()
    assert writes == ["workflows:worker:last"]
    assert status["last_error"] is None
    assert status["last_persist_error"] == "OSError"
    assert status["consecutive_errors"] == 0
    assert status["status"] == "waiting"
    assert "private" not in str(status)


def test_late_settings_failure_does_not_poison_replacement_service():
    old, replacement = Service(), Service()
    worker = WorkflowsWorker(old)
    observed = Event()
    def fail_after_swap():
        worker.set_service(replacement)
        raise ValueError("private settings failure")
    old.get_settings = fail_after_swap
    settings = replacement.get_settings
    def read_new_settings():
        observed.set()
        return settings()
    replacement.get_settings = read_new_settings
    worker.start()
    try:
        assert observed.wait(3)
        assert worker.status()["last_error"] is None
        assert "private" not in str(worker.status())
    finally:
        assert worker.stop()


def test_cancelled_service_exploration_never_touches_store():
    from whale_core.service.workflows import WorkflowsService
    class ClosedStore:
        def get(self, key):
            pytest.fail("cancelled exploration touched old store")
    service = WorkflowsService(SimpleNamespace(store=ClosedStore()))
    stop = Event()
    stop.set()
    assert service.explore(stop_event=stop) == {"status": "stopped", "drafts": [], "tasks": []}


def test_cancel_after_tool_completion_skips_old_store_reads_and_draft_creation():
    from whale_core.core.companion import WhaleCompanion
    from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
    from whale_core.service.workflows import WorkflowsService
    stop = Event()
    class GuardedStore(InMemoryStore):
        def get(self, key):
            assert not stop.is_set(), "read after cancellation"
            return super().get(key)
        def put(self, key, value):
            assert not stop.is_set(), "write after cancellation"
            return super().put(key, value)
    store = GuardedStore()
    runtime = ConversationOrchestrator(store, WhaleCompanion(store, SimpleNamespace(reply=lambda *a, **k: "x")), tools=ToolRegistry())
    runtime.tools.register(ToolSpec("search", "web.search", handler=lambda a: ""))
    runtime.tools.register(ToolSpec("fetch", "web.fetch", handler=lambda a: ""))
    service = WorkflowsService(runtime)
    service.update_settings({"quiet_start": "00:00", "quiet_end": "00:00"})
    def execute(*args, **kwargs):
        stop.set()
        return SimpleNamespace(task_id="finished-task", status="answered")
    runtime.execute_tool = execute
    result = service.explore(["topic"], stop_event=stop)
    assert result == {"status": "stopped", "drafts": [], "tasks": ["finished-task"]}
