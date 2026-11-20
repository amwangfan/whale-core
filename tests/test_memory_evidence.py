import json
import sqlite3
from datetime import datetime, timezone
from http.client import HTTPConnection
from urllib.parse import urlencode

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.group_archive import GroupArchive
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import MemoryRecord, MemoryKind


def test_evidence_http_authorization_reference_and_exact_source(tmp_path):
    store = SqliteMemoryStore(tmp_path / 'memory.sqlite3')
    store.put('space:group-123', {'space_id': 'group-123', 'type': 'group_space', 'name': '群'})
    store.put('space:vault', {'space_id': 'vault', 'type': 'protected_original', 'name': '原始库'})
    for key, space, eid in [('fact', 'group-123', 'qq:group:123:m1'), ('vault-record', 'vault', 'qq:group:123:m1'),
                            ('missing', 'group-123', 'qq:group:123:gone'), ('other', 'group-123', 'chat:123')]:
        store.put_record(MemoryRecord(kind=MemoryKind.PREFERENCE, key=key, space_id=space,
                         content={'summary': '偏好', 'evidence_id': eid}, created_at=datetime.now(timezone.utc)))
    archive = GroupArchive(tmp_path / 'group_archive.sqlite3')
    archive.db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?)', [
        ('123', 'm1', '2026-11-20T01:02:03+00:00', '77', '群友', '<script>这是原话</script>'),
        ('456', 'm1', '2026-11-20T01:02:03+00:00', '88', '另一人', '不应误查同 ID 的另一群消息')])
    archive.db.commit(); archive.close()
    server = WhaleApiServer(ApiContext(store=store), port=0, token='test-token')
    server.start()
    def get(key, eid, auth=True):
        conn = HTTPConnection('127.0.0.1', server.port, timeout=5)
        conn.request('GET', '/api/v1/studio/memory/evidence?' + urlencode({'record_key': key, 'evidence_id': eid}),
                     headers={'Authorization':'Bearer test-token'} if auth else {})
        response = conn.getresponse()
        status, data = response.status, json.loads(response.read())
        conn.close()
        return status, data
    try:
        assert get('fact', 'qq:group:123:m1', auth=False)[0] == 401
        assert get('fact', 'qq:group:456:m1')[0] == 404
        assert get('vault-record', 'qq:group:123:m1')[0] == 403
        status, body = get('fact', 'qq:group:123:m1')
        assert status == 200
        assert body['result']['sender_id'] == '77'
        assert body['result']['text'] == '<script>这是原话</script>'
        assert body['result']['sent_at'] == '2026-11-20T01:02:03+00:00'
        assert get('missing', 'qq:group:123:gone')[1]['result']['available'] is False
        assert get('other', 'chat:123')[1]['result']['reason'] == 'unsupported_source'
        store.delete('fact')
        assert get('fact', 'qq:group:123:m1')[0] == 404
    finally:
        server.stop(); store.close()
