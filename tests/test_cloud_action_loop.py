"""云端行动闭环 stub：入站 → 选行动 → 推送卡片 / shell 白名单（mock HTTP）。"""

from __future__ import annotations

import os

from whale_core.actions.gateway import (
    CompositeActionGateway,
    HttpDashboardGateway,
    MockHttpActionGateway,
)
from whale_core.actions.intents import ShellWhitelist, ShowCard
from whale_core.cloud import CloudActionLoop, InboundEvent, RuleActionSelector


def test_inbound_message_push_card_via_dashboard_stub() -> None:
    gateway = CompositeActionGateway(dashboard=HttpDashboardGateway())
    loop = CloudActionLoop(gateway=gateway)
    receipts = loop.handle(
        InboundEvent(kind="message", text="请推送一张卡片：下午开会", source="qq")
    )
    assert any(i.kind == "show_card" for i in loop.last_intents)
    assert any(r.startswith("stub-dashboard:") for r in receipts)
    assert "show_card" in receipts[0] or any("show_card" in r for r in receipts)


def test_inbound_shell_keyword_mock_http() -> None:
    mock = MockHttpActionGateway(base_url="http://127.0.0.1:18796")
    gateway = CompositeActionGateway(mock_http=mock)
    loop = CloudActionLoop(gateway=gateway, selector=RuleActionSelector())
    receipts = loop.handle(
        InboundEvent(kind="message", text="查一下桥状态", source="webhook")
    )
    assert any(i.kind == "shell_whitelist" for i in loop.last_intents)
    assert any(i.payload.get("tool_name") == "bridge_status" for i in loop.last_intents)
    assert any(r.startswith("mock-http:POST:") for r in receipts)
    assert mock.last_mock is not None
    assert mock.last_mock["method"] == "POST"
    assert mock.last_mock["url"].endswith("/v1/actions/shell")
    assert mock.last_mock["json"]["kind"] == "shell_whitelist"


def test_explicit_tool_prefix_and_allowlist() -> None:
    loop = CloudActionLoop()
    loop.handle(InboundEvent(kind="message", text="请执行 tool:what_time"))
    assert any(
        i.kind == "shell_whitelist" and i.payload.get("tool_name") == "what_time"
        for i in loop.last_intents
    )


def test_tool_result_emits_card_and_optional_shell() -> None:
    mock = MockHttpActionGateway()
    gateway = CompositeActionGateway(
        dashboard=HttpDashboardGateway(),
        mock_http=mock,
    )
    loop = CloudActionLoop(gateway=gateway)
    receipts = loop.handle(
        InboundEvent(
            kind="tool_result",
            text="现在是 Asia/Shanghai 2026-10-28 18:00:00 CST",
            tool_name="what_time",
            source="bridge_tool",
        )
    )
    kinds = {i.kind for i in loop.last_intents}
    assert "show_card" in kinds
    assert "shell_whitelist" in kinds
    assert len(receipts) == 2
    assert any(r.startswith("stub-dashboard:") for r in receipts)
    assert any(r.startswith("mock-http:POST:") for r in receipts)


def test_shell_whitelist_intent_helper() -> None:
    intent = ShellWhitelist(tool_name="list_workspace", args={"limit": 3}).to_intent()
    assert intent.kind == "shell_whitelist"
    assert intent.payload["executor"] == "bridge_tools_whitelist"
    assert intent.payload["args"]["limit"] == 3


def test_mock_http_base_from_env_only(monkeypatch) -> None:
    monkeypatch.setenv("WHALE_ACTIONS_HTTP_BASE", "http://127.0.0.1:19999")
    gw = MockHttpActionGateway()
    assert gw.base_url == "http://127.0.0.1:19999"
    receipt = gw.dispatch(ShowCard(title="t", body="b").to_intent())
    assert "19999" in receipt
    assert "card" in receipt
    # 不编造密钥：环境里即使没有 token 也能 mock
    assert "token" not in receipt.lower()
    monkeypatch.delenv("WHALE_ACTIONS_HTTP_BASE", raising=False)


def test_unknown_tool_not_selected() -> None:
    sel = RuleActionSelector()
    intents = sel.select(InboundEvent(kind="message", text="tool:rm_rf_everything"))
    assert all(
        i.kind != "shell_whitelist" or i.payload.get("tool_name") != "rm_rf_everything"
        for i in intents
    )


def test_default_ack_card_when_no_keyword() -> None:
    loop = CloudActionLoop()
    loop.handle(InboundEvent(kind="message", text="你好", source="test"))
    assert len(loop.last_intents) == 1
    assert loop.last_intents[0].kind == "show_card"
    assert loop.last_intents[0].payload.get("layout_hint") == "ack"
