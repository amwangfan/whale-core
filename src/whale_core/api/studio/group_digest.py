"""Owner console view of conversation segments and per-sender transcripts."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from subprocess import SubprocessError

from whale_core.memory import group_refresh

from whale_core.api.studio.shared import StudioError, _ok
from whale_core.memory.types import MemorySpaceType
from whale_core.memory.group_preferences import profile_key, saved_profile, scoped_claims
from whale_core.memory.group_directory import account_names, load_directory, read_portrait, roster_members


def attach_claims(payload, gid, db, ctx):
    people = {p['id']:p for p in payload['members']}
    for person in people.values():
        person['claims'] = []
        record = saved_profile(ctx.store, gid, person['id'])
        if record:
            person['stored_profile'] = dict(key=record.key, **record.content)
        elif ctx.store.get(profile_key(gid,person['id']) + ':imported'):
            person['portrait'] = None
    for member, claim in scoped_claims(ctx.store, db, gid, payload['members']):
        people[member]['claims'].append(claim)


def group_digest(handler, ctx, svc, *, update=False):
    def param(name):
        return handler.query.get(name, [''])[0]

    gid = param('group_id')
    ref = svc.spaces.ref('group-' + gid) if gid.isdigit() else None
    if not ref or ref.type != MemorySpaceType.GROUP_SPACE:
        raise StudioError(404, 'not_found', '群空间不存在')
    path = getattr(ctx.store, 'path', None)
    archive = Path(path).with_name('group_archive.sqlite3') if path else None
    if not archive or not archive.is_file():
        raise StudioError(404, 'not_found', '本群还没有聊天归档')
    if update:
        try:
            result = group_refresh.refresh(ctx.store, archive, gid)
        except group_refresh.RefreshInProgress:
            raise StudioError(409, 'busy', '群记忆正在更新，请稍后查看')
        except ValueError as exc:
            raise StudioError(409, 'unavailable', str(exc))
        except (SubprocessError, OSError, sqlite3.Error):
            raise StudioError(503, 'update_failed', '群记忆更新未完成，待处理发言已保留，可以重试')
        handler._send(200, _ok(result))
        return
    with closing(sqlite3.connect(archive.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_digest'").fetchone():
            raise StudioError(404, 'not_found', '尚未生成群聊索引')
        row = db.execute('SELECT payload FROM group_digest WHERE group_id=?', (gid,)).fetchone()
        if row is None:
            raise StudioError(404, 'not_found', '尚未生成该群索引')
        roster = load_directory(archive, gid)
        sender, segment = param('sender_id'), param('segment_id')
        if param('around'):
            mid = param('around')
            hit = db.execute('SELECT sent_at FROM messages JOIN group_digest_messages USING(group_id,message_id) '
                             'WHERE group_id=? AND message_id=?', (gid,mid)).fetchone()
            if not hit:
                raise StudioError(404, 'not_found', '本群没有这条来源发言')
            db.row_factory = sqlite3.Row
            source = ('SELECT m.message_id,m.sent_at,m.sender_name,m.sender_id,m.text FROM messages m '
                      'JOIN group_digest_messages i USING(group_id,message_id) WHERE m.group_id=? '
                      'AND (m.sent_at,m.message_id) ')
            bound = (gid,hit[0],mid)
            before = db.execute(source + '< (?,?) ORDER BY m.sent_at DESC,m.message_id DESC LIMIT 1',bound).fetchall()
            after = db.execute(source + '>= (?,?) ORDER BY m.sent_at,m.message_id LIMIT 2',bound).fetchall()
            names = account_names(roster) if roster else {}
            items = [dict(r,display_name=names.get(r['sender_id'],r['sender_name'])) for r in before + after]
            handler._send(200, _ok(dict(items=items,more=False)))
            return
        if not (sender or segment):
            payload = json.loads(row[0])
            payload["pending_messages"] = group_refresh.status(db, gid)
            if roster:
                accounts = payload['members']
                payload['members'] = roster_members(accounts, roster)
                for p, member in zip(payload['members'], roster['members']):
                    p['portrait'] = read_portrait(archive, gid, member) if saved_profile(ctx.store,gid,p['id']) else None
                payload['group'] = dict(name=roster['name'], description=roster['description'],
                                        people=len(roster['members']), archived_accounts=len(accounts),
                                        bots=[dict(name=b['name'], aliases=b['aliases'],
                                                   count=sum(p['count'] for p in accounts if p['id'] in b['accounts']))
                                              for b in roster['bots']])
            attach_claims(payload, gid, db, ctx)
            handler._send(200, _ok(payload))
            return
        try:
            offset = max(0, int(param('offset') or '0'))
        except ValueError:
            raise StudioError(422, 'invalid_request', '页码无效')
        where, args = ['i.group_id=?'], [gid]
        if sender:
            ids = next((p['accounts'] for p in (roster or {}).get('members', []) if p['accounts'][0] == sender), [sender])
            where.append('i.sender_id IN (' + ','.join('?' for _ in ids) + ')')
            args.extend(ids)
        if segment:
            where.append('i.segment_id=?')
            args.append(segment)
        if param('word'):
            sources = None
            if segment:
                s = next((item for item in json.loads(row[0])['segments'] if item['id']==segment), {})
                sources = s.get('keyword_sources', {}).get(param('word'))
            if sources:
                where.append('i.message_id IN (' + ','.join('?' for _ in sources) + ')')
                args.extend(sources)
            else:
                where.append('instr(lower(m.text),lower(?))>0')
                args.append(param('word'))
        db.row_factory = sqlite3.Row
        query = ('SELECT m.message_id,m.sent_at,m.sender_name,m.sender_id,m.text FROM group_digest_messages i '
                 'JOIN messages m USING(group_id,message_id) WHERE ' + ' AND '.join(where)
                 + ' ORDER BY m.sent_at,m.message_id LIMIT 51 OFFSET ?')
        rows = db.execute(query, [*args,offset]).fetchall()
        names = account_names(roster) if roster else {}
        items = [dict(r, display_name=names.get(r['sender_id'], r['sender_name'])) for r in rows[:50]]
        handler._send(200, _ok(dict(items=items, more=len(rows)>50)))
