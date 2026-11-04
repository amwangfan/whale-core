"""Shell ACL：主人 / QQ 非主人 / sudo 租约 三条路径回归。"""

from __future__ import annotations

import json

from whale_core.cloud.shell_policy import ShellPolicy, ShellTier
from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import (
    ConversationOrchestrator,
    InputEnvelope,
    ToolRegistry,
    ToolSpec,
)
from whale_core.core.runtime.policy import (
    _action_fingerprint,
    runtime_owner_verifier,
    runtime_shell_grant_verifier,
)
from whale_core.core.types import ActionIntent
from whale_core.life.context_resolver import (
    ConversationContextResolver,
    PRIVATE_OWNER,
    pairing_lookup_from_mapping,
)
from whale_core.life.conversation_context import (
    ConversationKind,
    SCOPE_TOOL_SHELL_SAFE,
)
from whale_core.life.temporary_auth import GRANTABLE_SCOPES, TemporaryAuthService
from whale_core.memory.spaces import SpaceStore, ensure_owner_working_copy
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.gateway import PluginGateway
from whale_core.plugins.intents import PluginAction
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.policy import PluginActionPolicy
from whale_core.plugins.registry import PluginRegistry


def test_grantable_includes_shell_safe() -> None:
    assert SCOPE_TOOL_SHELL_SAFE in GRANTABLE_SCOPES
    assert SCOPE_TOOL_SHELL_SAFE in PRIVATE_OWNER


def test_owner_private_resolve_has_shell_scope() -> None:
    store = InMemoryStore()
    spaces = SpaceStore(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "10001"): "owner"}),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(
            store, spaces),
        owner_id="owner",
        owner_default_space=ensure_owner_working_copy(store),
    )
    ctx = resolver.resolve(
        channel="qq", sender_id="10001", conversation_id="private:10001")
    assert ctx.principal.is_owner
    assert SCOPE_TOOL_SHELL_SAFE in ctx.capability_scopes


def test_stranger_private_resolve_no_shell_without_grant() -> None:
    store = InMemoryStore()
    spaces = SpaceStore(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({}),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(
            store, spaces),
        owner_id="owner",
    )
    ctx = resolver.resolve(
        channel="qq", sender_id="u_eve", conversation_id="private:u_eve")
    assert not ctx.principal.is_owner
    assert SCOPE_TOOL_SHELL_SAFE not in ctx.capability_scopes


def test_stranger_sudo_grant_adds_shell_scope() -> None:
    store = InMemoryStore()
    spaces = SpaceStore(store)
    auth = TemporaryAuthService(store)
    code = auth.create_code(
        allowed_scopes=(SCOPE_TOOL_SHELL_SAFE,), grant_max_uses=2, grant_ttl_s=600)
    grant = auth.verify(
        code, channel="qq", sender_id="u_eve",
        conversation_kind=ConversationKind.QQ_PRIVATE,
        conversation_id="private:u_eve")
    assert grant is not None
    assert SCOPE_TOOL_SHELL_SAFE in grant.scopes
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({}),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(
            store, spaces),
        grants=auth,
        owner_id="owner",
    )
    ctx = resolver.resolve(
        channel="qq", sender_id="u_eve", conversation_id="private:u_eve")
    assert not ctx.principal.is_owner
    assert SCOPE_TOOL_SHELL_SAFE in ctx.capability_scopes
    assert SCOPE_TOOL_SHELL_SAFE in ctx.grant_scopes


class _Brain:
    def reply(self, text, **kwargs):
        return "收到"


class _ShellPlanner:
    def plan(self, text, **kwargs):
        return json.dumps({
            "goal": "跑命令",
            "kind": "task",
            "capabilities": ("shell.safe",),
            "slots": {"command": "echo acl"},
        })


def _shell_registry_and_gateway(store, *, personal: bool = True):
    registry = PluginRegistry()
    calls = []

    class Adapter:
        def execute(self, action, target, args):
            calls.append(dict(args))
            return True, "ok"

    registry.register(
        PluginManifest(
            plugin_id="shell_sandbox", name="受控终端", version="1.0.0",
            kind="output", actions=("run_command",),
            action_risk={"run_command": "communicate"},
        ),
        executor=Adapter(),
    )
    auto = frozenset({"shell_sandbox.run_command"}) if personal else frozenset()
    gateway = PluginGateway(
        registry,
        PluginActionPolicy(registry, auto_approve_actions=auto),
        owner_verifier=runtime_owner_verifier(store),
        grant_verifier=runtime_shell_grant_verifier(store),
    )
    tools = ToolRegistry()
    tools.register(ToolSpec(
        name="run_shell", capability="shell.safe",
        plugin_id="shell_sandbox", action="run_command",
        risk_class="communicate",
    ))
    return registry, gateway, tools, calls


def test_orch_stranger_shell_denied_without_sudo() -> None:
    store = InMemoryStore()
    spaces = SpaceStore(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({}),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(
            store, spaces),
        owner_id="owner",
    )
    _, gateway, tools, calls = _shell_registry_and_gateway(store)
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()), intent_model=_ShellPlanner(),
        tools=tools, gateway=gateway, context_resolver=resolver)
    result = orch.handle_envelope(InputEnvelope(
        conversation_id="private:u_eve", text="执行 ls", channel="qq",
        sender_id="u_eve", source="qq"))
    assert calls == []
    joined = " ".join(result.receipts)
    assert "scope-tool.shell.safe" in joined or "plugin-denied" in joined or "denied" in joined


def test_orch_owner_qq_shell_allowed_with_personal_auto() -> None:
    store = InMemoryStore()
    spaces = SpaceStore(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "10001"): "owner"}),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(
            store, spaces),
        owner_id="owner",
        owner_default_space=ensure_owner_working_copy(store),
    )
    _, gateway, tools, calls = _shell_registry_and_gateway(store, personal=True)
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()), intent_model=_ShellPlanner(),
        tools=tools, gateway=gateway, context_resolver=resolver)
    result = orch.handle_envelope(InputEnvelope(
        conversation_id="private:10001", text="执行 echo", channel="qq",
        sender_id="10001", source="qq:owner"))
    assert calls, f"owner shell should run; receipts={result.receipts}"
    assert calls[0].get("_whale_is_owner") is True


def test_orch_stranger_shell_allowed_with_sudo_grant() -> None:
    store = InMemoryStore()
    spaces = SpaceStore(store)
    auth = TemporaryAuthService(store)
    code = auth.create_code(
        allowed_scopes=(SCOPE_TOOL_SHELL_SAFE,), grant_max_uses=1)
    assert auth.verify(
        code, channel="qq", sender_id="u_eve",
        conversation_kind=ConversationKind.QQ_PRIVATE,
        conversation_id="private:u_eve") is not None
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({}),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(
            store, spaces),
        grants=auth,
        owner_id="owner",
    )
    _, gateway, tools, calls = _shell_registry_and_gateway(store, personal=True)

    def _gate(grant_ids, fingerprint, required_scope=None):
        return any(
            auth.consume_grant_use(g, action_fingerprint=fingerprint,
                                   required_scope=required_scope)
            for g in grant_ids)

    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()), intent_model=_ShellPlanner(),
        tools=tools, gateway=gateway, context_resolver=resolver,
        grant_gate=_gate)
    result = orch.handle_envelope(InputEnvelope(
        conversation_id="private:u_eve", text="执行 echo", channel="qq",
        sender_id="u_eve", source="qq"))
    assert calls, f"sudo grant should unlock shell; receipts={result.receipts}"
    assert calls[0].get("_whale_shell_grant") is True
    assert calls[0].get("_whale_is_owner") is False


def test_policy_command_matrix() -> None:
    p = ShellPolicy(tier=ShellTier.PERSONAL)
    assert p.command_verdict("ls", is_owner=True).allowed
    assert not p.command_verdict("ls", is_owner=False).allowed
    assert p.command_verdict("ls", is_owner=False, has_shell_grant=True).allowed
    assert not ShellPolicy(tier=ShellTier.MANAGED).command_verdict(
        "ls", is_owner=True, has_shell_grant=True).allowed
