"""Text analysis for group spaces (ported from PR #95).

Everything here is pure: rows in, structures out. No model calls — portraits
and preferences are grounded in literal message text and every trait keeps
the message ids it came from.

* ``extract_terms``  — per-message content words: jieba POS tagging when jieba
  is installed, otherwise a CJK n-gram extractor (2–4 characters, function
  characters trimmed) so a fresh install still gets useful keywords.
* ``summarize``      — conversation segments (gap ≥ 20 min or 80 messages),
  TF-IDF-like keyword ranking per segment and per member.
* ``profiles``       — language portraits: catchphrases distinctive versus the
  rest of the group (≥ 3 uses on ≥ 2 days), recurring topics, typical
  reactions, self-report candidates.
* ``preference_clauses`` — explicit first-person preferences ("我喜欢跑团").
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SEGMENT_GAP_MS = 20 * 60_000
SEGMENT_MAX = 80

# -- keywords -----------------------------------------------------------------------------------
NOUNS = set("跑团 模组 开团时间 角色卡 人物卡 调查员 守秘人 骰子 骰点 线索 理智值 技能检定 理智检定 困难成功 "
            "极难成功 大成功 大失败 克苏鲁 世界观 星图 晚餐".split())
VERBS = set("掷骰 投骰 车卡 开团 检定 侦查 侦察 急救 潜行 调查 观测".split())
ABBREVIATIONS = re.compile(r"(?<![a-zA-Z])(?:COC|TRPG|DND|NPC|KP|PL|SAN)(?![a-zA-Z])", re.I)
TITLES = re.compile(r"《([^《》\n]{2,24})》")
STOP = set((
    "东西 事情 问题 时候 时间 情况 方面 样子 地方 部分 一点 一下 一种 一些 一堆 一句 一句话 玩意 玩意儿 "
    "个人 别人 人家 大家 对方 这边 那边 自己 一会 一次 今天 明天 昨天 现在 之前 之后 以前 以后 结果 原因 "
    "意思 感觉 想法 印象 话说 发现 知道 觉得 感到 以为 认为 看到 看见 听说 可以 能够 需要 可能 应该 "
    "还是 就是 没有 好像 不知道 不觉得 不用 不要 不会 不是 不能 属于 成为 开始 结束 进行 表示 说话 "
    "怎么 什么 为什么 那样 这样 怎么样 这样子 那个 这个 自然 确实 真的 当然 哈哈 笑死 绷不住 卧槽 好耶 "
    "反正 毕竟 总之 懂了 完了 算了 等等 没事 没关系 感谢 谢谢 对不起 没错 意义 类型 可能性 "
    "图片 表情 回复 转发 消息 动画 表情包 网页 链接 文件 聊天记录 "
    "你们 我们 他们 她们 它们 俺们 咱们 自个 那种 这种 哪个 这些 那些 那里 这里 "
    "还有 有没有 只有 出来 进去 进来 出去 起来 下去 过来 过去 起床了 看看 看着 听着 "
    "不想 想要 想到 想想 喜欢 好看 好吃 可惜 晚安 早安 早上好 拜拜 小时 分钟 秒钟 "
    "认识 碰到 看过 看完 看来 看起来 说过 不行 行不行 包括 使用 是不是 感受 属实 "
    "支持 变成 变得 带来 搞得 搞成 应当 拥有 记得 忘了 算是 像是 在于 对于 针对 "
    "不了 得了 我会 我能 我想 我要 你会 你能 你想 你要 他会 他能 她会 她能").split())
NOISE = re.compile(r"https?://\S+|\[[^\]\n]{1,300}\]|[a-f\d]{16,}|@[\w.-]+", re.I)
CONTENT = re.compile(r"[\u3400-\u9fff]{2,12}")
FILLER = re.compile(r"^[哈啊唉哦喔欸诶呃嗯哎呀哇呜嘿嘻嘛捏吧啦草噗乐笑]+$")
CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
# characters an n-gram must not start or end with (function words, pronouns, particles)
EDGE = set("的了吗呢吧啊呀么嘛哦是在和与跟就也都很太还又再才我你他她它这那有没不被把给让对从到")


def _tagger() -> Any:
    try:
        import jieba  # type: ignore
        import jieba.posseg as pos  # type: ignore
    except ImportError:
        return None
    tok = jieba.Tokenizer()
    tagger = pos.POSTokenizer(tok)
    for term in NOUNS | VERBS:
        tok.add_word(term, tag="n" if term in NOUNS else "v")
    return tagger


_TAGGER: Any = False


def engine() -> str:
    global _TAGGER
    if _TAGGER is False:
        _TAGGER = _tagger()
    return "jieba" if _TAGGER else "ngram"


def _ngrams(body: str) -> dict[str, str]:
    terms: dict[str, str] = {}
    for run in CJK_RUN.findall(body):
        for n in (4, 3, 2):
            for k in range(len(run) - n + 1):
                gram = run[k:k + n]
                if gram[0] in EDGE or gram[-1] in EDGE or gram in STOP or FILLER.fullmatch(gram):
                    continue
                if any(gram in t for t in terms):
                    continue  # prefer the longer phrase already taken
                terms[gram] = "名词" if gram in NOUNS else "动词" if gram in VERBS else "词组"
                if len(terms) >= 24:
                    return terms
    return terms


def extract_terms(texts: Iterable[str]) -> tuple[str, list[dict[str, str]]]:
    name = engine()
    out: list[dict[str, str]] = []
    for text in texts:
        body = NOISE.sub(" ", text or "")
        terms = {t: "作品名" for t in TITLES.findall(body) if t.strip() and t not in STOP and not FILLER.fullmatch(t)}
        terms.update((t.upper(), "专名") for t in ABBREVIATIONS.findall(body))
        if _TAGGER:
            previous = None
            for word, flag in _TAGGER.cut(body):
                kind = "名词" if flag.startswith("n") else "动词" if flag in {"v", "vn", "vd"} else ""
                if not kind or word in STOP or not CONTENT.fullmatch(word) or FILLER.fullmatch(word):
                    previous = None
                    continue
                terms.setdefault(word, kind)
                if previous and kind == "名词" and len(previous + word) <= 10:
                    terms.setdefault(previous + word, "原文短语")
                previous = word
        else:
            for t, kind in _ngrams(body).items():
                terms.setdefault(t, kind)
        out.append(terms)
    return name, out


# -- segments & ranking -------------------------------------------------------------------------
def rank(counter: Counter, background: Counter, total: int, limit: int = 5,
         types: dict[str, str] | None = None) -> list[str]:
    types = types or {}

    def score(w: str) -> float:
        weight = 1.2 if types.get(w) in ("作品名", "原文短语", "专名") else 1.0
        return counter[w] * math.log(1 + total / max(1, background[w])) * weight

    picked: list[str] = []
    for word in sorted(counter, key=lambda w: (counter[w] == 1, -score(w), w)):
        if not any(word in other or other in word for other in picked):
            picked.append(word)
        if len(picked) == limit:
            break
    return picked


def summarize(rows: list[dict[str, Any]], tokens: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, str]]:
    """rows: ordered {message_id, sent_at(ms), sender_id, sender_name}. Returns (digest, message_id→segment_id)."""
    types = {w: t for words in tokens for w, t in words.items()}
    freq = Counter(w for words in tokens for w in words)
    people: dict[str, dict[str, Any]] = {}
    member_terms: dict[str, Counter] = defaultdict(Counter)
    segments: list[dict[str, Any]] = []
    seg_of: dict[str, str] = {}
    previous = None
    segment: dict[str, Any] = {}
    for row, words in zip(rows, tokens):
        mid, at, uid, name = row["message_id"], row["sent_at"], row["sender_id"], row["sender_name"]
        if previous is None or at - previous >= SEGMENT_GAP_MS or segment["count"] >= SEGMENT_MAX:
            segment = {"id": mid, "start": at, "count": 0, "authors": set(), "terms": Counter(), "samples": []}
            segments.append(segment)
        previous = at
        segment["end"] = at
        segment["count"] += 1
        segment["authors"].add(uid)
        segment["terms"].update(words.keys())
        if words:
            segment["samples"].append((mid, words))
        seg_of[mid] = segment["id"]
        person = people.setdefault(uid, {"id": uid, "name": "", "aliases": [], "count": 0, "first": at})
        person["last"] = at
        person["count"] += 1
        if name:
            person["name"] = name
            if name not in person["aliases"]:
                person["aliases"].append(name)
        member_terms[uid].update(words.keys())
    for seg in segments:
        seg["members"] = len(seg.pop("authors"))
        seg["keywords"] = rank(seg.pop("terms"), freq, len(rows), types=types)
        samples = seg.pop("samples")
        seg["keyword_sources"] = {w: [mid for mid, words in samples if w in words] for w in seg["keywords"]}
        best = max(samples, key=lambda p: len(set(p[1]) & set(seg["keywords"])), default=None)
        seg["example_id"] = best[0] if best else seg["id"]
    for uid, person in people.items():
        person["keywords"] = rank(member_terms[uid], freq, len(rows), 8, types=types)
    seg_counts = Counter(w for seg in segments for w in seg["keywords"])
    keywords = [{"word": w, "segments": n, "type": types.get(w, "词组")}
                for w, n in sorted(seg_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:80]]
    digest = {"members": sorted(people.values(), key=lambda p: -p["count"]),
              "segments": list(reversed(segments)), "messages": len(rows), "keywords": keywords}
    return digest, seg_of


# -- portraits ----------------------------------------------------------------------------------
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


def speech_rows(rows: list[dict[str, Any]], tz: str) -> list[dict[str, Any]]:
    """Messages usable as personal speech: no bot commands, no links; adds the local day."""
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    out = []
    for r in rows:
        text = r["text"] or ""
        if not text.strip() or COMMAND.match(text) or "://" in text:
            continue
        day = datetime.fromtimestamp(r["sent_at"] / 1000, zone).date().isoformat()
        out.append({**r, "day": day})
    return out


def _samples(rows: list[dict[str, Any]], limit: int = 2) -> list[str]:
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_day.setdefault(row["day"], row)
    return [r["message_id"] for r in list(by_day.values())[:limit]]


def expressions(text: str) -> set[str]:
    found = {m[0] for m in MARKERS.finditer(text)}
    ending = re.search(r"([吧呢嘛啦呀啊哦诶欸捏呗哇喔噢哒])(?:[。！？~～.!?]*)$", text)
    if ending:
        found.add("句尾「" + ending[1] + "」")
    phrase = text.strip("！!？?。~～，, ")
    if 2 <= len(phrase) <= 9 and re.fullmatch(r"[\u3400-\u9fffA-Za-z]+", phrase):
        found.add(phrase)
    return found


def profiles(members: list[dict[str, Any]], speech: list[dict[str, Any]],
             member_keywords: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    """members: roster entries {name, accounts}. Returns primary account → portrait."""
    owner_of = {a: m["accounts"][0] for m in members for a in m["accounts"]}
    by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    phrases: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    background: Counter = Counter()
    for row in speech:
        primary = owner_of.get(row["sender_id"])
        if primary is None:
            continue
        by_person[primary].append(row)
        for word in expressions(row["text"]):
            phrases[primary][word].append(row)
            background[word] += 1
    total = sum(len(v) for v in by_person.values())
    out: dict[str, dict[str, Any]] = {}
    for m in members:
        primary = m["accounts"][0]
        rows = by_person.get(primary, [])
        n = len(rows)
        if not n:
            continue
        patterns = []
        for term, hits in phrases[primary].items():
            days = len({r["day"] for r in hits})
            if len(hits) < 3 or days < 2:
                continue
            patterns.append({"label": term, "count": len(hits), "days": days,
                             "other_count": background[term] - len(hits), "other_total": total - n,
                             "evidence": _samples(hits)})

        def distinct(p: dict[str, Any]) -> tuple[float, int, str]:
            rate = p["count"] / n
            other = p["other_count"] / max(1, p["other_total"])
            return (-(rate - other), -p["days"], p["label"])

        scenes = []
        for label_, pattern in SCENES.items():
            hits = [r for r in rows if re.search(pattern, r["text"])]
            if hits:
                scenes.append({"label": label_, "count": len(hits), "days": len({r["day"] for r in hits}),
                               "evidence": _samples(hits)})
        topics = []
        keys: list[str] = []
        for acc in m["accounts"]:
            keys.extend(k for k in member_keywords.get(acc, []) if k not in keys)
        for kw in keys[:8]:
            hits = [r for r in rows if kw.casefold() in r["text"].casefold()]
            days = len({r["day"] for r in hits})
            if days >= 2:
                topics.append({"label": kw, "count": len(hits), "days": days, "evidence": _samples(hits)})
        claims = [r for r in rows if SELF_REPORT.match(r["text"]) and not ROLE_OR_QUOTE.search(r["text"])
                  and "？" not in r["text"] and "?" not in r["text"] and len(r["text"]) <= 150]
        out[primary] = {
            "text_count": n, "active_days": len({r["day"] for r in rows}),
            "median_chars": median(len(r["text"]) for r in rows),
            "questions": sum(bool(re.search(r"[？?]$", r["text"])) for r in rows),
            "expressions": sorted(patterns, key=distinct)[:8], "topics": topics, "scenes": scenes,
            "self_reports": _samples(claims, 6),
            "last_at": max(r["sent_at"] for r in rows),
        }
    return out


def portrait_summary(name: str, p: dict[str, Any]) -> str:
    parts = [f"群友「{name}」的群内画像"]
    if p["expressions"]:
        parts.append("常说" + "、".join("「" + e["label"] + "」" for e in p["expressions"][:4]))
    if p["topics"]:
        parts.append("常聊" + "、".join(t["label"] for t in p["topics"][:4]))
    if p["scenes"]:
        parts.append("常见反应：" + "、".join(s["label"] for s in p["scenes"][:3]))
    parts.append(f"共 {p['text_count']} 条发言、活跃 {p['active_days']} 天，一般每条约 {int(p['median_chars'])} 字")
    return "；".join(parts) + "。"


# -- explicit preferences ------------------------------------------------------------------------
SENSITIVE = re.compile(r"\d{6,}|身份证|银行卡|密码|手机号|住址|病|药|医院")
PRIVATE_TOPICS = ("分手", "恋爱", "亲密", "心理", "抑郁", "咨询", "男朋友", "女朋友")
_PREFERENCE = re.compile(r"^(?:我|咱)(?:个人)?(?:现在|以后|一直|平时)?(?:其实|也|还|就)?"
                         r"(?:比较|特别|真的|非常|更|最|很|挺|蛮|超级|超|好)?"
                         r"(?:不再|不太|不怎么|不)?(?:喜欢|偏好|偏爱|讨厌|爱看|爱玩|爱听|爱吃|最爱)(?P<topic>.+)$")
_NON_CLAIM = re.compile(r"如果|假如|要是|[?？“”「」\"：:]|台词|扮演|我(?:的|家)角色|转述|是不是|喜不喜欢|[他她]说")


def preference_clauses(text: str) -> Iterable[tuple[str, str]]:
    """Yield (quote, slot) for explicit first-person preferences; slot groups updates of the same interest."""
    if SENSITIVE.search(text) or any(x in text for x in PRIVATE_TOPICS) or _NON_CLAIM.search(text):
        return
    for part in re.split(r"[，,。；;！!\n]", text):
        quote = part.strip()
        match = _PREFERENCE.fullmatch(quote)
        if match and len(quote) <= 100:
            topic = match["topic"].strip()
            if topic and topic not in {"你", "他", "她", "这个", "那个", "的", "了"} and not topic.endswith("吗"):
                activity = re.search(r"爱([看玩听吃])$", quote[:match.start("topic")])
                yield quote, "interest:" + (activity[1] if activity else "") + topic
