"""Read-only graph projection of stored memory; edges never infer new facts."""
import re
import sqlite3
from pathlib import Path
from whale_core.api.studio.memory_browse import _resolve_space_filter, _record_for_owner_op
from whale_core.api.studio.shared import StudioError, _ok
from whale_core.memory.store import MemoryKind
from whale_core.memory.types import MemorySpaceType


def build_graph(store, space_ids, *, limit=300):
    records = [r for k in MemoryKind for r in store.list_by_kind(k, space_ids=space_ids)]
    records.sort(key=lambda r: (r.created_at, r.key), reverse=True)
    total = len(records)
    records = records[:limit]
    nodes, edges = {}, []
    def node(node_id, label, category, **extra):
        nodes.setdefault(node_id, dict(id=node_id, label=label, category=category, **extra))
    def edge(source, target, label):
        edges.append(dict(source=source, target=target, label=label))
    keys = {r.key for r in records}
    for r in records:
        c = r.content
        mid = 'memory:' + r.key
        status = str(c.get('status') or ('superseded' if 'superseded' in r.tags else 'active'))
        node(mid, str(c.get('summary') or c.get('text') or r.key), 'memory',
             key=r.key, kind=r.kind.value, status=status, space_id=r.space_id,
             created_at=r.created_at.isoformat(), source=str(c.get('source') or ''),
             role=str(c.get('memory_role') or ''), expires_at=c.get('expires_at'),
             confidence=c.get('confidence'), observed_at=c.get('observed_at'))
        sid = 'space:' + r.space_id
        node(sid, r.space_id or '未分区', 'space')
        edge(sid, mid, '包含')
        subject = str(c.get('subject_id') or '')
        if subject:
            pid = 'person:' + r.space_id + ':' + subject
            node(pid, str(c.get('display_name') or subject), 'person')
            edge(pid, mid, '主体')
        evidence = list(c.get('evidence_ids') or [])
        if c.get('evidence_id'):
            evidence.append(c['evidence_id'])
        for value in dict.fromkeys(str(v) for v in evidence if v):
            eid = 'evidence:' + r.space_id + ':' + value
            node(eid, value, 'evidence', record_key=r.key, evidence_id=value)
            edge(mid, eid, '依据')
        for target in c.get('depends_on_keys') or []:
            if target in keys:
                edge(mid, 'memory:' + target, '依赖')
        target = c.get('superseded_by')
        if target in keys:
            edge(mid, 'memory:' + target, '更正为')
    return dict(nodes=list(nodes.values()), links=edges, total_records=total,
                shown_records=len(records), truncated=total > limit)


def graph(handler, ctx, svc):
    spaces = _resolve_space_filter(handler, ctx, svc)
    if any(svc.spaces.ref(s) is None for s in spaces):
        raise StudioError(404, 'not_found', '空间不存在或已注销')
    if any(svc.spaces.ref(s).type is MemorySpaceType.PROTECTED_ORIGINAL for s in spaces):
        raise StudioError(403, 'vault_restricted', '原始库请使用专用脱敏视图')
    if len(spaces) != 1 or svc.spaces.ref(spaces[0]).type != MemorySpaceType.GROUP_SPACE:
        raise StudioError(403, 'group_only', '请选择一个群空间')
    handler._send(200, _ok(build_graph(ctx.store, spaces)))


def evidence(handler, ctx, svc):
    """Owner management only; validate a stored reference before reading an archive row."""
    key = str(handler.query.get('record_key', [''])[0])
    evidence_id = str(handler.query.get('evidence_id', [''])[0])
    if not key or not evidence_id or len(key) > 500 or len(evidence_id) > 500:
        raise StudioError(422, 'invalid_request', '需要记忆记录及证据标识')
    record = _record_for_owner_op(ctx, svc, key)
    ref = svc.spaces.ref(record.space_id)
    if not ref or ref.type != MemorySpaceType.GROUP_SPACE:
        raise StudioError(403, 'group_only', '证据仅供对应群空间查看')
    refs = record.content.get('evidence_ids') or []
    if evidence_id != record.content.get('evidence_id') and evidence_id not in refs:
        raise StudioError(404, 'not_found', '此记忆没有关联该证据')
    match = re.fullmatch(r'qq:group:(\d+):(.+)', evidence_id)
    if not match:
        handler._send(200, _ok({'available': False, 'reason': 'unsupported_source',
                               'message': '此证据不是已导入的 QQ 群聊来源'}))
        return
    if record.space_id.startswith('group-') and record.space_id != 'group-' + match[1]:
        raise StudioError(404, 'not_found', '此来源不属于该群记忆')
    database = getattr(ctx.store, 'path', None)
    path = Path(database).with_name('group_archive.sqlite3') if database else None
    row = None
    if path and path.is_file():
        # mode=ro cannot create or mutate an archive while browsing.
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            row = db.execute('SELECT group_id,message_id,sender_id,sender_name,sent_at,text '
                             'FROM messages WHERE group_id=? AND message_id=?', match.groups()).fetchone()
        finally:
            db.close()
    if row is None:
        handler._send(200, _ok({'available': False, 'reason': 'source_missing',
                               'message': '原始群聊未导入或已删除，无法核验原话'}))
        return
    if record.kind is MemoryKind.GROUP_PROFILE and row[2] not in record.content.get('accounts', []):
        raise StudioError(404, 'not_found', '来源发送者与画像不符')
    handler._send(200, _ok({'available': True, 'evidence_id': evidence_id,
                           **dict(zip(('group_id', 'message_id', 'sender_id', 'sender_name',
                                       'sent_at', 'text'), row)),
                           'archive_note': '显示本地归档文本；旧导入最多保留每条 4000 字，图片和语音未转录。'}))
