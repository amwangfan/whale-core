"""Group memory spaces (ported from PR #95 onto the v1 core).

A group space is one chat group the owner chose to remember. Messages are
archived locally as they arrive (or imported from a QQ Chat Exporter file);
nothing is analysed until a refresh — manual by default, optionally on a
schedule. A refresh turns the archive into:

* a digest: topic segments, keywords, member activity (``group_digest``);
* one portrait memory per member (``kind=person``) and explicit preference
  memories (``kind=preference``), all in scope ``group:<platform>:<gid>``
  with ``subject=<platform>:<account>`` and message-id evidence.

Scopes keep the boundary from MEMORY.md: the group AI sees its own group,
the owner's conversations see every group, nothing flows the other way.
Original messages never enter prompts; they are only shown as evidence.
Opted-out members are not archived, not portrayed, and what was stored about
them is deleted. A paused space archives nothing and refuses refreshes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from ..kernel.db import Database, dumps, loads, migration
from ..kernel.errors import WhaleError, invalid, not_found
from ..kernel.ids import now_ms
from ..link.protocol import Client, NoParams
from . import analysis

log = logging.getLogger("whale.groups")

PREFERENCE_TTL_MS = 180 * 86_400_000
MAX_IMPORT = 20 * 1024 * 1024
MAX_TEXT = 4000

migration(
    "groups.001",
    """
    CREATE TABLE IF NOT EXISTS group_spaces (
        id TEXT PRIMARY KEY, platform TEXT NOT NULL, group_id TEXT NOT NULL, name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', roster TEXT NOT NULL, opted_out TEXT NOT NULL DEFAULT '[]',
        paused INTEGER NOT NULL DEFAULT 0, auto_refresh_min INTEGER NOT NULL DEFAULT 0,
        extractor TEXT NOT NULL DEFAULT '', refreshed_at INTEGER, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS group_messages (
        space TEXT NOT NULL, message_id TEXT NOT NULL, sent_at INTEGER NOT NULL, sender_id TEXT NOT NULL,
        sender_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, reply_to TEXT NOT NULL DEFAULT '',
        seq TEXT NOT NULL DEFAULT '', terms TEXT, segment_id TEXT, PRIMARY KEY (space, message_id));
    CREATE INDEX IF NOT EXISTS group_messages_time ON group_messages(space, sent_at, message_id);
    CREATE INDEX IF NOT EXISTS group_messages_sender ON group_messages(space, sender_id);
    CREATE INDEX IF NOT EXISTS group_messages_segment ON group_messages(space, segment_id);
    CREATE TABLE IF NOT EXISTS group_digest (space TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS group_memory (
        space TEXT NOT NULL, key TEXT NOT NULL, memory_id TEXT NOT NULL, account TEXT NOT NULL,
        observed_at INTEGER NOT NULL, PRIMARY KEY (space, key));
    CREATE TABLE IF NOT EXISTS group_seen (space TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY (space, evidence));
    """,
)


# -- roster validation ------------------------------------------------------------------------------
class RosterEntry(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    accounts: list[str] = Field(min_length=1, max_length=8)
    aliases: list[str] = Field(default_factory=list, max_length=12)


def clean_roster(members: list[Any], bots: list[Any]) -> dict[str, Any]:
    entries = []
    for kind, rows in (("members", members), ("bots", bots)):
        for row in rows:
            e = row if isinstance(row, RosterEntry) else RosterEntry.model_validate(row)
            accounts = [str(a).strip() for a in e.accounts if str(a).strip()]
            if not accounts or any(not a.isdigit() for a in accounts):
                raise invalid(f"「{e.name}」的 QQ 号必须是数字")
            entries.append((kind, {"name": e.name.strip(), "accounts": accounts,
                                   "aliases": [a.strip() for a in e.aliases if a.strip()][:12]}))
    names = [e["name"] for _, e in entries]
    accounts = [a for _, e in entries for a in e["accounts"]]
    if len(set(names)) != len(names):
        raise invalid("成员名称不能重复")
    if len(set(accounts)) != len(accounts):
        raise invalid("同一个 QQ 号只能属于一个成员")
    if not any(k == "members" for k, _ in entries):
        raise invalid("至少需要一位群成员")
    return {"members": [e for k, e in entries if k == "members"], "bots": [e for k, e in entries if k == "bots"]}


# -- QQ Chat Exporter ----------------------------------------------------------------------------------
def export_rows(data: dict[str, Any], gid: str) -> list[dict[str, Any]]:
    chat = data.get("chatInfo") or {}
    if chat.get("type") != "group" or str(chat.get("peerUid") or chat.get("id") or "") != gid:
        raise invalid("导出文件与目标群不符（需要 QQ Chat Exporter 导出的该群 JSON）")
    out = []
    for message in data.get("messages") or []:
        if message.get("recalled") or message.get("type") not in ("text", "reply"):
            continue
        sender = message.get("sender") or {}
        content = message.get("content") or {}
        elements = content.get("elements")
        if elements is not None:
            text = "".join(str((e.get("data") or {}).get("text") or "") for e in elements if e.get("type") == "text")
            reply = next((str((e.get("data") or {}).get("referencedMessageId") or "") for e in elements
                          if e.get("type") == "reply"), "")
        else:
            text = str(content.get("text") or "") if message.get("type") == "text" else ""
            reply = ""
        try:
            at = int(message["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"message_id": str(message.get("id") or ""), "sent_at": at if at > 10**12 else at * 1000,
                    "sender_id": str(sender.get("uin") or sender.get("uid") or ""),
                    "sender_name": str(sender.get("name") or sender.get("nickname") or ""), "text": text,
                    "reply_to": reply, "seq": str(message.get("seq") or "")})
    return out


# -- RPC params -----------------------------------------------------------------------------------------
class GroupRef(BaseModel):
    id: str = Field(min_length=3, max_length=80)


class GroupEnable(BaseModel):
    platform: str = Field("qq", pattern=r"^[a-z]{2,16}$")
    group_id: str = Field(pattern=r"^\d{4,20}$")
    name: str = Field(min_length=1, max_length=60)
    description: str = Field("", max_length=400)
    members: list[RosterEntry] = Field(min_length=1, max_length=500)
    bots: list[RosterEntry] = Field(default_factory=list, max_length=20)
    auto_refresh_min: int = Field(0, ge=0, le=10080)


class GroupUpdate(BaseModel):
    id: str
    name: str | None = Field(None, min_length=1, max_length=60)
    description: str | None = Field(None, max_length=400)
    members: list[RosterEntry] | None = None
    bots: list[RosterEntry] | None = None
    auto_refresh_min: int | None = Field(None, ge=0, le=10080)


class GroupPause(BaseModel):
    id: str
    paused: bool


class GroupOptOut(BaseModel):
    id: str
    account: str = Field(pattern=r"^\d{4,20}$")
    opted_out: bool


class GroupImport(BaseModel):
    id: str
    content: str = Field(min_length=2, max_length=MAX_IMPORT)


class GroupMessages(BaseModel):
    id: str
    sender: str | None = None
    segment: str | None = None
    word: str | None = Field(None, max_length=40)
    offset: int = Field(0, ge=0)


class GroupContext(BaseModel):
    id: str
    message_id: str


class GroupDelete(BaseModel):
    id: str
    wipe_memories: bool = False


class Evidence(BaseModel):
    id: str
    evidence_id: str = Field(min_length=3, max_length=300)


class GroupService:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.db: Database = app.db
        self._refreshing: set[str] = set()
        app.scheduler.handler("groups.refresh", self._scheduled_refresh)
        self._register_rpc()

    # -- spaces ---------------------------------------------------------------------------------------
    @staticmethod
    def space_id(platform: str, gid: str) -> str:
        return f"{platform}:{gid}"

    @staticmethod
    def scope(space: str) -> str:
        return "group:" + space

    def _row(self, space: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM group_spaces WHERE id=?", (space,))
        if not r:
            return None
        d = dict(r)
        d["roster"] = loads(d["roster"], {"members": [], "bots": []})
        d["opted_out"] = loads(d["opted_out"], [])
        d["paused"] = bool(d["paused"])
        return d

    def require(self, space: str) -> dict[str, Any]:
        row = self._row(space)
        if row is None:
            raise not_found("group space", space)
        return row

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        sid = row["id"]
        counts = self.db.one("SELECT COUNT(*), SUM(CASE WHEN terms IS NULL THEN 1 ELSE 0 END) "
                             "FROM group_messages WHERE space=?", (sid,))
        return {"id": sid, "platform": row["platform"], "group_id": row["group_id"], "name": row["name"],
                "description": row["description"], "enabled": True, "paused": row["paused"],
                "auto_refresh_min": row["auto_refresh_min"], "members": len(row["roster"]["members"]),
                "bots": len(row["roster"]["bots"]), "messages": int(counts[0] or 0), "pending": int(counts[1] or 0),
                "refreshed_at": row["refreshed_at"], "extractor": row["extractor"] or analysis.engine(),
                "created_at": row["created_at"]}

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM group_spaces ORDER BY created_at")
        return [self.view(self.require(r[0])) for r in rows]

    async def enable(self, platform: str, gid: str, name: str, description: str, members: list[Any],
                     bots: list[Any], auto_refresh_min: int = 0) -> dict[str, Any]:
        sid = self.space_id(platform, gid)
        if self._row(sid) is not None:
            raise WhaleError("conflict", "这个群已经是记忆空间了")
        roster = clean_roster(members, bots)
        now = now_ms()
        self.db.execute(
            "INSERT INTO group_spaces(id,platform,group_id,name,description,roster,auto_refresh_min,extractor,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, platform, gid, name, description, dumps(roster), auto_refresh_min, analysis.engine(), now, now))
        self._sync_job(sid, auto_refresh_min)
        view = self.view(self.require(sid))
        await self.app.bus.emit("group.updated", {"group": view})
        return view

    async def update(self, sid: str, **fields: Any) -> dict[str, Any]:
        row = self.require(sid)
        sets, args = [], []
        for key in ("name", "description", "auto_refresh_min"):
            if fields.get(key) is not None:
                sets.append(key + "=?")
                args.append(fields[key])
        if fields.get("members") is not None or fields.get("bots") is not None:
            roster = clean_roster(fields.get("members") if fields.get("members") is not None else row["roster"]["members"],
                                  fields.get("bots") if fields.get("bots") is not None else row["roster"]["bots"])
            sets.append("roster=?")
            args.append(dumps(roster))
        if sets:
            sets.append("updated_at=?")
            args.append(now_ms())
            self.db.execute(f"UPDATE group_spaces SET {', '.join(sets)} WHERE id=?", (*args, sid))
        if fields.get("auto_refresh_min") is not None:
            self._sync_job(sid, int(fields["auto_refresh_min"]))
        view = self.view(self.require(sid))
        await self.app.bus.emit("group.updated", {"group": view})
        return view

    async def set_paused(self, sid: str, paused: bool) -> dict[str, Any]:
        self.require(sid)
        self.db.execute("UPDATE group_spaces SET paused=?, updated_at=? WHERE id=?", (int(paused), now_ms(), sid))
        view = self.view(self.require(sid))
        await self.app.bus.emit("group.updated", {"group": view})
        return view

    async def set_opt_out(self, sid: str, account: str, opted_out: bool) -> list[str]:
        row = self.require(sid)
        member = self._member_of(row, account)
        accounts = member["accounts"] if member else [account]
        current = set(row["opted_out"])
        current = current | set(accounts) if opted_out else current - set(accounts)
        self.db.execute("UPDATE group_spaces SET opted_out=?, updated_at=? WHERE id=?",
                        (dumps(sorted(current)), now_ms(), sid))
        if opted_out:
            marks = ",".join("?" * len(accounts))
            self.db.execute(f"DELETE FROM group_messages WHERE space=? AND sender_id IN ({marks})", (sid, *accounts))
            for r in self.db.query(f"SELECT key, memory_id FROM group_memory WHERE space=? AND account IN ({marks})",
                                   (sid, *accounts)):
                await self._forget(r["memory_id"])
                self.db.execute("DELETE FROM group_memory WHERE space=? AND key=?", (sid, r["key"]))
        await self.app.bus.emit("group.updated", {"group": self.view(self.require(sid))})
        return sorted(current)

    async def delete(self, sid: str, wipe_memories: bool) -> dict[str, Any]:
        self.require(sid)
        forgotten = 0
        if wipe_memories:
            for r in self.db.query("SELECT memory_id FROM group_memory WHERE space=?", (sid,)):
                forgotten += await self._forget(r["memory_id"])
            items, _ = self.app.memory.store.list(scope=self.scope(sid), status=None, limit=5000)
            for item in items:
                forgotten += await self._forget(item.id)
        for table in ("group_messages", "group_digest", "group_memory", "group_seen"):
            self.db.execute(f"DELETE FROM {table} WHERE space=?", (sid,))
        self.db.execute("DELETE FROM group_spaces WHERE id=?", (sid,))
        self._sync_job(sid, 0)
        await self.app.bus.emit("group.deleted", {"id": sid})
        return {"deleted": sid, "memories_forgotten": forgotten}

    async def _forget(self, memory_id: str) -> int:
        try:
            item = self.app.memory.store.get(memory_id)
        except WhaleError:
            return 0
        if item.status == "forgotten":
            return 0
        await self.app.memory.forget(memory_id)
        return 1

    def _sync_job(self, sid: str, minutes: int) -> None:
        job = "group_refresh_" + sid.replace(":", "_")
        if minutes > 0:
            self.app.scheduler.add("群记忆自动更新 · " + sid, "groups.refresh", {"every_min": minutes},
                                   {"id": sid}, job_id=job)
        else:
            try:
                self.app.scheduler.remove(job)
            except Exception:
                pass

    async def _scheduled_refresh(self, args: dict[str, Any]) -> Any:
        row = self._row(str(args.get("id")))
        if row is None or row["paused"]:
            return None
        try:
            return await self.refresh(row["id"])
        except WhaleError as exc:
            if exc.code != "conflict":
                raise
            return None

    @staticmethod
    def _member_of(row: dict[str, Any], account: str) -> dict[str, Any] | None:
        for m in row["roster"]["members"] + row["roster"]["bots"]:
            if account in m["accounts"]:
                return m
        return None

    # -- capture --------------------------------------------------------------------------------------------
    def stage(self, sid: str, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Archive normalized messages; returns (added, skipped). Empty, duplicate and opted-out rows are skipped."""
        row = self._row(sid)
        if row is None or row["paused"]:
            return 0, len(rows)
        blocked = set(row["opted_out"])
        added = skipped = 0
        with self.db.transaction() as conn:
            for r in rows:
                mid, uid, text = str(r.get("message_id") or ""), str(r.get("sender_id") or ""), str(r.get("text") or "")
                if not mid or not uid or not text.strip() or uid in blocked:
                    skipped += 1
                    continue
                seq = str(r.get("seq") or "")
                if seq and conn.execute("SELECT 1 FROM group_messages WHERE space=? AND seq=? AND sender_id=? "
                                        "AND sent_at/1000=?/1000", (sid, seq, uid, int(r["sent_at"]))).fetchone():
                    skipped += 1
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO group_messages(space,message_id,sent_at,sender_id,sender_name,text,reply_to,seq) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (sid, mid, int(r["sent_at"]), uid, str(r.get("sender_name") or "")[:80], text[:MAX_TEXT],
                     str(r.get("reply_to") or ""), seq))
                if cur.rowcount:
                    added += 1
                else:
                    skipped += 1
        return added, skipped

    async def capture(self, platform: str, gid: str, message_id: str, sender_id: str, sender_name: str,
                      text: str, reply_to: str = "", sent_at: int | None = None) -> bool:
        """Called by channels for every inbound group message (whether or not Whale replies)."""
        sid = self.space_id(platform, gid)
        if self._row(sid) is None:
            return False
        added, _ = self.stage(sid, [{"message_id": message_id, "sender_id": sender_id, "sender_name": sender_name,
                                     "text": text, "reply_to": reply_to, "sent_at": sent_at or now_ms()}])
        if added:
            self.app.bus.publish_event("group.archived", {"id": sid, "name": self._row(sid)["name"], "count": added})
        return bool(added)

    async def import_export(self, sid: str, content: str) -> dict[str, int]:
        row = self.require(sid)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise invalid("不是有效的 JSON 文件") from exc
        if not isinstance(data, dict):
            raise invalid("不是 QQ Chat Exporter 的 JSON 导出")
        rows = export_rows(data, row["group_id"])
        added, skipped = self.stage(sid, rows)
        await self.app.bus.emit("group.archived", {"id": sid, "name": row["name"], "count": added, "imported": True})
        return {"added": added, "skipped": skipped}

    # -- refresh --------------------------------------------------------------------------------------------
    async def refresh(self, sid: str) -> dict[str, Any]:
        row = self.require(sid)
        if row["paused"]:
            raise WhaleError("conflict", "这个群空间已暂停记录")
        if sid in self._refreshing:
            raise WhaleError("conflict", "群记忆正在更新，请稍后查看")
        self._refreshing.add(sid)
        try:
            return await self._refresh(row)
        finally:
            self._refreshing.discard(sid)

    async def _refresh(self, row: dict[str, Any]) -> dict[str, Any]:
        sid = row["id"]
        engine = analysis.engine()
        if row["extractor"] and row["extractor"] != engine:
            self.db.execute("UPDATE group_messages SET terms=NULL WHERE space=?", (sid,))  # tokenizer changed: re-tag all
        rows = [dict(r) for r in self.db.query(
            "SELECT message_id, sent_at, sender_id, sender_name, text, reply_to, terms FROM group_messages "
            "WHERE space=? ORDER BY sent_at, message_id", (sid,))]
        pending = [r for r in rows if r["terms"] is None]
        if not pending and self.db.one("SELECT 1 FROM group_digest WHERE space=?", (sid,)):
            return {"processed": 0, "pending": 0, "counts": {}}
        bots = {a for b in row["roster"]["bots"] for a in b["accounts"]}
        _, fresh = await asyncio.to_thread(
            analysis.extract_terms, [r["text"] if r["sender_id"] not in bots else "" for r in pending])
        tagged = {r["message_id"]: t for r, t in zip(pending, fresh)}
        tokens = [tagged[r["message_id"]] if r["terms"] is None else loads(r["terms"], {}) for r in rows]
        digest, seg_of = analysis.summarize(rows, tokens)
        keywords = {m["id"]: m["keywords"] for m in digest["members"]}
        counts = await self._materialize(row, rows, keywords, set(tagged))
        with self.db.transaction() as conn:
            conn.executemany("UPDATE group_messages SET terms=? WHERE space=? AND message_id=?",
                             [(dumps(t), sid, mid) for mid, t in tagged.items()])
            conn.executemany("UPDATE group_messages SET segment_id=? WHERE space=? AND message_id=?",
                             [(seg, sid, mid) for mid, seg in seg_of.items()])
            conn.execute("INSERT OR REPLACE INTO group_digest(space,payload) VALUES (?,?)", (sid, dumps(digest)))
            conn.execute("UPDATE group_spaces SET refreshed_at=?, extractor=? WHERE id=?", (now_ms(), engine, sid))
        result = {"processed": len(tagged), "pending": self.view(self.require(sid))["pending"], "counts": counts}
        await self.app.bus.emit("group.refreshed", {"id": sid, "name": row["name"], **result})
        return result

    async def _materialize(self, row: dict[str, Any], rows: list[dict[str, Any]], keywords: dict[str, list[str]],
                           fresh_ids: set[str]) -> dict[str, int]:
        sid, scope = row["id"], self.scope(row["id"])
        blocked = set(row["opted_out"])
        members = [m for m in row["roster"]["members"] if not set(m["accounts"]) & blocked]
        speech = analysis.speech_rows([r for r in rows if r["sender_id"] not in blocked],
                                      self.app.config.value.agent.timezone)
        portraits = analysis.profiles(members, speech, keywords)
        counts = {"scanned": len(speech), "profiles_added": 0, "profiles_updated": 0, "profiles_preserved": 0,
                  "preferences_added": 0, "preferences_updated": 0, "preferences_expired": 0}
        platform = row["platform"]
        for m in members:
            primary = m["accounts"][0]
            p = portraits.get(primary)
            if not p:
                continue
            traits = [{"kind": kind, "label": t["label"], "count": t["count"], "days": t["days"],
                       "evidence_ids": [f"{platform}:group:{row['group_id']}:{mid}" for mid in t["evidence"]]}
                      for kind in ("expressions", "topics", "scenes") for t in p[kind]]
            source = {"group": sid, "member": m["name"], "accounts": m["accounts"], "traits": traits,
                      "evidence_ids": list(dict.fromkeys(e for t in traits for e in t["evidence_ids"])),
                      "stats": {k: p[k] for k in ("text_count", "active_days", "median_chars", "questions")},
                      "observed_at": p["last_at"], "from": "群聊画像"}
            summary = analysis.portrait_summary(m["name"], p)
            key = "portrait:" + primary
            existing = self.db.one("SELECT memory_id FROM group_memory WHERE space=? AND key=?", (sid, key))
            item = None
            if existing:
                try:
                    item = self.app.memory.store.get(existing[0])
                except WhaleError:
                    item = None
            if item is not None and item.status != "forgotten":
                if item.content == summary and item.source.get("traits") == traits:
                    counts["profiles_preserved"] += 1
                    continue
                await self.app.memory.edit(item.id, content=summary, source=source)
                counts["profiles_updated"] += 1
                memory_id = item.id
            else:
                res = await self.app.memory.remember(summary, kind="person", importance=0.5, scope=scope,
                                                     subject=f"{platform}:{primary}", consolidate=False,
                                                     tags=["群友画像", m["name"]], source=source)
                memory_id = res["item"]["id"]
                counts["profiles_added"] += 1
            self.db.execute("INSERT OR REPLACE INTO group_memory(space,key,memory_id,account,observed_at) "
                            "VALUES (?,?,?,?,?)", (sid, key, memory_id, primary, p["last_at"]))
        # explicit preferences from newly processed messages only
        owner_of = {a: m for m in members for a in m["accounts"]}
        now = now_ms()
        for r in speech:
            if r["message_id"] not in fresh_ids or r["sender_id"] not in owner_of:
                continue
            m = owner_of[r["sender_id"]]
            primary = m["accounts"][0]
            if primary not in portraits:
                continue
            for quote, slot in analysis.preference_clauses(r["text"]):
                evidence = f"{platform}:group:{row['group_id']}:{r['message_id']}"
                seen = hashlib.sha256((evidence + "\0" + quote).encode()).hexdigest()
                if self.db.one("SELECT 1 FROM group_seen WHERE space=? AND evidence=?", (sid, seen)):
                    continue
                self.db.execute("INSERT INTO group_seen(space,evidence) VALUES (?,?)", (sid, seen))
                if r["sent_at"] + PREFERENCE_TTL_MS <= now:
                    counts["preferences_expired"] += 1
                    continue
                key = "claim:" + primary + ":" + hashlib.sha256(slot.encode()).hexdigest()[:16]
                old = self.db.one("SELECT memory_id, observed_at FROM group_memory WHERE space=? AND key=?", (sid, key))
                if old and old["observed_at"] > r["sent_at"]:
                    continue
                res = await self.app.memory.remember(
                    f"群友「{m['name']}」说过：{quote}", kind="preference", importance=0.5, scope=scope,
                    subject=f"{platform}:{primary}", consolidate=False, tags=["群友喜好", m["name"]],
                    source={"group": sid, "member": m["name"], "quote": quote, "evidence_id": evidence,
                            "sent_at": r["sent_at"], "slot": slot, "from": "群聊发言"})
                new_id = res["item"]["id"]
                if old:
                    self.app.memory.store.link(new_id, old["memory_id"], "supersedes", 1.0)
                    try:
                        await self.app.memory.set_status(old["memory_id"], "superseded")
                    except WhaleError:
                        pass
                    counts["preferences_updated"] += 1
                else:
                    counts["preferences_added"] += 1
                self.db.execute("INSERT OR REPLACE INTO group_memory(space,key,memory_id,account,observed_at) "
                                "VALUES (?,?,?,?,?)", (sid, key, new_id, primary, r["sent_at"]))
        return counts

    # -- views ----------------------------------------------------------------------------------------------
    def digest(self, sid: str) -> dict[str, Any]:
        row = self.require(sid)
        r = self.db.one("SELECT payload FROM group_digest WHERE space=?", (sid,))
        payload = loads(r[0], {}) if r else {"members": [], "segments": [], "messages": 0, "keywords": []}
        payload["keyword_extractor"] = row["extractor"] or analysis.engine()
        archived = {p["id"]: p for p in payload.get("members", [])}
        blocked = set(row["opted_out"])
        members = []
        for m in row["roster"]["members"]:
            sources = [archived[a] for a in m["accounts"] if a in archived]
            primary = m["accounts"][0]
            entry = {"id": primary, "accounts": m["accounts"], "name": m["name"],
                     "aliases": [n for n in dict.fromkeys([*m["aliases"], *(n for s in sources for n in s["aliases"])])
                                 if n != m["name"]],
                     "count": sum(s["count"] for s in sources),
                     "first": min((s["first"] for s in sources), default=None),
                     "last": max((s["last"] for s in sources), default=None),
                     "keywords": list(dict.fromkeys(w for s in sources for w in s["keywords"]))[:8],
                     "opted_out": bool(set(m["accounts"]) & blocked), "portrait": None, "claims": []}
            for g in self.db.query("SELECT key, memory_id FROM group_memory WHERE space=? AND account=?", (sid, primary)):
                try:
                    item = self.app.memory.store.get(g["memory_id"])
                except WhaleError:
                    continue
                if item.status == "forgotten":
                    continue
                if g["key"].startswith("portrait:"):
                    entry["portrait"] = {"memory_id": item.id, "summary": item.content,
                                         "traits": item.source.get("traits", []), **item.source.get("stats", {})}
                elif item.status in ("active", "superseded"):
                    sent = int(item.source.get("sent_at") or 0)
                    entry["claims"].append({"memory_id": item.id, "content": item.content,
                                            "quote": item.source.get("quote", ""),
                                            "evidence_id": item.source.get("evidence_id", ""), "sent_at": sent,
                                            "current": item.status == "active" and sent + PREFERENCE_TTL_MS > now_ms()})
            entry["claims"].sort(key=lambda c: -c["sent_at"])
            members.append(entry)
        payload["members"] = sorted(members, key=lambda m: -m["count"])
        return payload

    def _names(self, row: dict[str, Any]) -> dict[str, str]:
        return {a: m["name"] for m in row["roster"]["members"] + row["roster"]["bots"] for a in m["accounts"]}

    def _msg(self, r: Any, names: dict[str, str]) -> dict[str, Any]:
        return {"message_id": r["message_id"], "sent_at": r["sent_at"], "sender_id": r["sender_id"],
                "sender_name": r["sender_name"], "display_name": names.get(r["sender_id"]) or r["sender_name"] or r["sender_id"],
                "text": r["text"]}

    def messages(self, sid: str, sender: str | None, segment: str | None, word: str | None,
                 offset: int) -> dict[str, Any]:
        row = self.require(sid)
        where, args = ["space=?"], [sid]
        if sender:
            m = self._member_of(row, sender)
            ids = m["accounts"] if m else [sender]
            where.append("sender_id IN (" + ",".join("?" * len(ids)) + ")")
            args.extend(ids)
        if segment:
            where.append("segment_id=?")
            args.append(segment)
        if word:
            where.append("instr(lower(text), lower(?)) > 0")
            args.append(word)
        rows = self.db.query("SELECT message_id, sent_at, sender_id, sender_name, text FROM group_messages WHERE "
                             + " AND ".join(where) + " ORDER BY sent_at, message_id LIMIT 51 OFFSET ?", (*args, offset))
        names = self._names(row)
        return {"items": [self._msg(r, names) for r in rows[:50]], "more": len(rows) > 50}

    def context(self, sid: str, message_id: str) -> dict[str, Any]:
        row = self.require(sid)
        hit = self.db.one("SELECT sent_at FROM group_messages WHERE space=? AND message_id=?", (sid, message_id))
        if not hit:
            raise not_found("message", message_id)
        cols = "SELECT message_id, sent_at, sender_id, sender_name, text FROM group_messages WHERE space=? AND "
        before = self.db.query(cols + "(sent_at < ? OR (sent_at = ? AND message_id < ?)) "
                               "ORDER BY sent_at DESC, message_id DESC LIMIT 1", (sid, hit[0], hit[0], message_id))
        after = self.db.query(cols + "(sent_at > ? OR (sent_at = ? AND message_id >= ?)) "
                              "ORDER BY sent_at, message_id LIMIT 2", (sid, hit[0], hit[0], message_id))
        names = self._names(row)
        return {"items": [self._msg(r, names) for r in [*before, *after]]}

    def evidence(self, memory_id: str, evidence_id: str) -> dict[str, Any]:
        """Show the original message a memory cites — only if the memory really cites it."""
        item = self.app.memory.store.get(memory_id)
        refs = list(item.source.get("evidence_ids") or [])
        if item.source.get("evidence_id"):
            refs.append(item.source["evidence_id"])
        for trait in item.source.get("traits") or []:
            refs.extend(trait.get("evidence_ids") or [])
        if evidence_id not in refs:
            raise not_found("evidence on this memory", evidence_id)
        parts = evidence_id.split(":", 3)
        if len(parts) != 4 or parts[1] != "group":
            return {"available": False, "reason": "unsupported_source", "message": "此依据不是群聊发言"}
        platform, _, gid, mid = parts
        sid = self.space_id(platform, gid)
        if item.scope != self.scope(sid):
            raise not_found("evidence on this memory", evidence_id)
        r = self.db.one("SELECT message_id, sent_at, sender_id, sender_name, text FROM group_messages "
                        "WHERE space=? AND message_id=?", (sid, mid))
        if r is None:
            return {"available": False, "reason": "source_missing", "message": "原始群聊未导入或已删除，无法核验原话"}
        accounts = item.source.get("accounts")
        if item.kind == "person" and accounts and r["sender_id"] not in accounts:
            raise not_found("evidence on this memory", evidence_id)
        row = self._row(sid)
        names = self._names(row) if row else {}
        return {"available": True, "evidence_id": evidence_id, "group_id": gid, **self._msg(r, names),
                "note": "本机归档的原始发言；每条最多保留 4000 字，图片和语音不转录。"}

    # -- RPC ------------------------------------------------------------------------------------------------
    def _register_rpc(self) -> None:
        m = self.app.rpc.method

        @m("groups.list", NoParams, "Group memory spaces")
        async def groups_list(p: NoParams, c: Client | None) -> Any:
            return {"groups": self.list(), "extractor": analysis.engine()}

        @m("groups.get", GroupRef, "A group space with roster and digest")
        async def groups_get(p: GroupRef, c: Client | None) -> Any:
            row = self.require(p.id)
            return {"group": {**self.view(row), "roster": row["roster"], "opted_out": row["opted_out"]},
                    "digest": self.digest(p.id)}

        @m("groups.enable", GroupEnable, "Make a group a memory space")
        async def groups_enable(p: GroupEnable, c: Client | None) -> Any:
            return await self.enable(p.platform, p.group_id, p.name, p.description, p.members, p.bots,
                                     p.auto_refresh_min)

        @m("groups.update", GroupUpdate, "Edit name, description, roster or auto refresh")
        async def groups_update(p: GroupUpdate, c: Client | None) -> Any:
            return await self.update(p.id, **p.model_dump(exclude={"id"}))

        @m("groups.set_paused", GroupPause, "Pause or resume archiving")
        async def groups_pause(p: GroupPause, c: Client | None) -> Any:
            return await self.set_paused(p.id, p.paused)

        @m("groups.set_opt_out", GroupOptOut, "Stop (or resume) remembering one member")
        async def groups_opt_out(p: GroupOptOut, c: Client | None) -> Any:
            return {"opted_out": await self.set_opt_out(p.id, p.account, p.opted_out)}

        @m("groups.import", GroupImport, "Import a QQ Chat Exporter JSON export")
        async def groups_import(p: GroupImport, c: Client | None) -> Any:
            return await self.import_export(p.id, p.content)

        @m("groups.refresh", GroupRef, "Process archived messages into the digest, portraits and preferences")
        async def groups_refresh(p: GroupRef, c: Client | None) -> Any:
            return await self.refresh(p.id)

        @m("groups.messages", GroupMessages, "Archived messages by member, segment or keyword (50 per page)")
        async def groups_messages(p: GroupMessages, c: Client | None) -> Any:
            return self.messages(p.id, p.sender, p.segment, p.word, p.offset)

        @m("groups.context", GroupContext, "A message with its neighbours")
        async def groups_context(p: GroupContext, c: Client | None) -> Any:
            return self.context(p.id, p.message_id)

        @m("groups.delete", GroupDelete, "Delete a space and its archive")
        async def groups_delete(p: GroupDelete, c: Client | None) -> Any:
            return await self.delete(p.id, p.wipe_memories)

        @m("memory.evidence", Evidence, "The original message a memory cites")
        async def memory_evidence(p: Evidence, c: Client | None) -> Any:
            return self.evidence(p.id, p.evidence_id)
