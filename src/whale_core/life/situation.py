"""G5.1：SituationState —— 有来源、有期限、可撤销的 Core 情境状态（LIFE-01）。

设计要点（对应计划 §7.1 与 §11 LIFE-01）：

- **事实与推断分开**：``fact_or_inference`` ∈ ``fact`` / ``inference``，存储层
  强制区分；推断永远不能当成确定事实消费（AutonomyController 据此降级）。
- **过期/撤销即失效**：``expires_at`` 到期或 ``retracted_at`` 非空 →
  ``is_valid`` 为 False，``active_for`` 不再返回；撤销来源事件可经
  ``retract_event`` 连带失效其全部派生情境。
- **来源可信性 fail closed**：只有构造函数显式登记的可信来源前缀
  （默认 ``device`` / ``calendar`` / ``owner`` / ``system``）能落「确定事实」；
  未配对渠道、群聊正文、模型推测等来源写事实一律拒绝（只能落推断）。
  注意：本层校验的是**来源声明的类别**；「device:* 事件确实来自已配对设备」
  的认证在入站路径完成（DeviceGateway 验签 / SimulatedDeviceBridge 查
  registry），本层不信任调用方注入的 scope/身份，只执行来源类别硬围栏。
- 持久化走 MemoryStore kv（``situation:{id}``），InMemory 与 SQLite
  双后端一致；索引键 ``situation:index:{subject}`` 与
  ``situation:by-event:{event_id}`` 只做查找，不做权限依据。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

#: fact_or_inference 取值
FACT = "fact"
INFERENCE = "inference"
KINDS = frozenset({FACT, INFERENCE})

#: 置信度词表（§7.1 示例：source_authoritative / medium …）
CONFIDENCE_LEVELS = ("source_authoritative", "high", "medium", "low")

#: 可落「确定事实」的来源前缀（``source.split(":", 1)[0]`` 匹配）。
#: 未配对渠道、群聊正文、模型推测等不可信来源**不在**此列——fail closed。
DEFAULT_TRUSTED_FACT_SOURCES: frozenset[str] = frozenset(
    {"device", "calendar", "owner", "system"})

MAX_VALUE_CHARS = 400
MAX_INDEX = 200


class SituationError(ValueError):
    """情境写入/查询被拒绝（不可信来源写事实、字段非法等）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class SituationState:
    """一条情境状态（不可变值对象；撤销/替换产生新记录而非原地改）。"""

    situation_id: str
    key: str                      # 事实/推断名，如 "meeting_at"
    value: str                    # 取值或短摘要（已截断）
    fact_or_inference: str        # FACT | INFERENCE
    subject_id: str               # 归属主体（principal_id / "owner"）
    source: str                   # 来源标识，如 "device:<id>" / "calendar:work"
    event_id: str                 # 溯源事件 ID（幂等去重与撤销锚点）
    observed_at: datetime
    expires_at: datetime | None
    confidence: str               # CONFIDENCE_LEVELS 之一
    privacy_space: str = ""       # 允许召回的记忆空间 id（空 = 不跨域召回）
    consent_version: str = ""     # 采集依据的授权版本（撤销比对用）
    retracted_at: datetime | None = None
    evidence: tuple[str, ...] = ()  # 依据引用（event/message id），不存正文

    def __post_init__(self) -> None:
        if self.fact_or_inference not in KINDS:
            raise SituationError("bad-request", "fact_or_inference 须为 fact|inference")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise SituationError("bad-request", f"confidence 须为 {CONFIDENCE_LEVELS}")
        if not self.key.strip() or not self.subject_id.strip():
            raise SituationError("bad-request", "key 与 subject_id 必填")
        if not self.source.strip():
            raise SituationError("bad-request", "source 必填（无来源不成情境）")

    def is_valid(self, now: datetime) -> bool:
        """过期/撤销即失效（LIFE-01）。"""
        if self.retracted_at is not None:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "situation_id": self.situation_id,
            "key": self.key,
            "value": self.value[:MAX_VALUE_CHARS],
            "fact_or_inference": self.fact_or_inference,
            "subject_id": self.subject_id,
            "source": self.source,
            "event_id": self.event_id,
            "observed_at": self.observed_at.isoformat(),
            "expires_at": _iso(self.expires_at),
            "confidence": self.confidence,
            "privacy_space": self.privacy_space,
            "consent_version": self.consent_version,
            "retracted_at": _iso(self.retracted_at),
            "evidence": list(self.evidence)[:10],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SituationState:
        return cls(
            situation_id=str(data["situation_id"]),
            key=str(data["key"]),
            value=str(data.get("value") or ""),
            fact_or_inference=str(data["fact_or_inference"]),
            subject_id=str(data["subject_id"]),
            source=str(data["source"]),
            event_id=str(data.get("event_id") or ""),
            observed_at=_parse_dt(data.get("observed_at")) or _utc_now(),
            expires_at=_parse_dt(data.get("expires_at")),
            confidence=str(data.get("confidence") or "low"),
            privacy_space=str(data.get("privacy_space") or ""),
            consent_version=str(data.get("consent_version") or ""),
            retracted_at=_parse_dt(data.get("retracted_at")),
            evidence=tuple(str(e) for e in (data.get("evidence") or ())),
        )


class SituationStore:
    """情境状态 kv 存储：``situation:{id}`` 本体 + 主体/事件两个索引。"""

    def __init__(
        self,
        store,
        *,
        trusted_fact_sources: Iterable[str] = DEFAULT_TRUSTED_FACT_SOURCES,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.trusted_fact_sources = frozenset(trusted_fact_sources)
        self._now = now_fn or _utc_now

    # ---- 写入 ----------------------------------------------------------

    def _check_source(self, fact_or_inference: str, source: str) -> None:
        prefix = source.split(":", 1)[0]
        if fact_or_inference == FACT and prefix not in self.trusted_fact_sources:
            # LIFE-01：未配对渠道 / 群正文 / 模型推测不得成为确定事实
            raise SituationError(
                "untrusted-source",
                f"来源类别 {prefix!r} 不可信，只能落推断（inference），不能落事实",
            )

    def record(
        self,
        *,
        key: str,
        value: str,
        fact_or_inference: str,
        subject_id: str,
        source: str,
        event_id: str,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
        confidence: str = "medium",
        privacy_space: str = "",
        consent_version: str = "",
        evidence: Iterable[str] = (),
        situation_id: str = "",
    ) -> SituationState:
        """写入一条情境。同 (event_id, key, subject) 幂等去重（重放安全）。"""
        self._check_source(fact_or_inference, source)
        event_id = str(event_id or "")
        if event_id:
            for existing in self.by_event(event_id):
                if existing.key == key and existing.subject_id == subject_id:
                    return existing  # 幂等：同一事件派生的同一事实不重复落
        state = SituationState(
            situation_id=situation_id or f"sit-{uuid4().hex[:20]}",
            key=key.strip(),
            value=str(value or "")[:MAX_VALUE_CHARS],
            fact_or_inference=fact_or_inference,
            subject_id=subject_id.strip(),
            source=source.strip(),
            event_id=event_id,
            observed_at=observed_at or self._now(),
            expires_at=expires_at,
            confidence=confidence,
            privacy_space=privacy_space,
            consent_version=consent_version,
            evidence=tuple(str(e) for e in evidence)[:10],
        )
        self.store.put(f"situation:{state.situation_id}", state.as_dict())
        self._index(f"situation:index:{state.subject_id}", state.situation_id)
        if event_id:
            self._index(f"situation:by-event:{event_id}", state.situation_id)
        return state

    def _index(self, key: str, situation_id: str) -> None:
        entries = self.store.get(key) or []
        if not isinstance(entries, list):
            entries = []
        if situation_id not in entries:
            entries.append(situation_id)
            self.store.put(key, entries[-MAX_INDEX:])

    # ---- 查询 ----------------------------------------------------------

    def get(self, situation_id: str) -> SituationState | None:
        data = self.store.get(f"situation:{situation_id}")
        if not isinstance(data, Mapping):
            return None
        try:
            return SituationState.from_dict(data)
        except (KeyError, SituationError):
            return None  # 损坏记录按失效处理（fail closed）

    def by_event(self, event_id: str) -> tuple[SituationState, ...]:
        out: list[SituationState] = []
        for sid in self.store.get(f"situation:by-event:{event_id}") or []:
            state = self.get(str(sid))
            if state is not None:
                out.append(state)
        return tuple(out)

    def active_for(
        self,
        subject_id: str,
        *,
        now: datetime | None = None,
        fact_or_inference: str | None = None,
    ) -> tuple[SituationState, ...]:
        """主体当前有效情境（过期/撤销一律不返回）。"""
        now = now or self._now()
        out: list[SituationState] = []
        for sid in self.store.get(f"situation:index:{subject_id}") or []:
            state = self.get(str(sid))
            if state is None or not state.is_valid(now):
                continue
            if fact_or_inference and state.fact_or_inference != fact_or_inference:
                continue
            out.append(state)
        return tuple(out)

    # ---- 撤销 ----------------------------------------------------------

    def retract(self, situation_id: str, *, now: datetime | None = None) -> bool:
        """撤销单条情境：立即失效（不参与评估/触发）。"""
        state = self.get(situation_id)
        if state is None or state.retracted_at is not None:
            return False
        from dataclasses import replace
        retracted = replace(state, retracted_at=now or self._now())
        self.store.put(f"situation:{situation_id}", retracted.as_dict())
        return True

    def retract_event(self, event_id: str, *, now: datetime | None = None) -> int:
        """撤销来源事件 → 其全部派生情境连带失效（授权/证据撤销语义）。"""
        count = 0
        for state in self.by_event(event_id):
            if self.retract(state.situation_id, now=now):
                count += 1
        return count
