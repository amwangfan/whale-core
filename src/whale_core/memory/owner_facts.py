"""Owner 私聊长期事实记忆（owner_fact_v1 首批）。

依据 ``docs/memory-consolidation-plan.md``（Astra 冻结 v1）实现：可信 owner
私聊/眼镜同 owner 授权空间内，明确用户自述经证据校验后自动成为 accepted 事实；
更正按同谓词明确冲突 supersede；遗忘以 generation fence + 证据墓碑防迟到复活。

纪律（冻结项）：
- 只依赖 memory/store 层，不反向依赖 companion / 渠道 / life；
- 不新增 SQL schema：事实用现有 MemoryRecord（tag=owner_fact_v1），
  任务/水位/证据/索引用现有 KV；所有写路径走 ``store.transact`` 且事务内
  只调用 tx 方法，禁止调用会独立 commit 的 store.put/delete；
- summary == 证据 quote 原句（模型不改写，render 层前缀由装配侧加）；
- ``accepted`` 语义 = 有证据支持的用户自述，不是已核实事实、不等于用户
  显式确认；``uncertain``/``pending`` 一律不召回；
- 删除写无 summary/quote/value 的墓碑；墓碑恒赢，import/backup 不得复活；
- 日志/诊断码不含用户原文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from whale_core.memory.curation import _terms  # 复用现有词项切分，避免新分词差异
from whale_core.memory.extraction import contains_sensitive
from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore

TAG_OWNER_FACT = "owner_fact_v1"
SCHEMA_VERSION = 1

# KV / record 键布局（无新 schema）
def _k_gen(owner_id: str, space_id: str) -> str:
    return f"ofgen:{owner_id}:{space_id}"


def _k_job(job_id: str) -> str:
    return f"ofjob:{job_id}"


def _k_ev(evidence_id: str) -> str:
    return f"ofev:{evidence_id}"


def _k_ev_tomb(evidence_id: str) -> str:
    return f"ofev_tomb:{evidence_id}"


def _k_fact(fact_id: str) -> str:
    return f"offact:{fact_id}"


def _k_idx(owner_id: str, space_id: str) -> str:
    return f"ofidx:{owner_id}:{space_id}"


_PENDING_Q_KEY = "ofq:pending"
#: 终态 job journal（completed/failed/cancelled 统一进入），有界；
#: 超出即删除最旧 ofjob 记录。evidence/墓碑永久保留（防重放/防复活水位）。
_TERMINAL_Q_KEY = "ofq:terminal"
TERMINAL_JOURNAL_LIMIT = 256

DEFAULT_QUEUE_LIMIT = 128
DEFAULT_PER_OWNER_LIMIT = 64
DEFAULT_MAX_INPUT_CHARS = 4000

# observe / commit / correct / forget 诊断码（无正文）
RC_INPUT_TOO_LONG = "input_too_long"
RC_NAIVE_TIME = "naive_time"
RC_SPACE_MISMATCH = "space_mismatch"
RC_BAD_ROLE = "bad_role"
RC_EMPTY_TEXT = "empty_text"
RC_EVIDENCE_REVOKED = "evidence_revoked"
RC_QUEUE_FULL = "queue_full"
RC_OWNER_QUEUE_FULL = "owner_queue_full"
RC_POLICY = "policy_rejected"

# owner_fact_v1 命名空间前置凭据启发式（非完整 DLP 承诺）：
# contains_sensitive 只覆盖 api_key/apikey 连写与密码类词，带空格的
# "API Key"、Bearer/访问令牌/私钥、知名 token 前缀（sk-/ghp_/github_pat_）
# 需要显式拦截——命中即整条证据不持久化（skipped_sensitive）。
# 只作用于 owner 事实入口，不改群旧 extractor 全局策略。
_CREDENTIAL_RES = (
    re.compile(r"(?i)\bapi[\s_\-]?key\b"),
    re.compile(r"(?i)\baccess[\s_-]?token\b"),
    re.compile(r"访问令牌|访问密钥|私钥"),
    re.compile(r"(?i)\bprivate[\s_\-]?key\b"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(sk-[a-z0-9_\-]{6,}|ghp_[a-z0-9]{6,}|github_pat_[a-z0-9_]{6,}|gho_[a-z0-9]{6,}|ghs_[a-z0-9]{6,}|ghr_[a-z0-9]{6,})"),
)


def contains_credential(text: str) -> bool:
    """常见凭据字段启发式：命中即拒自动证据持久化（可能有假阳性，宁拒）。"""
    return any(rx.search(text) for rx in _CREDENTIAL_RES)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_iso(value: Any) -> datetime | None:
    """解析 ISO 时间为 aware UTC；naive / 非法一律返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


class FactKind(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODE = "episode"


class Assertion(str, Enum):
    """模型建议值；服务层裁决不信任该字段（红队 P0）。"""

    SELF_REPORT = "self_report"
    UNCERTAIN = "uncertain"
    QUOTED = "quoted"
    HYPOTHETICAL = "hypothetical"
    NEGATED = "negated"
    THIRD_PARTY = "third_party"


class FactStatus(str, Enum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"


class ObserveStatus(str, Enum):
    QUEUED = "queued"
    DUPLICATE = "duplicate"
    SKIPPED_POLICY = "skipped_policy"
    SKIPPED_SENSITIVE = "skipped_sensitive"
    SKIPPED_CAPACITY = "skipped_capacity"
    FAILED_STORAGE = "failed_storage"


@dataclass(frozen=True)
class UserEvidence:
    """宿主可信解析器填写的入站用户原文证据。"""

    evidence_id: str
    owner_id: str
    principal_id: str
    conversation_id: str
    channel: str
    space_id: str
    text: str
    received_at: datetime  # aware UTC
    role: str = "user"
    timezone: str | None = None
    source_message_id: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class MemoryAuthorization:
    """服务端可信授权句柄。调用者不得把任意字符串包装成授权。"""

    owner_id: str
    space_id: str  # 唯一目标可写空间
    principal_id: str
    policy_version: str
    authority_version: str
    readable_space_ids: tuple[str, ...] = ()


class AuthorityProvider(Protocol):
    """授权现状提供者（宿主/Qwen 侧实现）。

    禁止捕获固定 ResolvedContext 或返回常量 True；每次调用必须现查
    绑定 / pause / opt-out / 录制开关。
    """

    def authorize_now(self, owner_id: str, space_id: str) -> MemoryAuthorization | None:
        """当前授权快照；任一条件不满足返回 None。只读，不得有副作用。

        注意：宿主实现若返回"任一活跃来源"的 canonical principal，则该方法
        不足以证明**证据自身的 principal** 仍 active——写路径应同时实现
        ``authorize_principal_now``（可选窄方法）。
        """
        ...

    def authorize_principal_now(
        self, owner_id: str, space_id: str, principal_id: str
    ) -> MemoryAuthorization | None:
        """可选窄方法：核实**该 principal** 当前仍绑定且可写该空间。

        QQ 解绑但眼镜活跃时，authorize_now 可能返回眼镜身份；本方法按
        证据/授权携带的真实 principal 现查，不自动换 principal。
        测试 fake 可不实现：服务层退化为 authorize_now + principal 比对。
        """
        ...

    def authority_version(self, owner_id: str, space_id: str) -> str:
        """授权/策略现状的单调令牌；绑定、pause、optout、策略版本任一变化即变。"""
        ...


@dataclass(frozen=True)
class SourceRef:
    evidence_id: str
    span_start: int
    span_end: int


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    owner_id: str
    space_id: str
    kind: FactKind
    summary: str  # v1: == 证据 quote 原句
    source_refs: tuple[SourceRef, ...]
    status: FactStatus
    revision: int
    created_at: datetime
    observed_at: datetime
    assertion: str = "user_asserted"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    expires_at: datetime | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    # 谓词/值：更正「同谓词明确冲突」判定的结构化依据（v1 增补可选字段，
    # 已向协调代理披露；模型可留空，留空则不触发自动 supersede）。
    predicate: str | None = None
    value: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class ObserveResult:
    status: ObserveStatus
    evidence_id: str
    reason_code: str = ""


@dataclass(frozen=True)
class EvidenceTombstoneRef:
    """forget 返回给装配侧（Qwen）做源 episode/history 抑制的最小引用。"""

    evidence_id: str
    conversation_id: str
    source_message_id: str | None
    received_at: datetime | None


@dataclass(frozen=True)
class MutationReport:
    ok: bool
    reason_code: str = ""
    affected_fact_ids: tuple[str, ...] = ()
    affected_evidence: tuple[EvidenceTombstoneRef, ...] = ()


@dataclass(frozen=True)
class CommitReport:
    status: str  # committed | rejected_lease | rejected_stale_generation |
    # rejected_policy | rejected_evidence
    job_id: str
    accepted_fact_ids: tuple[str, ...] = ()
    pending_fact_ids: tuple[str, ...] = ()
    superseded_fact_ids: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()  # (candidate key, reason_code)


# ---------------------------------------------------------------------------
# FactRecord <-> MemoryRecord 映射
# ---------------------------------------------------------------------------

_KIND_TO_MEMORY = {
    FactKind.FACT: MemoryKind.EPISODE,
    FactKind.PREFERENCE: MemoryKind.PREFERENCE,
    FactKind.EPISODE: MemoryKind.EPISODE,
}


def fact_to_record(fact: FactRecord) -> MemoryRecord:
    content: dict[str, Any] = {
        "fact_id": fact.fact_id,
        "owner_id": fact.owner_id,
        "space_id": fact.space_id,
        "fact_kind": fact.kind.value,
        "summary": fact.summary,
        "source_refs": [
            {"evidence_id": r.evidence_id, "span_start": r.span_start, "span_end": r.span_end}
            for r in fact.source_refs
        ],
        "assertion": fact.assertion,
        "status": fact.status.value,
        "revision": fact.revision,
        "fact_created_at": fact.created_at.isoformat(),
        "observed_at": fact.observed_at.isoformat(),
        "valid_from": _iso(fact.valid_from),
        "valid_to": _iso(fact.valid_to),
        "expires_at": _iso(fact.expires_at),
        "supersedes": fact.supersedes,
        "superseded_by": fact.superseded_by,
        "predicate": fact.predicate,
        "value": fact.value,
        "schema_version": fact.schema_version,
    }
    return MemoryRecord(
        kind=_KIND_TO_MEMORY[fact.kind],
        key=_k_fact(fact.fact_id),
        content=content,
        record_id=fact.fact_id,
        created_at=fact.created_at,
        tags=(TAG_OWNER_FACT,),
        space_id=fact.space_id,
    )


def fact_from_record(record: MemoryRecord) -> FactRecord | None:
    c = dict(record.content)
    if TAG_OWNER_FACT not in tuple(record.tags or ()):
        return None
    try:
        refs = tuple(
            SourceRef(
                evidence_id=str(r["evidence_id"]),
                span_start=int(r["span_start"]),
                span_end=int(r["span_end"]),
            )
            for r in (c.get("source_refs") or [])
        )
        return FactRecord(
            fact_id=str(c["fact_id"]),
            owner_id=str(c["owner_id"]),
            space_id=str(c["space_id"]),
            kind=FactKind(str(c.get("fact_kind") or "fact")),
            summary=str(c.get("summary") or ""),
            source_refs=refs,
            status=FactStatus(str(c["status"])),
            revision=int(c["revision"]),
            created_at=_parse_iso(str(c.get("fact_created_at") or "")) or _utc_now(),
            observed_at=_parse_iso(str(c.get("observed_at") or "")) or _utc_now(),
            assertion=str(c.get("assertion") or "user_asserted"),
            valid_from=_parse_iso(c.get("valid_from")),
            valid_to=_parse_iso(c.get("valid_to")),
            expires_at=_parse_iso(c.get("expires_at")),
            supersedes=c.get("supersedes"),
            superseded_by=c.get("superseded_by"),
            predicate=c.get("predicate"),
            value=c.get("value"),
            schema_version=int(c.get("schema_version") or 1),
        )
    except (KeyError, ValueError, TypeError):
        return None


def is_tombstone(record: MemoryRecord | None) -> bool:
    """status=deleted 墓碑恒赢：任何写路径不得复活。"""
    if record is None:
        return False
    return dict(record.content).get("status") == FactStatus.DELETED.value


def tombstone_record(old: MemoryRecord, *, now: datetime) -> MemoryRecord:
    """无 summary / quote / value / source_refs 的删除墓碑（保留最小归属元数据）。"""
    c = dict(old.content)
    content = {
        "fact_id": c.get("fact_id", ""),
        "owner_id": c.get("owner_id", ""),
        "space_id": c.get("space_id", old.space_id),
        "fact_kind": c.get("fact_kind", "fact"),
        "status": FactStatus.DELETED.value,
        "revision": int(c.get("revision") or 0) + 1,
        "deleted_at": now.isoformat(),
        "schema_version": SCHEMA_VERSION,
    }
    return MemoryRecord(
        kind=old.kind,
        key=old.key,
        content=content,
        record_id=old.record_id,
        created_at=old.created_at,
        tags=tuple(old.tags or ()),
        space_id=old.space_id,
    )


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class OwnerFactService:
    """owner_fact_v1 领域服务：受理 / 提交 / 更正 / 遗忘 / 检索。

    所有写路径在单个 ``store.transact`` 内完成；事务内只调用 tx 方法。
    ``authority`` 回调只做只读检查（宿主保证）。
    """

    def __init__(
        self,
        store: MemoryStore,
        authority: AuthorityProvider,
        *,
        now: Callable[[], datetime] = _utc_now,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        per_owner_limit: int = DEFAULT_PER_OWNER_LIMIT,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    ) -> None:
        self._store = store
        self._authority = authority
        self._now = now
        self._queue_limit = max(1, int(queue_limit))
        self._per_owner_limit = max(1, int(per_owner_limit))
        self._max_input_chars = max(1, int(max_input_chars))

    def _authorize_principal(
        self, owner_id: str, space_id: str, principal_id: str
    ) -> MemoryAuthorization | None:
        """按真实来源 principal 现查授权（job 自身 source 仍 active）。

        宿主实现 ``authorize_principal_now`` 则用之；否则退化为
        authorize_now + principal 一致性比对（不会自动换 principal）。
        """
        fn = getattr(self._authority, "authorize_principal_now", None)
        if callable(fn):
            return fn(owner_id, space_id, principal_id)
        auth = self._authority.authorize_now(owner_id, space_id)
        if auth is None or auth.principal_id != principal_id:
            return None
        return auth

    # -- 只读 ------------------------------------------------------------

    def current_generation(self, owner_id: str, space_id: str) -> int:
        """只读 generation；companion 主/重/followup 回写比对用。"""
        value = self._store.get(_k_gen(owner_id, space_id))
        return int(value) if isinstance(value, int) else 0

    def pending_count(self, owner_id: str | None = None) -> int:
        ids = self._store.get(_PENDING_Q_KEY) or []
        if owner_id is None:
            return len(ids)
        count = 0
        for jid in ids:
            job = self._store.get(_k_job(str(jid))) or {}
            if job.get("owner_id") == owner_id and job.get("status") in ("pending", "leased"):
                count += 1
        return count

    def get_fact(self, fact_id: str) -> FactRecord | None:
        rec = self._store.get_record(_k_fact(fact_id))
        if rec is None or is_tombstone(rec):
            return None
        return fact_from_record(rec)

    # -- 受理 --------------------------------------------------------------

    def observe(self, evidence: UserEvidence, authorization: MemoryAuthorization) -> ObserveResult:
        """主回答模型调用前的快速持久受理。模型调用不在此方法内。"""
        eid = evidence.evidence_id
        if evidence.role != "user":
            return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_BAD_ROLE)
        if (
            not authorization
            or authorization.owner_id != evidence.owner_id
            or authorization.space_id != evidence.space_id
            or not evidence.space_id
        ):
            return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_SPACE_MISMATCH)
        text = evidence.text or ""
        if not text.strip():
            return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_EMPTY_TEXT)
        if len(text) > self._max_input_chars:
            return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_INPUT_TOO_LONG)
        if evidence.received_at.tzinfo is None:
            return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_NAIVE_TIME)
        if contains_sensitive(text) or contains_credential(text):
            return ObserveResult(ObserveStatus.SKIPPED_SENSITIVE, eid, "sensitive_content")

        now = self._now()
        try:
            def _fn(tx) -> ObserveResult:
                # 事务内按证据真实 principal 现查授权（暂停/退出/解绑后不得
                # 再保存原文；authorize_now 返回其他活跃来源身份不算数）
                auth = self._authorize_principal(
                    evidence.owner_id, evidence.space_id, evidence.principal_id
                )
                if auth is None or auth.space_id != evidence.space_id:
                    return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_POLICY)
                if tx.get(_k_ev_tomb(eid)) is not None:
                    return ObserveResult(ObserveStatus.SKIPPED_POLICY, eid, RC_EVIDENCE_REVOKED)
                if tx.get(_k_ev(eid)) is not None:
                    return ObserveResult(ObserveStatus.DUPLICATE, eid, "evidence_seen")
                pending_ids = list(tx.get(_PENDING_Q_KEY) or [])
                if len(pending_ids) >= self._queue_limit:
                    return ObserveResult(ObserveStatus.SKIPPED_CAPACITY, eid, RC_QUEUE_FULL)
                owner_open = 0
                for jid in pending_ids:
                    job = tx.get(_k_job(str(jid))) or {}
                    if job.get("owner_id") == evidence.owner_id and job.get("status") in (
                        "pending",
                        "leased",
                    ):
                        owner_open += 1
                if owner_open >= self._per_owner_limit:
                    return ObserveResult(ObserveStatus.SKIPPED_CAPACITY, eid, RC_OWNER_QUEUE_FULL)

                generation = tx.get(_k_gen(evidence.owner_id, evidence.space_id)) or 0
                auth_version = self._authority.authority_version(
                    evidence.owner_id, evidence.space_id
                )
                job_id = uuid4().hex
                tx.put(
                    _k_ev(eid),
                    {
                        "evidence_id": eid,
                        "owner_id": evidence.owner_id,
                        "principal_id": evidence.principal_id,
                        "conversation_id": evidence.conversation_id,
                        "channel": evidence.channel,
                        "space_id": evidence.space_id,
                        "role": evidence.role,
                        "text": text,
                        "received_at": evidence.received_at.astimezone(timezone.utc).isoformat(),
                        "timezone": evidence.timezone,
                        "source_message_id": evidence.source_message_id,
                        "schema_version": evidence.schema_version,
                        "status": "pending",
                        "retained_quotes": [],
                    },
                )
                tx.put(
                    _k_job(job_id),
                    {
                        "job_id": job_id,
                        "evidence_id": eid,
                        "owner_id": evidence.owner_id,
                        "space_id": evidence.space_id,
                        "generation": int(generation),
                        "authority_version": auth_version,
                        "status": "pending",
                        "attempts": 0,
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "enqueued_at": now.isoformat(),
                        "last_error_code": "",
                    },
                )
                pending_ids.append(job_id)
                tx.put(_PENDING_Q_KEY, pending_ids)
                return ObserveResult(ObserveStatus.QUEUED, eid, "")

            return self._store.transact(_fn)
        except Exception:
            return ObserveResult(ObserveStatus.FAILED_STORAGE, eid, "storage_error")

    # -- 提交（worker 唯一写入口） -----------------------------------------

    def commit(
        self,
        job_id: str,
        candidates: Sequence[Any],
        expected_generation: int,
        authority_version: str,
        *,
        worker_id: str,
        lease_token: str,
        validate: Callable[[Any, UserEvidence, Mapping[str, int]], tuple[str, str]] | None = None,
    ) -> CommitReport:
        """单事务裁决：lease / generation / 授权现状 / 证据未撤销 / 目标 revision。

        ``validate(candidate, evidence, allowed_correction_targets)``：
        decision ∈ {accepted, pending, reject}；由提炼模块注入，服务层是唯一裁决点。
        allowed_correction_targets = 本轮展示给模型的 fact_id → revision 快照
        （worker 领取时写入 job，commit 只信快照不信全库猜测）。
        """
        if validate is None:
            validate = validate_candidate_service
        now = self._now()

        def _fn(tx) -> CommitReport:
            job = tx.get(_k_job(job_id))
            if not isinstance(job, Mapping):
                return CommitReport("rejected_lease", job_id)
            if job.get("status") == "cancelled":
                # 已被 fence 取消的迟到提交：按取消原因给出精确拒绝码
                code = str(job.get("last_error_code") or "")
                if code == "generation_fence":
                    return CommitReport("rejected_stale_generation", job_id)
                if code in ("evidence_revoked", "evidence_missing"):
                    return CommitReport("rejected_evidence", job_id)
                return CommitReport("rejected_lease", job_id)
            if (
                job.get("status") != "leased"
                or job.get("lease_owner") != worker_id
                or job.get("lease_token") != lease_token
            ):
                return CommitReport("rejected_lease", job_id)
            lease_exp = _parse_iso(job.get("lease_expires_at") or "")
            if lease_exp is not None and now > lease_exp:
                return CommitReport("rejected_lease", job_id)

            current_gen = int(tx.get(_k_gen(job["owner_id"], job["space_id"])) or 0)
            job_gen = job.get("generation")
            if (
                job_gen is None
                or int(job_gen) != int(expected_generation)
                or current_gen != int(expected_generation)
            ):
                job["status"] = "cancelled"
                job["last_error_code"] = "stale_generation"
                tx.put(_k_job(job_id), job)
                _remove_from_queue(tx, job_id)
                _archive_terminal_job(tx, job_id)
                _scrub_evidence(tx, str(job.get("evidence_id") or ""), "revoked")
                return CommitReport("rejected_stale_generation", job_id)

            ev = tx.get(_k_ev(job["evidence_id"]))
            if not isinstance(ev, Mapping) or ev.get("status") != "pending":
                job["status"] = "cancelled"
                job["last_error_code"] = "evidence_missing"
                tx.put(_k_job(job_id), job)
                _remove_from_queue(tx, job_id)
                _archive_terminal_job(tx, job_id)
                return CommitReport("rejected_evidence", job_id)
            if tx.get(_k_ev_tomb(job["evidence_id"])) is not None:
                job["status"] = "cancelled"
                job["last_error_code"] = "evidence_revoked"
                tx.put(_k_job(job_id), job)
                _remove_from_queue(tx, job_id)
                _archive_terminal_job(tx, job_id)
                _scrub_evidence(tx, str(job.get("evidence_id") or ""), "revoked")
                return CommitReport("rejected_evidence", job_id)

            # 授权现状复查（红队 P0：不信入队快照；版本变化保守拒绝；
            # 按 job 自身证据的 source principal 现查，不认其他活跃来源）
            auth = self._authorize_principal(
                job["owner_id"], job["space_id"], str(ev.get("principal_id") or "")
            )
            current_auth_version = self._authority.authority_version(
                job["owner_id"], job["space_id"]
            )
            if (
                auth is None
                or auth.space_id != job["space_id"]
                or current_auth_version != job.get("authority_version")
                or current_auth_version != authority_version
            ):
                job["status"] = "failed"
                job["last_error_code"] = RC_POLICY
                tx.put(_k_job(job_id), job)
                _remove_from_queue(tx, job_id)
                _archive_terminal_job(tx, job_id)
                # 暂停/解绑后不得在途保留原文
                _scrub_evidence(tx, str(job.get("evidence_id") or ""), "policy_blocked")
                return CommitReport("rejected_policy", job_id)

            evidence = UserEvidence(
                evidence_id=str(ev["evidence_id"]),
                owner_id=str(ev["owner_id"]),
                principal_id=str(ev["principal_id"]),
                conversation_id=str(ev["conversation_id"]),
                channel=str(ev["channel"]),
                space_id=str(ev["space_id"]),
                text=str(ev["text"]),
                received_at=_parse_iso(ev.get("received_at")) or now,
                timezone=ev.get("timezone"),
                source_message_id=ev.get("source_message_id"),
                schema_version=int(ev.get("schema_version") or 1),
            )

            existing = _load_space_facts(tx, job["owner_id"], job["space_id"])
            # 本轮展示给模型的 fact_id → revision 快照（领取时写入 job）
            snapshot_raw = job.get("existing_snapshot") or []
            snapshot: dict[str, int] = {}
            for entry in snapshot_raw:
                if isinstance(entry, Mapping) and entry.get("fact_id"):
                    snapshot[str(entry["fact_id"])] = int(entry.get("revision") or 0)
            accepted_ids: list[str] = []
            pending_ids: list[str] = []
            superseded_ids: list[str] = []
            rejected: list[tuple[str, str]] = []
            retained_quotes: list[dict[str, Any]] = []
            new_fact_ids: list[str] = []

            for cand in candidates:
                cand_key = f"{getattr(cand, 'source_evidence_id', '')}:{getattr(cand, 'span_start', -1)}:{getattr(cand, 'span_end', -1)}"
                decision, reason = validate(cand, evidence, snapshot)
                if decision == "reject":
                    rejected.append((cand_key, reason))
                    continue
                # 幂等：同 evidence+span+规范化 summary 已存在则跳过
                duplicate = False
                norm_summary = " ".join(str(cand.summary).split())
                for ex in existing.values():
                    if (
                        any(
                            r.evidence_id == cand.source_evidence_id
                            and r.span_start == cand.span_start
                            and r.span_end == cand.span_end
                            for r in ex.source_refs
                        )
                        and " ".join(ex.summary.split()) == norm_summary
                    ):
                        duplicate = True
                        break
                if duplicate:
                    rejected.append((cand_key, "duplicate"))
                    continue

                target_id = getattr(cand, "correction_target", None)
                supersedes: str | None = None
                revision = 1
                if target_id and decision == "accepted":
                    from whale_core.memory.owner_fact_extraction import (
                        has_correction_context,
                    )

                    target = existing.get(str(target_id))
                    bound = (
                        target is not None
                        and target.status == FactStatus.ACCEPTED
                        and snapshot.get(str(target_id)) == target.revision
                        and getattr(cand, "predicate", None)
                        and target.predicate
                        and cand.predicate == target.predicate
                        and str(getattr(cand, "value", "") or "") != str(target.value or "")
                    )
                    if not bound:
                        # 更正建议无法定位明确目标（快照外/revision 已变/
                        # 谓词缺失）→ 保守 pending，不覆盖旧事实
                        decision = "pending"
                    elif has_correction_context(evidence, cand):
                        # 明确更正语境 + 同谓词明确冲突 → supersede
                        supersedes = target.fact_id
                        revision = target.revision + 1
                    else:
                        # 同谓词不同值但无更正语境：多值关系不成立
                        # （"我喜欢咖啡"→"我也喜欢茶"、"猫年糕"→"狗饼干"），
                        # 忽略 target 作为独立新事实，绝不 supersede
                        supersedes = None
                        revision = 1

                fact_id = uuid4().hex
                fact = FactRecord(
                    fact_id=fact_id,
                    owner_id=job["owner_id"],
                    space_id=job["space_id"],
                    kind=cand.kind,
                    summary=str(cand.summary),
                    source_refs=(
                        SourceRef(cand.source_evidence_id, cand.span_start, cand.span_end),
                    ),
                    status=FactStatus.ACCEPTED if decision == "accepted" else FactStatus.PENDING,
                    revision=revision,
                    created_at=now,
                    observed_at=evidence.received_at,
                    assertion="user_asserted",
                    valid_from=getattr(cand, "valid_from", None),
                    valid_to=getattr(cand, "valid_to", None),
                    supersedes=supersedes,
                    predicate=getattr(cand, "predicate", None),
                    value=getattr(cand, "value", None),
                )
                tx.put_record(fact_to_record(fact))
                new_fact_ids.append(fact_id)
                retained_quotes.append(
                    {
                        "span_start": cand.span_start,
                        "span_end": cand.span_end,
                        "quote": str(cand.quote),
                    }
                )
                if supersedes:
                    old_rec = tx.get_record(_k_fact(supersedes))
                    if old_rec is not None and not is_tombstone(old_rec):
                        old_fact = fact_from_record(old_rec)
                        if old_fact is not None:
                            superseded = FactRecord(
                                **{
                                    **old_fact.__dict__,
                                    "status": FactStatus.SUPERSEDED,
                                    "superseded_by": fact_id,
                                }
                            )
                            tx.put_record(fact_to_record(superseded))
                            superseded_ids.append(supersedes)
                            existing[supersedes] = superseded
                if decision == "accepted":
                    accepted_ids.append(fact_id)
                else:
                    pending_ids.append(fact_id)
                existing[fact_id] = fact

            # 证据：完成后清原文，只保留支持仍在用事实的最小 quote
            ev = dict(ev)
            ev["text"] = ""
            ev["retained_quotes"] = retained_quotes
            ev["status"] = "processed"
            tx.put(_k_ev(evidence.evidence_id), ev)

            if new_fact_ids:
                idx = list(tx.get(_k_idx(job["owner_id"], job["space_id"])) or [])
                idx.extend(new_fact_ids)
                tx.put(_k_idx(job["owner_id"], job["space_id"]), idx)

            job["status"] = "completed"
            job["completed_at"] = now.isoformat()
            tx.put(_k_job(job_id), job)
            _remove_from_queue(tx, job_id)
            _archive_terminal_job(tx, job_id)
            return CommitReport(
                "committed",
                job_id,
                accepted_fact_ids=tuple(accepted_ids),
                pending_fact_ids=tuple(pending_ids),
                superseded_fact_ids=tuple(superseded_ids),
                rejected=tuple(rejected),
            )

        return self._store.transact(_fn)

    # -- 显式更正（确定性命令路径） -----------------------------------------

    def correct(
        self,
        fact_id: str,
        expected_revision: int,
        evidence: UserEvidence,
        replacement: Any,
        authorization: MemoryAuthorization,
        *,
        validate: Callable[[Any, UserEvidence, Mapping[str, FactRecord]], tuple[str, str]] | None = None,
    ) -> MutationReport:
        """仅同谓词明确冲突才 supersede；歧义拒绝，不盲改。"""
        if validate is None:
            validate = validate_candidate_service
        now = self._now()

        def _fn(tx) -> MutationReport:
            auth = self._authorize_principal(
                authorization.owner_id, authorization.space_id, evidence.principal_id
            )
            if auth is None:
                return MutationReport(False, RC_POLICY)
            rec = tx.get_record(_k_fact(fact_id))
            if rec is None:
                return MutationReport(False, "not_found")
            if is_tombstone(rec):
                return MutationReport(False, "target_deleted")
            old = fact_from_record(rec)
            if (
                old is None
                or old.owner_id != authorization.owner_id
                or old.space_id != authorization.space_id
                or evidence.owner_id != authorization.owner_id
                or evidence.space_id != authorization.space_id
            ):
                return MutationReport(False, "forbidden")
            if old.revision != int(expected_revision):
                return MutationReport(False, "revision_mismatch")
            if old.status != FactStatus.ACCEPTED:
                return MutationReport(False, "not_accepted")
            if tx.get(_k_ev_tomb(evidence.evidence_id)) is not None:
                return MutationReport(False, RC_EVIDENCE_REVOKED)

            # 显式更正路径：允许的快照即目标本身（当前 revision）
            decision, reason = validate(replacement, evidence, {fact_id: old.revision})
            if decision != "accepted":
                return MutationReport(False, f"candidate_rejected:{reason}")
            if (
                not getattr(replacement, "predicate", None)
                or not old.predicate
                or replacement.predicate != old.predicate
                or str(getattr(replacement, "value", "") or "") == str(old.value or "")
            ):
                return MutationReport(False, "ambiguous_correction")

            new_id = uuid4().hex
            new_fact = FactRecord(
                fact_id=new_id,
                owner_id=old.owner_id,
                space_id=old.space_id,
                kind=replacement.kind,
                summary=str(replacement.summary),
                source_refs=(
                    SourceRef(replacement.source_evidence_id, replacement.span_start, replacement.span_end),
                ),
                status=FactStatus.ACCEPTED,
                revision=old.revision + 1,
                created_at=now,
                observed_at=evidence.received_at,
                assertion="user_asserted",
                valid_from=getattr(replacement, "valid_from", None),
                valid_to=getattr(replacement, "valid_to", None),
                supersedes=old.fact_id,
                predicate=getattr(replacement, "predicate", None),
                value=getattr(replacement, "value", None),
            )
            tx.put_record(fact_to_record(new_fact))
            superseded = FactRecord(
                **{
                    **old.__dict__,
                    "status": FactStatus.SUPERSEDED,
                    "superseded_by": new_id,
                }
            )
            tx.put_record(fact_to_record(superseded))
            idx = list(tx.get(_k_idx(old.owner_id, old.space_id)) or [])
            idx.append(new_id)
            tx.put(_k_idx(old.owner_id, old.space_id), idx)
            # 更正证据：只留最小 quote，不留原文
            tx.put(
                _k_ev(evidence.evidence_id),
                {
                    "evidence_id": evidence.evidence_id,
                    "owner_id": evidence.owner_id,
                    "principal_id": evidence.principal_id,
                    "conversation_id": evidence.conversation_id,
                    "channel": evidence.channel,
                    "space_id": evidence.space_id,
                    "role": "user",
                    "text": "",
                    "received_at": evidence.received_at.astimezone(timezone.utc).isoformat(),
                    "timezone": evidence.timezone,
                    "source_message_id": evidence.source_message_id,
                    "schema_version": evidence.schema_version,
                    "status": "processed",
                    "retained_quotes": [
                        {
                            "span_start": replacement.span_start,
                            "span_end": replacement.span_end,
                            "quote": str(replacement.quote),
                        }
                    ],
                },
            )
            return MutationReport(True, "", (new_id, old.fact_id), ())

        return self._store.transact(_fn)

    # -- 遗忘（generation fence + 证据墓碑） --------------------------------

    def forget(
        self,
        fact_ids: Sequence[str],
        authorization: MemoryAuthorization,
        expected_revisions: Mapping[str, int],
    ) -> MutationReport:
        """事务内：generation+1 → 取消旧 generation 在途 job → 修订链墓碑 →
        证据墓碑/清原文 → 索引移除。返回受影响证据供装配侧抑制。"""
        now = self._now()

        def _fn(tx) -> MutationReport:
            auth = self._authorize_principal(
                authorization.owner_id, authorization.space_id, authorization.principal_id
            )
            if auth is None:
                return MutationReport(False, RC_POLICY)
            owner_id = authorization.owner_id
            space_id = authorization.space_id

            idx = list(tx.get(_k_idx(owner_id, space_id)) or [])
            space_facts = _load_space_facts(tx, owner_id, space_id)

            # 修订链扩展（双向，安全优先）
            targets: set[str] = set()
            queue = [str(f) for f in fact_ids]
            while queue:
                fid = queue.pop()
                if fid in targets:
                    continue
                fact = space_facts.get(fid)
                if fact is None:
                    rec = tx.get_record(_k_fact(fid))
                    if rec is None:
                        return MutationReport(False, f"not_found:{fid}")
                    if is_tombstone(rec):
                        continue  # 已删：幂等
                    fact = fact_from_record(rec)
                    if fact is None:
                        return MutationReport(False, f"not_found:{fid}")
                if fact.owner_id != owner_id or fact.space_id != space_id:
                    return MutationReport(False, "forbidden")
                expected = expected_revisions.get(fid)
                if expected is not None and fact.revision != int(expected):
                    return MutationReport(False, "revision_mismatch")
                targets.add(fid)
                for linked in (fact.supersedes, fact.superseded_by):
                    if linked and linked not in targets:
                        queue.append(linked)

            # generation fence
            new_gen = int(tx.get(_k_gen(owner_id, space_id)) or 0) + 1
            tx.put(_k_gen(owner_id, space_id), new_gen)

            # 取消同 owner+space 旧 generation 在途 job
            pending_ids = list(tx.get(_PENDING_Q_KEY) or [])
            kept: list[str] = []
            for jid in pending_ids:
                job = tx.get(_k_job(str(jid)))
                if (
                    isinstance(job, Mapping)
                    and job.get("owner_id") == owner_id
                    and job.get("space_id") == space_id
                    and int(job.get("generation") or 0) < new_gen
                    and job.get("status") in ("pending", "leased")
                ):
                    job = dict(job)
                    job["status"] = "cancelled"
                    job["last_error_code"] = "generation_fence"
                    tx.put(_k_job(str(jid)), job)
                    _archive_terminal_job(tx, str(jid))
                    # 取消的在途证据立即清原文：不留待提炼的全文
                    _scrub_evidence(tx, str(job.get("evidence_id") or ""), "revoked")
                    continue
                kept.append(str(jid))
            tx.put(_PENDING_Q_KEY, kept)

            # 收集受影响证据（先于墓碑化，需要 source_refs）
            affected_evidence_ids: list[str] = []
            for fid in targets:
                fact = space_facts.get(fid)
                if fact is not None:
                    for ref in fact.source_refs:
                        if ref.evidence_id not in affected_evidence_ids:
                            affected_evidence_ids.append(ref.evidence_id)

            # 墓碑化目标事实（清 payload / summary / quote / value / span）
            for fid in targets:
                rec = tx.get_record(_k_fact(fid))
                if rec is None or is_tombstone(rec):
                    continue
                tx.put_record(tombstone_record(rec, now=now))

            # 证据墓碑 + 清原文；其他非目标事实仍引用时保留其最小独立 quote
            affected_refs: list[EvidenceTombstoneRef] = []
            surviving = [
                f
                for f in space_facts.values()
                if f.fact_id not in targets and f.status in (FactStatus.ACCEPTED, FactStatus.PENDING)
            ]
            for eid in affected_evidence_ids:
                tx.put(
                    _k_ev_tomb(eid),
                    {"revoked_at": now.isoformat(), "generation": new_gen},
                )
                ev = tx.get(_k_ev(eid))
                if isinstance(ev, Mapping):
                    ev = dict(ev)
                    keep_quotes: list[dict[str, Any]] = []
                    for f in surviving:
                        for ref in f.source_refs:
                            if ref.evidence_id != eid:
                                continue
                            for q in ev.get("retained_quotes") or []:
                                if (
                                    _int_or_none(q.get("span_start")) == ref.span_start
                                    and _int_or_none(q.get("span_end")) == ref.span_end
                                ):
                                    keep_quotes.append(q)
                    ev["text"] = ""
                    ev["retained_quotes"] = keep_quotes
                    ev["status"] = "revoked" if not keep_quotes else "processed"
                    tx.put(_k_ev(eid), ev)
                    affected_refs.append(
                        EvidenceTombstoneRef(
                            evidence_id=eid,
                            conversation_id=str(ev.get("conversation_id") or ""),
                            source_message_id=ev.get("source_message_id"),
                            received_at=_parse_iso(ev.get("received_at")),
                        )
                    )
                else:
                    affected_refs.append(
                        EvidenceTombstoneRef(eid, "", None, None)
                    )

            remaining_idx = [fid for fid in idx if fid not in targets]
            tx.put(_k_idx(owner_id, space_id), remaining_idx)
            return MutationReport(
                True, "", tuple(sorted(targets)), tuple(affected_refs)
            )

        return self._store.transact(_fn)

    # -- 检索（读侧实时授权 + 证据 fence；仅 accepted + 未过期；预算有界） -----

    def retrieve(
        self,
        authorization: MemoryAuthorization,
        query: str,
        *,
        limit: int = 8,
    ) -> tuple[FactRecord, ...]:
        """读侧 fail-closed：逐 space 现查授权、复核归属、源证据墓碑 fence。

        非空查询要求词项重叠 >0（不相关不召回）；空查询返回最近 accepted
        （管理视图语义，调用方不得当作相关结果展示）。
        """
        now = self._now()
        query_terms = _terms((query or "").lower())
        out: list[tuple[float, FactRecord]] = []
        for space_id in authorization.readable_space_ids or (authorization.space_id,):
            auth = self._authority.authorize_now(authorization.owner_id, space_id)
            if auth is None or auth.owner_id != authorization.owner_id:
                continue
            auth_readable = auth.readable_space_ids or (auth.space_id,)
            if space_id not in auth_readable:
                continue
            idx = self._store.get(_k_idx(authorization.owner_id, space_id)) or []
            for fid in idx:
                rec = self._store.get_record(_k_fact(str(fid)))
                if rec is None or is_tombstone(rec):
                    continue
                if rec.space_id != space_id:  # 损坏索引/伪造导入不得跨 space
                    continue
                fact = fact_from_record(rec)
                if fact is None:
                    continue
                if fact.status != FactStatus.ACCEPTED or fact.assertion != "user_asserted":
                    continue
                if fact.owner_id != authorization.owner_id or fact.space_id != space_id:
                    continue
                # 未来生效的还不是现状；已过期的退出召回
                if fact.valid_from is not None and fact.valid_from > now:
                    continue
                if fact.valid_to is not None and fact.valid_to <= now:
                    continue
                if fact.expires_at is not None and fact.expires_at <= now:
                    continue
                # 源证据 fence：墓碑 / 缺失 / retained_quotes 不含该 span → 不召回
                if not self._sources_intact(fact):
                    continue
                score = 0.0
                if query_terms:
                    summary_terms = _terms(fact.summary.lower())
                    common = query_terms & summary_terms
                    # 修订链锚：省略主体的更正（"它叫团子"）借旧链 summary 做
                    # 只读检索锚（不注入旧 value、不返回 superseded；旧链
                    # deleted 不可读时锚断裂）
                    anchor_common = query_terms & self._chain_anchor_terms(fact)
                    if not common and not anchor_common:
                        continue
                    score = (len(common) + 0.5 * len(anchor_common)) / len(query_terms)
                out.append((score, fact))
        out.sort(key=lambda item: (item[0], item[1].observed_at), reverse=True)
        return tuple(f for _, f in out[: max(0, int(limit))])

    def _chain_anchor_terms(self, fact: FactRecord) -> set[str]:
        """沿 supersedes 链收集旧版本 summary 的词项（只读检索锚）。

        边界：同 owner/space、深度 ≤8、遇墓碑/缺失即断——已遗忘链不供锚。
        """
        terms: set[str] = set()
        seen = {fact.fact_id}
        current = fact.supersedes
        depth = 0
        while current and current not in seen and depth < 8:
            seen.add(current)
            rec = self._store.get_record(_k_fact(current))
            if rec is None or is_tombstone(rec):
                break
            old = fact_from_record(rec)
            if (
                old is None
                or old.owner_id != fact.owner_id
                or old.space_id != fact.space_id
            ):
                break
            terms |= _terms(old.summary.lower())
            current = old.supersedes
            depth += 1
        return terms

    def _sources_intact(self, fact: FactRecord) -> bool:
        """源证据 fence：空来源、证据墓碑/缺失、retained quote 与
        (span, summary) 不完全对应，一律不召回。"""
        if not fact.source_refs:
            return False
        for ref in fact.source_refs:
            if self._store.get(_k_ev_tomb(ref.evidence_id)) is not None:
                return False
            ev = self._store.get(_k_ev(ref.evidence_id))
            if not isinstance(ev, Mapping):
                return False
            quotes = ev.get("retained_quotes") or []
            if not any(
                isinstance(q, Mapping)
                and _int_or_none(q.get("span_start")) == ref.span_start
                and _int_or_none(q.get("span_end")) == ref.span_end
                and str(q.get("quote") or "") == fact.summary
                for q in quotes
            ):
                return False
        return True


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _int_or_none(value) -> int | None:
    """0 是合法 span 起点，不能被 ``or -1`` 吞掉。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _remove_from_queue(tx, job_id: str) -> None:
    ids = [str(j) for j in (tx.get(_PENDING_Q_KEY) or [])]
    if job_id in ids:
        ids.remove(job_id)
        tx.put(_PENDING_Q_KEY, ids)


def _archive_terminal_job(tx, job_id: str) -> None:
    """终态 job 记入有界 journal；超出 256 删除最旧 ofjob 记录。

    同事务维护，无全库扫描。只约束 job 记录数量；evidence/墓碑不在此列。
    """
    journal = [str(j) for j in (tx.get(_TERMINAL_Q_KEY) or [])]
    if job_id in journal:
        journal.remove(job_id)
    journal.append(job_id)
    while len(journal) > TERMINAL_JOURNAL_LIMIT:
        oldest = journal.pop(0)
        tx.delete(_k_job(oldest))
    tx.put(_TERMINAL_Q_KEY, journal)


def _scrub_evidence(tx, evidence_id: str, status: str) -> None:
    """清空在途证据原文与 retained_quotes（不留被删/被拒内容）。"""
    if not evidence_id:
        return
    ev = tx.get(_k_ev(evidence_id))
    if isinstance(ev, Mapping):
        ev = dict(ev)
        ev["text"] = ""
        ev["retained_quotes"] = []
        ev["status"] = status
        tx.put(_k_ev(evidence_id), ev)


def _load_space_facts(tx, owner_id: str, space_id: str) -> dict[str, FactRecord]:
    """按索引加载事实；逐条复核 owner/space，损坏索引/伪造导入不得越界。"""
    facts: dict[str, FactRecord] = {}
    idx = tx.get(_k_idx(owner_id, space_id)) or []
    for fid in idx:
        rec = tx.get_record(_k_fact(str(fid)))
        if rec is None or is_tombstone(rec):
            continue
        if rec.space_id != space_id:
            continue
        fact = fact_from_record(rec)
        if fact is not None and fact.owner_id == owner_id and fact.space_id == space_id:
            facts[fact.fact_id] = fact
    return facts


# 由 owner_fact_extraction 提供的候选类型在服务层仅按属性访问；
# 该别名避免循环 import（extraction 依赖本模块的领域类型）。
def validate_candidate_service(cand, evidence, allowed_correction_targets):
    from whale_core.memory.owner_fact_extraction import validate_candidate

    return validate_candidate(cand, evidence, allowed_correction_targets)
