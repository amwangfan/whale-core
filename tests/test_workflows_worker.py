from whale_core.service.worker import WorkflowsWorker

class Service:
    def __init__(self):
        self.settings = {"schedule_enabled": False, "paused": False, "interval_s": 60}
        self.calls = []
        self.fail = False
    def get_settings(self): return self.settings
    def explore(self, *, topics=None, stop_event=None):
        self.calls.append(topics)
        if self.fail: raise OSError("private provider message and key must not escape")
        return {"status": "completed"}

def test_disabled_worker_does_no_network_and_enabled_waits_full_interval():
    service = Service()
    worker = WorkflowsWorker(service)
    worker._tick(0); worker._tick(1000)
    assert service.calls == [] and worker.status()["status"] == "disabled"
    service.settings["schedule_enabled"] = True
    worker._tick(1001)
    assert service.calls == []
    worker._tick(1060)
    assert service.calls == []
    worker._tick(1061)
    assert service.calls == [None]
    assert worker.status()["last_result"] == "completed"

def test_worker_pause_reenable_and_service_swap_reset_interval():
    service = Service(); service.settings["schedule_enabled"] = True
    worker = WorkflowsWorker(service)
    worker._tick(0)
    service.settings["paused"] = True
    worker._tick(100)
    assert service.calls == []
    service.settings["paused"] = False
    worker._tick(101); worker._tick(160)
    assert service.calls == []
    worker._tick(161)
    assert len(service.calls) == 1
    replacement = Service(); replacement.settings["schedule_enabled"] = True
    worker.set_service(replacement)
    worker._tick(200)
    assert replacement.calls == []
    worker._tick(260)
    assert replacement.calls == [None]

def test_worker_failure_backoff_sanitizes_error():
    service = Service(); service.settings["schedule_enabled"] = True; service.fail = True
    worker = WorkflowsWorker(service)
    worker._tick(0); worker._tick(60)
    assert worker.status()["last_error"] == "OSError"
    worker._tick(179)
    assert len(service.calls) == 1
    worker._tick(180)
    assert len(service.calls) == 2
    assert "private" not in str(worker.status())

def test_unconfigured_start_and_stop():
    worker = WorkflowsWorker()
    worker.start()
    assert worker.stop()
    assert worker.status()["status"] == "not_configured"
