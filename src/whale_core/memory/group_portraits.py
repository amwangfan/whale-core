"""Form evidence-backed language portraits from authored group messages."""
from collections import Counter, defaultdict
from datetime import timezone, timedelta
import re
from statistics import median
from whale_core.memory.group_directory import account_names


SHANGHAI = timezone(timedelta(hours=8))
MARKERS = re.compile(r"我觉得|我的意思是|怎么说呢|说起来|等一下|确实|其实|好耶|原来如此|"
                     r"绷不住了?|笑死(?:我了)?|救命|好家伙|有道理|感觉|有一说一|"
                     r"反正|毕竟|总之|[哈嘿呜啊]{2,}|[？?]{2,}|[！!]{2,}")
SELF_REPORT = re.compile(r"^我(?:个人)?(?:比较|更|最|一直|一般|平时|不太|不)?(?:喜欢|习惯|倾向|偏好)")
ROLE_OR_QUOTE = re.compile(r"角色|扮演|台词|我家.{0,4}卡|[“”「」\"：:]|转述")
COMMAND = re.compile(r"^[.。/．][a-zA-Z]")
SCENES = {"表示赞同": r"^(?:确实|有道理|同意|没错|对对|是的)",
          "提出不同看法": r"^(?:我倒觉得|也不一定|不一定|但我觉得|我反而|不太同意)",
          "提议或邀约": r"^(?:要不|咱们要不要|我们要不要|有没有人想)",
          "询问规则": r"(?:规则|判定|检定|技能).{0,30}(?:[？?]|吗$|怎么算)"}


def evidence(row):
    return {k: row[k] for k in ("message_id", "sender_id", "sent_at", "text", "reply_to")}


def samples(rows, limit=2):
    by_day = {}
    for row in rows:
        by_day.setdefault(row["day"], row)
    return [evidence(r) for r in list(by_day.values())[:limit]]


def expressions(text):
    found = {m[0] for m in MARKERS.finditer(text)}
    ending = re.search(r"([吧呢嘛啦呀啊哦诶欸捏呗哇喔噢哒])(?:[。！？~～.!?]*)$", text)
    if ending:
        found.add("句尾「" + ending[1] + "」")
    # Discover short, repeated standalone expressions beyond the seed vocabulary.
    phrase = text.strip("！!？?。~～，, ")
    if 2 <= len(phrase) <= 9 and re.fullmatch(r"[\u3400-\u9fffA-Za-z]+", phrase):
        found.add(phrase)
    return found


def extract_profiles(gid, roster, rows, index, expected):
    accounts = account_names(roster)
    humans = {m["name"] for m in roster["members"]}
    by_person = defaultdict(list)
    phrases = defaultdict(lambda: defaultdict(list))
    background = Counter()
    for row in rows:
        name = accounts.get(row["sender_id"])
        if name not in humans:
            continue
        by_person[name].append(row)
        for word in expressions(row["text"]):
            phrases[name][word].append(row)
            background[word] += 1
    total = sum(map(len, by_person.values()))
    archived = Counter(expected.values())
    for member, indexed_member in zip(roster["members"], index):
        name = member["name"]
        speech = by_person[name]
        n = len(speech)
        patterns = []
        for term, hits in phrases[name].items():
            days = len({r["day"] for r in hits})
            if len(hits) < 3 or days < 2:
                continue
            rest = total - n
            other = background[term] - len(hits)
            patterns.append(dict(term=term, count=len(hits), days=days, other_count=other, other_total=rest,
                                 examples=samples(hits)))
        def distinctiveness(pattern):
            rate = pattern["count"] / n
            other_rate = pattern["other_count"] / max(1, pattern["other_total"])
            return (-(rate - other_rate), -pattern["days"], pattern["term"])
        scenes = []
        for label, regex in SCENES.items():
            matches = [r for r in speech if re.search(regex,r["text"])]
            if matches:
                scenes.append(dict(label=label, count=len(matches), examples=samples(matches)))
        claims = [r for r in speech if SELF_REPORT.match(r["text"]) and not ROLE_OR_QUOTE.search(r["text"])
                  and not any(c in r["text"] for c in ("？","?")) and len(r["text"]) <= 150]
        topics = []
        for keyword in indexed_member["keywords"]:
            hits = [r for r in speech if keyword.casefold() in r["text"].casefold()]
            days = len({r["day"] for r in hits})
            if days >= 2:
                topics.append(dict(term=keyword, count=len(hits), days=days, examples=samples(hits)))
        profile = dict(group_id=gid, name=name, accounts=member["accounts"], aliases=member["aliases"],
                       archived_count=sum(archived[a] for a in member["accounts"]), text_count=n,
                       active_days=len({r["day"] for r in speech}),
                       median_chars=median(len(r["text"]) for r in speech) if n else None,
                       no_end_punct=sum(r["text"][-1].isalnum() for r in speech),
                       questions=sum(bool(re.search(r"[？?]$", r["text"])) for r in speech),
                       expressions=sorted(patterns, key=distinctiveness)[:8], scenes=scenes, topics=topics,
                       self_report_candidates=samples(claims, 6))
        yield member, profile
