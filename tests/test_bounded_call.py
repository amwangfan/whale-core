"""2026-11-12：BoundedCall 有界调用测试。

覆盖：超时快速返回 default；worker 忙时后续调用立即短路（不并发堆积、
不真正调用）；慢调用完成后恢复可用；fn 异常返回 default 且正确释放。
"""

from __future__ import annotations

import threading
import time

from whale_core.life.bounded_call import BoundedCall


def _wait_idle(bc, timeout=5.0):
    deadline = time.monotonic() + timeout
    while bc.busy and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not bc.busy, "worker 未在预期内释放"


def test_slow_call_times_out_fast_with_default():
    gate = threading.Event()
    bc = BoundedCall(lambda p: gate.wait(5) or "late", default_timeout_s=0.5)
    t0 = time.monotonic()
    out = bc("x", default="fallback")
    dt = time.monotonic() - t0
    assert out == "fallback"
    assert dt < 1.5  # 不等慢调用收尾（旧 with 用法会堵到 5s）
    gate.set()
    _wait_idle(bc)


def test_busy_short_circuits_without_piling_up():
    gate = threading.Event()
    calls = []

    def fn(p):
        calls.append(p)
        gate.wait(5)
        return "done"
    bc = BoundedCall(fn, default_timeout_s=0.3)
    assert bc("first", default="d1") == "d1"      # 超时
    assert bc.busy                                 # 慢调用仍在后台
    t0 = time.monotonic()
    assert bc("second", default="d2") == "d2"      # busy → 立即短路
    assert time.monotonic() - t0 < 0.5
    gate.set()
    _wait_idle(bc)
    assert calls == ["first"]                      # 第二次未真正进 worker


def test_recovers_after_slow_call_finishes():
    gate = threading.Event()
    state = {"slow": True}

    def fn(p):
        if state["slow"]:
            gate.wait(5)
            return "slow-done"
        return "fast:" + p
    bc = BoundedCall(fn, default_timeout_s=0.3)
    assert bc("a", default="d") == "d"
    gate.set()
    _wait_idle(bc)
    state["slow"] = False
    assert bc("b") == "fast:b"                     # 完成后恢复正常调用


def test_exception_returns_default_and_releases():
    calls = []

    def boom(p):
        calls.append(p)
        raise RuntimeError("down")
    bc = BoundedCall(boom)
    assert bc("x", default="safe") == "safe"
    _wait_idle(bc)                                 # 异常也释放 busy
    assert bc("y", default="safe2") == "safe2"     # 后续调用仍进 worker
    _wait_idle(bc)
    assert calls == ["x", "y"]


def test_normal_call_passes_through():
    bc = BoundedCall(lambda p: "echo:" + p)
    assert bc("hi") == "echo:hi"
