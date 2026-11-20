"""Durably collect group utterances and materialize them on an owner's explicit refresh."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import Lock

from whale_core.memory import group_keywords
from whale_core.memory.group_directory import load_directory, roster_members, atomic_write, directory_path, account_names
from whale_core.memory.consent import MemberConsentStore
from whale_core.memory.group_archive import GroupArchive
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.types import MemorySpaceType
from whale_core.memory.group_portraits import COMMAND, SHANGHAI
from whale_core.memory.group_replay import replay
from whale_core.memory.group_topics import publish, summarize
from whale_core.memory.pause import is_recording_paused


def create_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS group_refresh_settings (
        group_id TEXT PRIMARY KEY, extractor TEXT NOT NULL)""")
    # NULL terms = captured and awaiting the next manual refresh; '{}' = processed with no keywords.
    db.execute("""CREATE TABLE IF NOT EXISTS group_features (
        group_id TEXT NOT NULL, message_id TEXT NOT NULL, origin TEXT NOT NULL, seq TEXT NOT NULL,
        authored TEXT NOT NULL, reply_to TEXT NOT NULL, terms TEXT, PRIMARY KEY(group_id,message_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS group_feature_seq ON group_features(group_id,seq)")


    db.execute("CREATE TABLE IF NOT EXISTS group_digest(group_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS group_digest_messages(group_id TEXT,message_id TEXT,segment_id TEXT,"
               "sender_id TEXT,PRIMARY KEY(group_id,message_id))")
    db.execute("CREATE INDEX IF NOT EXISTS digest_by_segment ON group_digest_messages(group_id,segment_id)")
    db.execute("CREATE INDEX IF NOT EXISTS digest_by_person ON group_digest_messages(group_id,sender_id)")


def enable(store, archive, gid, roster):
    if not gid.isdigit() or roster.get("group_id") != gid or not roster.get("members"):
        raise ValueError("需要匹配群号和成员名单")
    roster.setdefault("bots", [])
    entries = roster["members"] + roster["bots"]
    if (len({m["name"] for m in entries}) != len(entries) or
            any(not m["accounts"] or not m["name"] or Path(m["name"]).name!=m["name"]
                or not isinstance(m.get("aliases"),list) for m in entries)
            or len(account_names(roster)) != sum(len(m["accounts"]) for m in entries)):
        raise ValueError("成员名称和账号必须唯一")
    spaces = SpaceStore(store)
    sid = "group-" + gid
    ref = spaces.ref(sid)
    if ref is None:
        spaces.create(MemorySpaceType.GROUP_SPACE, name=roster["name"], owner="owner", space_id=sid)
    elif ref.type != MemorySpaceType.GROUP_SPACE:
        raise ValueError("目标不是群空间")
    existing = load_directory(archive, gid)
    if existing and existing != roster:
        raise ValueError("目标群已经配置了不同的成员名单")
    atomic_write(directory_path(archive,gid)/"群资料.json",roster)
    with closing(GroupArchive(archive)) as history, history.db as db:
        create_tables(db)
        if status(db,gid) is None:
            engine,_ = group_keywords.extract([])
            payload,_ = summarize(gid,[],[],engine)
            db.execute("INSERT INTO group_refresh_settings VALUES(?,?)",(gid,engine))
            publish(db,gid,payload,[])


def status(db, gid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='group_refresh_settings'").fetchone():
        return None
    if not db.execute("SELECT 1 FROM group_refresh_settings WHERE group_id=?", (gid,)).fetchone():
        return None
    return db.execute("SELECT count(*) FROM group_features WHERE group_id=? AND terms IS NULL", (gid,)).fetchone()[0]


def stage(store, archive, gid, rows):
    """Queue normalized messages in one transaction. Empty/duplicate inputs do not alter the digest."""
    if not archive.is_file() or is_recording_paused(store,"group-"+gid):
        return 0
    roster=load_directory(archive,gid)
    if not roster:
        return 0
    accounts={a:m["accounts"] for m in roster["members"] for a in m["accounts"]}
    consents=MemberConsentStore(store)
    inserted=0
    with closing(sqlite3.connect(archive,timeout=10)) as db, db:
        if status(db,gid) is None:
            return 0
        for mid,stamp,uid,name,text,reply,seq in rows:
            if not mid or not uid or not text.strip():
                continue
            if any(consents.is_opted_out("anon:qq:"+a) for a in accounts.get(uid,[uid])):
                continue
            if seq and db.execute("""SELECT 1 FROM group_features JOIN messages USING(group_id,message_id)
                    WHERE group_id=? AND seq=? AND sender_id=? AND strftime('%s',sent_at)=strftime('%s',?)""",
                    (gid,seq,uid,stamp)).fetchone():
                continue
            changed=db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)",
                               (gid,mid,stamp,uid,name[:80],text[:4000])).rowcount
            if changed:
                db.execute("INSERT INTO group_features VALUES(?,?,'group',?,?,?,NULL)",(gid,mid,seq,text[:4000],reply))
                inserted+=1
    return inserted


def export_rows(path,gid):
    data=json.loads(path.read_text(encoding="utf-8-sig"))
    chat=data["chatInfo"]
    if chat.get("type")!="group" or str(chat.get("peerUid"))!=gid:
        raise ValueError("导出文件与目标群不符")
    for message in data["messages"]:
        if message.get("recalled") or message.get("type") not in ("text","reply"):
            continue
        sender=message.get("sender") or {}
        content=message["content"]
        elements=content.get("elements")
        text="".join(e["data"].get("text","") for e in elements if e["type"]=="text") if elements is not None else (
            content.get("text","") if message["type"]=="text" else "")
        reply=next((str(e["data"].get("referencedMessageId") or "") for e in elements or [] if e["type"]=="reply"),"")
        stamp=datetime.fromtimestamp(message["timestamp"]/1000,timezone.utc).isoformat()
        yield (str(message.get("id") or ""),stamp,str(sender.get("uin") or sender.get("uid") or ""),
               str(sender.get("name") or sender.get("nickname") or ""),text,reply,str(message.get("seq") or ""))


def stage_timeline(store, conversation, mid, uid, text, send_time, refs):
    path=getattr(store,"path",None)
    if not path or not conversation.startswith("group:"):
        return
    gid=conversation[6:]
    if not gid.isdigit():
        return
    archive=Path(path).with_name("group_archive.sqlite3")
    if not archive.is_file():
        return
    value=str(send_time or "")
    try:
        if value.isdecimal():
            ts=int(value)
            moment=datetime.fromtimestamp(ts/1000 if ts >= 10**12 else ts,timezone.utc)
        else:
            moment=datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc)
    except (ValueError,OverflowError,OSError):
        moment=datetime.now(timezone.utc)
    reply=str(refs[2] if len(refs)>=3 else refs[0]) if refs else ""
    stage(store,archive,gid,[(str(mid),moment.isoformat(),str(uid),"",text,reply,"")])


def speech_row(mid, uid, stamp, text, reply, expected):
    if not text or COMMAND.match(text) or "://" in text:
        return None
    at = datetime.fromisoformat(stamp).astimezone(SHANGHAI)
    return dict(message_id=mid, sender_id=uid, sent_at=at.isoformat(), day=at.date().isoformat(),
                text=text, reply_to=reply if reply in expected else "")


def tag_batch(archive, texts, expected):
    engine, terms = group_keywords.extract(texts)
    if engine != expected or len(terms) != len(texts):
        raise ValueError("分词环境与现有群索引不一致，请核对 Studio 服务的分词环境")
    return terms


# Studio runs one server per instance. Do not process a second snapshot while the first is publishing.
_refresh_lock = Lock()


class RefreshInProgress(Exception):
    pass


def refresh(store, archive, gid):
    if not _refresh_lock.acquire(blocking=False):
        raise RefreshInProgress()
    try:
        with closing(sqlite3.connect(archive, timeout=10)) as db:
            pending = status(db, gid)
            if pending is None:
                raise ValueError("本群尚未配置手动更新")
            if is_recording_paused(store, "group-" + gid):
                raise ValueError("群空间已暂停记录")
            if not pending:
                return {"processed": 0, "pending": 0}
            roster = load_directory(archive, gid)
            if not roster:
                raise ValueError("本群缺少成员名单")
            (engine,) = db.execute("SELECT extractor FROM group_refresh_settings WHERE group_id=?",
                                       (gid,)).fetchone()
            # A single snapshot. Events collected while this runs remain pending for the next click.
            rows = db.execute("""SELECT m.message_id,m.sent_at,m.sender_id,m.sender_name,
                f.authored,f.reply_to,f.terms FROM messages m JOIN group_features f USING(group_id,message_id)
                WHERE m.group_id=? ORDER BY m.sent_at,m.message_id""", (gid,)).fetchall()
            fresh = [r for r in rows if r[6] is None]
            bots = {a for b in roster["bots"] for a in b["accounts"]}
            batch = dict(zip((r[0] for r in fresh), tag_batch(archive, [r[4] if r[2] not in bots else ""
                        for r in fresh], engine), strict=True))
            tokens = [batch[r[0]] if r[6] is None else json.loads(r[6]) for r in rows]
            payload, links = summarize(gid, [r[:5] for r in rows], tokens, engine)
            expected = {r[0]:r[2] for r in rows}
            speech = [s for mid,stamp,uid,_,text,reply,_ in rows
                      if (s := speech_row(mid,uid,stamp,text,reply,expected)) is not None]
            inputs = roster, speech, roster_members(payload["members"], roster), expected
            counts = replay(store,gid,inputs,pending_ids=batch.keys(),portrait_archive=archive)
            if counts.get("group_paused"):
                raise ValueError("群空间已暂停记录")
            # Fact decisions use stable evidence IDs and snapshots use per-member keys.
            # An interruption before this checkpoint can safely replay the same batch.
            with db:
                publish(db, gid, payload, links)
                db.executemany("UPDATE group_features SET terms=? WHERE group_id=? AND message_id=? AND terms IS NULL",
                               [(json.dumps(t,ensure_ascii=False),gid,mid) for mid,t in batch.items()])
            return {"processed": len(batch), "pending": status(db,gid), "counts": counts}
    finally:
        _refresh_lock.release()
