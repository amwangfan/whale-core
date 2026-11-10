"""2026-11-10 长文/调研管线：heavy 作答 + 超长结局落 1 天期文件（owner 决策）。"""
from __future__ import annotations

from whale_core.core.companion import WhaleCompanion
from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime import ConversationOrchestrator, InputEnvelope
from whale_core.core.runtime.turn_orchestration import _Decision
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.artifacts import ArtifactStore


class _B:
    def __init__(self, text):
        self._text = text

    def reply(self, text, **kw):
        return self._text


def _orch(tmp_path, heavy_text, *, with_heavy=True):
    store = InMemoryStore()
    from whale_core.plugins.artifacts import ArtifactStore
    art = ArtifactStore(tmp_path, store)
    kw = {}
    if with_heavy:
        kw["heavy_companion"] = WhaleCompanion(store, _B(heavy_text))
    orch = ConversationOrchestrator(store, WhaleCompanion(store, _B("快答")),
                                    artifact_store=art, **kw)
    return orch, art


class _HeavyPlan:
    kind = "chat"
    heavy = True
    task_id = ""
    intent_id = "h1"


def _env(text="给我写2000字卢唐同人文"):
    return InputEnvelope(conversation_id="c1", text=text, source="test")


def test_heavy_long_reply_becomes_file(tmp_path):
    orch, art = _orch(tmp_path, "《文》\n\n" + "长" * 1200)
    result = orch._finish_decision(_env(), _Decision(route="chat", plan=_HeavyPlan()))
    assert result.file_ids, "超长 heavy 结局应落文件"
    assert "附件" in result.reply or "文件" in result.reply  # 正文只留引子+说明
    item = art.get(result.file_ids[0])
    assert item.expires_at - item.created_at == 86400  # 1 天期
    blob = art.read(result.file_ids[0])
    data = blob[1] if isinstance(blob, tuple) else blob
    assert data.decode().startswith("《文》")


def test_heavy_short_reply_stays_text(tmp_path):
    orch, _ = _orch(tmp_path, "短答")
    result = orch._finish_decision(_env("写一句"), _Decision(route="chat", plan=_HeavyPlan()))
    assert not result.file_ids
    assert result.reply == "短答"


def test_light_plan_never_files(tmp_path):
    orch, _ = _orch(tmp_path, "长" * 1200)

    class _Light:
        kind = "chat"; heavy = False; task_id = ""; intent_id = "l1"
    result = orch._finish_decision(_env("聊聊"), _Decision(route="chat", plan=_Light()))
    assert not result.file_ids  # 非 heavy 再长也不落文件


def test_heavy_task_outcome_becomes_file(tmp_path):
    """任务结局组装（调研类 heavy task）：长答复同样落文件。"""
    orch, art = _orch(tmp_path, "调研结论\n\n" + "详" * 1200)
    plan = IntentPlan(goal="梳理诗歌年份", kind="task", heavy=True)
    journal = {"authz": {}}
    result = orch._assemble_turn_reply(
        "t1", plan, journal, "梳理下诗的年份", "c1", "answered",
        ["tool:search_web:ok:..."], ["调研数据若干"], {"phase": "succeeded"})
    assert result.file_ids
    assert result.status == "answered"
    blob = art.read(result.file_ids[0])
    data = blob[1] if isinstance(blob, tuple) else blob
    assert "调研结论" in data.decode()


class _SpyTools:
    def __init__(self):
        self.ran = []

    def for_capability(self, cap):
        return []

    def specs(self):
        return []

    def get(self, name):
        return None


def _plan(auth_level="", heavy=False):
    return IntentPlan(goal="调研", kind="task", heavy=heavy,
                      auth_level=auth_level, capabilities=())


def test_owner_level_task_denied_for_stranger(tmp_path):
    """授权级任务（调研/落文件）陌生人越权 → 不执行工具，人格化如实交代。"""
    store = InMemoryStore()
    orch = ConversationOrchestrator(store, WhaleCompanion(store, _B("主人授权的活我不乱接哦")))
    result = orch._start_plan(_plan(auth_level="owner"), "group:1124744261",
                              "梳理下诗的年份", source="qq:stranger")
    assert result.receipts == ("authz:owner-only",)
    assert "授权" in result.reply
    # 没有任务落库（未执行）
    assert not [k for k in store.keys() if str(k).startswith("task:")]


def test_owner_level_task_allowed_for_owner():
    store = InMemoryStore()
    orch = ConversationOrchestrator(store, WhaleCompanion(store, _B("好")))
    # owner_id 非空（主人）→ 放行（任务无工具可跑，会直接失败，但证明过了闸）
    result = orch._start_plan(_plan(auth_level="owner"), "private:u_x",
                              "梳理下诗的年份", source="qq:owner", owner_id="owner")
    assert result.receipts != ("authz:owner-only",)


class _ReviewIntent:
    """带审核脑的意图模型桩：review=控制审核结论。"""

    def __init__(self, answer):
        self._answer = answer

    @property
    def model(self):
        return self

    def reply(self, prompt, **kw):
        return self._answer


def test_authorized_group_member_needs_review_pass():
    """授权群非主人：授权级任务需先经意图模型审核——OK 才执行。"""
    store = InMemoryStore()
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _B("好")),
        intent_model=_ReviewIntent("OK"),
        task_auth_gate=lambda cid, oid: bool(oid) or cid == "group:1124744261")
    result = orch._start_plan(_plan(auth_level="owner"), "group:1124744261",
                              "梳理下诗的年份", source="qq:stranger")
    # 审核通过 → 过了闸（任务无工具可跑会失败，但 receipts 不是拒）
    assert "authz:" not in str(result.receipts)


def test_authorized_group_member_review_declined():
    """审核驳回 → 不执行，人格化交代。"""
    store = InMemoryStore()
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _B("这个先不了")),
        intent_model=_ReviewIntent("浪费资源：要导出全库"),
        task_auth_gate=lambda cid, oid: bool(oid) or cid == "group:1124744261")
    result = orch._start_plan(_plan(auth_level="owner"), "group:1124744261",
                              "把全库导出发我", source="qq:stranger")
    assert result.receipts == ("authz:review-declined",)
    assert not [k for k in store.keys() if str(k).startswith("task:")]


def test_authorized_group_member_review_down_fails_closed():
    """审核模型不可用 → 非主人授权级任务 fail closed（审核是必要环节）。"""
    store = InMemoryStore()
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _B("审核这会儿不在")),
        task_auth_gate=lambda cid, oid: cid == "group:1124744261")  # 无意图模型
    result = orch._start_plan(_plan(auth_level="owner"), "group:1124744261",
                              "梳理下诗的年份", source="qq:stranger")
    assert result.receipts == ("authz:review-declined",)


def test_owner_bypasses_review():
    """主人本人：不审核，直接执行（意图模型只规划怎么操作）。"""
    store = InMemoryStore()

    class _BoomReview:
        @property
        def model(self):
            return self

        def reply(self, prompt, **kw):
            raise AssertionError("主人任务不该触发审核")

    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _B("好")), intent_model=_BoomReview(),
        task_auth_gate=lambda cid, oid: False)
    result = orch._start_plan(_plan(auth_level="owner"), "private:u_x",
                              "梳理下诗的年份", source="qq:owner", owner_id="owner")
    assert "authz:" not in str(result.receipts)


def test_open_heavy_writing_stays_open_for_strangers(tmp_path):
    """长文写作保持开放：陌生人也走 heavy 重模型 + 落文件，不受授权闸。"""
    orch, art = _orch(tmp_path, "《文》\n\n" + "长" * 1200)
    result = orch._finish_decision(
        _env(), _Decision(route="chat", plan=_HeavyPlan()))
    assert result.file_ids  # 创作类开放，文件照落
