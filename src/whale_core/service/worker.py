"""Opt-in scheduled exploration; never dispatches outbound messages.

A new process or newly enabled schedule waits one full configured interval.
The worker uses the same exploration budget, quiet hours and backoff as manual
requests. stop() is cooperative; an in-flight provider request may finish, but
no later exploration tool step starts after cancellation is observed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from whale_core.service.workflows import WorkflowsService


class WorkflowsWorker:
    def __init__(self, service: WorkflowsService | None = None):
        self._service = service
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {"status": "stopped" if service else "not_configured",
                       "last_run_at": None, "last_error": None, "last_result": None,
                       "next_run_at": None, "consecutive_errors": 0}
        self._signature = None
        self._due = None

    def set_service(self, service: WorkflowsService | None) -> None:
        with self._lock:
            self._service = service
            self._signature = None
            self._due = None
            if service is None:
                self._state["status"] = "not_configured"

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "configured": self._service is not None,
                    "alive": self._thread is not None and self._thread.is_alive()}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._signature = None
            self._due = None
            self._thread = threading.Thread(target=self._run, name="whale-exploration", daemon=True)
            self._thread.start()

    def stop(self) -> bool:
        """Return whether the worker stopped; False means a request is in flight."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        stopped = thread is None or not thread.is_alive()
        with self._lock:
            self._state["status"] = ("stopped" if self._service is not None else "not_configured") if stopped else "stopping"
        return stopped

    def _set_due(self, now: float, interval: float) -> None:
        self._due = now + interval
        self._state["next_run_at"] = datetime.fromtimestamp(
            time.time() + interval, timezone.utc).isoformat()

    def _tick(self, now: float | None = None) -> None:
        """One scheduling tick, separated for deterministic clock-based tests."""
        simulated_clock = now is not None
        now = time.monotonic() if now is None else now
        with self._lock:
            service = self._service
        if service is None:
            with self._lock:
                self._state.update(status="not_configured", next_run_at=None)
            return
        settings = service.get_settings()
        enabled = bool(settings.get("schedule_enabled")) and not settings.get("paused")
        interval = max(60, int(settings.get("interval_s", 3600)))
        signature = (id(service), enabled, interval)
        with self._lock:
            if signature != self._signature:
                self._signature = signature
                self._set_due(now, interval)
            if not enabled:
                self._state.update(status="paused" if settings.get("paused") else "disabled", next_run_at=None)
                return
            if self._due is None or now < self._due:
                self._state["status"] = "waiting"
                return
            self._state.update(status="running", last_run_at=datetime.now(timezone.utc).isoformat())
        if self._stop.is_set():
            return
        try:
            result = service.explore(topics=None, stop_event=self._stop)
            with self._lock:
                self._state.update(last_result=str(result.get("status", "unknown"))[:40],
                                   last_error=None, consecutive_errors=0, status="waiting")
                self._set_due(now if simulated_clock else time.monotonic(), interval)
                self._persist(service)
        except Exception as exc:
            # Preserve only the class, never provider errors or private content.
            with self._lock:
                errors = min(6, self._state["consecutive_errors"] + 1)
                self._state.update(last_error=type(exc).__name__[:60],
                                   consecutive_errors=errors, status="backoff")
                self._set_due(now if simulated_clock else time.monotonic(), min(86400, interval * (2 ** errors)))
                self._persist(service)

    def _persist(self, service) -> None:
        store = getattr(service, "store", None)
        if store is not None:
            store.put("workflows:worker:last", {key: self._state[key] for key in
                ("last_run_at", "last_error", "last_result", "consecutive_errors")})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self._state.update(status="backoff", last_error=type(exc).__name__[:60])
                if self._stop.wait(60):
                    break
            self._stop.wait(1)
        with self._lock:
            self._state["status"] = "stopped" if self._service is not None else "not_configured"
