"""shell_tool Adapter 结果分类回归（假失败修复）。

真实案例：curl 计时探针拿到 stdout（TCP 39ms）但退出码非零，
旧分类 ``result.status == 'success'`` 把它判成失败。约定：
- success/failed 且 stdout 非空 → ok，回执保留 status/exit 告知 partial/nonzero；
- 空 stdout 的 failed、stderr-only → 不 ok；
- timeout / output_limit / denied / error → 永不伪称完成，残留 stdout 如实保留。
"""
from __future__ import annotations

from whale_core.cloud.shell_executor import ExecutionResult
from whale_core.plugins import shell_tool


class _Registry:
    def __init__(self) -> None:
        self.executor = None

    def register(self, manifest, executor):
        self.executor = executor


class _Tools:
    def register(self, spec):
        pass


def _adapter(monkeypatch, tmp_path, results):
    queue = list(results)

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, command, **kwargs):
            return queue.pop(0)

    monkeypatch.setattr(shell_tool, "ShellExecutor", FakeExecutor)
    registry = _Registry()
    shell_tool.register_shell(registry, _Tools(), tmp_path)
    return registry.executor


def _result(status, exit_code, stdout="", stderr="", reason=""):
    return ExecutionResult(status=status, exit_code=exit_code, stdout=stdout,
                           stderr=stderr, duration_s=0.1, verdict_reason=reason)


def test_success_exit0_ok(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [_result("success", 0, stdout="hi\n")])
    ok, receipt = adapter.execute("run_command", None, {"command": "echo hi"})
    assert ok is True
    assert "hi" in receipt
    assert "status=success" in receipt and "exit=0" in receipt


def test_failed_nonzero_with_stdout_counts_as_ok(monkeypatch, tmp_path):
    # curl 计时：拿到 stdout 但退出码非零 → 有输出即算拿到结果
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("failed", 7, stdout="time_connect: 0.039\n", stderr="curl: (7) failed")])
    ok, receipt = adapter.execute("run_command", None, {"command": "curl ..."})
    assert ok is True
    assert "0.039" in receipt
    assert "status=failed" in receipt and "exit=7" in receipt


def test_failed_empty_stdout_not_ok(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("failed", 1, stdout="", stderr="grep: no match")])
    ok, receipt = adapter.execute("run_command", None, {"command": "grep x f"})
    assert ok is False
    assert "grep: no match" in receipt
    assert "status=failed" in receipt and "exit=1" in receipt


def test_failed_whitespace_only_stdout_not_ok(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("failed", 2, stdout="  \n", stderr="usage: ...")])
    ok, receipt = adapter.execute("run_command", None, {"command": "bad"})
    assert ok is False
    assert "status=failed" in receipt


def test_timeout_with_partial_stdout_not_ok_but_kept(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("timeout", None, stdout="partial chunk", reason="killed after 30s")])
    ok, receipt = adapter.execute("run_command", None, {"command": "slow"})
    assert ok is False
    assert "status=timeout" in receipt
    assert "partial chunk" in receipt
    assert "killed after 30s" in receipt


def test_output_limit_with_stdout_not_ok_but_kept(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("output_limit", None, stdout="flood", reason="killed after output byte limit")])
    ok, receipt = adapter.execute("run_command", None, {"command": "yes"})
    assert ok is False
    assert "status=output_limit" in receipt and "flood" in receipt


def test_denied_never_ok(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("denied", None, stdout="", reason="tier denied")])
    ok, receipt = adapter.execute("run_command", None, {"command": "rm -rf /"})
    assert ok is False
    assert "status=denied" in receipt and "tier denied" in receipt


def test_error_never_ok(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("error", None, stderr="sandbox unavailable", reason="bwrap missing")])
    ok, receipt = adapter.execute("run_command", None, {"command": "ls"})
    assert ok is False
    assert "status=error" in receipt and "sandbox unavailable" in receipt


def test_receipt_truncated_to_4000(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [
        _result("success", 0, stdout="x" * 10000)])
    ok, receipt = adapter.execute("run_command", None, {"command": "big"})
    assert ok is True
    assert len(receipt) <= 4000


def test_missing_command_rejected(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, [])
    assert adapter.execute("run_command", None, {}) == (False, "command required")
    assert adapter.execute("run_command", None, {"command": "   "}) == (False, "command required")


def test_partial_result_survives_real_plugin_gateway(monkeypatch, tmp_path):
    from whale_core.core.runtime import ToolRegistry
    from whale_core.plugins.gateway import PluginGateway
    from whale_core.plugins.intents import PluginAction
    from whale_core.plugins.policy import PluginActionPolicy
    from whale_core.plugins.registry import PluginRegistry

    class Executor:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return _result("failed", 7, "TCP 39ms\n" + "x" * 300, "curl failed")

    monkeypatch.setattr(shell_tool, "ShellExecutor", Executor)
    registry, tools = PluginRegistry(), ToolRegistry()
    shell_tool.register_shell(registry, tools, tmp_path)
    gateway = PluginGateway(registry, PluginActionPolicy(
        registry, auto_approve_actions=frozenset({shell_tool.SHELL_AUTO_PAIR})),
        owner_verifier=lambda _: True)
    receipt = gateway.dispatch(PluginAction(plugin_id="shell_sandbox", action="run_command",
                                           args={"command": "curl probe"}).to_intent())
    assert receipt.startswith("plugin:shell_sandbox:run_command:ok:")
    assert "status=failed exit=7" in receipt
    assert "TCP 39ms" in receipt
