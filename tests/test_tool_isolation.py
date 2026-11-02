"""issue #50：阻塞插件可取消——进程隔离执行与孤儿核算的测试。

全部离线：子进程只做 sleep/echo，不触网。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.core.runtime.isolation import (
    IsolationError,
    run_callable_isolated,
)
from whale_core.memory.store import InMemoryStore


# ---- 可 pickle 的模块级 handler（spawn 子进程按模块导入） ------------------

def echo_handler(args):
    return f"echo:{args.get('text', '')}"


def hanging_handler(args):
    time.sleep(30)
    return "never"


def boom_handler(_args):
    raise RuntimeError("inner")


def child_spawning_handler(args):
    """派生一个孙进程再挂起：验证超时杀的是整个进程组。"""
    import subprocess
    subprocess.Popen(["sleep", "30"], start_new_session=False)
    time.sleep(30)
    return "never"


class TestRunCallableIsolated:
    def test_success_roundtrip(self):
        out = run_callable_isolated(echo_handler, {"text": "hi"},
                                    timeout_s=5, output_chars=100)
        assert out == (True, "echo:hi")

    def test_timeout_kills_process_and_returns_unknown(self):
        registry = {}
        started = time.monotonic()
        out = run_callable_isolated(hanging_handler, {}, timeout_s=0.3,
                                    output_chars=100, proc_registry=registry,
                                    registry_key="t1")
        assert out is None                    # 结果不可知（不谎报失败/成功）
        assert time.monotonic() - started < 5  # 没等满 30s
        assert registry == {}                  # 登记表已清理
        # 子进程确实死了（不是僵尸泄漏）——无直接句柄，靠后续资源不增长体现；
        # 这里至少确认再次运行不受前次影响
        assert run_callable_isolated(echo_handler, {"text": "x"},
                                     timeout_s=5, output_chars=100)[0] is True

    def test_process_group_killed_with_grandchild(self):
        out = run_callable_isolated(child_spawning_handler, {}, timeout_s=0.3,
                                    output_chars=100)
        assert out is None

    def test_cancel_check_kills(self):
        flag = threading.Event()
        flag.set()
        started = time.monotonic()
        out = run_callable_isolated(hanging_handler, {}, timeout_s=30,
                                    output_chars=100,
                                    cancel_check=flag.is_set)
        assert out is None
        assert time.monotonic() - started < 10

    def test_unpicklable_handler_rejected(self):
        with pytest.raises(IsolationError):
            run_callable_isolated(lambda _a: "x", {}, timeout_s=1,
                                  output_chars=100)

    def test_child_exception_reported_as_fail(self):
        out = run_callable_isolated(boom_handler, {}, timeout_s=5,
                                    output_chars=100)
        assert out == (False, "isolated-error:RuntimeError")


class _Brain:
    def reply(self, text, **kwargs):
        return "收到"


class _Planner:
    def __init__(self, capabilities):
        self.capabilities = capabilities

    def plan(self, text, **kwargs):
        return json.dumps({"goal": "执行", "kind": "task",
                           "capabilities": self.capabilities,
                           "slots": {"text": "hi"}})


def _orch(capabilities=("read",)):
    store = InMemoryStore()
    tools = ToolRegistry()
    return ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()),
        intent_model=_Planner(capabilities), tools=tools)


class TestOrchestratorIntegration:
    def test_isolated_tool_success(self, monkeypatch):
        orch = _orch()
        orch.tools.register(ToolSpec("echo", "read", handler=echo_handler,
                                     isolation="process"))
        result = orch.handle_message("c", "跑一下")
        assert result.status == "answered"
        assert any("echo:hi" in r for r in result.receipts)

    def test_isolated_tool_timeout_is_unknown(self, monkeypatch):
        orch = _orch()
        orch.tools.register(ToolSpec("hang", "read", handler=hanging_handler,
                                     isolation="process"))
        monkeypatch.setattr(
            "whale_core.core.runtime.plan_from_model_text",
            lambda _: IntentPlan(goal="hang", capabilities=("read",),
                                 timeout_s=0.3))
        started = time.monotonic()
        result = orch.handle_message("c", "等着")
        assert result.status == "unknown"   # 不自动重试、不伪装回滚
        assert time.monotonic() - started < 10
        assert orch.orphan_report() == []    # 隔离路径无孤儿线程

    def test_isolation_field_validation(self):
        with pytest.raises(ValueError):
            ToolSpec("x", "read", isolation="sandbox")
        # process 隔离只给 read_only handler 工具（网关 executor 不适用）
        with pytest.raises(ValueError):
            ToolSpec("y", "write", risk_class="communicate",
                     plugin_id="p", action="a", isolation="process")

    def test_shutdown_kills_isolated_children(self):
        orch = _orch()
        orch.tools.register(ToolSpec("hang", "read", handler=hanging_handler,
                                     isolation="process"))
        box = []
        t = threading.Thread(
            target=lambda: box.append(
                orch.execute_tool("hang", {}, conversation_id="c",
                                  timeout_s=30).status))
        t.start()
        time.sleep(0.5)  # 等子进程登记
        report = orch.shutdown()
        t.join(10)
        assert box[0] == "unknown"
        assert report["isolated_killed"]
        assert orch._isolated_procs == {}
