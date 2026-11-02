"""plugins/policy.py 按风险级免审配置（gap-analysis §4 R1）测试。"""

from __future__ import annotations

import pytest

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.policy import PluginActionPolicy
from whale_core.plugins.registry import PluginRegistry


def _registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(PluginManifest(
        plugin_id="chat", name="聊天", version="1.0.0",
        actions=("send_message", "read_inbox", "buy_thing"),
        action_risk={"send_message": "communicate", "read_inbox": "read_only",
                     "buy_thing": "transaction"},
    ))
    return registry


def test_default_policy_unchanged():
    """默认（不配置免审）：communicate 仍需逐笔审批（向后兼容）。"""
    policy = PluginActionPolicy(_registry())
    verdict = policy.evaluate("chat", "send_message", is_owner=True)
    assert verdict.status == "needs_approval"


def test_communicate_auto_approve_when_configured():
    policy = PluginActionPolicy(_registry(), auto_approve_risks=frozenset({"communicate"}))
    verdict = policy.evaluate("chat", "send_message", is_owner=True)
    assert verdict.status == "ready"
    assert "auto-approved" in verdict.reason


def test_auto_approve_still_requires_owner():
    policy = PluginActionPolicy(_registry(), auto_approve_risks=frozenset({"communicate"}))
    verdict = policy.evaluate("chat", "send_message", is_owner=False)
    assert verdict.status == "denied"  # 身份门不可放宽


def test_transaction_never_auto_approvable():
    with pytest.raises(ValueError):
        PluginActionPolicy(_registry(),
                           auto_approve_risks=frozenset({"transaction"}))


def test_transaction_still_manual_with_communicate_auto():
    registry = _registry()
    policy = PluginActionPolicy(
        registry, auto_approve_risks=frozenset({"communicate"}),
        max_transaction_amount=100.0, max_daily_transactions=5,
    )
    verdict = policy.evaluate("chat", "buy_thing", is_owner=True,
                              args={"amount": 10})
    assert verdict.status == "needs_approval"  # 交易仍逐笔人审


def test_invalid_risk_value_rejected():
    with pytest.raises(ValueError):
        PluginActionPolicy(_registry(), auto_approve_risks=frozenset({"yolo"}))


# ---------------------------------------------------------------------------
# issue #59：按动作免审（auto_approve_actions）
# ---------------------------------------------------------------------------


def test_action_level_auto_approve_scoped_to_pair():
    """只放开列出的 (plugin, action)，同插件其它 communicate 动作仍逐笔审批。"""
    policy = PluginActionPolicy(
        _registry(), auto_approve_actions=frozenset({"chat.send_message"}))
    assert policy.evaluate("chat", "send_message", is_owner=True).status == "ready"
    # 其它 communicate 动作不受影响
    registry = _registry()
    registry.register(PluginManifest(
        plugin_id="term", name="终端", version="1.0.0",
        actions=("run_command",), action_risk={"run_command": "communicate"}))
    policy2 = PluginActionPolicy(
        registry, auto_approve_actions=frozenset({"chat.send_message"}))
    assert policy2.evaluate("term", "run_command", is_owner=True).status == "needs_approval"


def test_action_auto_approve_still_requires_owner():
    policy = PluginActionPolicy(
        _registry(), auto_approve_actions=frozenset({"chat.send_message"}))
    assert policy.evaluate("chat", "send_message", is_owner=False).status == "denied"


def test_transaction_action_rejected_at_construction():
    with pytest.raises(ValueError):
        PluginActionPolicy(_registry(),
                           auto_approve_actions=frozenset({"chat.buy_thing"}))


def test_transaction_pair_unknown_plugin_defers_to_evaluate_guard():
    """构造时插件未注册无法判定风险；执行时 transaction 仍逐笔人审（兜底）。"""
    registry = _registry()
    policy = PluginActionPolicy(
        registry, auto_approve_actions=frozenset({"late.buy"}))
    registry.register(PluginManifest(
        plugin_id="late", name="后注册", version="1.0.0",
        actions=("buy",), action_risk={"buy": "transaction"}))
    assert policy.evaluate("late", "buy", is_owner=True,
                           args={"amount": 1}).status == "needs_approval"


def test_malformed_action_pair_rejected():
    with pytest.raises(ValueError):
        PluginActionPolicy(_registry(), auto_approve_actions=frozenset({"no-dot"}))
