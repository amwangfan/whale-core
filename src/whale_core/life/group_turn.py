"""G4/#18：GroupTurnPolicy —— quiet/balanced/lively 的群聊参与决策。

定位与边界（issue #18）：

- 元数据优先级不变：显式 @ / 引用回复鲸鱼 → 由 ReplyTargetResolver 先判
  ``reply``；本策略只处理 resolver 判 ``observe`` 之后的**主动参与**决策。
- 规则先行，不为每条消息调用大模型；不给 LLM 决定身份/权限的能力。
- ``quiet`` 只回应直接提及；``balanced`` 谨慎少量参与；``lively`` 可更
  积极——但有明确的每日上限、冷却与连续发言限制。
- **时间静默 ≠ 群活跃度**：静默时段只压制「没人找我」的自主插话；群友
  @/引用的被动回复在 resolver 层不受影响，直呼其名/承接请求的也会回应
  （2026-11-01 owner 决策修正：此前静默时段把非 @ 的定向消息一并压制了）。
- 群友之间的互损/争执未点名 → 旁观；纯表情/图片无文 → 旁观。
- 话题滚动可回（吃/考试/星期等关键词，或近期连续闲聊）且本 bot 近窗
  未发言时，按小概率接 1 条（仍受冷却/日配额约束）；不做隐私审查过滤。
- 可选 ``participation_judge``（2026-11-12 owner 决策：减少主动插话）：
  装配后，规则信号（直呼其名/提问/承接/话题滚动）只作为**候选**，统一交由
  语义审批裁定 directed / valuable_join / observe（固定枚举严格解析，非法
  输出与异常一律 fail-closed 为 observe）；judge 存在时取代
  ``directed_classifier``（不双调）。quiet 档与时间静默只允许 directed，
  valuable_join 不突破。未装配 judge = 旧规则行为完全不变（离线兼容）。
- 每次决策记录 ``reason_code`` 到 kv（可观测：Studio/日志可查「为何这次
  没有发言」），只存原因码与时间，不存消息正文/模型自由理由等私人内容。
- 本策略不拓宽任何 ACL/Shell/设备/sudo 权限（PER-01）。

存储键（kv，双后端一致）：
- ``life:turnbudget:{date}:{group}`` → int（当日主动参与次数）
- ``life:turncool:{group}`` → iso 时间（上次主动参与时刻）
- ``life:turnlast:{group}`` → {reason_code, at}（最近一次决策诊断）
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from whale_core.memory.store import MemoryStore

#: 主动参与的日配额（按活跃度）。quiet 档只允许「直呼其名」的参与，
#: 配额仍小；lively 最宽松。定向回复（@/引用）在 resolver 层处理，
#: 不消耗本配额。
#: 群插话日配额（按档位）。**0 = 不限**（2026-11-05 owner 决策：不设上限，
#: 计数仍写账供观测；冷却已同日归零——连续发言限制仍在，不连发）。
DAILY_QUOTA = {"quiet": 0, "balanced": 0, "lively": 0}

#: 两次主动参与之间的最小间隔（分钟）。
#: 2026-11-05 owner 决策：关闭插话冷却（连续发言限制与日配额仍作保险丝）。
COOLDOWN_MIN = {"quiet": 0, "balanced": 0, "lively": 0}

#: 明确指向鲸鱼的文本信号（配合元数据 @；这里是「没 @ 但直呼其名提问」）。

#: 提问/求助信号（lively 档的参与依据之一）。
_QUESTION_RE = re.compile(
    r"[?？]|怎么|如何|为什么|为啥|请问|求[助教]|有人[会知]|怎么办|是什么")

def _has_content(text: str) -> bool:
    """剥掉标点/空白后还剩 ≥2 个有效字符才算「有内容」（防 bare ？/！ 误判）。"""
    return len(re.sub(r"[\s\W_]+", "", str(text or ""))) >= 2


#: 承接请求信号（lively 档补充，2026-11-01 生产实证）：机器人近期在群里发过言
#: 时的「那你画个…吧」「你再唱一首」类跟进句式——无 @ 无名但明确是叫我做事。
_FOLLOWUP_RE = re.compile(
    r"^(?:那|来|快|再|接着|继续|要不)?\s*你(?:给我|给我来|来|快|再|接着|继续)?\s*"
    r"(?:画|搜|查|找|写|做|唱|讲|说|算|翻译|推荐|放|拍|帮|发|生成|介绍|念|读|背|猜)")

#: 诊断原因码（稳定枚举；Studio 可展示）。
REASON_AT_BOT = "at_bot"
REASON_REPLY_TO_BOT = "reply_to_bot"
REASON_QUIET_MODE = "quiet_mode"            # quiet 档：只回应直接提及
REASON_NOT_DIRECTED = "not_directed"        # 无指向信号
REASON_QUIET_HOURS = "quiet_hours"          # 时间静默：压制非定向自主插话
REASON_QUIET_DIRECTED = "quiet_directed"    # 时间静默但被明确指向（直呼其名/承接），仍回应
REASON_DND = "dnd"                          # 会话免打扰
REASON_BUDGET = "budget_exceeded"           # 当日主动参与配额用尽
REASON_COOLDOWN = "cooldown"                # 距上次主动参与太近
REASON_CONSECUTIVE = "consecutive_limit"    # 上一条已是鲸鱼发言，不连发
REASON_JOIN = "join"                        # 主动参与（lively/balanced 命中依据）
REASON_REPEAT_CHAIN = "repeat_chain"        # 复读跟风（群友刷屏链 +1）
REASON_TOPIC_ROLL = "topic_roll"            # 话题滚动小概率接话
REASON_MEDIA_ONLY = "media_only"            # 纯表情/图片无文
REASON_ALREADY_COVERED = "already_covered"  # 刚有人已完整接住
REASON_JUDGE_OBSERVE = "judge_observe"      # 语义审批否决候选（参与审批判旁观）
REASON_VALUABLE_JOIN = "valuable_join"      # 语义审批：非定向但明显值得接一句

#: participation_judge 的裁定枚举（固定取值，严格解析；此外一律 observe）。
VERDICT_DIRECTED = "directed"        # 这条是在找鲸鱼（叫她回应/让她做事/对她跟进）
VERDICT_VALUABLE = "valuable_join"   # 没人找她，但自然插一句明显有价值
VERDICT_OBSERVE = "observe"          # 旁观

_VERDICTS = (VERDICT_DIRECTED, VERDICT_VALUABLE, VERDICT_OBSERVE)


def parse_participation_verdict(raw) -> str:
    """严格解析裁定：只认固定枚举（去空白/尾标点、小写归一），其余 fail-closed
    为 observe。模型自由发挥的理由永不落账、永不放行。"""
    s = str(raw or "").strip().lower().strip("。．.!！?？,，;；:：\"' ")
    return s if s in _VERDICTS else VERDICT_OBSERVE

#: 话题滚动关键词（吃/考试/星期等）；命中 + bot 近窗未发言 → 小概率可回。
_TOPIC_RE = re.compile(
    r"吃|饭|餐|饿|外卖|食堂|考试|测验|期末|期中|作业|交卷|"
    r"星期|周[一二三四五六日天]|周末|周一|周五|放假|开学")

#: 媒体/表情占位（无实质正文）
_MEDIA_ONLY_RE = re.compile(
    r"^(?:\s*|\[(?:图|图片|表情|语音|视频|动画表情|戳一戳)\]|[🌀-🫿✀-➿]+)+$")

#: 话题滚动默认概率（可被构造参数覆盖）；冷却/配额仍生效。
DEFAULT_TOPIC_JOIN_PROB = 0.18
#: 判定「近窗未发言」的时间线条数
_TOPIC_SILENCE_WINDOW = 6
#: 「刚有人已完整接住」：上一条非 bot 且较长，且本条很短/附和
_COVERED_MIN_LEN = 16

#: 复读链判定上限：文本超长（copypasta/广告风险）不参与
_REPEAT_MAX_LEN = 50
#: 跟风时点窗口：链太短不成链、太长不凑热闹
_REPEAT_MIN = 3
_REPEAT_MAX = 12


def _repeat_chain(entries) -> "str | None":
    """时间线末尾的复读链判定（2026-11-02 owner 决策：刷屏时跟风 +1）。

    命中条件（全部）：
    - 末尾连续 ≥3 条规范化后同文本（含当前这条新消息——它本就是链的一环）；
    - ≥2 个不同发送者（同一个人刷屏不算链）；
    - 机器人不在链尾（已参与过就不再跟——每条链最多跟一次）；
    - 文本短、无链接/媒体占位/内部标记（复读内容原样发，安全起见只跟纯文本短链）。
    命中返回链文本；否则 None。
    """
    tail = list(entries or ())[-_REPEAT_MAX:]
    texts: list[str] = []
    senders: set[str] = set()
    for e in reversed(tail):
        if not isinstance(e, dict):
            break
        if e.get("is_self"):
            break  # 机器人已在链中 → 链对我关闭
        t = " ".join(str(e.get("text") or "").split())
        if not t:
            break
        if texts and t != texts[0]:
            break
        texts.append(t)
        senders.add(str(e.get("sender_id") or e.get("sender_uid") or ""))
    if len(texts) < _REPEAT_MIN or len(senders) < 2:
        return None
    t = texts[0]
    if (len(t) > _REPEAT_MAX_LEN or "http" in t or "[[" in t
            or "[图片" in t or "[语音]" in t or "@" in t):
        return None
    return t



def _media_only(text: str) -> bool:
    """纯表情/图片占位/无实质文字 → True（旁观）。

    短中文（如「嗯」）与标点（如「？」）不算媒体——交给后续 not_directed /
    question 规则；此处只认空串、平台媒体占位与纯 emoji。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if _MEDIA_ONLY_RE.match(t):
        return True
    stripped = re.sub(r"\[(?:图|图片|表情|语音|视频|动画表情|戳一戳)\]", "", t).strip()
    if not stripped:
        return True
    return bool(_MEDIA_ONLY_RE.match(stripped))


def _bot_silent_recently(entries, *, window: int = _TOPIC_SILENCE_WINDOW) -> bool:
    """近窗内机器人未发言。"""
    for e in list(entries or ())[-window:]:
        if isinstance(e, dict) and e.get("is_self"):
            return False
    return True


def _topic_roll_eligible(text: str, entries) -> bool:
    """话题在滚：关键词命中，或近窗连续闲聊（≥3 条他人有内容消息）。"""
    if _TOPIC_RE.search(str(text or "")):
        return True
    others = 0
    for e in list(entries or ())[-_TOPIC_SILENCE_WINDOW:]:
        if not isinstance(e, dict) or e.get("is_self"):
            continue
        if _has_content(str(e.get("text") or "")):
            others += 1
    return others >= 3


def _already_covered(text: str, entries) -> bool:
    """刚有人已完整接住：上一条他人长回复，本条短附和/表情 → 旁观。"""
    tail = [e for e in list(entries or ()) if isinstance(e, dict)]
    if len(tail) < 1:
        return False
    prev = tail[-1]
    if prev.get("is_self"):
        return False
    prev_text = str(prev.get("text") or "").strip()
    cur = str(text or "").strip()
    if len(prev_text) < _COVERED_MIN_LEN:
        return False
    # 本条很短或纯附和
    if len(cur) <= 8 or re.fullmatch(r"(?i)(哈哈+|嗯+|哦+|好+|收到|确实|同意|一样|＋1|\+1)+", cur):
        return True
    return False


def _split_timeline(entries, message_id: str):
    """按当前消息 ID 把时间线切成 (history, current_entry)。

    - 命中：history = 当前消息**之前**的条目（其后到达的「未来」条目丢弃，
      防止合批/并发期间新消息污染规则判定与审批上下文）；
    - ``message_id`` 为空：history 原样、current None（legacy 调用/测试兼容，
      调用方保证无未来条目）；
    - 给了 ID 但未命中：history = ()、current None——定位缺失时宁可没有
      历史，也不把可能是未来的条目喂给 bot_recent/topic/repeat 等规则
      （2026-11-12 reviewer 决策：不降级尾窗）。

    规则判定一律用 history；复读链用 history + current（当前消息本就是
    链的一环）；连续发言限制用 history + current（含当前：前一条是机器人
    自己而当前是群友新消息时，不构成「连发」，judge-directed 承接不被误挡）。
    """
    seq = [e for e in (entries or ()) if isinstance(e, dict)]
    if not message_id:
        return tuple(seq), None
    for i, e in enumerate(seq):
        if str(e.get("message_id") or "") == str(message_id):
            return tuple(seq[:i]), e
    return (), None


@dataclass(frozen=True)
class ParticipationQuery:
    """交给 participation_judge 的一次语义审批请求（宿主据此构造 prompt）。

    ``signals``：命中的规则候选（"named"/"question"/"follow_up"/"topic_roll"），
    仅供宿主在 prompt 中说明；裁定以语义为准。
    ``timeline_entries`` 为按 ``message_id`` 截断后的历史 + 当前条目（policy
    已排未来；宿主构造 prompt 时再截一次是幂等保险）。
    ``sender_uid``/``mentions``/``reply_to``/``send_time`` 为当前消息的真实
    元数据（从时间线条目提取；缺失为空）——prompt 据此标注当前消息的
    回复对象与 @ 关系，不猜、不坍缩。
    """

    text: str
    activity: str
    conversation: str = ""
    timeline_entries: tuple = ()
    signals: Tuple[str, ...] = ()
    bot_spoke_recently: bool = False
    sender_id: str = ""
    sender_uid: str = ""
    message_id: str = ""
    mentions: Tuple[str, ...] = ()
    reply_to: Tuple[str, ...] = ()
    send_time: str = ""


@dataclass(frozen=True)
class JoinDecision:
    """主动参与决策结果。action ∈ {"join", "observe"}。

    ``text``：复读跟风时携带的链原文（调用方直接发送，不过模型——
    复读的精髓就是一字不差）；普通参与为空串。
    """

    action: str
    reason_code: str
    text: str = ""


class GroupTurnPolicy:
    """基于规则的群聊主动参与策略（确定性；不调模型）。"""

    def __init__(self, store: MemoryStore, *,
                 bot_names: Tuple[str, ...] = (),
                 now_fn: Optional[Callable[[], datetime]] = None,
                 quiet_now: Optional[Callable[[], bool]] = None,
                 directed_classifier=None, chain_judge=None,
                 participation_judge=None,
                 topic_join_prob: float = DEFAULT_TOPIC_JOIN_PROB,
                 rng: Optional[Callable[[], float]] = None):
        self.store = store
        # Host supplies explicit bot aliases; core ships no character names.
        self._names = tuple(n for n in bot_names if n)
        self._name_re = (re.compile("|".join(re.escape(n) for n in self._names))
                         if self._names else None)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        # 时间静默判定由宿主注入（ProactiveOutbox._is_quiet + prefs）；
        # 缺省 = 永不静默。
        self._quiet_now = quiet_now or (lambda: False)
        # 2026-11-02 owner 决策：规则信号无法判定时，用意图模型兜底判「这条
        # 是不是在找鲸鱼」（生产实证：「说出来各位的名字哦」无「你」无问号，
        # 规则全不命中被静默压制）。可调用 (text, timeline_entries) -> bool；
        # 仅在机器人近窗发过言（对话进行中）且规则不命中时调用，调用有界。
        self._directed_classifier = directed_classifier
        # 2026-11-12 owner 决策：可选语义审批。装配后规则信号降级为候选，
        # 由 judge 统一裁定 directed/valuable_join/observe（严格解析，异常与
        # 非法输出 fail-closed 为 observe）；judge 存在时不再调用
        # directed_classifier（避免双调）。可调用 (ParticipationQuery) -> str。
        self._participation_judge = participation_judge
        # 2026-11-02 owner 决策：复读链命中后仍要判断「该不该跟」——可调用
        # (chain_text, timeline_entries) -> bool（意图模型）；None = 命中即跟
        # （测试/离线默认）。判定器异常 fail-closed 为不跟（宁可少跟不乱跟）。
        # 复读跟风不经过 participation_judge：chain_judge 是其唯一判定。
        self._chain_judge = chain_judge
        # 话题滚动小概率；rng 可注入以便测例确定性（返回 [0,1)）。
        self._topic_join_prob = max(0.0, min(1.0, float(topic_join_prob)))
        self._rng = rng or random.random

    # -- 主入口 -------------------------------------------------------------

    def maybe_join(self, *, conversation: str, text: str,
                   activity: str, dnd: bool = False,
                   timeline_entries=(),
                   sender_id: str = "", message_id: str = "") -> JoinDecision:
        """对一条 resolver 判 observe 的群消息，决定是否主动参与。

        ``activity`` 来自 ConversationRhythm（quiet/balanced/lively）。
        ``timeline_entries`` 为该群近期时间线（含 is_self 标记）。
        ``sender_id``/``message_id`` 是当前消息的发送者与 ID（供语义审批
        标注归属、按当前消息截断时间线）；缺省为空（旧调用兼容）。
        """
        group = str(conversation)
        if activity not in DAILY_QUOTA:
            activity = "balanced"
        if dnd:
            return self._record(group, JoinDecision("observe", REASON_DND))

        # 2026-11-12 reviewer 决策：入口先按当前 message_id 切快照——
        # 普通规则只看「当前之前」的历史，未来条目（合批/并发期间到达）
        # 绝不参与判定；复读链用 历史+当前；连续发言限制用 含当前 的快照。
        history, current_entry = _split_timeline(timeline_entries, message_id)
        chain_entries = history + ((current_entry,) if current_entry else ())
        snapshot_with_current = chain_entries

        raw_text = str(text or "")
        if _media_only(raw_text):
            return self._record(group, JoinDecision("observe", REASON_MEDIA_ONLY))
        # judge 路径下「刚有人已完整接住」的短句场景交模型语义判定
        # （短句也可能是明确请求「Enxi帮我」「那你画吧」——硬短句拦截会
        # 误杀真实请求，2026-11-12 reviewer 决策）；legacy 路径维持硬拦截不变。
        if self._participation_judge is None \
                and _already_covered(raw_text, history):
            return self._record(group, JoinDecision("observe", REASON_ALREADY_COVERED))

        # 复读链检测（lively 档；非静默时段）：群友刷屏复读时跟风 +1。
        # 复读文本由调用方原文直发（不过模型）；冷却/日配额与普通插话同一保险丝。
        # 命中后过 chain_judge 判断该不该跟（2026-11-02 owner 决策）。
        repeat_text: "str | None" = None
        chain_rejected = False
        if activity == "lively" and not self._quiet_now():
            repeat_text = _repeat_chain(chain_entries)
            if repeat_text is not None and self._chain_judge is not None:
                try:
                    if not self._chain_judge(repeat_text, chain_entries):
                        repeat_text = None
                        chain_rejected = True
                except Exception:
                    repeat_text = None  # 判定器故障 = 不跟（宁可少跟）
                    chain_rejected = True
        if chain_rejected:
            # 复读链由 chain_judge 唯一决策（2026-11-12 reviewer 决策）：
            # 否决 = 本轮旁观，不经 topic_roll / 语义审批复活，也不再送审
            # （不双调）。
            return self._record(group, JoinDecision("observe", REASON_NOT_DIRECTED))

        text = str(text or "")
        named = bool(self._name_re.search(text)) if self._name_re else False
        # 提问信号要有实际内容：光一个「？」不算提问（2026-11-02 生产实证：
        # 单个问号被 lively 当提问接话，还对上了旧话茬，答非所问）。
        question = bool(_QUESTION_RE.search(text)) and _has_content(text)
        # 机器人近窗（近 5 条历史）发过言 = 对话可能仍在进行（judge/兜底判定/
        # 承接句式都以它为门槛，保证语义调用有界——不是每条群消息都问模型）。
        bot_recent = any(isinstance(e, dict) and e.get("is_self")
                         for e in history[-5:])
        # lively 补充信号：时间线近窗有机器人自己的发言（我刚参与过），
        # 且本条是「你+请求动词」的承接句式 → 视为叫我做事的跟进请求。
        follow_up = (
            activity == "lively"
            and bool(_FOLLOWUP_RE.search(text))
            and bot_recent
        )

        # 话题滚动可回：关键词或近窗连续闲聊 + bot 近窗未发言 + 小概率。
        # 提前到静默判定之前计算：它是 judge 的候选之一（judge 仍可否决）。
        topic_roll = False
        if activity in ("balanced", "lively"):
            topic_hit = (
                not self._quiet_now()
                and _bot_silent_recently(history)
                and _topic_roll_eligible(text, history)
            )
            if topic_hit and self._topic_join_prob > 0:
                try:
                    topic_roll = bool(self._rng() < self._topic_join_prob)
                except Exception:
                    topic_roll = False

        # 2026-11-12 owner 决策：装配 participation_judge 时，规则信号
        # （named/question/follow_up/topic_roll）只是**候选**，统一交语义审批；
        # 无字面信号但机器人近窗发过言时同样送审（semantic followup——
        # 「说出来各位的名字哦」类无「你」无问号的定向跟进不被漏掉）。
        # 无候选且机器人近期没说话 → 不调用（调用有界）。judge 存在时
        # directed_classifier 不再调用（避免双调）。复读链命中直发不送审
        # （chain_judge 已唯一决策，不双调）。
        verdict: "str | None" = None
        if self._participation_judge is not None and repeat_text is None:
            sig = []
            if named:
                sig.append("named")
            if question and activity == "lively":
                sig.append("question")
            if follow_up:
                sig.append("follow_up")
            if topic_roll:
                sig.append("topic_roll")
            if sig or bot_recent:
                query = ParticipationQuery(
                    text=text, activity=activity, conversation=group,
                    timeline_entries=chain_entries,
                    signals=tuple(sig), bot_spoke_recently=bot_recent,
                    sender_id=str(
                        (current_entry or {}).get("sender_id")
                        or sender_id or ""),
                    sender_uid=str((current_entry or {}).get("sender_uid")
                                   or ""),
                    message_id=str(message_id or ""),
                    mentions=tuple(str(m) for m in
                                   ((current_entry or {}).get("mentions")
                                    or ())),
                    reply_to=tuple(str(r) for r in
                                   ((current_entry or {}).get("reply_to")
                                    or ())),
                    send_time=str((current_entry or {}).get("send_time")
                                  or ""))
                try:
                    verdict = parse_participation_verdict(
                        self._participation_judge(query))
                except Exception:
                    verdict = VERDICT_OBSERVE  # 审批故障 = 旁观（宁可少说）
        judge_directed = verdict == VERDICT_DIRECTED
        judge_valuable = verdict == VERDICT_VALUABLE

        # 意图模型兜底定向判定（2026-11-02 owner 决策）：规则全不命中、且
        # 机器人近窗发过言（对话进行中）时，问一句模型「这条是不是在找鲸鱼」。
        # 仅 legacy 路径（未装配 participation_judge）。
        llm_directed = False
        if self._participation_judge is None \
                and not (named or follow_up) \
                and self._directed_classifier is not None and bot_recent:
            try:
                llm_directed = bool(self._directed_classifier(
                    text, timeline_entries))
            except Exception:
                llm_directed = False  # 判定器故障 = 无信号，不放大

        # 时间静默（2026-11-01 owner 决策修正）：只压制「没人找我」的自主插话；
        # 直呼其名 / 承接请求 / 模型判定定向 = 群里明确在找我，照常回应
        # （且不耗主动配额/冷却——本质是回复而非插话；配额留给真正的主动插话）。
        # judge 路径：只有 directed 能突破静默；valuable_join 不能（2026-11-12）。
        if self._quiet_now():
            if self._participation_judge is not None:
                if judge_directed:
                    return self._record(
                        group, JoinDecision("join", REASON_QUIET_DIRECTED))
                return self._record(
                    group, JoinDecision("observe", REASON_QUIET_HOURS))
            if not (named or follow_up or llm_directed):
                return self._record(group, JoinDecision("observe", REASON_QUIET_HOURS))
            return self._record(group, JoinDecision("join", REASON_QUIET_DIRECTED))

        if activity == "quiet":
            # quiet：只回应直接提及（@/引用已被 resolver 拦截；judge 路径下
            # 仅 directed 放行——valuable_join 不突破 quiet 档）。
            if self._participation_judge is not None:
                if not judge_directed:
                    return self._record(group, JoinDecision(
                        "observe", REASON_JUDGE_OBSERVE if verdict is not None
                        else REASON_QUIET_MODE))
            elif not named:
                return self._record(group, JoinDecision("observe", REASON_QUIET_MODE))
        else:
            if self._participation_judge is not None:
                if not (judge_directed or judge_valuable
                        or repeat_text is not None):
                    # 审批否决候选；未送审（无候选且机器人近期未发言）维持原判
                    return self._record(group, JoinDecision(
                        "observe", REASON_JUDGE_OBSERVE if verdict is not None
                        else REASON_NOT_DIRECTED))
            elif not (named or follow_up or llm_directed or repeat_text is not None
                      or (activity == "lively" and question) or topic_roll):
                # balanced：直呼其名/承接/话题滚动；lively：另含提问/复读链
                return self._record(group, JoinDecision("observe", REASON_NOT_DIRECTED))

        # 连续发言限制：含当前的截断快照最新一条已是鲸鱼自己 → 不再主动接话。
        # （当前消息是群友新消息时不构成连发——前一条是机器人、当前有人承接
        # 的 judge-directed 放行；2026-11-12 reviewer 决策。）
        if snapshot_with_current:
            last = snapshot_with_current[-1]
            if isinstance(last, dict) and last.get("is_self"):
                return self._record(
                    group, JoinDecision("observe", REASON_CONSECUTIVE))

        # 冷却
        cooldown = COOLDOWN_MIN[activity]
        if cooldown > 0:
            last_join = self._get_iso(f"life:turncool:{group}")
            if last_join is not None and \
                    self._now() - last_join < timedelta(minutes=cooldown):
                return self._record(group, JoinDecision("observe", REASON_COOLDOWN))

        # 日配额（0=不限，仍计数供观测）
        budget_key = f"life:turnbudget:{self._now().date().isoformat()}:{group}"
        used = self.store.get(budget_key)
        used = int(used) if isinstance(used, (int, float)) else 0
        quota = DAILY_QUOTA[activity]
        if quota > 0 and used >= quota:
            return self._record(group, JoinDecision("observe", REASON_BUDGET))

        # 通过：记账 + 冷却 + 诊断
        self.store.put(budget_key, used + 1)
        self.store.put(f"life:turncool:{group}", self._now().isoformat())
        if repeat_text:
            reason = REASON_REPEAT_CHAIN
        elif judge_valuable and not judge_directed:
            reason = REASON_VALUABLE_JOIN
        elif topic_roll and not (named or follow_up or llm_directed
                                 or (activity == "lively" and question)):
            reason = REASON_TOPIC_ROLL
        else:
            reason = REASON_JOIN
        return self._record(group, JoinDecision(
            "join", reason, text=repeat_text or ""))

    def last_decision(self, conversation: str) -> Optional[dict]:
        """最近一次参与决策诊断（reason_code + at；无私人内容）。"""
        data = self.store.get(f"life:turnlast:{conversation}")
        return dict(data) if isinstance(data, dict) else None

    # -- 内部 ---------------------------------------------------------------

    def _record(self, group: str, decision: JoinDecision) -> JoinDecision:
        self.store.put(f"life:turnlast:{group}", {
            "reason_code": decision.reason_code,
            "action": decision.action,
            "at": self._now().isoformat(),
        })
        return decision

    def _get_iso(self, key: str) -> Optional[datetime]:
        raw = self.store.get(key)
        if not isinstance(raw, str) or not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
