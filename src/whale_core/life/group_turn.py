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
- 群友之间的表白、争执、私事不认领、不介入、不转发（无规则命中即
  observe）。
- 每次决策记录 ``reason_code`` 到 kv（可观测：Studio/日志可查「为何这次
  没有发言」），只存原因码与时间，不存消息正文等私人内容。
- 本策略不拓宽任何 ACL/Shell/设备/sudo 权限（PER-01）。

存储键（kv，双后端一致）：
- ``life:turnbudget:{date}:{group}`` → int（当日主动参与次数）
- ``life:turncool:{group}`` → iso 时间（上次主动参与时刻）
- ``life:turnlast:{group}`` → {reason_code, at}（最近一次决策诊断）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from whale_core.memory.store import MemoryStore

#: 主动参与的日配额（按活跃度）。quiet 档只允许「直呼其名」的参与，
#: 配额仍小；lively 最宽松。定向回复（@/引用）在 resolver 层处理，
#: 不消耗本配额。
DAILY_QUOTA = {"quiet": 3, "balanced": 6, "lively": 12}

#: 两次主动参与之间的最小间隔（分钟）。
COOLDOWN_MIN = {"quiet": 60, "balanced": 20, "lively": 8}

#: 明确指向鲸鱼的文本信号（配合元数据 @；这里是「没 @ 但直呼其名提问」）。

#: 提问/求助信号（lively 档的参与依据之一）。
_QUESTION_RE = re.compile(
    r"[?？]|怎么|如何|为什么|为啥|请问|求[助教]|有人[会知]|怎么办|是什么")

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


@dataclass(frozen=True)
class JoinDecision:
    """主动参与决策结果。action ∈ {"join", "observe"}。"""

    action: str
    reason_code: str


class GroupTurnPolicy:
    """基于规则的群聊主动参与策略（确定性；不调模型）。"""

    def __init__(self, store: MemoryStore, *,
                 bot_names: Tuple[str, ...] = (),
                 now_fn: Optional[Callable[[], datetime]] = None,
                 quiet_now: Optional[Callable[[], bool]] = None,
                 directed_classifier=None):
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

    # -- 主入口 -------------------------------------------------------------

    def maybe_join(self, *, conversation: str, text: str,
                   activity: str, dnd: bool = False,
                   timeline_entries=()) -> JoinDecision:
        """对一条 resolver 判 observe 的群消息，决定是否主动参与。

        ``activity`` 来自 ConversationRhythm（quiet/balanced/lively）。
        ``timeline_entries`` 为该群近期时间线（含 is_self 标记）。
        """
        group = str(conversation)
        if activity not in DAILY_QUOTA:
            activity = "balanced"
        if dnd:
            return self._record(group, JoinDecision("observe", REASON_DND))

        text = str(text or "")
        named = bool(self._name_re.search(text)) if self._name_re else False
        question = bool(_QUESTION_RE.search(text))
        # lively 补充信号：时间线近窗有机器人自己的发言（我刚参与过），
        # 且本条是「你+请求动词」的承接句式 → 视为叫我做事的跟进请求。
        follow_up = (
            activity == "lively"
            and bool(_FOLLOWUP_RE.search(text))
            and any(isinstance(e, dict) and e.get("is_self")
                    for e in list(timeline_entries)[-5:])
        )

        # 意图模型兜底定向判定（2026-11-02 owner 决策）：规则全不命中、且
        # 机器人近窗发过言（对话进行中）时，问一句模型「这条是不是在找鲸鱼」。
        llm_directed = False
        if not (named or follow_up) and self._directed_classifier is not None \
                and any(isinstance(e, dict) and e.get("is_self")
                        for e in list(timeline_entries)[-5:]):
            try:
                llm_directed = bool(self._directed_classifier(
                    text, timeline_entries))
            except Exception:
                llm_directed = False  # 判定器故障 = 无信号，不放大

        # 时间静默（2026-11-01 owner 决策修正）：只压制「没人找我」的自主插话；
        # 直呼其名 / 承接请求 / 模型判定定向 = 群里明确在找我，照常回应
        # （且不耗主动配额/冷却——本质是回复而非插话；配额留给真正的主动插话）。
        if self._quiet_now():
            if not (named or follow_up or llm_directed):
                return self._record(group, JoinDecision("observe", REASON_QUIET_HOURS))
            return self._record(group, JoinDecision("join", REASON_QUIET_DIRECTED))

        if activity == "quiet":
            # quiet：只回应直接提及（@/引用已被 resolver 拦截；这里是直呼其名）
            if not named:
                return self._record(group, JoinDecision("observe", REASON_QUIET_MODE))
        elif not (named or follow_up or llm_directed
                  or (activity == "lively" and question)):
            # balanced：需直呼其名或承接；lively：直呼其名 或 明确提问/求助/承接
            return self._record(group, JoinDecision("observe", REASON_NOT_DIRECTED))

        # 连续发言限制：时间线最新一条已是鲸鱼自己 → 不再主动接话
        if timeline_entries:
            last = timeline_entries[-1]
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

        # 日配额
        budget_key = f"life:turnbudget:{self._now().date().isoformat()}:{group}"
        used = self.store.get(budget_key)
        used = int(used) if isinstance(used, (int, float)) else 0
        if used >= DAILY_QUOTA[activity]:
            return self._record(group, JoinDecision("observe", REASON_BUDGET))

        # 通过：记账 + 冷却 + 诊断
        self.store.put(budget_key, used + 1)
        self.store.put(f"life:turncool:{group}", self._now().isoformat())
        return self._record(group, JoinDecision("join", REASON_JOIN))

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
