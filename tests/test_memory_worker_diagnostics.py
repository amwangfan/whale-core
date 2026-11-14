from types import SimpleNamespace
from whale_core.api.routes.system import memory_worker_status
from whale_core.memory.owner_fact_worker import WorkerStats


def test_worker_diagnostics_are_allowlisted_counts():
    stats = WorkerStats(1, 2, 3, 4, 5, 6, 7, False, True)
    view = memory_worker_status(SimpleNamespace(owner_fact_worker=SimpleNamespace(stats=lambda: stats)))
    assert view["state"] == "ok" and view["queue_depth"] == 7 and view["model_busy"] is True


def test_diagnostics_handle_uninstalled_and_failed_workers_without_secrets():
    assert memory_worker_status(SimpleNamespace()) == {"state": "unavailable"}
    def broken(): raise RuntimeError("SECRET DATA")
    assert memory_worker_status(SimpleNamespace(owner_fact_worker=SimpleNamespace(stats=broken))) == {
        "state": "failed", "error": "RuntimeError"}
