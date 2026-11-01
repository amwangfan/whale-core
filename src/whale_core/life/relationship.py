"""G4：RelationshipState —— 与当前主体的关系视图（下一阶段计划 §6.1）。

纪律（§6 / §11 MEM-03、PER-01/02）：

- 只从**有来源且已授权**的互动事实更新：
  1. 互动计数：群会话取 GroupTimeline 中该 sender 的消息条数；本人私聊取
     该会话历史中的用户轮数（私聊历史无 sender 标签，只在会话可结构性证明
     属于该 principal 时才计数）；
  2. 自述事实：只读 ``ResolvedContext.allowed_memory_spaces`` 里已授权下发的
     ``member_private`` 空间（owner == principal_id）。群聊 ctx 永不携带
     member_private，因此群里的关系视图天然不含任何私人记忆（MEM-03）。
- 不引入固定亲密数值：只有 陌生/初识/熟悉 三档，全部由上述可复核事实
  驱动；无依据不脑补亲密——陌生人保持礼貌与分寸。
- 关系与授权完全独立（PER-01）：本模块只产出表达层上下文，不产生、不消费
  任何权限语义，读路径 fail closed（空间不在授权列表 = 不存在）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from whale_core.life.conversation_context import (
    ConversationKind,
    MemorySpaceType,
    ResolvedContext,
    principal_matches_private_conversation,
)
from whale_core.memory.history import load_history
from whale_core.memory.store import MemoryKind, MemoryStore

#: 熟悉档互动计数下限（无自述事实时的纯互动门槛）。
FAMILIAR_MIN_INTERACTIONS = 8
#: 有授权自述事实时，配合互动计数达到「熟悉」的下限。
FAMILIAR_WITH_FACTS_MIN = 2

#: 注入提示用的自述事实条数/长度上限（极简渲染纪律）。
_MAX_SELF_FACTS = 6
_MAX_FACT_CHARS = 80
_MAX_TOPICS = 4
_MAX_BOUNDARIES = 3

#: 共同话题词表：只对已授权自述做关键词命中，话题名是归并标签而非原文。
_TOPIC_LEXICON: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("电脑故障", ("电脑", "卡死", "死机", "蓝屏", "动不了", "很卡", "卡顿")),
    ("论文/作业", ("论文", "作业", "答辩", "要交", "截止")),
    ("考试/面试", ("考试", "面试")),
    ("工作/加班", ("加班", "出差", "入职", "工作")),
    ("身体/健康", ("感冒", "发烧", "生病")),
    ("生活琐事", ("搬家", "丢了", "坏了")),
)

#: 称呼偏好：只从「我叫/叫我/我是」式自述提取（与提炼管道的自述口径一致）。
_ADDRESS_RES = (
    re.compile(r"叫我\s*([^\s，。,.！!？?；;]{1,12})"),
    re.compile(r"我叫\s*([^\s，。,.！!？?；;]{1,12})"),
)

#: 互动边界：只从「我不喜欢/我讨厌/我怕…」式自述提取，原样短引用。
_BOUNDARY_RE = re.compile(r"我(?:不喜欢|讨厌|怕)([^，。,.；;！!？?]{1,12})")

# ---- issue #41：关系证据的可解释化（置信度 + 时间衰减） --------------------
#: 互动计数的时间衰减权重（只对有 recorded_at 的时间线生效；私聊历史无
#: 时间戳，按等权 1.0 计数并在 evidence 里如实说明）。
DECAY_RECENT_DAYS = 7
DECAY_MID_DAYS = 30
W_RECENT, W_MID, W_OLD = 1.0, 0.5, 0.2


def _weight(entry_recorded_at: str, now: datetime) -> float:
    if not entry_recorded_at:
        return W_RECENT
    try:
        ts = datetime.fromisoformat(str(entry_recorded_at))
    except ValueError:
        return W_RECENT
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days = (now - ts).days
    if days <= DECAY_RECENT_DAYS:
        return W_RECENT
    if days <= DECAY_MID_DAYS:
        return W_MID
    return W_OLD


def _uid_of(principal_id: str) -> str:
    """``anon:qq:{uin}`` / ``paired:qq:{uin}`` → 渠道账号（与提炼管道同口径）。"""
    parts = str(principal_id or "").split(":")
    return parts[-1] if len(parts) >= 3 else ""


@dataclass(frozen=True)
class RelationshipState:
    """当前主体在此会话中的关系视图（§2.1 权威领域对象）。

    ``self_facts`` 是已授权自述事实的短摘要（每条 ≤80 字），供表达策略判定
    技能水平等；渲染时只引用结构化字段，不直接回放大段原文。
    """

    principal_id: str
    familiarity: str                      # stranger | acquainted | familiar
    interaction_count: int
    shared_topics: Tuple[str, ...] = ()
    address_as: str = ""                  # 称呼偏好（空 = 无自述依据）
    boundaries: Tuple[str, ...] = ()
    self_facts: Tuple[str, ...] = field(default_factory=tuple)
    evidence: Tuple[str, ...] = ()        # 来源说明（审计用；不含秘密）
    # ---- issue #41：可解释证据 ----
    interaction_score: float = 0.0        # 时间衰减加权互动分
    recent_7d: int = 0                    # 近 7 天互动次数
    span_days: int = 0                    # 最早互动距今的天数
    corrections: int = 0                  # 用户纠正过的记忆条数（superseded）
    confidence: float = 0.0               # 0..1 证据置信度（公式见 resolver）

    @property
    def label(self) -> str:
        return {"stranger": "陌生", "acquainted": "初识",
                "familiar": "熟悉"}.get(self.familiarity, "陌生")

    def render(self) -> str:
        """极简自然语言摘要（注入 persona 上下文；不改变任何权限）。"""
        if self.familiarity == "stranger":
            return (
                f"关系：与这位用户是陌生人（尚无互动记录）。"
                "保持礼貌与分寸，不假装熟悉、不制造亲昵。"
            )
        parts = [f"关系：{self.label}（有 {self.interaction_count} 次互动记录）。"]
        if self.recent_7d:
            parts.append(f"近 7 天有 {self.recent_7d} 次互动（时间衰减已计入判断）。")
        if self.shared_topics:
            parts.append("共同话题：" + "、".join(self.shared_topics) + "。")
        if self.address_as:
            parts.append(f"对方自述可以称呼：{self.address_as}。")
        if self.boundaries:
            parts.append("对方自述的边界：" + "；".join(self.boundaries) + "。")
        parts.append("以上只来自已授权的互动记录与本人自述，"
                     "不要表现得比实际更熟。")
        return "\n".join(parts)


class RelationshipResolver:
    """从 ResolvedContext 计算当前主体的关系视图。无状态、纯读取。"""

    def __init__(self, store: MemoryStore, *, timeline=None,
                 now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self.store = store
        # GroupTimelineStore（可选）；无时间线时群互动计数为 0（fail closed）。
        self.timeline = timeline
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- 互动统计（原始计数 + 时间衰减加权 + 近期性/跨度，issue #41） --------

    def interaction_stats(self, ctx: ResolvedContext,
                          conversation_id: str = "") -> tuple[int, float, int, int]:
        """返回 (原始计数, 加权分, 近7天次数, 跨度天数)。只数有归属证据的消息。"""
        conv = ctx.conversation
        cid = conversation_id or conv.conversation_id
        uid = _uid_of(ctx.principal.principal_id)
        now = self._now()
        if conv.kind is ConversationKind.QQ_GROUP:
            if self.timeline is None or not uid:
                return 0, 0.0, 0, 0
            raw = 0
            score = 0.0
            recent_7d = 0
            oldest_days = 0
            for e in self.timeline.recent(cid, limit=1000):
                if str(e.get("sender_id") or "") == uid and not e.get("is_self"):
                    raw += 1
                    w = _weight(str(e.get("recorded_at") or ""), now)
                    score += w
                    ts = str(e.get("recorded_at") or "")
                    try:
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        days = (now - dt).days
                        if days <= DECAY_RECENT_DAYS:
                            recent_7d += 1
                        oldest_days = max(oldest_days, days)
                    except ValueError:
                        pass
            return raw, score, recent_7d, oldest_days
        # 私聊：历史无 sender 标签，只有会话可结构性证明属于该 principal 时，
        # 其中的用户轮才能计为该主体的互动（否则 0，不猜）。
        if principal_matches_private_conversation(
                conv.channel, conv.kind, cid, ctx.principal.principal_id):
            raw = sum(
                1 for turn in load_history(self.store, cid)
                if turn.get("role") == "user"
            )
            # 私聊历史无时间戳：等权计数，近期性未知（evidence 如实说明）
            return raw, float(raw), 0, 0
        return 0, 0.0, 0, 0

    def interaction_count(self, ctx: ResolvedContext,
                          conversation_id: str = "") -> int:
        return self.interaction_stats(ctx, conversation_id)[0]

    # -- 已授权自述事实（member_private 双门已由 resolver 把守） ---------------

    def self_facts_for(self, ctx: ResolvedContext) -> Tuple[str, ...]:
        """读取授权下发的 member_private 空间里的本人自述摘要。

        只遍历 ``ctx.allowed_memory_spaces`` 中类型为 MEMBER_PRIVATE 且空间
        owner 等于当前 principal 的空间——这是 MEM-03 的第二道闸：
        别人的成员空间、群聊场景（空间本就不在下发列表）都读不到。
        """
        pid = ctx.principal.principal_id
        facts: list[str] = []
        for ref in ctx.allowed_memory_spaces:
            if ref.type is not MemorySpaceType.MEMBER_PRIVATE:
                continue
            meta = self.store.get(f"space:{ref.space_id}")
            if not isinstance(meta, dict) or str(meta.get("owner") or "") != pid:
                continue  # 空间归属与主体不符 = 不读（污染数据 fail closed）
            for kind in (MemoryKind.PREFERENCE, MemoryKind.EPISODE):
                for rec in self.store.list_by_kind(
                        kind, space_ids=(ref.space_id,)):
                    # 与 memory.list-own 同口径：只取提炼管道/本人管理写入的
                    # member 记录；superseded（已被纠正）退出。
                    if "member" not in rec.tags or "superseded" in rec.tags:
                        continue
                    summary = str(rec.content.get("summary") or "").strip()
                    if summary and summary[:_MAX_FACT_CHARS] not in facts:
                        facts.append(summary[:_MAX_FACT_CHARS])
                    if len(facts) >= _MAX_SELF_FACTS:
                        return tuple(facts)
        return tuple(facts)

    # -- 用户纠正证据（issue #41：纠正可生效、可解释） ------------------------

    def corrections_for(self, ctx: ResolvedContext) -> int:
        """授权 member_private 空间里被纠正（superseded）的记录条数。"""
        pid = ctx.principal.principal_id
        n = 0
        for ref in ctx.allowed_memory_spaces:
            if ref.type is not MemorySpaceType.MEMBER_PRIVATE:
                continue
            meta = self.store.get(f"space:{ref.space_id}")
            if not isinstance(meta, dict) or str(meta.get("owner") or "") != pid:
                continue
            for kind in (MemoryKind.PREFERENCE, MemoryKind.EPISODE):
                n += sum(
                    1 for rec in self.store.list_by_kind(
                        kind, space_ids=(ref.space_id,))
                    if "member" in rec.tags and "superseded" in rec.tags)
        return n

    # -- 主入口 ---------------------------------------------------------------

    def state_for(self, ctx: ResolvedContext,
                  conversation_id: str = "") -> RelationshipState:
        count, score, recent_7d, span_days = self.interaction_stats(
            ctx, conversation_id)
        facts = self.self_facts_for(ctx)
        corrections = self.corrections_for(ctx)
        topics: list[str] = []
        address_as = ""
        boundaries: list[str] = []
        for fact in facts:
            for label, words in _TOPIC_LEXICON:
                if label not in topics and any(w in fact for w in words):
                    topics.append(label)
            if not address_as:
                for rx in _ADDRESS_RES:
                    m = rx.search(fact)
                    if m:
                        address_as = m.group(1)
                        break
            m = _BOUNDARY_RE.search(fact)
            if m:
                entry = f"不喜欢{m.group(1)}"
                if entry not in boundaries:
                    boundaries.append(entry)
        topics = topics[:_MAX_TOPICS]
        boundaries = boundaries[:_MAX_BOUNDARIES]

        # issue #41：熟悉度按**时间衰减加权分**判定（不只按消息总数）；
        # 阈值常数沿用原口径，避免旧部署关系档位跳变。
        if count <= 0 and not facts:
            familiarity = "stranger"
        elif score >= FAMILIAR_MIN_INTERACTIONS or (
                facts and score >= FAMILIAR_WITH_FACTS_MIN):
            familiarity = "familiar"
        else:
            familiarity = "acquainted"

        # 置信度：互动饱和度 + 自述事实 + 时间跨度 + 纠正记录（封顶 1.0）。
        confidence = min(1.0,
                         0.5 * min(score / FAMILIAR_MIN_INTERACTIONS, 1.0)
                         + 0.3 * min(len(facts) / 3, 1.0)
                         + 0.1 * min(span_days / 14, 1.0)
                         + 0.1 * min(corrections, 1))
        if familiarity == "stranger":
            confidence = 0.0

        evidence: list[str] = [f"interactions:{count}",
                               f"weighted:{score:.1f}"]
        if recent_7d:
            evidence.append(f"recent_7d:{recent_7d}")
        if span_days:
            evidence.append(f"span_days:{span_days}")
        if facts:
            evidence.append(f"member_private_facts:{len(facts)}")
        if corrections:
            evidence.append(f"user_corrections:{corrections}")
        return RelationshipState(
            principal_id=ctx.principal.principal_id,
            familiarity=familiarity,
            interaction_count=count,
            shared_topics=tuple(topics),
            address_as=address_as,
            boundaries=tuple(boundaries),
            self_facts=facts,
            evidence=tuple(evidence),
            interaction_score=round(score, 2),
            recent_7d=recent_7d,
            span_days=span_days,
            corrections=corrections,
            confidence=round(confidence, 2),
        )
