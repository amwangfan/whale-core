"""owner_fact_v1 领域测试：InMemory / SQLite 对等。

覆盖：受理（敏感/容量/角色/时间/授权）、commit 裁决（generation/lease/授权
现状/证据墓碑/幂等）、Astra 红队 4 例误 accepted、更正（明确同谓词冲突 /
歧义 pending）、遗忘 fence（墓碑恒赢、关联证据返回、在途取消、迟到拒绝）、
检索隔离与读侧加固。全部临时库/合成数据。
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from whale_core.memory.owner_facts import (
    FactKind,
    FactRecord,
    FactStatus,
    MemoryAuthorization,
    ObserveStatus,
    OwnerFactService,
    UserEvidence,
    _k_ev,
    _k_ev_tomb,
    _k_fact,
    _k_idx,
    _k_job,
    is_tombstone,
)
from whale_core.memory.owner_fact_extraction import FactCandidate, validate_candidate
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore

NOW = datetime(2026, 11, 12, 6, 0, 0, tzinfo=timezone.utc)
OWNER = "owner1"
SPACE = "spaceA"
PRINCIPAL = "paired:qq:123"


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryStore()
    else:
        s = SqliteMemoryStore(tmp_path / "owner_facts.sqlite3")
        yield s


class FakeAuthority:
    """现查式授权替身：allow=False 或版本变化均可模拟。"""

    def __init__(self, version: str = "v1"):
        self.allow = True
        self.version = version

    def authorize_now(self, owner_id: str, space_id: str):
        if not self.allow or owner_id != OWNER:
            return None
        return MemoryAuthorization(
            owner_id=owner_id,
            space_id=space_id,
            principal_id=PRINCIPAL,
            policy_version="p1",
            authority_version=self.version,
            readable_space_ids=(space_id,),
        )

    def authority_version(self, owner_id: str, space_id: str) -> str:
        return self.version


def make_auth(space_id: str = SPACE, version: str = "v1") -> MemoryAuthorization:
    return MemoryAuthorization(
        owner_id=OWNER,
        space_id=space_id,
        principal_id=PRINCIPAL,
        policy_version="p1",
        authority_version=version,
        readable_space_ids=(space_id,),
    )


def make_evidence(text: str, eid: str = "ev-1", space_id: str = SPACE, **kw) -> UserEvidence:
    return UserEvidence(
        evidence_id=eid,
        owner_id=kw.get("owner_id", OWNER),
        principal_id=kw.get("principal_id", PRINCIPAL),
        conversation_id=kw.get("conversation_id", "private:123"),
        channel=kw.get("channel", "qq"),
        space_id=space_id,
        text=text,
        received_at=kw.get("received_at", NOW),
        timezone=kw.get("timezone", "Asia/Shanghai"),
        source_message_id=kw.get("source_message_id"),
    )


def make_candidate(evidence: UserEvidence, quote: str, **kw) -> FactCandidate:
    start = evidence.text.index(quote)
    return FactCandidate(
        kind=kw.get("kind", FactKind.FACT),
        summary=quote,
        source_evidence_id=evidence.evidence_id,
        quote=quote,
        span_start=start,
        span_end=start + len(quote),
        predicate=kw.get("predicate"),
        value=kw.get("value"),
        valid_from=kw.get("valid_from"),
        valid_to=kw.get("valid_to"),
        correction_target=kw.get("correction_target"),
    )


def make_service(store, authority=None, **kw) -> OwnerFactService:
    return OwnerFactService(store, authority or FakeAuthority(), now=lambda: NOW, **kw)


def lease_first_job(store, worker_id: str = "w-test", snapshot=(), token: str = "tok-1"):
    """白盒：把队列中第一个 job 置为 leased（模拟 worker 领取）。"""

    def _fn(tx):
        ids = tx.get("ofq:pending") or []
        assert ids, "no pending job"
        jid = str(ids[0])
        job = dict(tx.get(_k_job(jid)))
        job["status"] = "leased"
        job["lease_owner"] = worker_id
        job["lease_token"] = token
        job["lease_expires_at"] = (NOW + timedelta(seconds=30)).isoformat()
        job["existing_snapshot"] = [
            {"fact_id": f.fact_id, "revision": f.revision} for f in snapshot
        ]
        tx.put(_k_job(jid), job)
        return job

    return store.transact(_fn)


def observe_and_lease(store, service, evidence, auth, snapshot=()):
    result = service.observe(evidence, auth)
    assert result.status == ObserveStatus.QUEUED, result
    return lease_first_job(store, snapshot=snapshot)


def commit_first_job(store, service, job, candidates, worker_id: str = "w-test"):
    return service.commit(
        str(job["job_id"]),
        tuple(candidates),
        int(job["generation"]),
        str(job["authority_version"]),
        worker_id=worker_id,
        lease_token=str(job.get("lease_token") or ""),
    )


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------


class TestObserve:
    def test_queued_and_duplicate(self, store):
        svc = make_service(store)
        r = svc.observe(make_evidence("我养的猫叫年糕"), make_auth())
        assert r.status == ObserveStatus.QUEUED and r.reason_code == ""
        r2 = svc.observe(make_evidence("我养的猫叫年糕"), make_auth())
        assert r2.status == ObserveStatus.DUPLICATE
        # 同 ID 异 text：first write wins，不覆盖
        r3 = svc.observe(make_evidence("完全不同的正文"), make_auth())
        assert r3.status == ObserveStatus.DUPLICATE
        assert svc.pending_count() == 1

    def test_rejects(self, store):
        svc = make_service(store)
        auth = make_auth()
        bad_role = UserEvidence(
            evidence_id="e-role", owner_id=OWNER, principal_id=PRINCIPAL,
            conversation_id="c", channel="qq", space_id=SPACE, text="hi",
            received_at=NOW, role="assistant")
        assert svc.observe(bad_role, auth).status == ObserveStatus.SKIPPED_POLICY
        assert svc.observe(
            make_evidence("", eid="e-empty"), auth
        ).status == ObserveStatus.SKIPPED_POLICY
        assert svc.observe(
            make_evidence("x" * 4001, eid="e-long"), auth
        ).status == ObserveStatus.SKIPPED_POLICY
        naive = make_evidence("我养猫", eid="e-naive")
        naive = UserEvidence(**{**naive.__dict__, "received_at": datetime(2026, 11, 12)})
        assert svc.observe(naive, auth).status == ObserveStatus.SKIPPED_POLICY
        assert svc.observe(
            make_evidence("我电话13812345678", eid="e-sensitive"), auth
        ).status == ObserveStatus.SKIPPED_SENSITIVE
        # 凭据启发式（全部合成假字符串）：
        assert svc.observe(
            make_evidence("我的 API Key 是 sk-testnotreal1234567890", eid="e-cred1"), auth
        ).status == ObserveStatus.SKIPPED_SENSITIVE
        assert svc.observe(
            make_evidence("我的访问令牌是 Bearer faketoken123", eid="e-cred2"), auth
        ).status == ObserveStatus.SKIPPED_SENSITIVE
        assert svc.observe(
            make_evidence("这是我的私钥", eid="e-cred3"), auth
        ).status == ObserveStatus.SKIPPED_SENSITIVE
        assert svc.observe(
            make_evidence("token: ghp_faketesttoken123", eid="e-cred4"), auth
        ).status == ObserveStatus.SKIPPED_SENSITIVE
        # 凭据证据不持久化
        assert store.get(_k_ev("e-cred1")) is None
        # 正常自述不误伤
        assert svc.observe(
            make_evidence("我养的猫叫年糕", eid="e-normal"), auth
        ).status == ObserveStatus.QUEUED
        # 空间不匹配
        assert svc.observe(
            make_evidence("我养猫", eid="e-space"), make_auth(space_id="other")
        ).status == ObserveStatus.SKIPPED_POLICY
        # 只有 e-normal 入队
        assert svc.pending_count() == 1

    def test_pause_blocks_observe(self, store):
        authority = FakeAuthority()
        svc = make_service(store, authority)
        authority.allow = False  # 暂停/解绑后
        r = svc.observe(make_evidence("我养猫"), make_auth())
        assert r.status == ObserveStatus.SKIPPED_POLICY and r.reason_code == "policy_rejected"
        assert store.get(_k_ev("ev-1")) is None  # 原文未保存

    def test_principal_unbound_but_other_source_active(self, store):
        """QQ 解绑但眼镜活跃：authorize_now 会返回眼镜 canonical principal，
        旧 QQ 证据不得受理/提交。"""

        class SwapAuthority(FakeAuthority):
            """宿主 any-active-source 行为：authorize_now 返回 glasses 身份。"""

            def authorize_now(self, owner_id, space_id):
                if not self.allow:
                    return None
                return MemoryAuthorization(
                    owner_id=owner_id, space_id=space_id,
                    principal_id="glasses:owner1",  # canonical 换成了眼镜
                    policy_version="p1", authority_version=self.version,
                    readable_space_ids=(space_id,),
                )

        svc = make_service(store, SwapAuthority())
        ev = make_evidence("我养的猫叫年糕")  # principal = QQ
        # 退化路径（无 authorize_principal_now）：principal 比对不一致 → 拒
        assert svc.observe(ev, make_auth()).status == ObserveStatus.SKIPPED_POLICY
        assert store.get(_k_ev(ev.evidence_id)) is None

    def test_authorize_principal_now_narrow_method(self, store):
        """宿主实现窄方法：按证据真实 principal 现查。"""

        class NarrowAuthority(FakeAuthority):
            def __init__(self):
                super().__init__()
                self.unbound_principals: set[str] = set()

            def authorize_now(self, owner_id, space_id):
                # any-active-source：QQ 解绑后仍返回 glasses 身份
                return MemoryAuthorization(
                    owner_id=owner_id, space_id=space_id,
                    principal_id="glasses:owner1",
                    policy_version="p1", authority_version=self.version,
                    readable_space_ids=(space_id,),
                )

            def authorize_principal_now(self, owner_id, space_id, principal_id):
                if owner_id != OWNER or principal_id in self.unbound_principals:
                    return None
                return MemoryAuthorization(
                    owner_id=owner_id, space_id=space_id, principal_id=principal_id,
                    policy_version="p1", authority_version=self.version,
                    readable_space_ids=(space_id,),
                )

        authority = NarrowAuthority()
        svc = make_service(store, authority)
        # QQ principal 仍绑定：可受理
        ev = make_evidence("我养的猫叫年糕", eid="ev-nb")
        assert svc.observe(ev, make_auth()).status == ObserveStatus.QUEUED
        # QQ 解绑（眼镜仍活跃）：旧 QQ 在途 job 提交被拒
        job = lease_first_job(store)
        authority.unbound_principals.add(PRINCIPAL)
        cand = make_candidate(ev, "我养的猫叫年糕")
        report = commit_first_job(store, svc, job, [cand])
        assert report.status == "rejected_policy"
        # 且此后该 principal 新证据不再受理
        ev2 = make_evidence("我喜欢吃辣", eid="ev-nb2")
        assert svc.observe(ev2, make_auth()).status == ObserveStatus.SKIPPED_POLICY

    def test_capacity(self, store):
        svc = make_service(store, queue_limit=2, per_owner_limit=5)
        for i in range(2):
            assert svc.observe(
                make_evidence(f"事实{i}", eid=f"e{i}"), make_auth()
            ).status == ObserveStatus.QUEUED
        r = svc.observe(make_evidence("溢出", eid="e2"), make_auth())
        assert r.status == ObserveStatus.SKIPPED_CAPACITY


# ---------------------------------------------------------------------------
# 校验（Astra 红队 4 例 + span/语境规则）
# ---------------------------------------------------------------------------


class TestValidation:
    def v(self, text, quote, **kw):
        ev = make_evidence(text)
        cand = make_candidate(ev, quote, **kw)
        return validate_candidate(cand, ev, kw.get("allowed", {}))

    def test_self_report_accepted(self):
        assert self.v("我养的猫叫年糕", "我养的猫叫年糕") == ("accepted", "")
        assert self.v("我不喜欢咖啡", "我不喜欢咖啡") == ("accepted", "")  # 否定保留在 quote 内

    def test_astra_case_no_owner_subject(self):
        decision, reason = self.v("她喜欢咖啡", "她喜欢咖啡")
        assert decision == "reject" and reason == "no_first_person"

    def test_astra_case_suffix_negation(self):
        decision, reason = self.v("我喜欢咖啡才怪", "我喜欢咖啡")
        assert decision == "reject" and reason == "context_truncation_suffix"

    def test_astra_case_uncertain(self):
        decision, reason = self.v("我似乎喜欢咖啡", "我似乎喜欢咖啡")
        assert decision == "pending" and reason == "uncertain"

    def test_astra_case_ungrounded_year(self):
        ev = make_evidence("我有3只猫和5本书")
        cand = make_candidate(
            ev, "我有3只猫和5本书", valid_from=datetime(2099, 3, 5, tzinfo=timezone.utc)
        )
        decision, reason = validate_candidate(cand, ev, {})
        assert decision == "reject" and reason == "ungrounded_time"

    def test_grounded_time_accepted(self):
        ev = make_evidence("我2026年3月5日入职了新公司")
        cand = make_candidate(
            ev, "我2026年3月5日入职了新公司",
            valid_from=datetime(2026, 3, 5, tzinfo=timezone.utc),
        )
        assert validate_candidate(cand, ev, {}) == ("accepted", "")

    def test_prefix_negation_truncation(self):
        decision, reason = self.v("我不喜欢咖啡", "喜欢咖啡")
        assert decision == "reject" and reason == "context_truncation_negation"

    def test_third_party_speech(self):
        decision, reason = self.v("朋友说我养猫", "我养猫")
        assert decision == "reject" and reason == "context_truncation_speech"

    def test_first_person_opener_accepted(self):
        # 真实入口 case："跟你说个事"是第一人称交流引导，不是第三方转述
        decision, reason = self.v("跟你说个事，我养的猫叫年糕", "我养的猫叫年糕")
        assert decision == "accepted", reason
        decision, reason = self.v("我想告诉你，我喜欢咖啡", "我喜欢咖啡")
        assert decision == "accepted", reason

    def test_third_party_speech_still_blocked(self):
        # 反例不放松：他/她/第三方 + 言说动词仍 reject/pending
        decision, reason = self.v("他说我养猫", "我养猫")
        assert decision == "reject" and reason == "context_truncation_speech"
        decision, reason = self.v("朋友告诉我她养了只猫", "她养了只猫")
        assert decision == "reject"  # 无第一人称 + 第三方转述前缀

    def test_question_rejected(self):
        decision, reason = self.v("我喜欢咖啡吗？", "我喜欢咖啡")
        assert decision == "reject" and reason == "question"

    def test_hypothetical_pending(self):
        decision, reason = self.v("如果我养猫就好了", "如果我养猫就好了")
        assert decision == "pending" and reason == "hypothetical"

    def test_summary_rewrite_rejected(self):
        ev = make_evidence("我养的猫叫年糕")
        cand = make_candidate(ev, "我养的猫叫年糕")
        cand = FactCandidate(**{**cand.__dict__, "summary": "用户养猫"})
        assert validate_candidate(cand, ev, {})[1] == "summary_rewrite"

    def test_value_must_be_in_quote(self):
        decision, reason = self.v(
            "我养的猫叫年糕", "我养的猫叫年糕", predicate="pet_name", value="团子"
        )
        assert decision == "reject" and reason == "value_not_grounded"

    def test_correction_target_must_be_in_snapshot(self):
        decision, reason = self.v(
            "我的猫叫团子", "我的猫叫团子",
            predicate="pet_name", value="团子", correction_target="not-shown-id",
        )
        assert decision == "reject" and reason == "bad_correction_target"

    # -- Astra 语义探针 -------------------------------------------------

    def test_probe_third_party_quote_rejected(self):
        # quote 级主体：句子含"我"≠quote 是 owner 自述
        decision, reason = self.v("我朋友喜欢咖啡", "朋友喜欢咖啡")
        assert decision == "reject" and reason == "no_first_person"
        decision, reason = self.v("我问过她喜欢咖啡", "她喜欢咖啡")
        assert decision == "reject" and reason in (
            "no_first_person", "context_truncation_speech")

    def test_probe_suffix_disclaimer(self):
        decision, reason = self.v("我喜欢咖啡，这不是真的", "我喜欢咖啡")
        assert decision == "reject" and reason == "context_truncation_suffix"

    def test_probe_cross_line_hypothetical(self):
        decision, reason = self.v("假设如下：\n我住在北京", "我住在北京")
        assert decision == "pending" and reason == "hypothetical"

    def test_correction_exception_allows_omitted_subject(self):
        # "名字说错了，它叫团子"：省略主体的定向更正在快照目标+更正语境+value
        # 有 quote 支撑时允许 accepted
        decision, reason = self.v(
            "名字说错了，它叫团子", "它叫团子",
            predicate="pet_name", value="团子",
            correction_target="f1", allowed={"f1": 1},
        )
        assert decision == "accepted", reason

    def test_correction_exception_needs_context(self):
        # 有快照目标但无更正语境标记：省略主体仍拒
        decision, reason = self.v(
            "它叫团子", "它叫团子",
            predicate="pet_name", value="团子",
            correction_target="f1", allowed={"f1": 1},
        )
        assert decision == "reject" and reason == "no_first_person"

    def test_correction_exception_not_for_third_party_override(self):
        # "他说它叫团子"：无更正语境，第三方不可覆盖
        decision, reason = self.v(
            "他说它叫团子", "它叫团子",
            predicate="pet_name", value="团子",
            correction_target="f1", allowed={"f1": 1},
        )
        assert decision == "reject" and reason == "context_truncation_speech"

    def test_correction_exception_negation_phrasing(self):
        # "不是年糕，是团子"：更正措辞中的"不"不触发截断拒绝
        decision, reason = self.v(
            "不是年糕，是团子", "是团子",
            predicate="pet_name", value="团子",
            correction_target="f1", allowed={"f1": 1},
        )
        assert decision == "accepted", reason

    def test_correction_exception_not_for_third_party_message(self):
        # "我朋友更正了：它叫团子"：整条消息是第三方更正 → 例外失效，拒绝
        decision, reason = self.v(
            "我朋友更正了：它叫团子", "它叫团子",
            predicate="pet_name", value="团子",
            correction_target="f1", allowed={"f1": 1},
        )
        assert decision == "reject" and reason in (
            "no_first_person", "context_truncation_speech")

    def test_message_level_revocation(self):
        # "我喜欢咖啡。其实前面那句不是真的。"：span 之外有撤销语 → pending
        decision, reason = self.v("我喜欢咖啡。其实前面那句不是真的。", "我喜欢咖啡")
        assert decision == "pending" and reason == "message_revocation"

    def test_mixed_subject_no_owner_predicate(self):
        # "我朋友喜欢咖啡"：可存 pending 原文，但不得携带 owner 谓词
        decision, reason = self.v("我朋友喜欢咖啡", "我朋友喜欢咖啡")
        assert decision == "pending" and reason == "mixed_subject"
        decision, reason = self.v(
            "我朋友喜欢咖啡", "我朋友喜欢咖啡",
            predicate="likes", value="咖啡",
        )
        assert decision == "reject" and reason == "predicate_subject_mismatch"

    def test_real_model_n05_sarcasm_full_quote(self):
        # 真实评测 N05：模型选整句含"才怪"，suffix-only 检查漏网 → 现在 pending
        decision, reason = self.v("我可太喜欢加班了，才怪。", "我可太喜欢加班了，才怪")
        assert decision == "pending" and reason == "sarcasm_in_quote"
        # 反例：正常否定不一刀切
        assert self.v("我不喜欢咖啡", "我不喜欢咖啡") == ("accepted", "")

    def test_real_model_n09_relative_plan_pending(self):
        # 真实评测 N09：无期限相对计划不当永久现状
        decision, reason = self.v("我下周三去看牙医", "我下周三去看牙医")
        assert decision == "pending" and reason == "relative_time_unresolved"


class TestParseLayerValueGrounding:
    """真实评测 P02/P05：模型归一化 value 非 quote 子串时，解析层丢弃
    predicate/value 但保留精确 quote 事实；服务直接候选仍严格拒绝。"""

    class _ScriptModel:
        def __init__(self, out):
            self.out = out

        def complete(self, prompt, *, timeout):
            return self.out

    def test_parse_drops_ungrounded_value_keeps_fact(self):
        from whale_core.memory.owner_fact_extraction import OwnerFactExtractor

        text = "我的生日是六月十八"
        model = self._ScriptModel(
            '{"candidates": [{"kind": "fact", "quote": "我的生日是六月十八",'
            ' "predicate": "birthday", "value": "1994-06-18"}]}'
        )
        extractor = OwnerFactExtractor(model)
        ev = make_evidence(text, eid="ev-p02")
        cands = extractor.extract(ev, ())
        assert len(cands) == 1
        assert cands[0].quote == text and cands[0].predicate is None and cands[0].value is None
        # 丢弃谓词后的精确 quote 事实正常 accepted
        assert validate_candidate(cands[0], ev, {}) == ("accepted", "")

    def test_service_direct_candidate_still_strict(self):
        # 服务层直接构造的候选（非解析层）：未支撑 value 仍严格拒绝
        ev = make_evidence("我的生日是六月十八", eid="ev-p02b")
        cand = make_candidate(ev, "我的生日是六月十八", predicate="birthday", value="1994-06-18")
        decision, reason = validate_candidate(cand, ev, {})
        assert decision == "reject" and reason == "value_not_grounded"


# ---------------------------------------------------------------------------
# commit 生命周期
# ---------------------------------------------------------------------------


class TestCommit:
    def test_commit_accepted_and_evidence_scrubbed(self, store):
        svc = make_service(store)
        ev = make_evidence("我养的猫叫年糕，很可爱")
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(ev, "我养的猫叫年糕", predicate="pet_name", value="年糕")
        report = commit_first_job(store, svc, job, [cand])
        assert report.status == "committed" and len(report.accepted_fact_ids) == 1
        # 证据原文已清，保留最小 quote
        stored_ev = store.get(_k_ev(ev.evidence_id))
        assert stored_ev["text"] == "" and stored_ev["status"] == "processed"
        assert stored_ev["retained_quotes"][0]["quote"] == "我养的猫叫年糕"
        # 检索命中
        hits = svc.retrieve(make_auth(), "我的猫叫什么")
        assert len(hits) == 1 and hits[0].summary == "我养的猫叫年糕"
        assert hits[0].predicate == "pet_name" and hits[0].value == "年糕"

    def test_stale_generation_rejected(self, store):
        svc = make_service(store)
        ev1 = make_evidence("我养的猫叫年糕", eid="ev-a")
        job1 = observe_and_lease(store, svc, ev1, make_auth())
        cand1 = make_candidate(ev1, "我养的猫叫年糕", predicate="pet_name", value="年糕")
        report1 = commit_first_job(store, svc, job1, [cand1])
        fact_id = report1.accepted_fact_ids[0]

        # 第二个证据在途（generation 0），随后 forget 使 generation 升 1
        ev2 = make_evidence("我今天吃了火锅", eid="ev-b")
        job2 = observe_and_lease(store, svc, ev2, make_auth())
        report_forget = svc.forget([fact_id], make_auth(), {fact_id: 1})
        assert report_forget.ok
        assert svc.current_generation(OWNER, SPACE) == 1
        # 迟到提交必须拒绝且不复活
        cand2 = make_candidate(ev2, "我今天吃了火锅")
        report2 = commit_first_job(store, svc, job2, [cand2])
        assert report2.status == "rejected_stale_generation"
        assert svc.retrieve(make_auth(), "火锅") == ()
        # 在途证据原文已清
        assert store.get(_k_ev("ev-b"))["text"] == ""

    def test_pause_between_enqueue_and_commit(self, store):
        authority = FakeAuthority()
        svc = make_service(store, authority)
        ev = make_evidence("我养的猫叫年糕")
        job = observe_and_lease(store, svc, ev, make_auth())
        authority.allow = False  # 入队后提交前暂停
        cand = make_candidate(ev, "我养的猫叫年糕")
        report = commit_first_job(store, svc, job, [cand])
        assert report.status == "rejected_policy"
        assert store.get(_k_idx(OWNER, SPACE)) is None or store.get(_k_idx(OWNER, SPACE)) == []
        assert store.get(_k_ev(ev.evidence_id))["text"] == ""

    def test_authority_version_change_rejected(self, store):
        authority = FakeAuthority()
        svc = make_service(store, authority)
        ev = make_evidence("我养的猫叫年糕")
        job = observe_and_lease(store, svc, ev, make_auth())
        authority.version = "v2"  # 授权现状变化
        cand = make_candidate(ev, "我养的猫叫年糕")
        report = commit_first_job(store, svc, job, [cand])
        assert report.status == "rejected_policy"

    def test_duplicate_candidate_idempotent(self, store):
        svc = make_service(store)
        ev = make_evidence("我养的猫叫年糕")
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(ev, "我养的猫叫年糕")
        # 同一 commit 内重复候选：第二个按幂等键拒绝
        r1 = commit_first_job(store, svc, job, [cand, cand])
        assert len(r1.accepted_fact_ids) == 1
        assert r1.rejected and r1.rejected[0][1] == "duplicate"
        # 同 evidence 不可再入队
        assert svc.observe(make_evidence("我养的猫叫年糕"), make_auth()).status == ObserveStatus.DUPLICATE

    def test_multi_candidate_one_message(self, store):
        svc = make_service(store)
        ev = make_evidence("我养的猫叫年糕。我喜欢吃辣。")
        job = observe_and_lease(store, svc, ev, make_auth())
        cands = [
            make_candidate(ev, "我养的猫叫年糕", predicate="pet_name", value="年糕"),
            make_candidate(ev, "我喜欢吃辣", kind=FactKind.PREFERENCE),
        ]
        report = commit_first_job(store, svc, job, cands)
        assert report.status == "committed" and len(report.accepted_fact_ids) == 2

    def test_lease_owner_mismatch_rejected(self, store):
        svc = make_service(store)
        ev = make_evidence("我养的猫叫年糕")
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(ev, "我养的猫叫年糕")
        report = commit_first_job(store, svc, job, [cand], worker_id="other-worker")
        assert report.status == "rejected_lease"


# ---------------------------------------------------------------------------
# 更正
# ---------------------------------------------------------------------------


class TestCorrection:
    def _commit_pet(self, store, svc, name="年糕", eid="ev-pet"):
        ev = make_evidence(f"我养的猫叫{name}", eid=eid)
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(ev, f"我养的猫叫{name}", predicate="pet_name", value=name)
        report = commit_first_job(store, svc, job, [cand])
        return report.accepted_fact_ids[0], ev

    def test_explicit_conflict_supersedes(self, store):
        svc = make_service(store)
        old_id, _ = self._commit_pet(store, svc)
        old_fact = svc.get_fact(old_id)
        assert old_fact is not None
        # 新一轮：快照包含旧事实，模型给同谓词不同值 + correction_target
        # 注：quote 必须含第一人称，故用完整自述句
        ev2 = make_evidence("我说错了，我养的猫叫团子", eid="ev-fix")
        job2 = observe_and_lease(store, svc, ev2, make_auth(), snapshot=[old_fact])
        cand2 = make_candidate(
            ev2, "我养的猫叫团子", predicate="pet_name", value="团子",
            correction_target=old_id,
        )
        report2 = commit_first_job(store, svc, job2, [cand2])
        assert report2.status == "committed"
        assert report2.superseded_fact_ids == (old_id,)
        old_after = svc.get_fact(old_id)
        assert old_after is not None and old_after.status == FactStatus.SUPERSEDED
        hits = svc.retrieve(make_auth(), "我的猫叫什么")
        assert len(hits) == 1 and "团子" in hits[0].summary
        assert hits[0].revision == 2 and hits[0].supersedes == old_id

    def test_correction_chain_anchor_recall(self, store):
        """省略主体的更正（"它叫团子"）借旧链 summary 做只读检索锚：
        问"我的猫名字"命中新版；旧版不返回；遗忘后锚断裂。"""
        svc = make_service(store)
        old_id, _ = self._commit_pet(store, svc)
        old_fact = svc.get_fact(old_id)
        ev2 = make_evidence("名字说错了，它叫团子", eid="ev-anchor")
        job2 = observe_and_lease(store, svc, ev2, make_auth(), snapshot=[old_fact])
        cand2 = make_candidate(
            ev2, "它叫团子", predicate="pet_name", value="团子",
            correction_target=old_id,
        )
        report2 = commit_first_job(store, svc, job2, [cand2])
        assert report2.status == "committed" and report2.superseded_fact_ids == (old_id,)
        new_id = report2.accepted_fact_ids[0]
        # probe 确认的 miss 查询现在经锚命中（不是靠"叫"字偶合）
        hits = svc.retrieve(make_auth(), "我的猫名字")
        assert len(hits) == 1 and hits[0].fact_id == new_id
        assert all(h.fact_id != old_id for h in hits)  # superseded 不返回
        # 遗忘新版：修订链整体墓碑，锚断裂，检索为空
        report3 = svc.forget([new_id], make_auth(), {new_id: 2})
        assert report3.ok
        assert svc.retrieve(make_auth(), "我的猫名字") == ()

    def test_multi_value_not_superseded_without_correction_context(self, store):
        """同谓词不同值 ≠ 明确矛盾：'我喜欢咖啡'后'我也喜欢茶'（模型误挂
        correction_target）不得失效旧事实，作为独立新事实共存。"""
        svc = make_service(store)
        ev1 = make_evidence("我喜欢咖啡", eid="ev-mv1")
        job1 = observe_and_lease(store, svc, ev1, make_auth())
        c1 = make_candidate(ev1, "我喜欢咖啡", kind=FactKind.PREFERENCE,
                            predicate="likes", value="咖啡")
        old_id = commit_first_job(store, svc, job1, [c1]).accepted_fact_ids[0]
        old = svc.get_fact(old_id)
        ev2 = make_evidence("我也喜欢茶", eid="ev-mv2")
        job2 = observe_and_lease(store, svc, ev2, make_auth(), snapshot=[old])
        c2 = make_candidate(ev2, "我也喜欢茶", kind=FactKind.PREFERENCE,
                            predicate="likes", value="茶", correction_target=old_id)
        r2 = commit_first_job(store, svc, job2, [c2])
        assert r2.status == "committed" and not r2.superseded_fact_ids
        assert r2.accepted_fact_ids  # 独立新事实，不是 pending
        assert len(svc.retrieve(make_auth(), "")) == 2
        # 旧事实仍 accepted
        assert svc.get_fact(old_id).status == FactStatus.ACCEPTED

    def test_different_pet_not_same_entity(self, store):
        """猫年糕 + 狗饼干：同谓词不同值无更正语境，不得当同一宠物覆盖。"""
        svc = make_service(store)
        old_id, _ = self._commit_pet(store, svc)
        old = svc.get_fact(old_id)
        ev2 = make_evidence("我养的狗叫饼干", eid="ev-dog")
        job2 = observe_and_lease(store, svc, ev2, make_auth(), snapshot=[old])
        c2 = make_candidate(ev2, "我养的狗叫饼干", predicate="pet_name",
                            value="饼干", correction_target=old_id)
        r2 = commit_first_job(store, svc, job2, [c2])
        assert r2.status == "committed" and not r2.superseded_fact_ids
        assert len(svc.retrieve(make_auth(), "")) == 2

    def test_ambiguous_correction_goes_pending(self, store):
        svc = make_service(store)
        old_id, _ = self._commit_pet(store, svc)
        ev2 = make_evidence("我最近想叫它团子", eid="ev-amb")
        old_fact = svc.get_fact(old_id)
        job2 = observe_and_lease(store, svc, ev2, make_auth(), snapshot=[old_fact])
        # 无 predicate：无法证明同谓词明确冲突 → pending，不覆盖
        cand2 = make_candidate(ev2, "我最近想叫它团子", correction_target=old_id)
        report2 = commit_first_job(store, svc, job2, [cand2])
        assert report2.status == "committed"
        assert report2.pending_fact_ids and not report2.superseded_fact_ids
        hits = svc.retrieve(make_auth(), "猫叫什么")
        assert any("年糕" in h.summary for h in hits)  # 旧现状仍在
        assert not any("团子" in h.summary for h in hits)  # pending 不召回

    def test_correct_command_path(self, store):
        svc = make_service(store)
        old_id, _ = self._commit_pet(store, svc)
        ev_fix = make_evidence("更正：我养的猫叫团子", eid="ev-cmd")
        repl = make_candidate(ev_fix, "我养的猫叫团子", predicate="pet_name", value="团子")
        report = svc.correct(old_id, 1, ev_fix, repl, make_auth())
        assert report.ok and old_id in report.affected_fact_ids
        hits = svc.retrieve(make_auth(), "猫叫什么")
        assert len(hits) == 1 and "团子" in hits[0].summary
        # 已被取代的旧事实不可再更正
        report2 = svc.correct(old_id, 1, ev_fix, repl, make_auth())
        assert not report2.ok and report2.reason_code == "not_accepted"

    def test_correct_cross_space_forbidden(self, store):
        svc = make_service(store)
        old_id, _ = self._commit_pet(store, svc)
        ev_fix = make_evidence("我养的猫叫团子", eid="ev-x")
        repl = make_candidate(ev_fix, "我养的猫叫团子", predicate="pet_name", value="团子")
        report = svc.correct(old_id, 1, ev_fix, repl, make_auth(space_id="spaceB"))
        assert not report.ok and report.reason_code in ("forbidden", "policy_rejected")


# ---------------------------------------------------------------------------
# 遗忘 fence
# ---------------------------------------------------------------------------


def _commit_fact(store, svc, text, eid, **kw):
    """观察→领取→提交一条事实，返回 (fact_id, evidence)。"""
    ev = make_evidence(text, eid=eid)
    job = observe_and_lease(store, svc, ev, make_auth())
    cand = make_candidate(ev, kw_quote(text), **kw)
    report = commit_first_job(store, svc, job, [cand])
    assert report.status == "committed"
    return report.accepted_fact_ids[0], ev


class TestForget:
    def _commit(self, store, svc, text, eid, **kw):
        return _commit_fact(store, svc, text, eid, **kw)

    def test_forget_tombstone_and_fence(self, store):
        svc = make_service(store)
        fid, ev = self._commit(store, svc, "我养的猫叫年糕", "ev-f1",
                               predicate="pet_name", value="年糕")
        report = svc.forget([fid], make_auth(), {fid: 1})
        assert report.ok and fid in report.affected_fact_ids
        # 墓碑：无 summary/quote/value
        rec = store.get_record(_k_fact(fid))
        assert is_tombstone(rec)
        assert "summary" not in rec.content and "source_refs" not in rec.content
        # 证据墓碑 + 返回受影响证据供装配侧抑制
        assert store.get(_k_ev_tomb(ev.evidence_id)) is not None
        assert report.affected_evidence[0].evidence_id == ev.evidence_id
        assert report.affected_evidence[0].conversation_id == "private:123"
        # 检索为空（读侧 fence）
        assert svc.retrieve(make_auth(), "猫") == ()
        # 同证据再入队被拒
        r = svc.observe(make_evidence("我养的猫叫年糕", eid=ev.evidence_id), make_auth())
        assert r.status == ObserveStatus.SKIPPED_POLICY
        # generation 已递增
        assert svc.current_generation(OWNER, SPACE) == 1

    def test_forget_revision_chain(self, store):
        svc = make_service(store)
        fid1, _ = self._commit(store, svc, "我养的猫叫年糕", "ev-c1",
                               predicate="pet_name", value="年糕")
        # 更正出新版
        ev2 = make_evidence("我养的猫叫团子", eid="ev-c2")
        repl = make_candidate(ev2, "我养的猫叫团子", predicate="pet_name", value="团子")
        assert svc.correct(fid1, 1, ev2, repl, make_auth()).ok
        hits = svc.retrieve(make_auth(), "猫")
        fid2 = hits[0].fact_id
        # 遗忘新版 → 修订链整体墓碑
        report = svc.forget([fid2], make_auth(), {fid2: 2})
        assert report.ok
        assert set(report.affected_fact_ids) == {fid1, fid2}
        assert svc.retrieve(make_auth(), "猫") == ()

    def test_forget_canceled_job_journaled(self, store):
        svc = make_service(store)
        fid, _ = self._commit(store, svc, "我养的猫叫年糕", "ev-jc1",
                              predicate="pet_name", value="年糕")
        ev2 = make_evidence("我今天吃了火锅", eid="ev-jc2")
        job2 = observe_and_lease(store, svc, ev2, make_auth())
        svc.forget([fid], make_auth(), {fid: 1})
        journal = store.get("ofq:terminal") or []
        assert str(job2["job_id"]) in journal  # cancelled 也入终态 journal


class TestTerminalJournal:
    def test_terminal_jobs_bounded(self, store):
        svc = make_service(store)
        total = 260
        for i in range(total):
            ev = make_evidence(f"我记住第{i}件事", eid=f"ev-j{i}")
            job = observe_and_lease(store, svc, ev, make_auth())
            cand = make_candidate(ev, f"我记住第{i}件事")
            report = commit_first_job(store, svc, job, [cand])
            assert report.status == "committed"
        job_keys = [k for k in store.keys() if k.startswith("ofjob:")]
        assert len(job_keys) <= 256
        journal = store.get("ofq:terminal") or []
        assert len(journal) == 256
        # 证据水位（无正文）永久保留，防重放
        assert store.get(_k_ev("ev-j0")) is not None
        assert store.get(_k_ev("ev-j0"))["text"] == ""
        assert (store.get("ofq:pending") or []) == []

    def test_forget_revision_mismatch_atomic(self, store):
        svc = make_service(store)
        fid1, _ = _commit_fact(store, svc, "我养的猫叫年糕", "ev-m1",
                               predicate="pet_name", value="年糕")
        fid2, _ = _commit_fact(store, svc, "我喜欢吃辣", "ev-m2")
        report = svc.forget([fid1, fid2], make_auth(), {fid1: 99, fid2: 1})
        assert not report.ok and report.reason_code == "revision_mismatch"
        # 原子失败：两条都还在
        assert len(svc.retrieve(make_auth(), "")) == 2

    def test_late_evidence_new_id_allowed_after_forget(self, store):
        # 用户明确重新告知 = 新证据，可以重新记录（不是从旧 history 恢复）
        svc = make_service(store)
        fid, _ = _commit_fact(store, svc, "我养的猫叫年糕", "ev-r1",
                              predicate="pet_name", value="年糕")
        svc.forget([fid], make_auth(), {fid: 1})
        fid2, _ = _commit_fact(store, svc, "我养的猫叫年糕", "ev-r2",
                               predicate="pet_name", value="年糕")
        hits = svc.retrieve(make_auth(), "猫")
        assert len(hits) == 1 and hits[0].fact_id == fid2


def kw_quote(text: str) -> str:
    return text


# ---------------------------------------------------------------------------
# 检索读侧加固
# ---------------------------------------------------------------------------


class TestRetrieve:
    def _commit(self, store, svc, text, eid, **kw):
        ev = make_evidence(text, eid=eid)
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(ev, text, **kw)
        report = commit_first_job(store, svc, job, [cand])
        return report.accepted_fact_ids[0]

    def test_pending_and_uncertain_not_recalled(self, store):
        svc = make_service(store)
        ev = make_evidence("我似乎喜欢咖啡", eid="ev-u")
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(ev, "我似乎喜欢咖啡")
        report = commit_first_job(store, svc, job, [cand])
        assert report.pending_fact_ids and not report.accepted_fact_ids
        assert svc.retrieve(make_auth(), "咖啡") == ()

    def test_unrelated_query_no_recall(self, store):
        svc = make_service(store)
        self._commit(store, svc, "我养的猫叫年糕", "ev-q1")
        assert svc.retrieve(make_auth(), "今天天气怎么样") == ()

    def test_empty_query_returns_recent_for_management(self, store):
        svc = make_service(store)
        self._commit(store, svc, "我养的猫叫年糕", "ev-e1")
        hits = svc.retrieve(make_auth(), "")
        assert len(hits) == 1  # 管理视图：最近 accepted，不声称相关

    def test_future_valid_from_not_current(self, store):
        svc = make_service(store)
        # 注：避开现有敏感词表中的"住在"（contains_sensitive 复用现有策略）
        ev = make_evidence("我2027年1月1日入职杭州分部", eid="ev-fut")
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(
            ev, ev.text, valid_from=datetime(2027, 1, 1, tzinfo=timezone.utc)
        )
        report = commit_first_job(store, svc, job, [cand])
        assert report.accepted_fact_ids
        assert svc.retrieve(make_auth(), "入职") == ()  # 未来生效 ≠ 现状

    def test_past_valid_to_expired(self, store):
        svc = make_service(store)
        ev = make_evidence("我2025年1月5日离开了北京", eid="ev-past")
        job = observe_and_lease(store, svc, ev, make_auth())
        cand = make_candidate(
            ev, ev.text, valid_to=datetime(2025, 1, 5, tzinfo=timezone.utc)
        )
        commit_first_job(store, svc, job, [cand])
        assert svc.retrieve(make_auth(), "北京") == ()

    def test_cross_space_isolation(self, store):
        svc = make_service(store)
        self._commit(store, svc, "我养的猫叫年糕", "ev-iso")
        # 其他 space 的授权看不到
        assert svc.retrieve(make_auth(space_id="spaceB"), "猫") == ()
        # 授权消失（pause/解绑）→ 读侧 fail-closed
        authority = FakeAuthority()
        svc2 = make_service(store, authority)
        ev = make_evidence("我养的猫叫年糕", eid="ev-iso2")
        job = observe_and_lease(store, svc2, ev, make_auth())
        commit_first_job(store, svc2, job, [make_candidate(ev, ev.text)])
        authority.allow = False
        assert svc2.retrieve(make_auth(), "猫") == ()

    def test_corrupted_index_no_cross_space(self, store):
        svc = make_service(store)
        fid = self._commit(store, svc, "我养的猫叫年糕", "ev-cidx")
        # 损坏：把 fact_id 塞进别人 space 的索引
        def _fn(tx):
            idx = list(tx.get(_k_idx(OWNER, "spaceEvil")) or [])
            idx.append(fid)
            tx.put(_k_idx(OWNER, "spaceEvil"), idx)

        store.transact(_fn)
        evil_auth = MemoryAuthorization(
            owner_id=OWNER, space_id="spaceEvil", principal_id=PRINCIPAL,
            policy_version="p1", authority_version="v1",
            readable_space_ids=("spaceEvil",),
        )
        assert svc.retrieve(evil_auth, "猫") == ()

    def test_empty_source_refs_not_recalled(self, store):
        svc = make_service(store)
        fid = self._commit(store, svc, "我养的猫叫年糕", "ev-esr")
        # 白盒损坏：清掉 source_refs
        from whale_core.memory.owner_facts import fact_from_record, fact_to_record
        rec = store.get_record(_k_fact(fid))
        fact = fact_from_record(rec)
        store.put_record(fact_to_record(FactRecord(**{**fact.__dict__, "source_refs": ()})))
        assert svc.retrieve(make_auth(), "猫") == ()

    def test_retained_quote_mismatch_not_recalled(self, store):
        svc = make_service(store)
        fid = self._commit(store, svc, "我养的猫叫年糕", "ev-rqm")
        # 白盒损坏：retained quote 的 quote 与 summary 不对应
        def _fn(tx):
            ev = dict(tx.get(_k_ev("ev-rqm")))
            ev["retained_quotes"] = [
                {"span_start": 0, "span_end": 7, "quote": "被篡改的引文"}
            ]
            tx.put(_k_ev("ev-rqm"), ev)

        store.transact(_fn)
        assert svc.retrieve(make_auth(), "猫") == ()
