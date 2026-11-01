"""插件信任级别：manifest.trust 字段、加载透传、policy/gateway 最小钩子。

（原 tests/test_g4_trust.py，G4 里程碑命名；issue #29 起按领域与层级组织。）

契约（docs/g4-operations.md）：

- ``PluginManifest.trust`` 合法值 ``"bundled"``（默认，既有行为不变）/
  ``"external"``；其它值构造即拒绝；
- trust 由放置清单的宿主/运维者声明（显式加载面），插件自身无法提权；
- ``trust="external"`` 的插件执行**非 read_only** 动作时强制
  ``needs_approval``——宿主的 ``auto_approve_risks`` 放宽对其不适用；
  显式批准后仍走原有 ready 路径（沿用既有 approval 通道，不新造状态机）；
- read_only 动作不受信任级别影响（本就免审）。

全部离线；执行器为测试替身（同 tests/test_plugins.py 约定）。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from whale_core.core.types import ActionIntent
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.gateway import PluginGateway
from whale_core.plugins.loader import PluginLoadError, read_plugin_manifest
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.manifest import (
    DEFAULT_TRUST,
    TRUST_LEVELS,
    PluginManifest,
)
from whale_core.plugins.intents import PluginAction
from whale_core.plugins.policy import PluginActionPolicy
from whale_core.plugins.registry import PluginRegistry


# ---------------------------------------------------------------------------
# 示例清单与替身（形状对齐 tests/test_plugins.py）
# ---------------------------------------------------------------------------

EXT_CHAT = PluginManifest(
    plugin_id="ext_chat",
    name="第三方外发插件（external 示例）",
    version="0.1.0",
    actions=("send_message", "search_contacts"),
    action_risk={"send_message": "communicate", "search_contacts": "read_only"},
    trust="external",
)
EXT_SHOP = PluginManifest(
    plugin_id="ext_shop",
    name="第三方交易插件（external 示例）",
    version="0.1.0",
    actions=("purchase",),
    action_risk={"purchase": "transaction"},
    trust="external",
)
BUNDLED_CHAT = dataclasses.replace(  # 对照组：默认 trust（replace 会继承字段，需显式覆盖）
    EXT_CHAT, plugin_id="bundled_chat", trust="bundled")
BUNDLED_SHOP = dataclasses.replace(
    EXT_SHOP, plugin_id="bundled_shop", trust="bundled")


class SpyExecutor:
    """记录调用、可脚本化成败的执行替身；不触网。"""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.ok = ok

    def execute(self, action: str, target: str, args) -> tuple[bool, str]:
        self.calls.append((action, target, dict(args)))
        return self.ok, "spy-ok" if self.ok else "spy-fail"


def _gateway_for(*manifests: PluginManifest, auto_approve=("communicate",),
                 approved: bool = False):
    """装配 registry + policy + gateway；返回 (gateway, spies)。

    auto_approve 默认含 communicate——用于验证 external 插件**不被**宿主放宽。
    """
    registry = PluginRegistry()
    spies: dict[str, SpyExecutor] = {}
    for manifest in manifests:
        spy = SpyExecutor()
        registry.register(manifest, executor=spy)
        spies[manifest.plugin_id] = spy
    policy = PluginActionPolicy(
        registry,
        max_transaction_amount=200.0,
        max_daily_transactions=5,
        transaction_count_today=lambda: 0,
        auto_approve_risks=frozenset(auto_approve),
    )
    gateway = PluginGateway(
        registry, policy,
        owner_verifier=lambda _intent: True,
        approval_verifier=(lambda _intent: True) if approved else None,
    )
    return gateway, spies


# ---------------------------------------------------------------------------
# manifest 层：字段与校验
# ---------------------------------------------------------------------------

def test_trust_default_is_bundled() -> None:
    assert DEFAULT_TRUST == "bundled"
    assert PluginManifest(
        plugin_id="p1", name="p", version="0.1.0",
        actions=("ping",), action_risk={"ping": "read_only"},
    ).trust == "bundled"


def test_trust_levels_frozen_set() -> None:
    assert TRUST_LEVELS == frozenset({"bundled", "external"})


def test_trust_external_accepted() -> None:
    assert EXT_CHAT.trust == "external"
    assert EXT_SHOP.trust == "external"


@pytest.mark.parametrize("bad", ["trusted", "BUNDLED", "", "internal", 1, None])
def test_trust_invalid_rejected(bad) -> None:
    with pytest.raises(ValueError, match="invalid trust"):
        PluginManifest(
            plugin_id="p1", name="p", version="0.1.0",
            actions=("ping",), action_risk={"ping": "read_only"}, trust=bad,
        )


# ---------------------------------------------------------------------------
# 加载透传：manager（manifests 目录 JSON）与 loader（外部目录 plugin.json）
# ---------------------------------------------------------------------------

def _manifest_json(pid: str, **extra) -> str:
    return json.dumps({
        "plugin_id": pid, "name": "示例", "version": "0.1.0",
        "actions": ["ping"], "action_risk": {"ping": "read_only"}, **extra,
    }, ensure_ascii=False)


def test_manager_load_dir_passthrough(tmp_path) -> None:
    (tmp_path / "ext.json").write_text(
        _manifest_json("ext_one", trust="external"), encoding="utf-8")
    (tmp_path / "old.json").write_text(
        _manifest_json("old_one"), encoding="utf-8")  # 无 trust 键 → 默认 bundled
    manager = PluginManager(PluginRegistry(), InMemoryStore())
    loaded = manager.load_dir(tmp_path)
    assert sorted(loaded) == ["ext_one", "old_one"]
    rows = {row["plugin_id"]: row for row in manager.status()}
    assert rows["ext_one"]["trust"] == "external"
    assert rows["old_one"]["trust"] == "external"  # 无 trust 字段不提权


def test_manager_load_dir_invalid_trust_recorded_not_loaded(tmp_path) -> None:
    (tmp_path / "bad.json").write_text(
        _manifest_json("bad_one", trust="root"), encoding="utf-8")
    manager = PluginManager(PluginRegistry(), InMemoryStore())
    assert manager.load_dir(tmp_path) == []
    assert len(manager.errors) == 1
    assert "invalid trust" in manager.errors[0]["error"]


def test_loader_plugin_json_passthrough(tmp_path) -> None:
    (tmp_path / "plugin.json").write_text(
        _manifest_json("dir_one", trust="external"), encoding="utf-8")
    assert read_plugin_manifest(tmp_path).trust == "external"


def test_loader_plugin_json_default_and_invalid(tmp_path) -> None:
    (tmp_path / "plugin.json").write_text(_manifest_json("dir_two"), encoding="utf-8")
    assert read_plugin_manifest(tmp_path).trust == "external"
    (tmp_path / "plugin.json").write_text(
        _manifest_json("dir_three", trust="root"), encoding="utf-8")
    with pytest.raises(PluginLoadError):
        read_plugin_manifest(tmp_path)


# ---------------------------------------------------------------------------
# policy 层：external 强制人审，auto_approve 不适用
# ---------------------------------------------------------------------------

def _evaluate(manifest: PluginManifest, action: str, **kwargs):
    registry = PluginRegistry()
    registry.register(manifest, executor=SpyExecutor())
    policy = PluginActionPolicy(
        registry,
        max_transaction_amount=200.0,
        max_daily_transactions=5,
        transaction_count_today=lambda: 0,
        auto_approve_risks=frozenset({"communicate"}),  # 宿主级放宽（R1）
    )
    kwargs.setdefault("is_owner", True)
    return policy.evaluate(manifest.plugin_id, action, **kwargs)


def test_external_read_only_still_free() -> None:
    verdict = _evaluate(EXT_CHAT, "search_contacts")
    assert verdict.status == "ready"  # read_only 本就免审，信任级别不影响


def test_external_communicate_bypasses_host_auto_approve() -> None:
    verdict = _evaluate(EXT_CHAT, "send_message")
    assert verdict.status == "needs_approval"
    assert "external" in verdict.reason


def test_bundled_communicate_keeps_host_auto_approve() -> None:
    # 对照组：bundled 插件吃宿主 auto_approve 放宽（既有 R1 行为不回归）
    verdict = _evaluate(BUNDLED_CHAT, "send_message")
    assert verdict.status == "ready"
    assert "auto-approved" in verdict.reason


def test_external_communicate_ready_after_explicit_approval() -> None:
    verdict = _evaluate(EXT_CHAT, "send_message", explicitly_approved=True)
    assert verdict.status == "ready"


def test_external_denied_for_non_owner() -> None:
    verdict = _evaluate(EXT_CHAT, "send_message", is_owner=False)
    assert verdict.status == "denied"  # 身份门不变


def test_external_transaction_needs_approval_then_ready_within_caps() -> None:
    verdict = _evaluate(EXT_SHOP, "purchase", args={"amount": 10})
    assert verdict.status == "needs_approval"
    assert "external" in verdict.reason
    approved = _evaluate(EXT_SHOP, "purchase", explicitly_approved=True,
                         args={"amount": 10})
    assert approved.status == "ready"  # 审批通道与限额裁决照常


# ---------------------------------------------------------------------------
# gateway 端到端：external 插件的非只读动作走既有 hold 回执
# ---------------------------------------------------------------------------

def test_gateway_external_held_despite_auto_approve() -> None:
    gateway, spies = _gateway_for(EXT_CHAT, BUNDLED_CHAT)
    intent = PluginAction(plugin_id="ext_chat", action="send_message",
                          target="friend").to_intent()
    assert gateway.dispatch(intent) == "plugin-hold:ext_chat:send_message"
    assert spies["ext_chat"].calls == []  # 未执行
    # 对照组 bundled：宿主 auto_approve 生效，直接执行
    control = PluginAction(plugin_id="bundled_chat", action="send_message",
                           target="friend").to_intent()
    assert gateway.dispatch(control).startswith("plugin:bundled_chat:send_message:ok:")
    assert len(spies["bundled_chat"].calls) == 1


def test_gateway_external_executes_after_host_approval() -> None:
    gateway, spies = _gateway_for(EXT_CHAT, approved=True)
    intent = PluginAction(plugin_id="ext_chat", action="send_message",
                          target="friend").to_intent()
    assert gateway.dispatch(intent).startswith("plugin:ext_chat:send_message:ok:")
    assert len(spies["ext_chat"].calls) == 1


def test_gateway_external_read_only_unaffected() -> None:
    gateway, spies = _gateway_for(EXT_CHAT)
    intent = PluginAction(plugin_id="ext_chat", action="search_contacts",
                          target="friend").to_intent()
    assert gateway.dispatch(intent).startswith("plugin:ext_chat:search_contacts:ok:")
    assert len(spies["ext_chat"].calls) == 1


def test_gateway_external_transaction_hold_and_release() -> None:
    gateway, spies = _gateway_for(EXT_SHOP)
    intent = PluginAction(plugin_id="ext_shop", action="purchase",
                          target="sku-1", args={"amount": 10}).to_intent()
    assert gateway.dispatch(intent) == "plugin-hold:ext_shop:purchase"
    assert spies["ext_shop"].calls == []
    # 宿主显式批准后同一通道放行（新 gateway 模拟批准后重派）
    approved_gw, approved_spies = _gateway_for(EXT_SHOP, approved=True)
    receipt = approved_gw.dispatch(
        PluginAction(plugin_id="ext_shop", action="purchase",
                     target="sku-1", args={"amount": 10}).to_intent())
    assert receipt.startswith("plugin:ext_shop:purchase:ok:")
    assert len(approved_spies["ext_shop"].calls) == 1


def test_gateway_forged_payload_flags_still_fail_closed() -> None:
    # external 插件同样不得信 payload 里的身份/审批伪造（网关既有不变量）
    gateway, spies = _gateway_for(EXT_CHAT)
    forged = ActionIntent(
        kind="plugin_action",
        payload={"plugin_id": "ext_chat", "action": "send_message",
                 "target": "friend", "args": {},
                 "is_owner": True, "explicitly_approved": True},
    )
    gateway_no_owner = PluginGateway(
        gateway.registry, gateway.policy)  # 无宿主 verifier → 全拒绝
    receipt = gateway_no_owner.dispatch(forged)
    assert receipt.startswith("plugin-denied:ext_chat:send_message:")
    assert spies["ext_chat"].calls == []


def test_manifest_directory_claimed_bundled_is_capped(tmp_path):
    (tmp_path / "claim.json").write_text(
        _manifest_json("claim", trust="bundled"), encoding="utf-8")
    manager = PluginManager(PluginRegistry(), InMemoryStore())
    assert manager.load_dir(tmp_path) == ["claim"]
    assert manager.status()[0]["trust"] == "external"


def test_explicit_host_manifest_still_supports_bundled():
    manager = PluginManager(PluginRegistry(), InMemoryStore())
    manager.load_manifest(PluginManifest(
        plugin_id="host", name="host", version="1", trust="bundled"))
    assert manager.status()[0]["trust"] == "bundled"
