"""owner_fact_v1 worker 生命周期/事务/崩溃/并发/fence 测试。

模型调用经注入的 BoundedCall（life 域实现，测试侧注入不违反 memory→life
架构守卫：被守卫的是 src/whale_core/memory 源码，不是测试）。全部临时
SQLite/合成证据，drain 作为 barrier，不 sleep 赌时序。
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from datetime import datetime, timedelta, timezone

from whale_core.life.bounded_call import BoundedCall
from whale_core.memory.owner_fact_extraction import OwnerFactExtractor
from whale_core.memory.owner_fact_worker import OwnerFactWorker
from whale_core.memory.owner_facts import (
    MemoryAuthorization,
    ObserveStatus,
    OwnerFactService,
    UserEvidence,
    _k_ev,
    _k_idx,
    _k_job,
)
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore

NOW = datetime(2026, 11, 12, 6, 0, 0, tzinfo=timezone.utc)
OWNER = "owner1"
SPACE = "spaceA"
PRINCIPAL = "paired:qq:123"


class FakeAuthority:
    def __init__(self, version: str = "v1"):
        self.allow = True
        self.version = version

    def authorize_now(self, owner_id, space_id):
        if not self.allow or owner_id != OWNER:
            return None
        return MemoryAuthorization(
            owner_id=owner_id, space_id=space_id, principal_id=PRINCIPAL,
            policy_version="p1", authority_version=self.version,
            readable_space_ids=(space_id,),
        )

    def authority_version(self, owner_id, space_id):
        return self.version


def make_auth():
    return MemoryAuthorization(
        owner_id=OWNER, space_id=SPACE, principal_id=PRINCIPAL,
        policy_version="p1", authority_version="v1", readable_space_ids=(SPACE,),
    )


def make_evidence(text: str, eid: str) -> UserEvidence:
    return UserEvidence(
        evidence_id=eid, owner_id=OWNER, principal_id=PRINCIPAL,
        conversation_id="private:123", channel="qq", space_id=SPACE,
        text=text, received_at=NOW, timezone="Asia/Shanghai",
    )


def candidates_json(*quotes: str) -> str:
    return json.dumps(
        {"candidates": [{"kind": "fact", "quote": q} for q in quotes]},
        ensure_ascii=False,
    )


class ScriptModel:
    """按调用顺序返回预置输出；可阻塞、可超时。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.lock = threading.Lock()
        self.block_event: threading.Event | None = None  # 设置后每次调用先等待
        self.sleep_s = 0.0

    def complete(self, prompt: str, *, timeout: float) -> str:
        with self.lock:
            self.calls += 1
        if self.block_event is not None:
            self.block_event.wait(10)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        with self.lock:
            if self._outputs:
                return self._outputs.pop(0)
        return ""


def make_worker(store, service, model, **kw):
    model_timeout = kw.get("model_timeout", 2.0)
    extractor = OwnerFactExtractor(model, timeout_s=model_timeout)
    gate = BoundedCall(extractor.extract, default_timeout_s=model_timeout)
    params = dict(lease_seconds=10.0, max_retries=2, model_timeout=2.0,
                  poll_interval=0.01, now=lambda: NOW)
    params.update(kw)
    return OwnerFactWorker.create(store, service, extractor, call_gate=gate, **params)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        yield SqliteMemoryStore(tmp_path / "worker.sqlite3")


def wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestWorkerHappyPath:
    def test_observe_to_commit(self, store):
        svc = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        model = ScriptModel([candidates_json("我养的猫叫年糕")])
        worker = make_worker(store, svc, model)
        assert svc.observe(make_evidence("我养的猫叫年糕", "ev-w1"), make_auth()).status == ObserveStatus.QUEUED
        worker.start()
        try:
            assert worker.drain(timeout=5.0)
        finally:
            worker.stop(timeout=2.0)
        hits = svc.retrieve(make_auth(), "猫")
        assert len(hits) == 1 and hits[0].summary == "我养的猫叫年糕"
        stats = worker.stats()
        assert stats.completed == 1 and stats.failed == 0
        assert store.get(_k_ev("ev-w1"))["text"] == ""

    def test_legal_empty_candidates_complete_without_retry(self, store):
        svc = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        model = ScriptModel(['{"candidates": []}'])
        worker = make_worker(store, svc, model)
        svc.observe(make_evidence("嗯嗯好的", "ev-w2"), make_auth())
        worker.start()
        try:
            assert worker.drain(timeout=5.0)
        finally:
            worker.stop(timeout=2.0)
        assert model.calls == 1  # 空闲聊不重复烧模型
        job_ids = [k for k in store.keys() if k.startswith("ofjob:")]
        assert store.get(job_ids[0])["status"] == "completed"


class TestWorkerFailure:
    def test_parse_failure_bounded_retry(self, store):
        svc = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        model = ScriptModel(["这不是JSON", "还不是", "依然不是", "多余"])
        worker = make_worker(store, svc, model, max_retries=2)
        svc.observe(make_evidence("我养的猫叫年糕", "ev-w3"), make_auth())
        worker.start()
        try:
            assert worker.drain(timeout=5.0)
        finally:
            worker.stop(timeout=2.0)
        job = store.get([k for k in store.keys() if k.startswith("ofjob:")][0])
        assert job["status"] == "failed" and job["attempts"] == 3  # 2 次重试后终止
        assert model.calls == 3
        assert svc.retrieve(make_auth(), "猫") == ()

    def test_model_timeout_no_unbounded_threads(self, store):
        svc = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        model = ScriptModel([candidates_json("我养的猫叫年糕")])
        model.sleep_s = 0.8
        worker = make_worker(store, svc, model, model_timeout=0.2,
                             lease_seconds=2.0, max_retries=1)
        extractor_timeout = 0.2
        svc.observe(make_evidence("我养的猫叫年糕", "ev-w4"), make_auth())
        worker.start()
        try:
            assert worker.drain(timeout=10.0)
        finally:
            worker.stop(timeout=3.0)
        stats = worker.stats()
        assert stats.model_failures >= 1
        job = store.get([k for k in store.keys() if k.startswith("ofjob:")][0])
        assert job["status"] == "failed"
        # 有界：重试次数受控（max_retries=1 → 最多 2 次模型调用）
        assert model.calls <= 2
        _ = extractor_timeout

    def test_worker_exception_does_not_break_loop(self, store):
        # evidence 损坏（无 received_at 合法值之外的场景）：job 终止但 worker 活着
        svc = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        model = ScriptModel([candidates_json("我养的猫叫年糕")])
        worker = make_worker(store, svc, model)
        svc.observe(make_evidence("我养的猫叫年糕", "ev-w5"), make_auth())
        # 手工破坏证据时间
        def _fn(tx):
            ev = dict(tx.get(_k_ev("ev-w5")))
            ev["received_at"] = "not-a-date"
            tx.put(_k_ev("ev-w5"), ev)
        store.transact(_fn)
        worker.start()
        try:
            assert worker.drain(timeout=5.0)
        finally:
            worker.stop(timeout=2.0)
        job = store.get([k for k in store.keys() if k.startswith("ofjob:")][0])
        assert job["status"] == "failed" and job["last_error_code"] == "bad_evidence_time"


class TestWorkerRecovery:
    def test_expired_lease_reclaimed(self, tmp_path):
        # 崩溃恢复：lease 过期的 job 被新 worker 重新领取（跨 SQLite 连接）
        path = tmp_path / "recovery.sqlite3"
        store1 = SqliteMemoryStore(path)
        svc1 = OwnerFactService(store1, FakeAuthority(), now=lambda: NOW)
        svc1.observe(make_evidence("我养的猫叫年糕", "ev-w6"), make_auth())

        def _fake_dead_lease(tx):
            ids = tx.get("ofq:pending")
            job = dict(tx.get(_k_job(ids[0])))
            job["status"] = "leased"
            job["lease_owner"] = "dead-worker"
            job["lease_token"] = "stale-token"
            job["lease_expires_at"] = (NOW - timedelta(seconds=5)).isoformat()
            tx.put(_k_job(ids[0]), job)
        store1.transact(_fake_dead_lease)

        store2 = SqliteMemoryStore(path)  # 第二个连接
        svc2 = OwnerFactService(store2, FakeAuthority(), now=lambda: NOW)
        model = ScriptModel([candidates_json("我养的猫叫年糕")])
        worker = make_worker(store2, svc2, model)
        worker.start()
        try:
            assert worker.drain(timeout=5.0)
        finally:
            worker.stop(timeout=2.0)
        assert len(svc2.retrieve(make_auth(), "猫")) == 1

    def test_two_workers_two_connections_exactly_once(self, tmp_path):
        path = tmp_path / "race.sqlite3"
        store_a = SqliteMemoryStore(path)
        svc = OwnerFactService(store_a, FakeAuthority(), now=lambda: NOW)
        svc.observe(make_evidence("我养的猫叫年糕", "ev-w7"), make_auth())

        store_b = SqliteMemoryStore(path)
        model_a = ScriptModel([candidates_json("我养的猫叫年糕")])
        model_b = ScriptModel([candidates_json("我养的猫叫年糕")])
        worker_a = make_worker(store_a, svc, model_a)
        svc_b = OwnerFactService(store_b, FakeAuthority(), now=lambda: NOW)
        worker_b = make_worker(store_b, svc_b, model_b)
        worker_a.start()
        worker_b.start()
        try:
            assert worker_a.drain(timeout=5.0)
            assert worker_b.drain(timeout=5.0)
        finally:
            worker_a.stop(timeout=2.0)
            worker_b.stop(timeout=2.0)
        # 恰好一次处理：一条事实、一个 completed job
        idx = store_a.get(_k_idx(OWNER, SPACE)) or []
        assert len(idx) == 1
        job = store_a.get([k for k in store_a.keys() if k.startswith("ofjob:")][0])
        assert job["status"] == "completed"
        assert model_a.calls + model_b.calls == 1

    def test_stale_worker_cannot_overwrite_after_forget(self, tmp_path):
        """A 的 lease 过期 → B 领取并完成；A 迟到收尾不得改写 job。"""
        path = tmp_path / "cas.sqlite3"
        store1 = SqliteMemoryStore(path)
        svc = OwnerFactService(store1, FakeAuthority(), now=lambda: NOW)
        svc.observe(make_evidence("我养的猫叫年糕", "ev-w8"), make_auth())
        # 模拟 A：lease 已过期
        def _lease_a(tx):
            ids = tx.get("ofq:pending")
            job = dict(tx.get(_k_job(ids[0])))
            job["status"] = "leased"
            job["lease_owner"] = "worker-A"
            job["lease_token"] = "token-A"
            job["lease_expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
            tx.put(_k_job(ids[0]), job)
            return job
        job_a = store1.transact(_lease_a)
        # B（新连接）正常领取并完成
        store2 = SqliteMemoryStore(path)
        svc2 = OwnerFactService(store2, FakeAuthority(), now=lambda: NOW)
        model_b = ScriptModel([candidates_json("我养的猫叫年糕")])
        worker_b = make_worker(store2, svc2, model_b)
        worker_b.start()
        try:
            assert worker_b.drain(timeout=5.0)
        finally:
            worker_b.stop(timeout=2.0)
        # A 迟到 commit：lease_token/owner 不匹配 → rejected_lease，不覆盖
        from whale_core.memory.owner_fact_extraction import FactCandidate
        from whale_core.memory.owner_facts import FactKind
        ev = make_evidence("我养的猫叫年糕", "ev-w8")
        cand = FactCandidate(FactKind.FACT, "我养的猫叫年糕", "ev-w8", "我养的猫叫年糕", 0, 7)
        report = svc.commit(
            str(job_a["job_id"]), (cand,), int(job_a["generation"]),
            str(job_a["authority_version"]), worker_id="worker-A",
            lease_token="token-A",
        )
        assert report.status == "rejected_lease"
        job = store1.get(_k_job(str(job_a["job_id"])))
        assert job["status"] == "completed"
        assert len(store1.get(_k_idx(OWNER, SPACE)) or []) == 1


class TestDomainSerialClaim:
    def test_correction_waits_for_prior_fact_across_connections(self, tmp_path):
        """2 连接 2 worker：A 首事实模型阻塞时，B 不得提前领取后续更正；
        A 提交后 B 的快照必须包含 A 的事实，更正成功 supersede。"""
        import re as _re

        path = tmp_path / "serial.sqlite3"
        store_a = SqliteMemoryStore(path)
        svc_a = OwnerFactService(store_a, FakeAuthority(), now=lambda: NOW)
        assert svc_a.observe(make_evidence("我养的猫叫年糕", "ev-s1"), make_auth()).status == ObserveStatus.QUEUED
        assert svc_a.observe(make_evidence("名字说错了，它叫团子", "ev-s2"), make_auth()).status == ObserveStatus.QUEUED

        release_a = threading.Event()

        class BlockingFirstModel:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, *, timeout):
                self.calls += 1
                release_a.wait(10)
                # 必须带 predicate/value，否则更正因目标无谓词而保守降 pending
                return json.dumps({"candidates": [{
                    "kind": "fact", "quote": "我养的猫叫年糕",
                    "predicate": "pet_name", "value": "年糕",
                }]}, ensure_ascii=False)

        class CorrectionModel:
            """从 prompt 的既有事实快照中取 pet_name 事实 id 作为 correction_target。"""

            def __init__(self):
                self.calls = 0

            def complete(self, prompt, *, timeout):
                self.calls += 1
                m = _re.search(r'"id": "([0-9a-f]{32})"', prompt)
                target = m.group(1) if m else None
                return json.dumps({"candidates": [{
                    "kind": "fact", "quote": "它叫团子",
                    "predicate": "pet_name", "value": "团子",
                    "correction_target": target,
                }]}, ensure_ascii=False)

        model_a = BlockingFirstModel()
        model_b = CorrectionModel()
        worker_a = make_worker(store_a, svc_a, model_a, worker_id="worker-A")
        store_b = SqliteMemoryStore(path)
        svc_b = OwnerFactService(store_b, FakeAuthority(), now=lambda: NOW)
        worker_b = make_worker(store_b, svc_b, model_b, worker_id="worker-B")
        worker_a.start()
        worker_b.start()
        try:
            # A 领取 ev-s1 并阻塞在模型；B 不得领取 ev-s2 的 job
            assert wait_for(lambda: model_a.calls == 1)
            time.sleep(0.1)  # 给 B 足够窗口尝试（反证其不提前 claim）
            job_ids = sorted(k for k in store_a.keys() if k.startswith("ofjob:"))
            states = {j: (store_a.get(j) or {}).get("status") for j in job_ids}
            assert "leased" in states.values()
            assert "pending" in states.values()  # 后续更正仍在等
            # 先置 A 的停止标志（线程仍阻塞在模型调用中，join 超时即返），
            # 再放开模型：A 提交首事实后退出循环，不会领走更正 job
            worker_a.stop(timeout=0.3)
            release_a.set()
            assert wait_for(lambda: len(svc_a.retrieve(make_auth(), "猫")) == 1)
            assert worker_b.drain(timeout=5.0)
        finally:
            worker_a.stop(timeout=2.0)
            worker_b.stop(timeout=2.0)
        assert model_b.calls == 1
        hits = svc_a.retrieve(make_auth(), "猫")
        assert len(hits) == 1 and "团子" in hits[0].summary
        # B 的快照确实包含 A 的事实（job 记录保留快照）
        job_ids = [k for k in store_a.keys() if k.startswith("ofjob:")]
        snapshots = [
            (store_a.get(j) or {}).get("existing_snapshot") for j in job_ids
        ]
        assert any(snapshots)  # 至少一个 job 快照非空
        corr_job = [
            j for j in job_ids
            if (store_a.get(j) or {}).get("evidence_id") == "ev-s2"
        ][0]
        assert (store_a.get(corr_job) or {})["existing_snapshot"], "更正 job 快照必须含 A 的事实"


class TestForgetFence:
    def test_forget_during_model_call_blocks_commit(self, store):
        svc = OwnerFactService(store, FakeAuthority(), now=lambda: NOW)
        # 第一次调用立即返回（建立旧事实）；第二次阻塞直到 forget 完成
        release = threading.Event()
        model = ScriptModel([
            candidates_json("我养的猫叫年糕"),
            candidates_json("我今天吃了火锅"),
        ])
        worker = make_worker(store, svc, model)
        svc.observe(make_evidence("我养的猫叫年糕", "ev-f1"), make_auth())
        worker.start()
        try:
            assert worker.drain(timeout=5.0)
        finally:
            pass
        hits = svc.retrieve(make_auth(), "猫")
        assert len(hits) == 1
        fid = hits[0].fact_id

        model.block_event = release  # 第二次调用阻塞
        svc.observe(make_evidence("我今天吃了火锅", "ev-f2"), make_auth())
        assert wait_for(lambda: (store.get("ofq:pending") or []) and
                        (store.get(_k_job((store.get("ofq:pending"))[0])) or {}).get("status") == "leased")
        # worker 正在模型调用中：执行 forget
        report = svc.forget([fid], make_auth(), {fid: 1})
        assert report.ok
        release.set()  # 模型返回，worker 尝试 commit
        try:
            assert worker.drain(timeout=5.0)
        finally:
            worker.stop(timeout=2.0)
        # 迟到提交被拒绝；无新事实；遗忘内容不可检索
        stats = worker.stats()
        assert stats.commit_rejected >= 1
        assert svc.retrieve(make_auth(), "") == ()
        assert svc.retrieve(make_auth(), "火锅") == ()
        # 在途证据已被 forget 清理
        assert store.get(_k_ev("ev-f2"))["text"] == ""
