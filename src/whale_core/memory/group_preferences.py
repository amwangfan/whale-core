"""Group-scoped observations and first-person preference candidates."""
from dataclasses import replace
from datetime import datetime, timezone
import re

from whale_core.memory.extraction import contains_sensitive
from hashlib import sha256
from whale_core.memory.curation import _is_expired
from whale_core.memory.consent import MemberConsentStore

_PRIVATE_TOPICS = ("分手","恋爱","亲密","心理","抑郁","咨询","男朋友","女朋友")
from whale_core.memory.store import MemoryKind, MemoryRecord


SOURCE = "qce-group-preference"
_PREFERENCE = re.compile(r"^(?:我|咱)(?:个人)?(?:现在|以后|一直|平时)?(?:其实|也|还|就)?"
                         r"(?:比较|特别|真的|非常|更|最|很|挺|蛮|超级|超|好)?"
                         r"(?:不再|不太|不怎么|不)?(?:喜欢|偏好|偏爱|讨厌|爱看|爱玩|爱听|爱吃|最爱)(?P<topic>.+)$")
_NON_CLAIM = re.compile(r"如果|假如|要是|[?？“”「」\"：:]|台词|扮演|我(?:的|家)角色|转述|是不是|喜不喜欢|[他她]说")


def preference_clauses(text):
    if contains_sensitive(text) or any(x in text for x in _PRIVATE_TOPICS) or _NON_CLAIM.search(text):
        return
    for part in re.split(r"[，,。；;！!\n]", text):
        quote = part.strip()
        match = _PREFERENCE.fullmatch(quote)
        if match and len(quote) <= 100:
            topic = match["topic"].strip()
            if topic and topic not in {"你", "他", "她", "这个", "那个", "的", "了"} and not topic.endswith("吗"):
                # Keep the activity in the slot (爱看 X and 爱玩 X can coexist).
                activity = re.search(r"爱([看玩听吃])$", quote[:match.start("topic")])
                yield quote, "interest:" + (activity[1] if activity else "") + topic


def profile_key(gid, account):
    return f"qce:profile:{gid}:{account}"


def saved_profile(store, gid, member_id):
    record = store.get_record(profile_key(gid, member_id))
    if (record and record.kind is MemoryKind.GROUP_PROFILE and record.space_id == "group-" + gid
            and record.content.get("source") == "qce-group-profile" and record.content.get("status", "active") == "active"
            and (not record.content.get("expires_at") or datetime.fromisoformat(record.content["expires_at"]) > datetime.now(timezone.utc))):
        if not any(MemberConsentStore(store).is_opted_out("anon:qq:"+a) for a in record.content.get("accounts",[])):
            return record


def save_claim(store, gid, subject, slot, quote, eid, stamp, expiry):
    """Keep the newest explicit claim per group, member and interest; deduplicate evidence."""
    digest = sha256((subject + "\0" + slot).encode()).hexdigest()
    key = f"group-claim:{gid}:{digest}"
    seen = "group-claim-seen:" + sha256((eid + "\0" + quote).encode()).hexdigest()
    def write(tx):
        if tx.get(seen):
            return "already_decided"
        old = tx.get_record(key)
        tx.put(seen, True)
        if old and (old.content.get("status") != "active" or
                    datetime.fromisoformat(old.content["observed_at"]) > stamp):
            return "preserved"
        content = dict(source=SOURCE, subject_id=subject, evidence_id=eid, summary=quote, status="active",
                       slot_key=slot, observed_at=stamp.isoformat(), expires_at=expiry.isoformat())
        record = MemoryRecord(kind=MemoryKind.PREFERENCE, key=key, space_id="group-"+gid,
                              content=content, created_at=stamp, tags=("group_preference",))
        tx.put_record(record)
        return "fact_updated" if old else "fact_added"
    return store.transact(write)


def scoped_claims(store, db, gid, members):
    people = {a:p for p in members for a in p.get("accounts",[p["id"]])}
    prefix = f"qq:group:{gid}:"
    for record in store.list_by_kind(MemoryKind.PREFERENCE, space_ids=("group-"+gid,)):
        c = record.content
        eid = str(c.get("evidence_id") or "")
        if c.get("source") != SOURCE or not eid.startswith(prefix):
            continue
        row = db.execute("SELECT sender_id FROM group_digest_messages WHERE group_id=? AND message_id=?",
                         (gid,eid[len(prefix):])).fetchone()
        member = people.get(row[0]) if row else None
        if not member or c.get("subject_id") != "anon:qq:"+member["id"]:
            continue
        if any(MemberConsentStore(store).is_opted_out("anon:qq:"+a) for a in member.get("accounts",[member["id"]])):
            continue
        active = c.get("status")=="active" and not _is_expired(c, datetime.now(timezone.utc))
        yield member["id"], dict(key=record.key, evidence_id=eid, summary=c["summary"], status=c.get("status"),
                                 expires_at=c.get("expires_at"), current_preference=active)


def save_observations(store, gid, member, profile, speech, observed_at, subject):
    """One snapshot per person and group; keep original text in the archive."""
    traits = []
    for field, label_key in (("expressions", "term"), ("topics", "term"), ("scenes", "label")):
        for item in profile[field]:
            refs = []
            for example in item["examples"]:
                row = speech.get(example["message_id"])
                if (not row or row["sender_id"] not in member["accounts"] or
                        row["sender_id"] != example["sender_id"] or row["text"] != example["text"]):
                    raise ValueError("画像依据与群归档中的本人文字不符，请刷新画像")
                refs.append(f"qq:group:{gid}:{example['message_id']}")
            if refs:
                traits.append(dict(kind=field, label=item[label_key], evidence_ids=refs,
                                   **{k:item[k] for k in ("count","days","other_count","other_total") if k in item}))
    key = profile_key(gid, member["accounts"][0])
    space = "group-" + gid
    content = dict(source="qce-group-profile", summary=member["name"] + "的群内画像",
                   subject_id=subject, display_name=member["name"], accounts=member["accounts"],
                   traits=traits, evidence_ids=list(dict.fromkeys(ref for t in traits for ref in t["evidence_ids"])),
                   observed_at=observed_at, **{k:profile[k] for k in
                   ("text_count","active_days","median_chars","questions","no_end_punct")})
    def write(tx):
        old = tx.get_record(key)
        if old and (old.content == content or old.content.get("status", "active") != "active" or old.content.get("expires_at")):
            return "preserved"
        if not old and tx.get(key + ":imported"):
            return "preserved"
        if old and old.content.get("observed_at","") > observed_at:
            return "preserved"
        record = (replace(old, content=content) if old else MemoryRecord(
            kind=MemoryKind.GROUP_PROFILE, key=key, space_id=space, content=content,
            created_at=datetime.fromisoformat(observed_at), tags=("group_profile",)))
        tx.put_record(record)
        tx.put(key + ":imported", True)
        return "updated" if old else "added"
    return store.transact(write)
