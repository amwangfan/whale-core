"""G2：成员记忆提炼管道（GroupTimeline → member_private）。

纪律（下一阶段计划 §4.2 / §11 MEM-02/03/04）：

- 只把**该成员自己明确说过**的事实与情节归到其名下；其他群友的评论与
  推测不进入任何人的长期档案。
- 默认规则提取，刻意保守：显式自述「我…」+ 明确事件关键词；「不懂电脑」
  类能力画像不抽取；问句、过短/过长片段不抽取。
- 敏感内容（手机号/身份证/银行卡/地址/密码类）**提高阈值直接不抽取**：
  命中即整条消息放弃。
- 幂等键 = principal + source_message_ids + extraction_version；重复跑
  不产生重复记录，撤销后不重跑复活。
- consent 无效即丢弃候选（写入闸门在 consent.valid_for_group）。
- 每条记忆带来源群、消息 ID、置信度、TTL（expires_at）、consent 版本，
  支持回看依据（MEM-04）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from whale_core.memory.bindings import BindingStore
from whale_core.memory.consent import MemberConsentStore
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore
from whale_core.memory.timeline import GroupTimelineStore

#: 提炼规则版本：规则变更时递增，幂等键随之变化（旧记录按 TTL 自然过期）。
EXTRACTION_VERSION = 1

#: 回填水位键：``member:backfill:{principal_id}`` →
#: ``{"not_before": iso, "done_at": iso}``。not_before 只约束回填窗口
#: （退出期间不补采）；正常新消息提炼不看它。
BACKFILL_KEY_PREFIX = "member:backfill:"

#: 单次回填最多覆盖的来源群数（有界；时间线本身还有 200 条/48h 上限）。
BACKFILL_MAX_GROUPS = 50

#: 情节默认保留 90 天；带明确近期时间词的事件 14 天；自述偏好 180 天。
DEFAULT_EPISODE_TTL_S = 90 * 86400
SHORT_EVENT_TTL_S = 14 * 86400
PREFERENCE_TTL_S = 180 * 86400

# ---------------------------------------------------------------------------
# 规则表（保守；宁缺毋滥）
# ---------------------------------------------------------------------------

_FIRST_PERSON = ("我", "咱")

#: 敏感内容：命中即整条消息不抽取（提高阈值 = 直接放弃，不是降级保存）。
_SENSITIVE_RES = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),        # 手机号
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),        # 身份证
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),           # 银行卡/长号码
)
_SENSITIVE_WORDS = (
    "密码", "口令", "验证码", "身份证", "银行卡", "住址", "地址", "收货",
    "住在", "家住",
    "password", "passwd", "secret", "api_key", "apikey",
)

#: 明确事件词（情节候选）。
_EVENT_WORDS = (
    "卡死", "死机", "动不了", "蓝屏", "崩溃", "很卡", "卡顿",
    "要交", "截止", "deadline", "考试", "面试", "出差", "加班",
    "感冒", "发烧", "生病", "搬家", "丢了", "坏了",
    "论文", "答辩", "开学", "入职",
)

#: 显式自述（稳定偏好候选）。「我不懂/不会…」类能力画像**不**抽取（§4.2：
#: 低置信度自述不应固化成永久能力画像）。
_SELF_FACT_RE = re.compile(r"我(?:是|叫|来自|喜欢|讨厌|怕|养|住)")
_ABILITY_CLAIM_RE = re.compile(r"我(?:不懂|不会|不清楚|不擅长)")

#: 近期时间词 → 短 TTL（「明天交论文」是临时事件，不是长期现状）。
_DEADLINE_WORDS = ("明天", "今天", "今晚", "后天", "下周", "周末", "马上", "一会")

_SEGMENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def contains_sensitive(text: str) -> bool:
    """敏感内容检测（手机号/身份证/银行卡/地址/凭据类）。"""
    if any(rx.search(text) for rx in _SENSITIVE_RES):
        return True
    lowered = text.lower()
    return any(w in lowered for w in _SENSITIVE_WORDS)


def sender_id_for_principal(principal_id: str) -> str:
    """``anon:qq:{uin}`` / ``paired:qq:{uin}`` → 渠道账号（uin）。"""
    parts = str(principal_id or "").split(":")
    return parts[-1] if len(parts) >= 3 else ""


class ExtractionError(ValueError):
    """提炼被拒绝（空间缺失 / 参数非法）。"""


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待写入的成员记忆候选（§2.1 权威领域对象）。"""

    principal_id: str
    source_group: str
    source_message_ids: Tuple[str, ...]
    summary: str
    kind: MemoryKind           # EPISODE / PREFERENCE
    confidence: float
    ttl_s: float
    consent_version: int
    observed_at: str

    @property
    def idempotency_key(self) -> str:
        """principal + source_message_ids + extraction_version 去重键。"""
        h = hashlib.sha256()
        h.update(self.principal_id.encode("utf-8"))
        for mid in sorted(self.source_message_ids):
            h.update(b"\x00")
            h.update(mid.encode("utf-8"))
        h.update(b"\x00v")
        h.update(str(EXTRACTION_VERSION).encode("ascii"))
        return f"member:ex:{h.hexdigest()[:24]}"

    @property
    def record_key(self) -> str:
        h = hashlib.sha256()
        h.update(self.idempotency_key.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.summary.encode("utf-8"))
        return f"member-mem:{h.hexdigest()[:20]}"


@dataclass
class ExtractionStats:
    """一次提炼运行的可观测结果。"""

    scanned: int = 0              # 该成员在来源群的消息条数
    candidates: int = 0
    written: int = 0
    skipped_sensitive: int = 0
    skipped_duplicate: int = 0
    dropped_no_consent: int = 0   # consent 无效被丢弃的候选数
    record_keys: list = field(default_factory=list)


class MemberMemoryExtractor:
    """从群时间线提炼某成员的本人自述记忆。"""

    def __init__(self, store: MemoryStore, timeline: GroupTimelineStore,
                 consents: MemberConsentStore, bindings: BindingStore,
                 spaces: SpaceStore):
        self.store = store
        self.timeline = timeline
        self.consents = consents
        self.bindings = bindings
        self.spaces = spaces

    # -- 候选生成（纯函数，便于单测） ---------------------------------------

    def candidates_from_text(self, text: str, *, principal_id: str,
                             source_group: str, message_id: str,
                             consent_version: int, observed_at: str
                             ) -> Tuple[MemoryCandidate, ...]:
        """从一条本人消息提炼候选（不做敏感/consent 判断——调用方负责）。"""
        out: list[MemoryCandidate] = []
        for segment in _SEGMENT_SPLIT_RE.split(text or ""):
            seg = segment.strip()
            if not (4 <= len(seg) <= 120):
                continue
            if not any(marker in seg for marker in _FIRST_PERSON):
                continue  # 只收本人自述
            if _ABILITY_CLAIM_RE.search(seg):
                continue  # 能力画像不固化
            if _SELF_FACT_RE.search(seg):
                kind, confidence, ttl_s = (
                    MemoryKind.PREFERENCE, 0.7, float(PREFERENCE_TTL_S))
            elif any(w in seg for w in _EVENT_WORDS):
                kind, confidence = MemoryKind.EPISODE, 0.8
                ttl_s = float(SHORT_EVENT_TTL_S if any(
                    w in seg for w in _DEADLINE_WORDS) else DEFAULT_EPISODE_TTL_S)
            else:
                continue  # 保守：无规则命中不提炼
            out.append(MemoryCandidate(
                principal_id=principal_id, source_group=source_group,
                source_message_ids=(message_id,), summary=seg,
                kind=kind, confidence=confidence, ttl_s=ttl_s,
                consent_version=consent_version, observed_at=observed_at))
        return tuple(out)

    # -- 管道 ---------------------------------------------------------------

    def extract_for_principal(self, principal_id: str, source_group: str,
                              *, now: Optional[datetime] = None,
                              not_before: str = "") -> ExtractionStats:
        """提炼某成员在某来源群时间线中的本人记忆。

        consent 无效 → 候选全部丢弃（dropped_no_consent），0 写入。
        ``not_before``（ISO 时间串，水位）：只处理 recorded_at ≥ 该时刻的
        消息——回填/恢复场景用它保证「退出期间不补采」（issue #16）。
        """
        stats = ExtractionStats()
        current = now or datetime.now(timezone.utc)
        uid = sender_id_for_principal(principal_id)
        if not uid:
            return stats
        # owner 决策 2026-11-01：成员记忆默认开启（私聊即同意）；
        # 「别记我了」写持久退出标记——退出者零扫描零写入。
        if self.consents.is_opted_out(principal_id):
            return stats
        entries = [e for e in self.timeline.recent(source_group, limit=1000)
                   if str(e.get("sender_id") or "") == uid
                   and not e.get("is_self")]
        if not_before:
            entries = [e for e in entries
                       if str(e.get("recorded_at") or "") >= not_before]
        stats.scanned = len(entries)

        # consent 记录只作审计/版本证据，不再是写入闸门（owner 决策）。
        consent = self.consents.valid_for_group(
            principal_id, source_group, now=current)

        candidates: list[MemoryCandidate] = []
        for entry in entries:
            text = str(entry.get("text") or "")
            if contains_sensitive(text):
                stats.skipped_sensitive += 1
                continue
            candidates.extend(self.candidates_from_text(
                text, principal_id=principal_id, source_group=source_group,
                message_id=str(entry.get("message_id") or ""),
                consent_version=consent.version if consent else 0,
                observed_at=str(entry.get("send_time")
                                or entry.get("recorded_at") or "")))
        stats.candidates = len(candidates)

        # owner 决策 2026-11-01：默认采集（私聊即同意）；consent 记录只作
        # 审计/版本证据，不再作写入闸门。退出（opt-out）在上方已零扫描早退。
        if consent is None:
            consent_version = 0  # 默认采集路径：无 consent 记录
        # 继续写入（binding/空间校验在下方）

        binding = self.bindings.member_binding_for(principal_id)
        space_id = str(binding.get("space_id") or "") if binding else ""
        ref = self.spaces.ref(space_id) if space_id else None
        if ref is None:
            raise ExtractionError(
                "member_space_missing: 授权有效但成员空间不存在/已注销")

        for cand in candidates:
            if self.store.get(cand.idempotency_key) is not None:
                stats.skipped_duplicate += 1
                continue
            self._write_candidate(cand, space_id, current)
            stats.written += 1
            stats.record_keys.append(cand.record_key)
        return stats

    # -- 首次私聊后的历史回填（issue #16） ----------------------------------

    def backfill_for_principal(self, principal_id: str,
                               *, now: Optional[datetime] = None,
                               max_groups: int = BACKFILL_MAX_GROUPS,
                               force: bool = False
                               ) -> ExtractionStats:
        """首次建档后的历史回填：有界、增量、幂等。

        - 有界：最多 ``max_groups`` 个来源群 × 时间线自身保留窗（200 条/48h）；
        - 幂等：候选级幂等键去重，重复运行不产生重复记忆；
        - 增量/退出纪律：水位 ``not_before``（恢复记录时刻）之前的消息不补采；
          正在退出（opt-out）状态的成员零扫描零写入。
        返回合并后的可观测统计（scanned/written/... 可追踪）。
        """
        total = ExtractionStats()
        current = now or datetime.now(timezone.utc)
        if self.consents.is_opted_out(principal_id):
            return total
        wm = backfill_watermark(self.store, principal_id) or {}
        if wm.get("done_at") and not force:
            return total  # 已回填过；增量由新消息的实时提炼覆盖（issue #16）
        not_before = str(wm.get("not_before") or "")
        keys = sorted(
            k for k in self.store.keys()
            if isinstance(k, str) and k.startswith("timeline:group:")
        )[:max(0, int(max_groups))]
        for key in keys:
            group = key[len("timeline:"):]
            stats = self.extract_for_principal(
                principal_id, group, now=current, not_before=not_before)
            total.scanned += stats.scanned
            total.candidates += stats.candidates
            total.written += stats.written
            total.skipped_sensitive += stats.skipped_sensitive
            total.skipped_duplicate += stats.skipped_duplicate
            total.dropped_no_consent += stats.dropped_no_consent
            total.record_keys.extend(stats.record_keys)
        self.store.put(f"{BACKFILL_KEY_PREFIX}{principal_id}", {
            "not_before": not_before, "done_at": current.isoformat(),
            "groups": len(keys)})
        return total

    def _write_candidate(self, cand: MemoryCandidate, space_id: str,
                         now: datetime) -> None:
        expires = (now + timedelta(seconds=cand.ttl_s)).isoformat() \
            if cand.ttl_s > 0 else None
        record = MemoryRecord(
            kind=cand.kind,
            key=cand.record_key,
            content={
                "summary": cand.summary[:1500],
                "source": f"member-extract:{cand.source_group}"[:120],
                "evidence_id": ",".join(cand.source_message_ids)[:160],
                "importance": 0.6,
                "confidence": cand.confidence,
                "stable_fact": cand.kind is MemoryKind.PREFERENCE,
                # MEM-04 来源链：来源群/消息/同意版本/观察与提炼时间/TTL
                "layer": "member",
                "consent": True,
                "principal_id": cand.principal_id,
                "source_group": cand.source_group,
                "source_message_ids": list(cand.source_message_ids),
                "consent_version": cand.consent_version,
                "observed_at": cand.observed_at,
                "extracted_at": now.isoformat(),
                "expires_at": expires,
                "last_confirmed": now.isoformat(),
            },
            tags=("curated", "member"),
            space_id=space_id,
        )
        self.store.put_record(record)
        self.store.put(cand.idempotency_key, {
            "record_key": cand.record_key,
            "principal_id": cand.principal_id,
            "extraction_version": EXTRACTION_VERSION,
            "written_at": now.isoformat(),
        })


# ---------------------------------------------------------------------------
# issue #16：首次私聊后的历史回填（有界/增量/幂等）与提炼状态清理
# ---------------------------------------------------------------------------



def backfill_watermark(store: MemoryStore, principal_id: str) -> Optional[dict]:
    data = store.get(f"{BACKFILL_KEY_PREFIX}{principal_id}")
    return dict(data) if isinstance(data, dict) else None


def note_optin_watermark(store: MemoryStore, principal_id: str,
                         *, now: Optional[datetime] = None) -> None:
    """「恢复记录我」时钉水位：回填窗口从恢复时刻起算——
    退出期间留在公共时间线里的消息**不补采**（issue #16）。"""
    at = (now or datetime.now(timezone.utc)).isoformat()
    store.put(f"{BACKFILL_KEY_PREFIX}{principal_id}",
              {"not_before": at, "done_at": None})


def purge_member_extraction_state(store: MemoryStore,
                                  principal_id: str) -> int:
    """「删掉我的记忆」配套：清掉该成员的提炼幂等键与回填水位（真删）。

    幂等键是内容寻址的（无法按 principal 前缀枚举），逐个读值比对
    principal_id；返回删除条数。群公共时间线与平台侧聊天记录的保留边界
    不同，不在此删除（issue #16 的说明义务由回复文案承担）。
    """
    n = 0
    for key in list(store.keys()):
        if not key.startswith("member:ex:"):
            continue
        val = store.get(key)
        if isinstance(val, dict) and val.get("principal_id") == principal_id:
            store.delete(key)
            n += 1
    store.delete(f"{BACKFILL_KEY_PREFIX}{principal_id}")
    return n
