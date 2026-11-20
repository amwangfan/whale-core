from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.types import MemorySpaceType
from whale_core.api.studio.memory_graph import build_graph, graph
from whale_core.api.studio.shared import StudioError


def test_graph_preserves_explicit_edges_and_scope():
    store = InMemoryStore()
    for key, space, content in [
        ('old', 'a', {'summary': '旧事实', 'status': 'revoked', 'superseded_by': 'new'}),
        ('new', 'a', {'summary': '新事实', 'subject_id': 'owner', 'evidence_id': 'qq:1', 'depends_on_keys': ['old', 'secret']}),
        ('secret', 'b', {'summary': '不可见'})]:
        store.put_record(MemoryRecord(kind=MemoryKind.PREFERENCE, key=key, content=content,
                                     created_at=datetime.now(timezone.utc), space_id=space))
    result = build_graph(store, ('a',))
    assert result['total_records'] == 2
    assert not any(n['label'] == '不可见' for n in result['nodes'])
    assert {l['label'] for l in result['links']} >= {'主体', '依据', '更正为', '依赖'}
    assert all(l['source'] in {n['id'] for n in result['nodes']} and
               l['target'] in {n['id'] for n in result['nodes']} for l in result['links'])
    limited = build_graph(store, ('a',), limit=1)
    assert limited['truncated'] and limited['shown_records'] == 1


def test_graph_rejects_vault():
    store = InMemoryStore()
    spaces = SimpleNamespace(ref=lambda sid: SimpleNamespace(type=MemorySpaceType.PROTECTED_ORIGINAL))
    with pytest.raises(StudioError) as exc:
        graph(SimpleNamespace(query={'space_id': ['vault']}), SimpleNamespace(store=store), SimpleNamespace(spaces=spaces))
    assert exc.value.status == 403
