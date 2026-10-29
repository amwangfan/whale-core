"""泛化插件层：清单校验、注册表、事件流、风险门控与网关派发（全部离线）。

示例插件对应 docs/plugin-architecture.md 的目录：邮件、微信、桌面、购物、
雷鸟纪要。所有执行器都是测试替身，不发起网络 / 子进程。
"""

from __future__ import annotations

import pytest

from whale_core.actions.gateway import CompositeActionGateway
from whale_core.core.agent import WhaleAgent
from whale_core.core.router import StubModelRouter
from whale_core.core.types import ActionIntent, Decision
from whale_core.plugins import (
    CORE_EVENT_KINDS,
    FORBIDDEN_ACTION_NAMES,
    NullPluginExecutor,
    PluginAction,
    PluginActionPolicy,
    PluginEvent,
    PluginGateway,
    PluginManifest,
    PluginRegistry,
    RiskClass,
)


# ---------------------------------------------------------------------------
# 示例清单（文档目录同款）
# ---------------------------------------------------------------------------

EMAIL = PluginManifest(
    plugin_id="email",
    name="邮件插件（示例）",
    version="0.1.0",
    inputs=("email.received", "email.daily_digest"),
    actions=("search_mailbox", "send_email"),
    action_risk={"search_mailbox": "read_only", "send_email": "communicate"},
)
WECHAT = PluginManifest(
    plugin_id="wechat",
    name="微信插件（示例）",
    version="0.1.0",
    inputs=("wechat.message",),
    actions=("send_message",),
    action_risk={"send_message": "communicate"},
)
SHOP = PluginManifest(
    plugin_id="shop",
    name="购物插件（示例）",
    version="0.1.0",
    actions=("search_goods", "purchase"),
    action_risk={"search_goods": "read_only", "purchase": "transaction"},
)
DESKTOP = PluginManifest(
    plugin_id="desktop",
    name="电脑桌面插件（示例）",
    version="0.1.0",
    inputs=("desktop.focus_window",),
    actions=("notify", "open_app"),
    action_risk={"notify": "communicate", "open_app": "communicate"},
)
MINUTES = PluginManifest(
    plugin_id="rayneo_minutes",
    name="雷鸟全天纪要（示例，只入不出）",
    version="0.1.0",
    inputs=("minutes.segment",),
)


class SpyExecutor:
    def __init__(self, ok: bool = True, raise_exc: bool = False) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.ok = ok
        self.raise_exc = raise_exc

    def execute(self, action: str, target: str, args) -> tuple[bool, str]:
        if self.raise_exc:
            raise RuntimeError("boom")
        self.calls.append((action, target, dict(args)))
        return self.ok, "sent" if self.ok else "channel down"


# ---------------------------------------------------------------------------
# 清单校验
# ---------------------------------------------------------------------------


def test_valid_manifests_and_risk_lookup() -> None:
    assert EMAIL.risk_of("send_email") is RiskClass.COMMUNICATE
    assert SHOP.risk_of("purchase") is RiskClass.TRANSACTION
    assert EMAIL.risk_of("unknown_action") is None


def test_manifest_rejects_core_event_impersonation() -> None:
    for kind in ("phone.foreground", "context.weather"):
        assert kind in CORE_EVENT_KINDS
        with pytest.raises(ValueError, match="impersonates"):
            PluginManifest(
                plugin_id="fake", name="x", version="1", inputs=(kind,)
            )
    # 核心命名空间前缀同样拒绝（即使具体 kind 不存在）
    with pytest.raises(ValueError, match="impersonates"):
        PluginManifest(plugin_id="fake", name="x", version="1", inputs=("nav.custom",))


def test_manifest_rejects_shell_wrapper_actions() -> None:
    for name in sorted(FORBIDDEN_ACTION_NAMES):
        with pytest.raises(ValueError, match="reserved"):
            PluginManifest(
                plugin_id="bad", name="x", version="1",
                actions=(name,), action_risk={name: "read_only"},
            )


def test_manifest_shape_validation() -> None:
    with pytest.raises(ValueError):
        PluginManifest(plugin_id="Bad ID", name="x", version="1")
    with pytest.raises(ValueError):
        PluginManifest(plugin_id="ok", name="", version="1")
    with pytest.raises(ValueError):  # 无命名空间的事件 kind
        PluginManifest(plugin_id="ok", name="x", version="1", inputs=("flatkind",))
    with pytest.raises(ValueError):  # risk 指向未声明动作
        PluginManifest(plugin_id="ok", name="x", version="1", actions=("a",),
                       action_risk={"b": "read_only"})
    with pytest.raises(ValueError):  # 非法风险级
        PluginManifest(plugin_id="ok", name="x", version="1", actions=("a",),
                       action_risk={"a": "yolo"})


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_registry_register_lookup_unregister() -> None:
    reg = PluginRegistry()
    reg.register(EMAIL)
    reg.register(WECHAT, executor=SpyExecutor())
    assert reg.manifest("email") is EMAIL
    assert reg.risk_of("wechat", "send_message") is RiskClass.COMMUNICATE
    assert isinstance(reg.executor("email"), NullPluginExecutor)
    assert reg.owner_of_kind("wechat.message") == "wechat"

    with pytest.raises(ValueError, match="already registered"):
        reg.register(EMAIL)

    reg.unregister("email")
    assert reg.manifest("email") is None


def test_registry_kind_collision_across_plugins_denied() -> None:
    reg = PluginRegistry()
    reg.register(PluginManifest(plugin_id="alpha", name="a", version="1", inputs=("mail.inbox",)))
    with pytest.raises(ValueError, match="already owned"):
        reg.register(PluginManifest(plugin_id="beta", name="b", version="1", inputs=("mail.inbox",)))
    # 单字符 id 不合法（PLUGIN_ID_RE 至少两位）
    with pytest.raises(ValueError, match="invalid plugin_id"):
        PluginManifest(plugin_id="a", name="a", version="1")


# ---------------------------------------------------------------------------
# 泛化事件：不改核心即可进 perceive 链路
# ---------------------------------------------------------------------------


def test_plugin_event_validation() -> None:
    with pytest.raises(ValueError):
        PluginEvent(kind="email.received")  # 缺 plugin_id
    with pytest.raises(ValueError):
        PluginEvent(kind="flat", plugin_id="email")  # 无命名空间
    with pytest.raises(TypeError):
        PluginEvent(kind="email.received", plugin_id="email", trust="owner_verified")
    with pytest.raises(ValueError, match="core event"):
        PluginEvent(kind="phone.foreground", plugin_id="email")
    with pytest.raises(ValueError, match="valid plugin_id"):
        PluginEvent(kind="email.received", plugin_id="Bad ID")


def test_plugin_event_flows_through_whale_agent() -> None:
    agent = WhaleAgent(router=StubModelRouter())
    events = [
        PluginEvent(kind="email.received", plugin_id="email",
                    payload={"from": "boss@example", "subject_digest": "周报"}),
        PluginEvent(kind="minutes.segment", plugin_id="rayneo_minutes",
                    payload={"text_digest": "上午讨论了部署计划"}),
    ]
    decision, receipts = agent.step(events)
    obs_summary = agent.last_continuity() is not None  # perceive 正常完成
    assert obs_summary
    assert isinstance(decision, Decision)
    assert receipts == []  # Stub router 不产意图
    # 事件 kind 进入 Observation 摘要（既有链路无需感知插件类型）
    observation = agent.perceive(events)
    assert "email.received" in observation.summary
    assert "minutes.segment" in observation.summary
    assert all(event.trust == "external" for event in events)


# ---------------------------------------------------------------------------
# 风险门控
# ---------------------------------------------------------------------------


def make_policy(**kwargs) -> tuple[PluginRegistry, PluginActionPolicy]:
    reg = PluginRegistry()
    reg.register(EMAIL)
    reg.register(SHOP)
    return reg, PluginActionPolicy(reg, **kwargs)


def test_policy_unknown_plugin_and_undeclared_action_denied() -> None:
    _, policy = make_policy()
    assert policy.evaluate("ghost", "send_email", is_owner=True).status == "denied"
    assert policy.evaluate("email", "delete_all_mail", is_owner=True).status == "denied"


def test_policy_owner_gate_applies_to_all_risk_levels() -> None:
    _, policy = make_policy()
    assert policy.evaluate("email", "search_mailbox", is_owner=False).status == "denied"
    assert policy.evaluate("email", "send_email", is_owner=False,
                           explicitly_approved=True).status == "denied"
    assert policy.evaluate("shop", "purchase", is_owner=False,
                           explicitly_approved=True).status == "denied"


def test_policy_risk_matrix_owner() -> None:
    reg, _ = make_policy()
    policy = PluginActionPolicy(
        reg, max_transaction_amount=200.0, max_daily_transactions=5,
        transaction_count_today=lambda: 0,
    )
    assert policy.evaluate("email", "search_mailbox", is_owner=True).status == "ready"
    assert policy.evaluate("email", "send_email", is_owner=True).status == "needs_approval"
    assert policy.evaluate("email", "send_email", is_owner=True,
                           explicitly_approved=True).status == "ready"
    assert policy.evaluate("shop", "purchase", is_owner=True,
                           explicitly_approved=True, args={"amount": 10}).status == "ready"
    assert policy.evaluate("shop", "purchase", is_owner=True).status == "needs_approval"


def test_policy_transaction_caps() -> None:
    _, policy = make_policy(max_transaction_amount=200.0, max_daily_transactions=2,
                            transaction_count_today=lambda: 2)
    over = policy.evaluate("shop", "purchase", is_owner=True,
                           explicitly_approved=True, args={"amount": 201})
    assert over.status == "denied" and "over cap" in over.reason
    capped = policy.evaluate("shop", "purchase", is_owner=True,
                             explicitly_approved=True, args={"amount": 10})
    assert capped.status == "denied" and "daily" in capped.reason

    _, policy2 = make_policy(max_transaction_amount=200.0, max_daily_transactions=5,
                              transaction_count_today=lambda: 0)
    ok = policy2.evaluate("shop", "purchase", is_owner=True,
                          explicitly_approved=True, args={"amount": 199})
    assert ok.status == "ready"


def test_policy_undeclared_risk_denied() -> None:
    reg = PluginRegistry()
    reg.register(PluginManifest(plugin_id="loose", name="x", version="1", actions=("do_it",)))
    policy = PluginActionPolicy(reg)
    verdict = policy.evaluate("loose", "do_it", is_owner=True, explicitly_approved=True)
    assert verdict.status == "denied" and "risk" in verdict.reason


def test_policy_audit_has_no_args() -> None:
    _, policy = make_policy()
    verdict = policy.evaluate("shop", "purchase", is_owner=True, args={"amount": 5})
    record = policy.audit("sku-123", verdict, source="qq:owner")
    assert record["action"] == "purchase" and record["status"] == "needs_approval"
    assert "amount" not in record and "args" not in record


def test_policy_rejects_missing_or_invalid_amount_when_cap_is_configured() -> None:
    _, policy = make_policy(max_transaction_amount=200.0)
    for args in ({}, {"amount": "not-a-number"}, {"amount": float("nan")},
                 {"amount": float("inf")}, {"amount": -1}, {"amount": True}):
        verdict = policy.evaluate(
            "shop", "purchase", is_owner=True, explicitly_approved=True, args=args
        )
        assert verdict.status == "denied"
        assert "amount" in verdict.reason


def test_policy_daily_cap_fails_closed_without_counter() -> None:
    reg, _ = make_policy()
    policy = PluginActionPolicy(reg, max_transaction_amount=200.0, max_daily_transactions=2)
    verdict = policy.evaluate(
        "shop", "purchase", is_owner=True, explicitly_approved=True,
        args={"amount": 5}, reserve_transaction=True,
    )
    assert verdict.status == "denied"
    assert "counter not configured" in verdict.reason


def test_transaction_fails_closed_when_either_cap_is_missing() -> None:
    reg, _ = make_policy()
    no_amount_cap = PluginActionPolicy(
        reg, max_daily_transactions=5, transaction_count_today=lambda: 0
    )
    verdict = no_amount_cap.evaluate(
        "shop", "purchase", is_owner=True, explicitly_approved=True, args={"amount": 10}
    )
    assert verdict.status == "denied" and "amount cap not configured" in verdict.reason

    no_daily_cap = PluginActionPolicy(reg, max_transaction_amount=200.0)
    verdict = no_daily_cap.evaluate(
        "shop", "purchase", is_owner=True, explicitly_approved=True, args={"amount": 10}
    )
    assert verdict.status == "denied" and "daily transaction cap not configured" in verdict.reason


# ---------------------------------------------------------------------------
# 网关派发与组合路由
# ---------------------------------------------------------------------------


def test_gateway_ignores_forged_owner_and_approval_payload_flags() -> None:
    reg, policy = make_policy()
    spy = SpyExecutor()
    reg.register(WECHAT, executor=spy)
    gw = PluginGateway(reg, policy)  # no host verifier => fail closed
    forged = ActionIntent(
        kind="plugin_action",
        payload={
            "plugin_id": "wechat", "action": "send_message", "target": "friend",
            "args": {"text": "在吗"}, "is_owner": True, "explicitly_approved": True,
        },
    )
    receipt = gw.dispatch(forged)
    assert receipt.startswith("plugin-denied:wechat:send_message:")
    assert spy.calls == []


def test_gateway_denied_and_hold_do_not_execute() -> None:
    reg, policy = make_policy()
    spy = SpyExecutor()
    reg.register(WECHAT, executor=spy)
    gw = PluginGateway(reg, policy, owner_verifier=lambda _intent: True)
    denied = gw.dispatch(PluginAction(plugin_id="shop", action="purchase").to_intent())
    assert denied == "plugin-hold:shop:purchase"
    hold = gw.dispatch(PluginAction(plugin_id="wechat", action="send_message",
                                    target="friend").to_intent())
    assert hold == "plugin-hold:wechat:send_message"
    assert spy.calls == []


def test_gateway_ready_executes_only_after_host_verified_approval() -> None:
    reg, policy = make_policy()
    spy = SpyExecutor(ok=True)
    reg.register(WECHAT, executor=spy)
    gw = PluginGateway(reg, policy, owner_verifier=lambda _intent: True,
                       approval_verifier=lambda _intent: True)
    receipt = gw.dispatch(PluginAction(plugin_id="wechat", action="send_message",
        target="friend", args={"text": "在吗"}).to_intent())
    assert receipt.startswith("plugin:wechat:send_message:ok:")
    assert spy.calls == [("send_message", "friend", {"text": "在吗"})]
    broken = SpyExecutor(ok=False)
    reg.register(DESKTOP, executor=broken)
    assert "fail" in gw.dispatch(PluginAction(plugin_id="desktop", action="notify").to_intent())


def test_gateway_executor_exception_does_not_raise() -> None:
    reg, policy = make_policy()
    reg.register(WECHAT, executor=SpyExecutor(raise_exc=True))
    gw = PluginGateway(reg, policy, owner_verifier=lambda _intent: True,
                       approval_verifier=lambda _intent: True)
    receipt = gw.dispatch(PluginAction(plugin_id="wechat", action="send_message").to_intent())
    assert receipt.startswith("plugin:wechat:send_message:fail:executor error")


def test_gateway_transaction_daily_limit_reserves_before_execution() -> None:
    reg = PluginRegistry()
    spy = SpyExecutor()
    reg.register(SHOP, executor=spy)
    policy = PluginActionPolicy(reg, max_transaction_amount=200.0, max_daily_transactions=1,
                                transaction_count_today=lambda: 0)
    gw = PluginGateway(reg, policy, owner_verifier=lambda _intent: True,
                       approval_verifier=lambda _intent: True)
    intent = PluginAction(plugin_id="shop", action="purchase", target="sku-1",
                          args={"amount": 10}).to_intent()
    first, second = gw.dispatch(intent), gw.dispatch(intent)
    assert first.startswith("plugin:shop:purchase:ok:")
    assert second.startswith("plugin-denied:shop:purchase:daily transaction cap reached")
    assert len(spy.calls) == 1


def test_gateway_rejects_malformed_payloads_without_raising() -> None:
    reg, policy = make_policy()
    spy = SpyExecutor()
    reg.register(WECHAT, executor=spy)
    gw = PluginGateway(reg, policy, owner_verifier=lambda _intent: True,
                       approval_verifier=lambda _intent: True)
    bad_payloads = (
        "not-a-mapping",
        ["not", "a", "mapping"],
        {"plugin_id": "wechat", "action": "send_message", "args": "not-a-mapping"},
    )
    for payload in bad_payloads:
        receipt = gw.dispatch(ActionIntent(kind="plugin_action", payload=payload))
        assert receipt.startswith("plugin-denied:")
    assert spy.calls == []


def test_gateway_ignores_other_kinds() -> None:
    reg, policy = make_policy()
    gw = PluginGateway(reg, policy)
    assert gw.dispatch(ActionIntent(kind="show_card", payload={})) == \
        "plugin-gateway:ignored:show_card"


def test_composite_gateway_routes_plugin_action() -> None:
    reg, policy = make_policy()
    spy = SpyExecutor()
    reg.register(WECHAT, executor=spy)
    composite = CompositeActionGateway(plugin=PluginGateway(
        reg, policy, owner_verifier=lambda _intent: True,
        approval_verifier=lambda _intent: True,
    ))
    receipt = composite.dispatch(PluginAction(
        plugin_id="wechat", action="send_message").to_intent())
    assert receipt.startswith("plugin:wechat:send_message:ok:")


def test_composite_gateway_without_plugin_falls_back_to_null() -> None:
    composite = CompositeActionGateway()
    receipt = composite.dispatch(
        PluginAction(plugin_id="wechat", action="send_message").to_intent()
    )
    assert receipt == "null:plugin_action"


def test_agent_cannot_self_authorize_plugin_intent_end_to_end() -> None:
    """模型产出的动作意图不能通过 payload 自行授予 owner/审批权限。"""
    reg = PluginRegistry()
    spy = SpyExecutor()
    reg.register(EMAIL, executor=spy)
    policy = PluginActionPolicy(reg)

    class EmailRouter(StubModelRouter):
        def route(self, observation) -> Decision:
            return Decision(
                intents=(
                    PluginAction(plugin_id="email", action="send_email",
                                 target="boss@example").to_intent(),
                ),
                rationale="demo: approved email",
            )

    agent = WhaleAgent(
        router=EmailRouter(),
        gateway=CompositeActionGateway(plugin=PluginGateway(reg, policy)),
    )
    decision, receipts = agent.step([])
    assert decision.intents[0].kind == "plugin_action"
    assert receipts and receipts[0].startswith("plugin-denied:email:send_email:")
    assert spy.calls == []
