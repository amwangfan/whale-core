"""Request-count budgets, not made-up currency/token prices.

Counters count admitted provider calls; a failed request still spends its slot.
Single host lock covers CLI/API threads. Actual vendor token billing is external.
"""

from datetime import datetime
from threading import RLock
from zoneinfo import ZoneInfo
from whale_core.core.deepseek import ModelError

_LOCK = RLock()
DEFAULTS = {"model_calls": 200, "media_jobs": 50}


def settings(store):
    return {**DEFAULTS, **(store.get("settings:budget") or {})}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("expected model_calls and media_jobs")
    if any(
        isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 100000
        for n in value.values()
    ):
        raise ValueError("budget values must be 0..100000 integers")
    return value


def reserve(store, capability):
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    key = f"budget:{day}:{capability}"
    with _LOCK:
        used = int(store.get(key) or 0)
        if used >= settings(store)[capability]:
            raise ModelError("budget", "daily request budget reached")
        store.put(key, used + 1)
