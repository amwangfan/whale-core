"""owner_fact_v1 系统集成（Qwen 装配侧；Astra 冻结方案 §7-§8）。

覆盖（全部走**真实入口/真实 composition root**，不用 store.seed 替代）：

1. root 装配断言：profiles.assemble_runtime 与 glasses build_delegates 默认
   真实装配（service/worker/companion 注入 + 启动日志 note），紧急开关可关；
2. QQ owner 私聊贯通：handle_owner_event → observe 受理 → 后台 worker 提交
   → 下一轮 companion 召回（含 30/100/300 轮干扰 + 重启 + 跨渠道）；
3. 真实 source 动态鉴权：enqueue 后、commit 前解绑/暂停空间 → 拒绝写入
   （其他来源活跃不替代；owner 停记 = 空间 pause 开关）；
4. 遗忘闭环：显式「忘掉」→ 墓碑 + 源/跨会话 echo episode 整删 + 短历史
   清理 + barrier（迟到主/补发/情节写回不复活）+ 同证据重放拒绝；
5. 最终预算硬界（≤8 条 / 单条 ≤300 / 总 ≤2400 字符）；
6. API 窄分派：/memory DELETE 提前 return（服务未装配 409、绝不物理删除
   墓碑）、GET 墓碑 404、recent 排除 owner_fact_v1；studio correct/expire/
   delete 明确 409；export/import 拒绝该命名空间（防旧备份复活）。

诚实标注：本文件使用**确定性 Fake 提炼输出**（FakeModelReply 罐装 JSON，
经真实 OwnerFactExtractor 校验与真实服务事务），验证的是系统集成链路；
真实模型的语义提炼质量（precision/recall）由独立质量 eval 另行报告，
两者分数不混用。
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPConnection
from urllib.parse import quote

import pytest

from whale_core.core.companion import WhaleCompanion, apply_memory_budget
from whale_core.core.owner_fact_host import (
    LiveAuthorityProvider,
    build_owner_fact_stack,
    owner_conversations,
    remember_owner_conversation,
    register_owner_source,
)
from whale_core.life.channels import OwnerEvent
from whale_core.life.context_resolver import ConversationContextResolver
from whale_core.life.identity import OwnerRegistry
from whale_core.life.initiative import ProactivePrefs
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.history import (
    append_assistant_followup,
    append_assistant_message,
    history_generation,
    load_history,
)
from whale_core.memory.spaces import ensure_owner_working_copy
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import InMemoryStore, MemoryKind

OWNER_QQ = "12345"
QQ_CONV = f"private:{OWNER_QQ}"
GLASSES_CONV = "glasses:session-1"
FACT_TEXT = "我养的猫叫年糕"
TOKEN = "owner-fact-entry-test"


# --------------------------------------------------------------------------
# 确定性替身（Fake 链路 ≠ 真实模型质量；见模块 docstring）
# --------------------------------------------------------------------------


class FakeModelReply:
    """罐装提炼输出：prompt 里出现标记句 → 返回对应 candidates JSON。

    输出仍经真实 OwnerFactExtractor 解析/span 定位/校验与真实服务事务，
    只有「模型语义判断」这一环是确定性的。
    """

    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str, *, timeout: float) -> str:
        self.calls += 1
        if FACT_TEXT in prompt:
            return json.dumps({"candidates": [{
                "kind": "fact", "quote": FACT_TEXT,
                "predicate": "pet_name", "value": "年糕",
                "valid_from": None, "valid_to": None,
                "correction_target": None,
            }]}, ensure_ascii=False)
        return json.dumps({"candidates": []})


class FakeBrain:
    """捕获 reply() 收到的 memories（召回断言用）。"""

    def __init__(self, reply_text="好的呀"):
        self.calls: list[dict] = []
        self.reply_text = reply_text

    def reply(self, text, *, persona="", memories=(), history=(), images=()):
        self.calls.append({"text": text, "persona": persona,
                           "memories": list(memories), "history": list(history)})
        return self.reply_text


def _resolver(store):
    return ConversationContextResolver(
        pairing_lookup=lambda ch, sender: (
            "owner" if (ch, sender) in {("qq", OWNER_QQ), ("phone", "phone")}
            else None),
        owner_id="owner",
        owner_default_space=ensure_owner_working_copy(store, owner="owner"))


def _stack(store, *, start_worker=True, reply=None):
    register_owner_source(store, "owner", "qq", OWNER_QQ)
    register_owner_source(store, "owner", "phone", "phone")
    # 本机管理通道（与 profiles 装配一致）：API 窄分派经 console 主体核查
    register_owner_source(store, "owner", "api", "console")
    stack = build_owner_fact_stack(
        store, reply if reply is not None else FakeModelReply(),
        env={}, start_worker=start_worker, model_timeout=5.0)
    assert stack.enabled, stack.note  # root 装配必须真实成功（不许静默）
    return stack


def _auth(stack):
    return stack.authority.authorize_principal_now(
        "owner", "owner-working", f"paired:qq:{OWNER_QQ}")


def _facts(store):
    out = []
    for kind in (MemoryKind.EPISODE, MemoryKind.PREFERENCE):
        for rec in store.list_by_kind(kind, space_ids=("owner-working",)):
            if "owner_fact_v1" in rec.tags:
                out.append(rec)
    return out


def _workflow(store, brain, stack=None, resolver=None):
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}),
                           passive_daily_limit=500)
    resolver = resolver or _resolver(store)
    return QqInboundWorkflow(
        store, brain, prefs=prefs, context_resolver=resolver,
        owner_facts=stack.service if stack else None,
        owner_fact_authority=stack.authority if stack else None)


def _event(text, message_id="m1", conversation=QQ_CONV):
    return OwnerEvent(owner_id="owner", channel="qq", conversation=conversation,
                      message_id=message_id, text=text, sender_id=OWNER_QQ)


def _drain(stack, timeout=10.0):
    assert stack.worker.drain(timeout=timeout), "worker 未在限时内排空"


# --------------------------------------------------------------------------
# 1. root 装配（真实 composition root 实例断言）
# --------------------------------------------------------------------------


def test_service_root_assembles_real_instances(tmp_path):
    from whale_core.profiles import assemble_runtime

    bundle = assemble_runtime(
        "local", store=SqliteMemoryStore(tmp_path / "m.sqlite3"),
        env={"LOCAL_MODEL_BASE_URL": "http://127.0.0.1:1"},
        use_saved_config=False)
    try:
        assert bundle.owner_facts is not None
        assert bundle.owner_fact_authority is not None
        assert bundle.owner_fact_worker is not None
        # worker 真实启动（线程存活），不是只给了个默认 off 的模块
        thread = getattr(bundle.owner_fact_worker, "_thread", None)
        assert thread is not None and thread.is_alive()
        assert bundle.companion.owner_facts is bundle.owner_facts
        assert bundle.companion.owner_fact_authority is bundle.owner_fact_authority
        assert any("owner_fact_v1=on" in n for n in bundle.notes), bundle.notes
        # 管理通道来源已登记 + OwnerRegistry 物化（窄鉴权可回查）
        auth = bundle.owner_fact_authority.authorize_principal_now(
            "owner", "owner-working", "paired:api:console")
        assert auth is not None and auth.principal_id == "paired:api:console"
        # shutdown 后线程真实退出（幂等；不靠进程退出兜底）
        bundle.shutdown()
        assert not thread.is_alive()
        bundle.shutdown()
    finally:
        bundle.shutdown()


def test_validation_assembly_starts_no_worker(tmp_path):
    """settings 试装（start_workers=False）构建 service 但不起线程——
    重复装配不得遗留 worker。"""
    from whale_core.profiles import assemble_runtime

    bundle = assemble_runtime(
        "local", store=InMemoryStore(),
        env={"LOCAL_MODEL_BASE_URL": "http://127.0.0.1:1"},
        use_saved_config=False, start_workers=False)
    try:
        assert bundle.owner_facts is not None
        assert getattr(bundle.owner_fact_worker, "_thread", None) is None
    finally:
        bundle.shutdown()


def test_service_root_emergency_switch_is_explicit(tmp_path):
    from whale_core.profiles import assemble_runtime

    bundle = assemble_runtime(
        "local", store=SqliteMemoryStore(tmp_path / "m.sqlite3"),
        env={"LOCAL_MODEL_BASE_URL": "http://127.0.0.1:1",
             "WHALE_OWNER_FACT_MEMORY": "0"},
        use_saved_config=False)
    try:
        assert bundle.owner_facts is None
        assert bundle.companion.owner_facts is None
        # 关闭必须显式可见（不静默）
        assert any("owner_fact_v1=off" in n for n in bundle.notes), bundle.notes
    finally:
        if bundle.owner_fact_worker is not None:
            bundle.owner_fact_worker.stop()


def test_stack_reports_import_or_build_failure_loudly(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")

    class Boom:
        def complete(self, prompt, *, timeout):
            raise AssertionError("不应被调用")

    # model_reply=None → 明确未装配 note（隐藏失败 = 违规）
    stack = build_owner_fact_stack(store, None, env={}, start_worker=False)
    assert not stack.enabled and "未装配" in stack.note


def test_glasses_root_entry_observes(tmp_path):
    from whale_core.api.glasses import build_delegates

    env = {"WHALE_GLASSES_STORE": str(tmp_path / "g.sqlite3"),
           "WHALE_GLASSES_TOKEN": TOKEN}
    brain = FakeBrain("眼镜回复")
    delegates = build_delegates(env, brain_factory=lambda: brain,
                                quick_brain_factory=lambda: brain)
    try:
        reply, _timing = delegates["companion_chat"](
            "顺便说下，我养的猫叫年糕", GLASSES_CONV)
        assert reply
        # 真实受理：证据已持久（glasses root 默认装配，无需额外开关）
        store = delegates["store"]
        ev_keys = [k for k in store.keys() if k.startswith("ofev:")]
        assert ev_keys, "眼镜入口未产生 owner_fact 证据（root 未装配？）"
        # 生命周期钩子必须暴露（宿主收尾调用；重复 build 不遗留 worker）
        assert callable(delegates.get("owner_fact_shutdown"))
    finally:
        delegates["owner_fact_shutdown"]()


# --------------------------------------------------------------------------
# 2. QQ owner 入口贯通（observe → worker → commit → recall）
# --------------------------------------------------------------------------


def test_qq_owner_entry_end_to_end(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _stack(store, start_worker=False)
    brain = FakeBrain("年糕这名字真可爱")
    wf = _workflow(store, brain, stack)
    try:
        out = wf.handle_owner_event(_event(f"跟你说个事，{FACT_TEXT}", "msg-1"))
        assert out.get("reply")
        # 受理即持久（主模型未等待提炼）
        assert stack.service.pending_count() >= 1
        stack.worker.start()  # separate durable admission from asynchronous completion
        _drain(stack)
        facts = _facts(store)
        assert len(facts) == 1
        content = facts[0].content
        assert content["status"] == "accepted"
        assert content["summary"] == FACT_TEXT
        assert content["assertion"] == "user_asserted"
        assert content["space_id"] == "owner-working"
        assert content["source_refs"][0]["evidence_id"] == f"qq:{OWNER_QQ}:msg-1"
        # 同 evidence 重放（inbox 重投）→ duplicate，不产生第二条
        from whale_core.core.owner_fact_host import observe_owner_text
        ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                       conversation_id=QQ_CONV)
        res = observe_owner_text(
            stack.service, stack.authority, store, context=ctx,
            conversation_id=QQ_CONV, channel="qq", text=FACT_TEXT,
            evidence_id=f"qq:{OWNER_QQ}:msg-1")
        assert res is not None
        assert getattr(res.status, "value", str(res.status)) == "duplicate"
        _drain(stack)
        assert len(_facts(store)) == 1
        # 下一轮真实召回：companion memories 注入带归属标注
        brain2 = FakeBrain("你之前说过呀")
        wf2 = _workflow(store, brain2, stack)
        wf2.handle_owner_event(_event("我的猫叫什么名字来着", "msg-2"))
        injected = brain2.calls[-1]["memories"]
        assert any("长期记忆（用户自述）" in m and FACT_TEXT in m
                   for m in injected), injected
    finally:
        stack.shutdown()


@pytest.mark.parametrize("filler_turns", [30, 100, 300])
def test_long_interference_restart_and_cross_channel(tmp_path, filler_turns):
    db = tmp_path / "m.sqlite3"
    store = SqliteMemoryStore(db)
    stack = _stack(store)
    brain = FakeBrain("嗯嗯")
    wf = _workflow(store, brain, stack)
    try:
        wf.handle_owner_event(_event(FACT_TEXT, "msg-a"))
        _drain(stack)
        assert len(_facts(store)) == 1
        # N 轮干扰（同会话闲聊，写历史+情节）
        resolver = _resolver(store)
        ctx = resolver.resolve(channel="qq", sender_id=OWNER_QQ,
                               conversation_id=QQ_CONV)
        filler = FakeBrain("闲聊回复")
        comp = WhaleCompanion(store, filler, curator=MemoryCurator(store),
                              owner_facts=stack.service,
                              owner_fact_authority=stack.authority)
        for i in range(filler_turns):
            comp.chat(f"随便聊聊第{i}轮天气不错", conversation_id=QQ_CONV,
                      context=ctx)
    finally:
        stack.shutdown()
        store.close()

    # 重启：同一 SQLite 重新装配（新 worker/新 service），事实仍在且可召回
    store2 = SqliteMemoryStore(db)
    stack2 = _stack(store2, start_worker=False)
    try:
        facts = _facts(store2)
        assert len(facts) == 1 and facts[0].content["status"] == "accepted"
        # 同 owner 另一授权渠道（眼镜会话）询问 → 召回命中（跨渠道连续性）
        ctx_g = _resolver(store2).resolve(channel="phone", sender_id="phone",
                                          conversation_id=GLASSES_CONV)
        brain_g = FakeBrain("记得呀")
        comp_g = WhaleCompanion(store2, brain_g, curator=MemoryCurator(store2),
                                owner_facts=stack2.service,
                                owner_fact_authority=stack2.authority)
        comp_g.chat("我的猫叫什么", conversation_id=GLASSES_CONV, context=ctx_g)
        injected = brain_g.calls[-1]["memories"]
        assert any(FACT_TEXT in m for m in injected), injected
    finally:
        stack2.shutdown()
        store2.close()


# --------------------------------------------------------------------------
# 3. 真实 source 动态鉴权（enqueue 后 commit 前失效 → 拒绝）
# --------------------------------------------------------------------------


def _enqueue_without_worker(store):
    stack = _stack(store, start_worker=False)
    ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                   conversation_id=QQ_CONV)
    from whale_core.core.owner_fact_host import observe_owner_text
    res = observe_owner_text(stack.service, stack.authority, store,
                             context=ctx, conversation_id=QQ_CONV,
                             channel="qq", text=FACT_TEXT,
                             evidence_id=f"qq:{OWNER_QQ}:m-9")
    assert res is not None and getattr(res.status, "value", "") == "queued"
    return stack


def test_unbind_between_enqueue_and_commit_rejects(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _enqueue_without_worker(store)
    try:
        # 第二连接/管理面解绑该来源（OwnerRegistry 真删）
        assert OwnerRegistry(store).unbind("qq", OWNER_QQ)
        stack.worker.start()
        _drain(stack)
        assert _facts(store) == []  # 拒绝写入
        stats = stack.worker.stats()
        assert stats.commit_rejected + stats.failed >= 1
        # 在途原文不得保留（policy_blocked 清理）
        ev = store.get("ofev:qq:%s:m-9" % OWNER_QQ)
        assert ev is None or ev.get("text") == ""
    finally:
        stack.shutdown()


def test_space_pause_between_enqueue_and_commit_rejects(tmp_path):
    """owner「停止记录」= 空间级 pause 开关（Owner 无 MemberConsent 语义，
    不假称检查了不存在的 opt-out）。"""
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _enqueue_without_worker(store)
    try:
        store.put("space:paused:owner-working",
                  {"paused": True, "at": "2026-11-12T00:00:00+00:00",
                   "by": "test"})
        stack.worker.start()
        _drain(stack)
        assert _facts(store) == []
        assert stack.worker.stats().commit_rejected + \
            stack.worker.stats().failed >= 1
    finally:
        stack.shutdown()


def test_other_source_active_does_not_substitute(tmp_path):
    """QQ 来源解绑后，即使 phone 来源仍活跃，QQ 证据的迟到提交也拒绝
    （无 any-source 后门）；且解绑后 QQ 入口 observe 直接 fail closed。"""
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _enqueue_without_worker(store)
    try:
        OwnerRegistry(store).unbind("qq", OWNER_QQ)
        # phone 来源仍活跃
        assert stack.authority.authorize_principal_now(
            "owner", "owner-working", "paired:phone:phone") is not None
        # QQ 具体来源已失效
        assert stack.authority.authorize_principal_now(
            "owner", "owner-working", f"paired:qq:{OWNER_QQ}") is None
        stack.worker.start()
        _drain(stack)
        assert _facts(store) == []
    finally:
        stack.shutdown()


def test_context_none_never_collects(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _stack(store, start_worker=False)
    try:
        from whale_core.core.owner_fact_host import observe_owner_text
        assert observe_owner_text(
            stack.service, stack.authority, store, context=None,
            conversation_id=QQ_CONV, channel="qq", text=FACT_TEXT,
            evidence_id="x") is None
        # 群会话同样不采集（即使 principal 是 owner）
        ctx_group = _resolver(store).resolve(
            channel="qq", sender_id=OWNER_QQ, conversation_id="group:777")
        assert observe_owner_text(
            stack.service, stack.authority, store, context=ctx_group,
            conversation_id="group:777", channel="qq", text=FACT_TEXT,
            evidence_id="y") is None
        assert stack.service.pending_count() == 0
    finally:
        stack.shutdown()


# --------------------------------------------------------------------------
# 4. 遗忘闭环（echo 整删 + 历史清理 + barrier 不复活 + 重放拒绝）
# --------------------------------------------------------------------------


def test_forget_echo_history_barrier_and_replay(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _stack(store)
    brain = FakeBrain("年糕这个猫名很可爱")  # 助手 echo 进情节
    wf = _workflow(store, brain, stack)
    try:
        wf.handle_owner_event(_event(FACT_TEXT, "msg-f1"))
        _drain(stack)
        assert len(_facts(store)) == 1
        # 源会话情节含 echo；短历史含原话
        eps = [r for r in store.list_by_kind(MemoryKind.EPISODE)
               if "chat" in r.tags and r.content.get("conversation_id") == QQ_CONV]
        assert eps and any("年糕" in str(r.content.get("summary")) for r in eps)
        # 另一 owner 会话（眼镜）里的跨会话 echo（无完整 quote，仅 value）
        remember_owner_conversation(store, "owner", GLASSES_CONV)
        store.put("chat:history:" + GLASSES_CONV,
                  [{"role": "user", "content": "我的猫叫什么"},
                   {"role": "assistant", "content": "你的猫叫年糕"}])
        from whale_core.memory.continuity import remember_episode
        remember_episode(store, summary="用户：我的猫叫什么\nEnxi：你的猫叫年糕",
                         key="ep:chat:glasses-echo", tags=("chat",),
                         extra={"conversation_id": GLASSES_CONV},
                         space_id="owner-working")
        # Astra 负例（2026-11-12 隐私闭环）：**无 value、不同句式**的助手
        # 改写 echo——不含 quote 也不含「年糕」，词面匹配永远抓不到；
        # 登记会话整删语义下必须一并消失。
        remember_episode(store,
                         summary="用户：你还记得吗\nEnxi：你之前提过家里那只小猫的名字",
                         key="ep:chat:glasses-paraphrase", tags=("chat",),
                         extra={"conversation_id": GLASSES_CONV},
                         space_id="owner-working")
        # 未登记会话（非来源索引）：绝不触碰（他人/未归属数据保留）
        remember_episode(store, summary="用户：随便聊聊\nEnxi：好呀",
                         key="ep:chat:unregistered", tags=("chat",),
                         extra={"conversation_id": "private:99999"},
                         space_id="owner-working")
        # InnerState：登记会话的 scoped 未完话题（**无 value 词面关联**的
        # 改写句式）须整清；全局人格/兴趣与他人 scope 必须原样保留。
        from whale_core.life.state import InnerStateStore
        InnerStateStore(store, scope=GLASSES_CONV).update(
            open_topic="年糕最近的体重")
        InnerStateStore(store, scope="private:99999").update(
            open_topic="别人的话题")
        InnerStateStore(store).update(add_interest="猫")

        # 在途轮的代际快照（barrier：模拟 forget 前已读 prompt 的旧轮）
        stale_gen = history_generation(store, QQ_CONV)

        # 显式忘掉（确定性精确匹配路径）
        reply = wf._memory_command_reply(f"忘掉：{FACT_TEXT}",
                                         _event("", "msg-f2"))
        assert reply and reply.startswith("已忘掉"), reply

        # 事实墓碑（key 保留、正文清空、状态 deleted）
        assert _facts(store) == [] or all(
            r.content.get("status") == "deleted" for r in _facts(store))
        rec_keys = [k for k in store.keys() if k.startswith("offact:")]
        assert rec_keys, "墓碑被物理删除（防复活失效）"
        tomb = store.get_record(rec_keys[0])
        assert tomb.content.get("status") == "deleted"
        assert not tomb.content.get("summary")
        # 召回为零
        assert stack.service.retrieve(_auth(stack), "猫") == ()
        # echo 清理（隐私闭环）：登记 owner 会话的 chat 情节**整条删除**
        # ——含 value 命中、无 value 不同句式的助手改写、源会话原话情节；
        # 不依赖词面匹配，不可能经 episode_retrieval 复召。
        for r in store.list_by_kind(MemoryKind.EPISODE):
            if "chat" in r.tags and r.content.get("conversation_id") in (
                    QQ_CONV, GLASSES_CONV):
                pytest.fail(f"登记会话 chat 情节未整删：{r.key}")
        assert store.get_record("ep:chat:glasses-paraphrase") is None
        assert store.get_record("ep:chat:glasses-echo") is None
        # 未登记会话的情节保留（他人/未归属数据不碰）
        assert store.get_record("ep:chat:unregistered") is not None
        # 短历史保守整清（owner 自有登记会话）
        assert load_history(store, QQ_CONV) == []
        assert load_history(store, GLASSES_CONV) == []
        # InnerState：登记会话 scoped 话题整清（无词面关联也清）；
        # 他人 scope 与全局人格/兴趣原样保留
        from whale_core.life.state import InnerStateStore
        assert InnerStateStore(store, scope=GLASSES_CONV).load().unfinished_topics == ()
        assert InnerStateStore(store, scope=QQ_CONV).load().unfinished_topics == ()
        assert "别人的话题" in InnerStateStore(
            store, scope="private:99999").load().unfinished_topics
        assert "猫" in InnerStateStore(store).load().interests

        # barrier：迟到的主模型/补发/情节写回全部作废，不重建旧用户话
        assert append_assistant_message(store, QQ_CONV, "迟到的回复",
                                        generation=stale_gen) is False
        assert append_assistant_followup(store, QQ_CONV, FACT_TEXT,
                                         "迟到的补发",
                                         generation=stale_gen) is False
        from whale_core.memory.continuity import remember_episode as _re
        assert _re(store, summary=f"用户：{FACT_TEXT}\nEnxi：迟到",
                   key="ep:chat:late", tags=("chat",),
                   extra={"conversation_id": QQ_CONV}, space_id="owner-working",
                   generation=stale_gen, generation_conversation=QQ_CONV) is None
        assert load_history(store, QQ_CONV) == []
        assert store.get_record("ep:chat:late") is None

        # 同证据重放（重试/旧 job 重入队）→ 拒绝；新证据明确重述 → 允许
        from whale_core.core.owner_fact_host import observe_owner_text
        ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                       conversation_id=QQ_CONV)
        replay = observe_owner_text(stack.service, stack.authority, store,
                                    context=ctx, conversation_id=QQ_CONV,
                                    channel="qq", text=FACT_TEXT,
                                    evidence_id=f"qq:{OWNER_QQ}:msg-f1")
        assert getattr(replay.status, "value", "") == "skipped_policy"
        assert replay.reason_code == "evidence_revoked"
        fresh = observe_owner_text(stack.service, stack.authority, store,
                                   context=ctx, conversation_id=QQ_CONV,
                                   channel="qq", text=FACT_TEXT,
                                   evidence_id=f"qq:{OWNER_QQ}:msg-f3")
        assert getattr(fresh.status, "value", "") == "queued"
    finally:
        stack.shutdown()


def test_forget_command_never_fuzzy_top1(tmp_path):
    """模糊线索不删任何长期事实：零命中 → 明确「没有找到」，事实完好；
    多义 → 列出澄清。绝不相似度 top-1 盲删。"""
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _stack(store)
    try:
        from whale_core.core.memory_commands import handle_memory_command
        ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                       conversation_id=QQ_CONV)
        from whale_core.core.owner_fact_host import observe_owner_text
        observe_owner_text(stack.service, stack.authority, store, context=ctx,
                           conversation_id=QQ_CONV, channel="qq",
                           text=FACT_TEXT, evidence_id="e-1")
        _drain(stack)
        assert len(_facts(store)) == 1

        def _forget(text):
            return handle_memory_command(
                store, text, source="qq:owner", evidence_id="e-cmd",
                curator=MemoryCurator(store), space_id="owner-working",
                owner_facts=stack.service, owner_fact_authority=stack.authority,
                principal_id=f"paired:qq:{OWNER_QQ}")

        # 模糊线索：零命中 → 不删（回落旧路径也没有 curated 可删）
        reply = _forget("忘掉：那个")
        assert reply in ("没有找到相关的记忆诶",) or "哪一条" in reply
        assert len([r for r in _facts(store)
                    if r.content.get("status") == "accepted"]) == 1
        # 精确整句：唯一命中 → 删除
        reply = _forget(f"忘掉：{FACT_TEXT}")
        assert reply and reply.startswith("已忘掉")
        assert all(r.content.get("status") != "accepted" for r in _facts(store))
    finally:
        stack.shutdown()


def test_read_blocked_after_source_unbind_cross_source_active(tmp_path):
    """Astra P0：QQ 来源解绑后，即使 phone 来源仍活跃，持 QQ context 的
    会话也**读不到** owner 事实（读侧同走 authorize_principal_now 精确
    source 校验，无 any-source 后门）；phone 入口不受影响。"""
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _stack(store)
    try:
        ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                       conversation_id=QQ_CONV)
        from whale_core.core.owner_fact_host import observe_owner_text
        observe_owner_text(stack.service, stack.authority, store, context=ctx,
                           conversation_id=QQ_CONV, channel="qq",
                           text=FACT_TEXT, evidence_id="e-rb")
        _drain(stack)
        assert len(_facts(store)) == 1

        # 解绑前：QQ context 可读
        brain = FakeBrain()
        comp = WhaleCompanion(store, brain, curator=MemoryCurator(store),
                              owner_facts=stack.service,
                              owner_fact_authority=stack.authority)
        comp.chat("我的猫叫什么", conversation_id=QQ_CONV, context=ctx)
        assert any(FACT_TEXT in m for m in brain.calls[-1]["memories"])

        # 解绑 QQ 来源（phone 仍活跃）
        assert OwnerRegistry(store).unbind("qq", OWNER_QQ)
        brain2 = FakeBrain()
        comp2 = WhaleCompanion(store, brain2, curator=MemoryCurator(store),
                               owner_facts=stack.service,
                               owner_fact_authority=stack.authority)
        comp2.chat("我的猫叫什么", conversation_id=QQ_CONV, context=ctx)
        assert not any(FACT_TEXT in m for m in brain2.calls[-1]["memories"])

        # phone（眼镜）来源仍活跃：同 owner 空间照常可读
        ctx_g = _resolver(store).resolve(channel="phone", sender_id="phone",
                                         conversation_id=GLASSES_CONV)
        brain3 = FakeBrain()
        comp3 = WhaleCompanion(store, brain3, curator=MemoryCurator(store),
                               owner_facts=stack.service,
                               owner_fact_authority=stack.authority)
        comp3.chat("我的猫叫什么", conversation_id=GLASSES_CONV, context=ctx_g)
        assert any(FACT_TEXT in m for m in brain3.calls[-1]["memories"])
    finally:
        stack.shutdown()


def test_two_roots_share_queue_lease_single_commit(tmp_path):
    """生产 3 root 同库：两个 worker 竞争同一队列，lease CAS 保证一条
    evidence 只被消费一次（不双写、不误拒他源 job）。"""
    db = tmp_path / "m.sqlite3"
    store_a = SqliteMemoryStore(db)
    stack_a = _stack(store_a)
    store_b = SqliteMemoryStore(db)
    stack_b = _stack(store_b)
    try:
        ctx = _resolver(store_a).resolve(channel="qq", sender_id=OWNER_QQ,
                                         conversation_id=QQ_CONV)
        from whale_core.core.owner_fact_host import observe_owner_text
        res = observe_owner_text(stack_a.service, stack_a.authority, store_a,
                                 context=ctx, conversation_id=QQ_CONV,
                                 channel="qq", text=FACT_TEXT,
                                 evidence_id="e-two")
        assert getattr(res.status, "value", "") == "queued"
        assert stack_a.worker.drain(timeout=10)
        assert stack_b.worker.drain(timeout=10)
        facts = _facts(store_a)
        assert len(facts) == 1 and facts[0].content["status"] == "accepted"
        total_claims = (stack_a.worker.stats().claimed
                        + stack_b.worker.stats().claimed)
        assert total_claims == 1  # 恰好一个 worker 领到（lease 互斥）
    finally:
        stack_a.shutdown()
        stack_b.shutdown()
        store_a.close()
        store_b.close()


def test_slow_model_inner_busy_blocks_next_claim(tmp_path):
    """Astra 状态机（2026-11-12）：内层模型调用超时后底层仍在收尾时，
    组合 gate busy 必须阻止 worker 领取下一 job——后续证据保持 pending，
    不被快速重试耗尽误丢；底层收尾后自动恢复处理。"""
    import threading

    class SlowThenFastReply:
        """脑 reply 替身（BoundedModelReply 包装的是裸 reply(prompt) 回调）：
        第一次调用比适配层下限慢（真实阻塞收尾），之后立即返回。"""

        def __init__(self):
            self.calls = 0
            self.first_done = threading.Event()

        def complete(self, prompt, *, timeout=0.0):
            self.calls += 1
            if self.calls == 1:
                # 超过适配层下限（floor 2s）仍在收尾：内层超时后 busy 保持
                time.sleep(max(timeout, 2.0) + 1.5)
                self.first_done.set()
                raise RuntimeError("inner timeout (simulated)")
            if FACT_TEXT in prompt:
                return json.dumps({"candidates": [{
                    "kind": "fact", "quote": FACT_TEXT,
                    "predicate": "pet_name", "value": "年糕"}]},
                    ensure_ascii=False)
            return json.dumps({"candidates": []})

    from whale_core.core.owner_fact_host import (
        BoundedModelReply, observe_owner_text)

    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    ensure_owner_working_copy(store, owner="owner")
    register_owner_source(store, "owner", "qq", OWNER_QQ)
    inner = SlowThenFastReply()
    reply = BoundedModelReply(inner.complete)
    stack = build_owner_fact_stack(store, reply, env={}, start_worker=False,
                                   model_timeout=1.0)
    assert stack.enabled, stack.note
    ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                   conversation_id=QQ_CONV)
    try:
        # 两条证据入队（两个不同会话轮）
        r1 = observe_owner_text(stack.service, stack.authority, store,
                                context=ctx, conversation_id=QQ_CONV,
                                channel="qq", text=FACT_TEXT,
                                evidence_id="e-slow-1")
        r2 = observe_owner_text(stack.service, stack.authority, store,
                                context=ctx, conversation_id=QQ_CONV,
                                channel="qq", text=FACT_TEXT,
                                evidence_id="e-slow-2")
        assert getattr(r1.status, "value") == "queued"
        assert getattr(r2.status, "value") == "queued"
        stack.worker.start()
        # 等第一次（慢）调用进入收尾：内层 busy 期间第二条必须仍是 pending，
        # 不允许被领取后秒失败（重试快速耗尽 = 丢非故障证据）
        deadline = time.time() + 5
        saw_busy_with_pending = False
        while time.time() < deadline:
            stats = stack.worker.stats()
            if stats.model_busy and stats.queue_depth >= 1:
                # 保护窗口内不得出现快速重试风暴（初次领取 + 至多一次重试）
                assert stats.claimed <= 2, stats
                saw_busy_with_pending = True
                break
            time.sleep(0.05)
        assert saw_busy_with_pending, "内层忙碌期间未观察到 pending 保护窗口"
        # 内层收尾后恢复：最终两条证据都被处理（重试成功 + 第二条正常），
        # 没有任何 job 以 failed 终态丢弃
        assert stack.worker.drain(timeout=20)
        stats = stack.worker.stats()
        assert stats.failed == 0, stats
        assert stats.completed >= 1, stats
        assert stats.claimed <= 4, stats  # 无快速重试耗尽（claimed 有界）
        assert stats.queue_depth == 0
        assert stack.service.pending_count() == 0
        # 两条相同文本证据 → 幂等去重后至少一条 accepted 事实
        accepted = [r for r in _facts(store)
                    if r.content.get("status") == "accepted"]
        assert len(accepted) >= 1
    finally:
        stack.shutdown()
        store.close()


# --------------------------------------------------------------------------
# 5. 最终预算硬界
# --------------------------------------------------------------------------


def test_final_memory_budget_hard_bounds(tmp_path):
    store = InMemoryStore()
    ensure_owner_working_copy(store, owner="owner")
    curator = MemoryCurator(store)
    for i in range(12):
        curator.remember(f"事实编号{i}：" + "长内容" * 40, source="t",
                         evidence_id=f"e{i}", stable_fact=True, consent=True,
                         space_id="owner-working")
    brain = FakeBrain()
    comp = WhaleCompanion(store, brain, curator=curator)
    ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                   conversation_id=QQ_CONV)
    comp.chat("事实编号", conversation_id=QQ_CONV, context=ctx)
    memories = brain.calls[-1]["memories"]
    assert len(memories) <= 8
    assert all(len(m) <= 300 for m in memories)
    assert sum(len(m) for m in memories) <= 2400
    # 单元口径：优先级保序 + 条数/单条/总字符三重硬界
    out = apply_memory_budget(["a" * 400, "b", "c" * 500], max_items=2)
    assert len(out) == 2 and len(out[0]) == 300
    overflow = apply_memory_budget([f"item{i}" + "x" * 290 for i in range(12)])
    assert len(overflow) <= 8
    assert all(len(m) <= 300 for m in overflow)
    assert sum(len(m) for m in overflow) <= 2400


# --------------------------------------------------------------------------
# 6. API 窄分派与 import 兼容
# --------------------------------------------------------------------------


@contextmanager
def _api(store, *, wire_service=False, stack=None):
    from whale_core.api.server import ApiContext, WhaleApiServer

    ctx = ApiContext(store=store, curator=MemoryCurator(store))
    if wire_service and stack is not None:
        ctx.owner_facts = stack.service
        ctx.owner_fact_authority = stack.authority
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read() or b"{}"
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, {}


def _seed_fact(store, stack):
    ctx = _resolver(store).resolve(channel="qq", sender_id=OWNER_QQ,
                                   conversation_id=QQ_CONV)
    from whale_core.core.owner_fact_host import observe_owner_text
    observe_owner_text(stack.service, stack.authority, store, context=ctx,
                       conversation_id=QQ_CONV, channel="qq",
                       text=FACT_TEXT, evidence_id="e-api")
    _drain(stack)
    facts = _facts(store)
    assert len(facts) == 1
    return facts[0]


def test_api_delete_refuses_without_service_and_never_kills_tombstone(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    stack = _stack(store)
    try:
        rec = _seed_fact(store, stack)
        key = quote(rec.key, safe="")
        # 服务未装配：明确 409，记录分毫不动（绝不落到无条件 store.delete）
        with _api(store, wire_service=False) as server:
            status, body = _req(server, "DELETE", f"/api/v1/memory/{key}")
            assert status == 409
            assert store.get_record(rec.key) is not None
            assert store.get_record(rec.key).content.get("status") == "accepted"
        # 服务装配：走领域 forget → 墓碑保留
        with _api(store, wire_service=True, stack=stack) as server:
            status, body = _req(server, "DELETE", f"/api/v1/memory/{key}")
            assert status == 200, body
            tomb = store.get_record(rec.key)
            assert tomb is not None and tomb.content.get("status") == "deleted"
            assert not tomb.content.get("summary")
            # GET 墓碑 → 404（不泄露、不展示已删内容）
            status, _ = _req(server, "GET", f"/api/v1/memory/{key}")
            assert status == 404
            # recent 列表不出现 owner_fact_v1（pending/deleted 不作召回显示）
            status, body = _req(server, "GET", "/api/v1/memory/search")
            assert status == 200
            assert all(not str(h["key"]).startswith("offact:")
                       for h in body["result"]["hits"])
    finally:
        stack.shutdown()


def test_studio_governance_rejects_owner_fact_namespace(tmp_path):
    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    ensure_owner_working_copy(store, owner="owner")
    stack = _stack(store)
    try:
        rec = _seed_fact(store, stack)
        with _api(store, wire_service=True, stack=stack) as server:
            for path, body in (
                    ("/api/v1/studio/memory/correct",
                     {"key": rec.key, "new_summary": "改写"}),
                    ("/api/v1/studio/memory/expire", {"key": rec.key}),
                    ("/api/v1/studio/memory/delete", {"key": rec.key})):
                status, payload = _req(server, "POST", path, body=body)
                assert status == 409, (path, status, payload)
            # 记录未被旧路径改写
            assert store.get_record(rec.key).content.get("status") == "accepted"
    finally:
        stack.shutdown()


def test_import_rejects_owner_fact_namespace(tmp_path):
    import io

    from whale_core.memory.export import export_jsonl, import_jsonl
    from whale_core.memory.owner_facts import tombstone_record

    store = SqliteMemoryStore(tmp_path / "m.sqlite3")
    ensure_owner_working_copy(store, owner="owner")
    stack = _stack(store)  # worker 启动（真实受理→提交）
    try:
        rec = _seed_fact(store, stack)
    finally:
        stack.shutdown()
    buf = io.StringIO()
    export_jsonl(store, buf)
    payload = buf.getvalue()
    assert "offact:" in payload  # 导出含数据（备份视角；非生命周期恢复）

    target = InMemoryStore()
    imported, skipped = import_jsonl(target, io.StringIO(payload),
                                     overwrite=True)
    assert target.get_record(rec.key) is None  # 新命名空间拒绝导入
    assert skipped >= 1

    # 墓碑覆盖攻击：目标库已有删除墓碑，overwrite 导入旧备份也不能复活
    tomb = tombstone_record(rec, now=datetime.now(timezone.utc))
    target.put_record(tomb)
    import_jsonl(target, io.StringIO(payload), overwrite=True)
    after = target.get_record(rec.key)
    assert after is not None and after.content.get("status") == "deleted"
    assert not after.content.get("summary")


# --------------------------------------------------------------------------
# 7. 双后端对等（generation 守卫在 InMemory 与 SQLite 语义一致）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("store_factory", [InMemoryStore, SqliteMemoryStore])
def test_generation_guard_parity(store_factory, tmp_path):
    store = (store_factory(tmp_path / "p.sqlite3")
             if store_factory is SqliteMemoryStore else store_factory())
    try:
        from whale_core.memory.history import (
            append_user_message, clear_history)
        cid = "private:parity"
        append_user_message(store, cid, "旧用户话")
        gen = history_generation(store, cid)
        clear_history(store, cid)  # 遗忘抑制：原子清历史 + bump
        assert history_generation(store, cid) == gen + 1
        assert append_assistant_message(store, cid, "迟到回复",
                                        generation=gen) is False
        assert append_assistant_followup(store, cid, "旧用户话", "迟到补发",
                                         generation=gen) is False
        assert load_history(store, cid) == []  # fallback 不重建用户话
        # 新代际正常写入
        new_gen = history_generation(store, cid)
        append_user_message(store, cid, "新一轮")
        assert append_assistant_message(store, cid, "新回复",
                                        generation=new_gen) is True
        assert len(load_history(store, cid)) == 2
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
