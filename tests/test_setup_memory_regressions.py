"""Regressions for Studio memory write ACL and atomic Fork."""
import pytest

from whale_core.core.companion import _recall, WhaleCompanion
from whale_core.life.context_resolver import ConversationContextResolver, pairing_lookup_from_mapping
from whale_core.memory.bindings import BindingStore
from whale_core.memory.fork import ForkService
from whale_core.memory.spaces import SpaceStore, default_write_space, ensure_owner_working_copy
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.life.conversation_context import MemorySpaceType


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        db = SqliteMemoryStore(tmp_path / "test.sqlite3")
        try:
            yield db
        finally:
            db.close()


def test_read_only_group_binding_does_not_gain_write_permission(store):
    spaces = SpaceStore(store)
    space = spaces.create(MemorySpaceType.GROUP_SPACE, name="class")
    bindings = BindingStore(store, spaces)
    bindings.bind_memory("qq:qq_group:group:7", space.space_id,
                         can_read=True, can_write=False, granted_by="owner")
    refs = bindings.memory_spaces_for("qq:qq_group:group:7")
    assert len(refs) == 1
    assert refs[0].can_write is False
    assert default_write_space(refs) == ""
    bindings.bind_memory("qq:qq_group:group:7", space.space_id,
                         can_read=True, can_write=True, granted_by="owner")
    refs = bindings.memory_spaces_for("qq:qq_group:group:7")
    assert refs[0].can_write is True
    assert default_write_space(refs) == space.space_id


def test_owner_default_private_copy_remains_writable(store):
    ref = ensure_owner_working_copy(store)
    assert ref.can_write is True
    assert default_write_space((ref,)) == ref.space_id
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "owner1"): "owner"}),
        bindings=BindingStore(store, SpaceStore(store)),
        owner_default_space=ref, owner_id="owner")
    private = resolver.resolve(channel="qq", sender_id="owner1",
                               conversation_id="private:owner1")
    group = resolver.resolve(channel="qq", sender_id="owner1",
                             conversation_id="group:11")
    assert default_write_space(private.allowed_memory_spaces) == ref.space_id
    assert default_write_space(group.allowed_memory_spaces) == ""


def test_authorized_recall_does_not_fall_back_to_unscoped_search():
    class LegacyCurator:
        def __init__(self):
            self.calls = []

        def recall(self, query, *, limit=5):
            self.calls.append(query)
            return ()

    curator = LegacyCurator()
    with pytest.raises(TypeError):
        _recall(curator, "secret", (), limit=5)
    assert curator.calls == []
    assert _recall(curator, "public", None, limit=5) == ()
    assert curator.calls == ["public"]


def test_fork_batch_failure_rolls_back_all_records_and_space(store):
    spaces = SpaceStore(store)
    source = spaces.create(MemorySpaceType.OWNER_WORKING_COPY, name="private")
    for k in ("first", "second"):
        store.put_record(MemoryRecord(
            kind=MemoryKind.PREFERENCE, key=k,
            content={"summary": k}, tags=("curated",), space_id=source.space_id,
        ))
    original = set(store.keys())

    def redact(content):
        if content["summary"] == "second":
            raise RuntimeError("redaction failed midway")
        return content

    with pytest.raises(RuntimeError, match="midway"):
        ForkService(store, spaces).fork(
            source.space_id, ("first", "second"),
            target_type=MemorySpaceType.SHAREABLE_FORK,
            target_name="partial", created_by="owner",
            confirmed=True, redact=redact,
        )
    assert set(store.keys()) == original
    assert all(not r.key.startswith("sp:sp-")
               for r in store.list_by_kind(MemoryKind.PREFERENCE))
