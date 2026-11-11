import pytest

from whale_core.api.glasses_identity import (
    build_glasses_context_resolver, migrate_legacy_glasses_episodes,
)
from whale_core.life.conversation_context import MemorySpaceType
from whale_core.memory.bindings import BindingStore
from whale_core.memory.history import append_turn, load_history
from whale_core.memory.spaces import SpaceStore, default_write_space, migrate_legacy_to_vault
from whale_core.memory.store import InMemoryStore


PHONE_KEY = "phone:voice:glasses:s1"
OWNER_QQ_KEY = "qq:qq_private:private:123"


def resolve(resolver, sid="s1", sender="phone"):
    return resolver.resolve(channel="phone", sender_id=sender,
                            conversation_id=f"glasses:{sid}")


def test_phone_owner_scopes_and_default_working_copy():
    store = InMemoryStore()
    resolver = build_glasses_context_resolver(store)
    ctx = resolve(resolver)
    assert ctx.principal.is_owner and ctx.principal.paired
    assert ctx.principal.principal_id == "paired:phone:phone"
    assert "tool.tts.synthesize" in ctx.capability_scopes
    assert ctx.conversation.as_key() == PHONE_KEY
    assert default_write_space(ctx.allowed_memory_spaces) == "owner-working"
    assert SpaceStore(store).get("owner-working")["owner"] == "owner"
    assert not resolve(resolver, sender="someone-else").principal.is_owner


def test_explicit_session_persona_precedes_exact_owner_qq_binding():
    store = InMemoryStore()
    bindings = BindingStore(store, SpaceStore(store))
    bindings.bind_persona(OWNER_QQ_KEY, "owner-persona", updated_by="owner")
    bindings.bind_persona(PHONE_KEY, "session-persona", updated_by="owner")
    resolver = build_glasses_context_resolver(store, "123")
    assert resolve(resolver).persona_id == "session-persona"
    assert resolve(resolver, "s2").persona_id == "owner-persona"


def test_only_configured_qq_persona_is_used_no_arbitrary_fallback():
    store = InMemoryStore()
    bindings = BindingStore(store, SpaceStore(store))
    bindings.bind_persona("qq:qq_private:private:999", "other", updated_by="owner")
    assert resolve(build_glasses_context_resolver(store, "123")).persona_id is None
    assert resolve(build_glasses_context_resolver(store)).persona_id is None
    bindings.bind_persona(OWNER_QQ_KEY, "correct", updated_by="owner")
    assert resolve(build_glasses_context_resolver(store, "123")).persona_id == "correct"
    assert resolve(build_glasses_context_resolver(store)).persona_id is None


def test_session_history_and_bound_memory_scope_remain_separate():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    bindings = BindingStore(store, spaces, owner_conversations=frozenset({OWNER_QQ_KEY}))
    legacy = spaces.create(MemorySpaceType.OWNER_WORKING_COPY, name="legacy", owner="owner")
    qq_only = spaces.create(MemorySpaceType.OWNER_WORKING_COPY, name="qq only", owner="owner")
    bindings.bind_memory(PHONE_KEY, legacy.space_id, can_write=True, granted_by="owner")
    bindings.bind_memory(OWNER_QQ_KEY, qq_only.space_id, can_write=True, granted_by="owner")
    migrate_legacy_to_vault(store)
    bindings.bind_memory(OWNER_QQ_KEY, "vault-default", granted_by="owner")
    append_turn(store, "private:123", "qq secret", "qq reply")
    append_turn(store, "glasses:s1", "phone text", "phone reply")
    resolver = build_glasses_context_resolver(store, "123")
    ctx = resolve(resolver)
    ids = {ref.space_id for ref in ctx.allowed_memory_spaces}
    assert ids == {legacy.space_id, "owner-working"}
    assert default_write_space(ctx.allowed_memory_spaces) == legacy.space_id
    assert load_history(store, ctx.conversation.conversation_id)[0]["content"] == "phone text"
    assert load_history(store, "glasses:s2") == []
    assert load_history(store, "private:123")[0]["content"] == "qq secret"
    assert {ref.space_id for ref in resolve(resolver, "s2").allowed_memory_spaces} == {"owner-working"}


def test_companion_uses_global_active_persona_when_no_qq_binding():
    from whale_core.core.companion import WhaleCompanion
    from whale_core.ui.persona_cards import CARD_KEY_PREFIX, PersonaCard, switch_card

    class Model:
        def __init__(self):
            self.calls = []

        def reply(self, text, **kwargs):
            self.calls.append(kwargs)
            return "好的"

    store = InMemoryStore()
    card = PersonaCard(card_id="global", title="global", name="全局角色")
    store.put(f"{CARD_KEY_PREFIX}global", card.as_dict())
    switch_card(store, "global")
    ctx = resolve(build_glasses_context_resolver(store, "123"))
    model = Model()
    WhaleCompanion(store, model).chat("你好", conversation_id="glasses:s1", context=ctx)
    assert ctx.persona_id is None
    assert "全局角色" in model.calls[0]["persona"]


@pytest.mark.parametrize("sqlite", [False, True])
def test_migrate_only_unscoped_legacy_glasses_chat(tmp_path, sqlite):
    from dataclasses import replace
    from whale_core.memory.sqlite_store import SqliteMemoryStore
    from whale_core.memory.store import MemoryKind, MemoryRecord

    store = SqliteMemoryStore(tmp_path / "memory.sqlite") if sqlite else InMemoryStore()
    migrate_legacy_to_vault(store)
    rows = [
        MemoryRecord(MemoryKind.EPISODE, "phone", {"conversation_id": "glasses:s1"}, tags=("chat", "other")),
        MemoryRecord(MemoryKind.EPISODE, "qq", {"conversation_id": "group:123"}, tags=("chat",)),
        MemoryRecord(MemoryKind.EPISODE, "not-chat", {"conversation_id": "glasses:s1"}, tags=("other",)),
        MemoryRecord(MemoryKind.PREFERENCE, "preference", {"conversation_id": "glasses:s1"}, tags=("chat",)),
        MemoryRecord(MemoryKind.EPISODE, "member", {"conversation_id": "glasses:s1"}, tags=("chat",), space_id="member-space"),
        MemoryRecord(MemoryKind.EPISODE, "vault", {"conversation_id": "glasses:s1"}, tags=("chat",), space_id="vault-default"),
        MemoryRecord(MemoryKind.EPISODE, "already-owner", {"conversation_id": "glasses:s1"}, tags=("chat",), space_id="owner-working"),
        MemoryRecord(MemoryKind.EPISODE, "missing-conversation", {}, tags=("chat",)),
        MemoryRecord(MemoryKind.EPISODE, "invalid-conversation", {"conversation_id": 123}, tags=("chat",)),
    ]
    for row in rows:
        store.put_record(row)
    assert migrate_legacy_glasses_episodes(store) == 1
    assert store.get_record("phone") == replace(rows[0], space_id="owner-working")
    for row in rows[1:]:
        assert store.get_record(row.key) == row
    assert migrate_legacy_glasses_episodes(store) == 0
    ctx = resolve(build_glasses_context_resolver(store))
    found = store.list_by_kind(MemoryKind.EPISODE, space_ids=tuple(
        space.space_id for space in ctx.allowed_memory_spaces))
    assert {record.key for record in found} == {"phone", "already-owner"}
    if sqlite:
        store.close()


@pytest.mark.parametrize("binding", ["valid-qq", "missing-qq", "missing-session", "no-global"])
def test_bound_persona_reaches_model_with_defined_fallback(binding):
    from whale_core.core.companion import WhaleCompanion
    from whale_core.ui.persona_cards import CARD_KEY_PREFIX, PersonaCard, switch_card

    class Model:
        def reply(self, text, **kwargs):
            self.persona = kwargs["persona"]
            return "收到"

    store = InMemoryStore()
    global_card = PersonaCard(card_id="global", title="global", name="全局鲸鲸")
    qq_card = PersonaCard(card_id="qq", title="qq", name="私聊鲸鲸")
    for card in (global_card, qq_card):
        store.put(CARD_KEY_PREFIX + card.card_id, card.as_dict())
    if binding != "no-global":
        switch_card(store, "global")
    bindings = BindingStore(store, SpaceStore(store))
    bindings.bind_persona(OWNER_QQ_KEY, "deleted" if binding in ("missing-qq", "no-global") else "qq",
                          updated_by="owner")
    if binding == "missing-session":
        bindings.bind_persona(PHONE_KEY, "deleted-session", updated_by="owner")
    ctx = resolve(build_glasses_context_resolver(store, "123"))
    model = Model()
    WhaleCompanion(store, model).chat("你好", conversation_id="glasses:s1", context=ctx)
    if binding == "valid-qq":
        assert "私聊鲸鲸" in model.persona
        assert "全局鲸鲸" not in model.persona
    elif binding == "no-global":
        assert "全局鲸鲸" not in model.persona and "私聊鲸鲸" not in model.persona
    else:
        # A stale explicit session binding falls back globally, not to the QQ card.
        assert "全局鲸鲸" in model.persona
        assert "私聊鲸鲸" not in model.persona
