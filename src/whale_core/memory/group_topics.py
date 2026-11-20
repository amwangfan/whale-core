"""Rank group topics and assemble conversation segments from per-message terms."""
from collections import Counter, defaultdict
from datetime import datetime
import json
import math


def rank(counter, background, total, limit=5, *, types=None):
    types = types or {}
    def score(w):
        weight = 1.20 if types.get(w) in ("作品名","原文短语","专名") else 1.0
        return counter[w] * math.log(1 + total / background[w]) * weight
    picked = []
    for word in sorted(counter, key=lambda w:(counter[w] == 1, -score(w), w)):
        if not any(word in other or other in word for other in picked):
            picked.append(word)
        if len(picked) == limit:
            break
    return picked


def summarize(gid, speech, tokens, extractor):
    types = {w:t for words in tokens for w,t in words.items()}
    freq = Counter(w for words in tokens for w in words)
    people, segments, links = {}, [], []
    member_terms = defaultdict(Counter)
    previous = None
    for row, words in zip(speech, tokens, strict=True):
        mid, stamp, uid, name, text = row
        moment = datetime.fromisoformat(stamp)
        if previous is None or (moment - previous).total_seconds() >= 1200 or segment['count'] >= 80:
            segment = dict(id=mid, start=stamp, count=0, authors=set(), terms=Counter(), samples=[])
            segments.append(segment)
        previous = moment
        segment.update(end=stamp, count=segment['count'] + 1)
        segment['authors'].add(uid)
        segment['terms'].update(words.keys())
        if words:
            segment['samples'].append((mid, words))
        links.append((gid, mid, segment['id'], uid))
        person = people.setdefault(uid, dict(id=uid, name='', aliases=[], count=0, first=stamp))
        person.update(last=stamp, count=person['count'] + 1)
        if name:
            person['name'] = name
            if name not in person['aliases']:
                person['aliases'].append(name)
        member_terms[uid].update(words.keys())
    for s in segments:
        s['members'] = len(s.pop('authors'))
        s['keywords'] = rank(s.pop('terms'), freq, len(speech), types=types)
        samples = s.pop('samples')
        s['keyword_sources'] = {w:[mid for mid, words in samples if w in words] for w in s['keywords']}
        s['example_id'] = max(samples, key=lambda p: len(set(p[1]) & set(s['keywords'])))[0] if samples else s['id']
    for uid, person in people.items():
        person['keywords'] = rank(member_terms[uid], freq, len(speech), 8, types=types)
    payload = dict(members=sorted(people.values(), key=lambda p: -p['count']),
                   segments=list(reversed(segments)), messages=len(speech), keyword_extractor=extractor,
                   keyword_types={w: types[w] for s in segments for w in s['keywords']})
    return payload, links


def publish(db, gid, payload, links):
    db.executemany("INSERT INTO group_digest_messages(group_id,message_id,segment_id,sender_id) VALUES(?,?,?,?) "
                   "ON CONFLICT(group_id,message_id) DO UPDATE SET segment_id=excluded.segment_id, sender_id=excluded.sender_id "
                   "WHERE segment_id<>excluded.segment_id OR sender_id<>excluded.sender_id", links)
    db.execute("INSERT OR REPLACE INTO group_digest VALUES(?,?)", (gid,json.dumps(payload,ensure_ascii=False)))
