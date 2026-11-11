"""Authenticated phone context for glasses sessions.

The caller must authenticate the phone token before setting channel and sender
both to ``phone``. The optional QQ uid must come from server configuration.
"""
from dataclasses import replace

from whale_core.life.context_resolver import (
    ConversationContextResolver, pairing_lookup_from_mapping,
)
from whale_core.memory.bindings import BindingStore
from whale_core.memory.spaces import SpaceStore, ensure_owner_working_copy
from whale_core.memory.store import MemoryKind


def migrate_legacy_glasses_episodes(store):
    """Move unscoped chat episodes from the historically single-owner phone service.

    Explicitly invoked by the host; returns the number moved. No QQ, other
    channels, already scoped records, or protected vault records are migrated.
    """
    candidates = store.list_by_kind(MemoryKind.EPISODE, space_ids=("",))
    owner_space = ensure_owner_working_copy(store, owner="owner")

    def migrate(tx):
        moved = 0
        for candidate in candidates:
            record = tx.get_record(candidate.key)
            if (record is not None and record.kind == MemoryKind.EPISODE
                    and record.space_id == "" and "chat" in record.tags
                    and isinstance(record.content.get("conversation_id"), str)
                    and record.content["conversation_id"].startswith("glasses:")):
                tx.put_record(replace(record, space_id=owner_space.space_id))
                moved += 1
        return moved

    return store.transact(migrate)


class _GlassesBindings(BindingStore):
    def __init__(self, store, spaces, owner_qq_uid):
        super().__init__(store, spaces)
        self._owner_qq_key = (
            f"qq:qq_private:private:{owner_qq_uid}" if owner_qq_uid else None
        )

    def persona_for(self, conversation_key):
        persona = super().persona_for(conversation_key)
        if persona is None and self._owner_qq_key and conversation_key.startswith(
                "phone:voice:glasses:"):
            persona = super().persona_for(self._owner_qq_key)
        return persona


def build_glasses_context_resolver(store, owner_qq_uid=None):
    """Fall back to the configured QQ persona, not its memory or history."""
    spaces = SpaceStore(store)
    bindings = _GlassesBindings(store, spaces, (owner_qq_uid or "").strip())
    return ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("phone", "phone"): "owner"}),
        bindings=bindings,
        owner_id="owner",
        owner_default_space=ensure_owner_working_copy(store, owner="owner"),
    )
