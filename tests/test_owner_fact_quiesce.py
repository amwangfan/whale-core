import threading

import pytest

from whale_core.memory.owner_facts import OwnerFactService, _k_job, _PENDING_Q_KEY
from test_owner_fact_worker import (store, FakeAuthority, NOW, make_auth, make_evidence,
    make_worker, ScriptModel, candidates_json)


def test_quiesce_requeues_without_retry_and_late_model_never_touches_store(store):
    entered, release = threading.Event(), threading.Event()
    late_io = []
    class Guard:
        closed = False
        def __getattr__(self, name):
            target = getattr(store, name)
            def call(*args, **kwargs):
                if self.closed:
                    late_io.append(name)
                    raise AssertionError("store used after quiescence")
                return target(*args, **kwargs)
            return call
    guard = Guard()
    service = OwnerFactService(guard, FakeAuthority(), now=lambda: NOW)
    service.observe(make_evidence("我养的猫叫年糕", "quiesce"), make_auth())
    job_id = store.get(_PENDING_Q_KEY)[0]
    class Model:
        def complete(self, prompt, **kwargs):
            entered.set()
            assert release.wait(5)
            return candidates_json("我养的猫叫年糕")
    old = make_worker(guard, service, Model(), model_timeout=4)
    old.start()
    try:
        assert entered.wait(2)
        assert old.quiesce(timeout=0.1) is False
        pending = store.get(_k_job(job_id))
        assert pending["status"] == "pending" and pending.get("attempts", 0) == 0
        assert not pending["lease_token"]
        guard.closed = True  # host may now close/reload its original store
        old.quiesce(timeout=0)  # idempotent even after the original store is unavailable
        recovered_service = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        recovered = make_worker(store, recovered_service,
            ScriptModel([candidates_json("我养的猫叫年糕")]))
        recovered.start()
        try:
            assert recovered.drain(3)
        finally:
            recovered.stop(timeout=1)
        release.set()
        old.stop(timeout=2)
        assert store.get(_k_job(job_id))["status"] == "completed"
        assert not late_io
        assert old._counters["completed"] == 0
    finally:
        release.set()
        old.stop(timeout=2)


@pytest.mark.parametrize("lease", ["", "broken", "2026-11-12T08:00:00", ["invalid"]])
def test_corrupt_lease_can_be_recovered_without_poisoning_domain(store, lease):
    service = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
    service.observe(make_evidence("我养的猫叫年糕", "bad-lease"), make_auth())
    job_id = store.get(_PENDING_Q_KEY)[0]
    job = store.get(_k_job(job_id))
    job.update(status="leased", lease_owner="dead-host", lease_expires_at=lease)
    store.put(_k_job(job_id), job)
    worker = make_worker(store, service, ScriptModel([candidates_json("我养的猫叫年糕")]))
    worker.start()
    try:
        assert worker.drain(3)
        assert store.get(_k_job(job_id))["status"] == "completed"
    finally:
        worker.stop(timeout=1)


def test_stopped_worker_cannot_restart(store):
    service = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
    worker = make_worker(store, service, ScriptModel([]))
    worker.stop()
    with pytest.raises(RuntimeError):
        worker.start()


def test_concurrent_start_creates_only_one_worker(store):
    service = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
    worker = make_worker(store, service, ScriptModel([]))
    barrier = threading.Barrier(2)
    outcomes = []
    def start():
        barrier.wait()
        try:
            worker.start()
            outcomes.append("started")
        except RuntimeError:
            outcomes.append("rejected")
    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(2)
    worker.stop(timeout=1)
    assert sorted(outcomes) == ["rejected", "started"]
