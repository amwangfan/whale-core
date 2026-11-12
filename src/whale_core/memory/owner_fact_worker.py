"""owner_fact_v1 持久任务 worker（单宿主单实例，默认并发 1）。

纪律（冻结 v1 §5 + 协调代理 2026-11-12）：
- 复用 ``life.bounded_call.BoundedCall`` 单线程门调用模型：超时未真正结束时
  不再发新请求（busy 期间不领取新 job），不开无限线程掩盖超时；
- job 状态机 pending/leased/completed/failed/cancelled；lease 有过期恢复；
  重试最多 ``max_retries`` 次（默认 2），模型请求超时默认 10s（可配置）；
- 领取/重排/快照全部走 ``store.transact``，事务内只用 tx 方法；
- 崩溃恢复：重启后 lease 过期 job 被重新领取；queued 受理即持久；
- stop(drain=False) 立即停止领取新任务，不谎称清空；drain(timeout) 为测试
  barrier，不 sleep 赌时序。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from whale_core.memory.owner_facts import (
    FactRecord,
    FactStatus,
    OwnerFactService,
    UserEvidence,
    _archive_terminal_job,
    _k_ev,
    _k_fact,
    _k_idx,
    _k_job,
    _PENDING_Q_KEY,
    fact_from_record,
    is_tombstone,
)
from whale_core.memory.owner_fact_extraction import OwnerFactExtractor
from whale_core.memory.store import MemoryStore


class CallGate(Protocol):
    """受控模型调用门（宿主注入，如 life.bounded_call.BoundedCall）。

    语义要求：单 worker 线程；超时/异常返回 ``default``；超时后底层调用
    未真正收尾前 ``busy`` 保持 True，期间不再发新请求。memory 域不依赖
    life 域（架构守卫），故此处仅声明协议。
    """

    @property
    def busy(self) -> bool:
        ...

    def __call__(self, *args, default=None, timeout_s: float = 0.0, **kw):
        ...

DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MODEL_TIMEOUT = 10.0
DEFAULT_POLL_INTERVAL = 0.2
# 领取时为模型快照的既有事实数量（与提炼器 max_existing 对齐）
EXISTING_SNAPSHOT_LIMIT = 8
# 领取时为控制事务大小，只扫索引尾部最近若干条
IDX_SCAN_TAIL = 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WorkerStats:
    claimed: int
    completed: int
    failed: int
    retried: int
    model_failures: int
    commit_rejected: int
    queue_depth: int
    in_flight: bool
    model_busy: bool


class OwnerFactWorker:
    """持久 job worker。宿主生命周期持有单实例；禁止每请求建线程。"""

    @classmethod
    def create(
        cls,
        store: MemoryStore,
        service: OwnerFactService,
        extractor: OwnerFactExtractor,
        *,
        call_gate: CallGate,
        worker_id: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        model_timeout: float = DEFAULT_MODEL_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        now: Callable[[], datetime] = _utc_now,
    ) -> "OwnerFactWorker":
        """``call_gate`` 必须包装 ``extractor.extract`` 并由宿主注入
        （如 ``BoundedCall(extractor.extract)``）；worker 不自行创建线程池。"""
        return cls(
            store,
            service,
            extractor,
            call_gate=call_gate,
            worker_id=worker_id or f"ofw-{uuid4().hex[:12]}",
            lease_seconds=lease_seconds,
            max_retries=max_retries,
            model_timeout=model_timeout,
            poll_interval=poll_interval,
            now=now,
        )

    def __init__(
        self,
        store: MemoryStore,
        service: OwnerFactService,
        extractor: OwnerFactExtractor,
        *,
        call_gate: CallGate,
        worker_id: str,
        lease_seconds: float,
        max_retries: int,
        model_timeout: float,
        poll_interval: float,
        now: Callable[[], datetime],
    ) -> None:
        for name, value in (("lease_seconds", lease_seconds), ("model_timeout", model_timeout),
                            ("poll_interval", poll_interval)):
            if type(value) not in (int, float) or not 0 < value <= threading.TIMEOUT_MAX:
                raise ValueError(f"{name} must be positive and within platform limits")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if lease_seconds <= model_timeout:
            raise ValueError("lease_seconds 必须大于 model_timeout")
        self._store = store
        self._service = service
        self._extractor = extractor
        self._gate = call_gate
        self._worker_id = worker_id
        self._lease_seconds = float(lease_seconds)
        self._max_retries = int(max_retries)
        self._model_timeout = float(model_timeout)
        self._poll = float(poll_interval)
        self._now = now
        self._stop = threading.Event()
        self._in_flight = threading.Event()
        self._thread: threading.Thread | None = None
        self._counters = {"claimed": 0, "completed": 0, "failed": 0, "retried": 0,
                          "model_failures": 0, "commit_rejected": 0}
        self._counters_lock = threading.Lock()

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker 已启动；单宿主单实例")
        self._thread = threading.Thread(
            target=self._loop, name=f"owner-fact-worker-{self._worker_id}", daemon=True
        )
        self._thread.start()

    def stop(self, *, drain: bool = False, timeout: float = 5.0) -> None:
        """停止领取新任务。drain=True 时等待在途 job 完成（有界 timeout）。

        不谎称清空：返回后队列可能仍有 pending job（下次启动恢复）。
        计时用单调时钟，不受注入的业务时钟影响。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout)))
        if drain and self._in_flight.is_set():
            # 模型调用可能仍在 call gate 后台线程收尾；等待其自然释放
            deadline = time.monotonic() + max(0.1, float(timeout))
            while self._in_flight.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.02)

    def drain(self, timeout: float = 10.0) -> bool:
        """测试 barrier：等队列无 pending/leased、无在途且模型门空闲；
        返回是否真正排空（gate busy 时不得虚报）。"""
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if self._queue_open_count() == 0 and not self._in_flight.is_set() and not self._gate.busy:
                return True
            threading.Event().wait(0.02)
        return (
            self._queue_open_count() == 0
            and not self._in_flight.is_set()
            and not self._gate.busy
        )

    def stats(self) -> WorkerStats:
        with self._counters_lock:
            c = dict(self._counters)
        return WorkerStats(
            claimed=c["claimed"],
            completed=c["completed"],
            failed=c["failed"],
            retried=c["retried"],
            model_failures=c["model_failures"],
            commit_rejected=c["commit_rejected"],
            queue_depth=self._queue_open_count(),
            in_flight=self._in_flight.is_set(),
            model_busy=self._gate.busy,
        )

    # -- 主循环 --------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._gate.busy:
                    # 上一次模型调用尚未真正收尾：不发新请求（防线程堆积）
                    self._stop.wait(self._poll)
                    continue
                claimed = self._claim_next()
                if claimed is None:
                    self._stop.wait(self._poll)
                    continue
                self._in_flight.set()
                try:
                    self._process(claimed)
                finally:
                    self._in_flight.clear()
            except Exception:
                # worker 异常不得杀死循环，也不得影响主对话
                with self._counters_lock:
                    self._counters["failed"] += 1
                self._stop.wait(self._poll)

    def _queue_open_count(self) -> int:
        ids = self._store.get(_PENDING_Q_KEY) or []
        count = 0
        for jid in ids:
            job = self._store.get(_k_job(str(jid))) or {}
            if job.get("status") in ("pending", "leased"):
                count += 1
        return count

    # -- 领取（事务内：状态翻转 + lease + 快照） ------------------------------

    def _claim_next(self) -> dict[str, Any] | None:
        now = self._now()
        lease_exp = now + timedelta(seconds=self._lease_seconds)

        def _fn(tx):
            ids = [str(j) for j in (tx.get(_PENDING_Q_KEY) or [])]
            # 同 owner+space 按受理顺序串行（跨进程）：domain 头部 job 仍
            # pending/有效 leased 时，后续同 domain job 不得被提前领取——
            # 否则更正会先于被更正事实处理而快照落空。不同 domain 可并发。
            blocked_domains: set[tuple[str, str]] = set()
            for jid in ids:
                job = tx.get(_k_job(jid))
                if not isinstance(job, Mapping):
                    continue
                domain = (str(job.get("owner_id") or ""), str(job.get("space_id") or ""))
                if domain in blocked_domains:
                    continue
                status = job.get("status")
                expired = False
                if status == "leased":
                    lease_end = job.get("lease_expires_at") or ""
                    try:
                        expired = bool(lease_end) and now > datetime.fromisoformat(lease_end)
                    except ValueError:
                        expired = True  # 损坏 lease 视为过期，可恢复
                if status == "leased" and not expired:
                    # 有效 leased 的 domain 头部阻塞后续
                    blocked_domains.add(domain)
                    continue
                if status != "pending" and not expired:
                    continue
                job = dict(job)
                job["status"] = "leased"
                job["lease_owner"] = self._worker_id
                job["lease_token"] = uuid4().hex  # 每次 claim 唯一，收尾 CAS 用
                job["lease_expires_at"] = lease_exp.isoformat()
                # 本轮展示给模型的既有事实快照（id+revision，≤8；索引尾部扫描有界）
                job["existing_snapshot"] = self._snapshot_existing(
                    tx, job.get("owner_id") or "", job.get("space_id") or ""
                )
                tx.put(_k_job(jid), job)
                return job
            return None

        job = self._store.transact(_fn)
        if job is not None:
            with self._counters_lock:
                self._counters["claimed"] += 1
        return job

    def _snapshot_existing(self, tx, owner_id: str, space_id: str) -> list[dict[str, Any]]:
        idx = [str(f) for f in (tx.get(_k_idx(owner_id, space_id)) or [])]
        facts: list[FactRecord] = []
        for fid in idx[-IDX_SCAN_TAIL:]:
            rec = tx.get_record(_k_fact(fid))
            if rec is None or is_tombstone(rec) or rec.space_id != space_id:
                continue
            fact = fact_from_record(rec)
            if (
                fact is not None
                and fact.status == FactStatus.ACCEPTED
                and fact.owner_id == owner_id
                and fact.space_id == space_id
            ):
                facts.append(fact)
        facts.sort(key=lambda f: f.observed_at, reverse=True)
        return [
            {"fact_id": f.fact_id, "revision": f.revision}
            for f in facts[:EXISTING_SNAPSHOT_LIMIT]
        ]

    # -- 处理 ----------------------------------------------------------------

    def _process(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        token = str(job.get("lease_token") or "")
        ev = self._store.get(_k_ev(str(job.get("evidence_id") or "")))
        if not isinstance(ev, Mapping) or not ev.get("text"):
            self._finish_terminal(job_id, token, "evidence_missing")
            return
        received = ev.get("received_at") or ""
        try:
            received_at = datetime.fromisoformat(str(received))
        except ValueError:
            self._finish_terminal(job_id, token, "bad_evidence_time")
            return
        evidence = UserEvidence(
            evidence_id=str(ev["evidence_id"]),
            owner_id=str(ev["owner_id"]),
            principal_id=str(ev["principal_id"]),
            conversation_id=str(ev["conversation_id"]),
            channel=str(ev["channel"]),
            space_id=str(ev["space_id"]),
            text=str(ev["text"]),
            received_at=received_at,
            timezone=ev.get("timezone"),
            source_message_id=ev.get("source_message_id"),
            schema_version=int(ev.get("schema_version") or 1),
        )
        existing = self._load_snapshot_facts(job)
        candidates = self._gate(
            evidence, existing, default=None, timeout_s=self._model_timeout
        )
        if candidates is None:
            # 模型超时/异常/解析失败：有界重试（合法空候选走正常 commit 完成）
            with self._counters_lock:
                self._counters["model_failures"] += 1
            self._fail_or_retry(job_id, token)
            return
        report = self._service.commit(
            job_id,
            tuple(candidates),
            int(job.get("generation") or 0),
            str(job.get("authority_version") or ""),
            worker_id=self._worker_id,
            lease_token=token,
        )
        with self._counters_lock:
            if report.status == "committed":
                self._counters["completed"] += 1
            else:
                self._counters["commit_rejected"] += 1

    def _load_snapshot_facts(self, job: Mapping[str, Any]) -> tuple[FactRecord, ...]:
        out: list[FactRecord] = []
        for entry in job.get("existing_snapshot") or []:
            if not isinstance(entry, Mapping):
                continue
            rec = self._store.get_record(_k_fact(str(entry.get("fact_id") or "")))
            if rec is None or is_tombstone(rec):
                continue
            fact = fact_from_record(rec)
            if fact is not None and fact.status == FactStatus.ACCEPTED:
                out.append(fact)
        return tuple(out)

    # -- 失败/收尾（全部事务化） ----------------------------------------------

    def _still_mine(self, job: Mapping[str, Any], token: str) -> bool:
        """CAS：只有仍被自己 lease（worker_id + lease_token 双匹配）才允许收尾写。

        防：lease 过期后被他人领取，旧 worker 的失败/完成写覆盖新 lease；
        防：forget 已把 job 取消后，迟到收尾把 cancelled 改回 pending。
        """
        return (
            job.get("status") == "leased"
            and job.get("lease_owner") == self._worker_id
            and bool(token)
            and job.get("lease_token") == token
        )

    def _fail_or_retry(self, job_id: str, token: str) -> None:
        def _fn(tx):
            job = tx.get(_k_job(job_id))
            if not isinstance(job, Mapping) or not self._still_mine(job, token):
                return None  # 已被取消/被他人领取：不写
            job = dict(job)
            attempts = int(job.get("attempts") or 0) + 1
            job["attempts"] = attempts
            job["lease_owner"] = ""
            job["lease_token"] = ""
            job["lease_expires_at"] = ""
            if attempts > self._max_retries:
                job["status"] = "failed"
                job["last_error_code"] = "model_unavailable"
                ids = [str(j) for j in (tx.get(_PENDING_Q_KEY) or [])]
                if job_id in ids:
                    ids.remove(job_id)
                    tx.put(_PENDING_Q_KEY, ids)
                _archive_terminal_job(tx, job_id)
            else:
                job["status"] = "pending"
                job["last_error_code"] = "model_retry"
            tx.put(_k_job(job_id), job)
            return job.get("status")

        status = self._store.transact(_fn)
        with self._counters_lock:
            if status == "failed":
                self._counters["failed"] += 1
            elif status == "pending":
                self._counters["retried"] += 1

    def _finish_terminal(self, job_id: str, token: str, reason: str) -> None:
        def _fn(tx):
            job = tx.get(_k_job(job_id))
            if not isinstance(job, Mapping) or not self._still_mine(job, token):
                return
            job = dict(job)
            job["status"] = "failed"
            job["last_error_code"] = reason
            job["lease_owner"] = ""
            job["lease_token"] = ""
            job["lease_expires_at"] = ""
            tx.put(_k_job(job_id), job)
            _archive_terminal_job(tx, job_id)
            ids = [str(j) for j in (tx.get(_PENDING_Q_KEY) or [])]
            if job_id in ids:
                ids.remove(job_id)
                tx.put(_PENDING_Q_KEY, ids)

        self._store.transact(_fn)
        with self._counters_lock:
            self._counters["failed"] += 1
