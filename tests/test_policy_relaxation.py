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
