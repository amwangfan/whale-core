"""瞬时时间信息过滤：「现在几点 / 当前时间 / 今天是…」不进长期记忆。

背景（owner 2026-11-05 反馈）：眼镜 09:51 问「现在几点」仍答「一点多」——
凌晨的报时被写进情节记忆与会话历史，下一轮语义召回把「现在几点→凌晨一点」
当背景记忆注入，模型信了旧时间。当前时间每轮由 prompt_composer 现算注入
（只在本轮临时上下文），**不写入**情节 / InnerState / 摘要，也不从旧记忆
里注入报时句。

纯函数、确定性、离线可测；只剥「此刻是几点/哪天」这类瞬时陈述，保留有
意义的事实（如「用户常熬夜」「每天七点起床」这类习惯/计划不受影响）。
"""
from __future__ import annotations

import re

_CN_NUM = "零〇一二两三四五六七八九十"
_HOUR = rf"(?:\d{{1,2}}|[{_CN_NUM}]{{1,3}})"
_PERIOD = r"(?:凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|深夜|半夜|午夜)"

# 用户询问当前时间/日期（整句即瞬时问题）。
_TIME_QUERY = re.compile(
    r"(现在|此刻|目前|当前|这会儿?)?\s*(是)?\s*(几点|几时|什么时间|啥时候了|多少点)"
    r"|今天(是)?\s*(几号|星期几|周几|礼拜几|什么日子|几月几号)"
    r"|(现在|当前)(的)?(时间|日期)"
    r"|what\s+time\s+is\s+it|what'?s\s+the\s+time|current\s+time",
    re.I,
)

# 对当前时刻的陈述（报时）。注意不吃「每天七点起床」「下次三点开会」这类
# 带计划/习惯语境的句子——要求有「现在/已经/快/刚/啦」等此刻锚点，或
# 显式日期时间戳。
_NOW_ANCHOR = r"(?:现在|此刻|目前|当前|这会儿?|已经|都|快|刚过|刚|才)"
_TIME_STATEMENT = re.compile(
    "|".join([
        # 现在是凌晨一点多 / 已经快两点了 / 都快一点了
        rf"{_NOW_ANCHOR}\s*(?:是|到)?\s*{_PERIOD}?\s*{_HOUR}\s*(?:点|时|[:：]\d{{2}})",
        rf"{_NOW_ANCHOR}\s*(?:是)?\s*{_PERIOD}(?:了|啦|呢|啊|呀)",
        # 「凌晨快一点啦」「两点十五啦」「零点四十九」「上午九点五十一」
        rf"{_PERIOD}?\s*快?\s*{_HOUR}\s*点\s*(?:多|半|整|{_HOUR}\s*分?|\d{{1,2}}\s*分?)?\s*(?:啦|了|呢|哦|喔)",
        rf"{_PERIOD}\s*{_HOUR}\s*点\s*(?:{_HOUR}|\d{{1,2}})\s*分?",
        rf"[{_CN_NUM}]{{1,2}}点[{_CN_NUM}]{{2,3}}",
        # 显式时钟 / 日期戳：9:51、09:51:00、2026-11-05、今天是 11 月 5 号 星期四
        r"(?<![\d.])\d{1,2}[:：]\d{2}(?:[:：]\d{2})?(?![\d])",
        r"今天(?:是)?\s*\d{4}\s*[-年/.]\s*\d{1,2}\s*[-月/.]\s*\d{1,2}",
        r"今天(?:是)?\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]",
        r"今天(?:是)?\s*(?:星期|周|礼拜)[一二三四五六日天]",
        r"当前时间",
        # 只靠「这么晚」的时段寒暄
        r"这么晚(?:了|啦)?还没睡",
        r"(?:这边|这里)已经是.{0,6}凌晨",
    ]),
    re.I,
)

# 习惯/计划语境：句中带这些词的保留（「每天凌晨一点睡」是事实不是报时）。
_HABIT = re.compile(r"(每天|每晚|每周|经常|常常|总是|习惯|一般|通常|平时|下次|明天|后天|计划|约了|提醒|闹钟|定时)")

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n；;…~～])")


def is_time_query(text: str) -> bool:
    """用户这句主要是在问当前时间/日期吗（短句 + 命中时间问法）。"""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    return bool(_TIME_QUERY.search(t))


def _is_time_sentence(sentence: str) -> bool:
    s = sentence.strip()
    if not s:
        return False
    if _HABIT.search(s):
        return False
    return bool(_TIME_STATEMENT.search(s) or (len(s) <= 40 and _TIME_QUERY.search(s)))


def scrub_transient_time(text: str) -> str:
    """剥掉报时/当前日期类句子，返回剩余文本（可能为空串）。"""
    raw = str(text or "")
    if not raw:
        return ""
    kept = [p for p in _SENT_SPLIT.split(raw) if p and not _is_time_sentence(p)]
    out = "".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def is_transient_time_only(text: str) -> bool:
    """整段只有报时/问时间（剥完没有实质内容）。"""
    t = str(text or "").strip()
    if not t:
        return False
    rest = re.sub(r"[\s\W_]+", "", scrub_transient_time(t))
    return len(rest) <= 2


def scrub_episode_summary(summary: str) -> str:
    """情节摘要（``用户：…\\nEnxi：…``）过滤：问时间的轮次整条丢弃（返回空串），
    否则逐句剥报时；剥完两侧都空 → 空串（调用方跳过写入）。"""
    s = str(summary or "")
    m = re.match(r"^用户：(.*?)\n(\S{1,12})：(.*)$", s, re.S)
    if not m:
        return "" if is_transient_time_only(s) else scrub_transient_time(s)
    user, who, bot = m.group(1), m.group(2), m.group(3)
    if is_time_query(user) or is_transient_time_only(user):
        return ""
    u = scrub_transient_time(user)
    b = scrub_transient_time(bot)
    if not re.sub(r"[\s\W_]+", "", u):
        return ""
    return f"用户：{u}\n{who}：{b}" if b else f"用户：{u}"
