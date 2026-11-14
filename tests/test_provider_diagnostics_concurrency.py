"""Diagnostics must not serialize model I/O or lose overlapping completions."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json

import pytest

from whale_core.core.diagnostics import InstrumentedProvider, provider_health_report
from whale_core.memory.store import InMemoryStore
from whale_core.memory.sqlite_store import SqliteMemoryStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_overlapping_wrappers_preserve_all_completions(backend, tmp_path):
    count = 12
    stores = ([InMemoryStore()] * count if backend == "memory" else
              [SqliteMemoryStore(tmp_path / "stats.db") for _ in range(count)])
    barrier = Barrier(count, timeout=10)

    class Model:
        def __init__(self, fail):
            self.fail = fail

        def reply(self, text, **kwargs):
            # A transaction around model I/O deadlocks this barrier.
            barrier.wait()
            if self.fail:
                raise ValueError("private model message")
            return "result"

    def call(i):
        provider = InstrumentedProvider("shared", Model(i % 2), stores[i])
        try:
            return provider.reply("hello")
        except ValueError:
            return "failed"

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(call, range(count)))
        assert results.count("result") == count // 2
        report = provider_health_report(stores[0])
        assert report["totals"] == {"calls": count, "errors": count // 2}
        view = report["providers"][0]
        assert view["chars_in"] == count * 5
        assert view["chars_out"] == count // 2 * 6
        assert view["latency"]["samples"] == count
        assert "private model message" not in json.dumps(report)
    finally:
        if backend == "sqlite":
            for store in stores:
                store.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_report_pages_only_provider_keys(backend, tmp_path, monkeypatch):
    store = InMemoryStore() if backend == "memory" else SqliteMemoryStore(tmp_path / "scan.db")
    try:
        for i in range(1005):
            store.put(f"provider:health:{i:04}", {"calls": 1})
        store.put("unrelated:secret", "must not read")
        monkeypatch.setattr(store, "keys", lambda: pytest.fail("full store scan"))
        report = provider_health_report(store)
        assert len(report["providers"]) == 1005
        assert report["totals"] == {"calls": 1005, "errors": 0}
        assert report["providers"][-1]["name"] == "1004"
    finally:
        if backend == "sqlite":
            store.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_latency_cannot_break_json_or_percentiles(value):
    store = InMemoryStore()
    store.put("provider:health:bad", {"calls": 1, "total_latency_ms": value})
    assert provider_health_report(store)["totals"]["calls"] == 0
    store.put("provider:health:bad", {
        "calls": 1, "latency_samples": [value, 5.0],
        "error_events": [[value, "invalid"], [0, "old"]],
    })
    report = provider_health_report(store)
    assert report["providers"][0]["latency"] == {"p50_ms": 5, "p95_ms": 5, "samples": 1}
    json.dumps(report, allow_nan=False)


def test_legacy_report_backend_without_prefix_scan():
    class Legacy:
        def keys(self):
            return ["other", "provider:health:old"]

        def get(self, key):
            return {"calls": 2}

    assert provider_health_report(Legacy())["totals"]["calls"] == 2
