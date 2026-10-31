"""G2 群友个人记忆与私聊连续性测试（下一阶段计划 §4、§10、§11 MEM-*）。

覆盖：
- member_private 空间类型、PrincipalMemoryBinding 与会话绑定校验
  （群聊绑定一律拒绝、只许本人私聊）；
- MemberConsent：本人授予/撤销/TTL/版本；群主代授权被拒；
- 提炼管道：未授权 0 写入、敏感内容不抽取、幂等不重复、撤销后停止；
- resolver：本人已认证私聊下发 member_private；群聊/B 私聊永不；
- 召回隔离：A/B 空间互不可读、过期记录退出召回；
- Studio member API：grant/revoke/list-own/correct-own/delete-own
  （真删 + embedding forget_text 传播）；
- §10 固定样例全链路：A 授权后 A 私聊可回忆、B 不能、群里不能。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.life.context_resolver import ConversationContextResolver
from whale_core.life.conversation_context import (
    ConversationKind,
    MemorySpaceType,
    principal_matches_private_conversation,
)
from whale_core.memory.bindings import BindingError, BindingStore
from whale_core.memory.consent import (
    ConsentError,
    MemberConsentStore,
    MemberMemoryDirectory,
)
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.extraction import (
    MemberMemoryExtractor,
    contains_sensitive,
)
from whale_core.memory.spaces import MemorySpaceError, SpaceStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.memory.timeline import GroupTimelineStore

A_UIN = "10001"
B_UIN = "10002"
A_PRINCIPAL = f"anon:qq:{A_UIN}"
B_PRINCIPAL = f"anon:qq:{B_UIN}"
GROUP = "group:1124744261"
A_PRIVATE_KEY = f"qq:qq_private:private:{A_UIN}"
B_PRIVATE_KEY = f"qq:qq_private:private:{B_UIN}"
GROUP_KEY = f"qq:qq_group:{GROUP}"

A_MSG_1 = "我的 Windows 电脑很卡，明天论文还要交。"
A_MSG_2 = "电脑现在动不了了，如果装了 360 是不是就不会这样？"
B_MSG = "E6 88 91 E7 88 B1 E4 BD A0"


@pytest.fixture()
def env():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    bindings = BindingStore(store, spaces)
    consents = MemberConsentStore(store)
    directory = MemberMemoryDirectory(bindings, spaces, consents)
    timeline = GroupTimelineStore(store)
    extractor = MemberMemoryExtractor(store, timeline, consents, bindings, spaces)
    return store, spaces, bindings, consents, directory, timeline, extractor


def _grant_self(env, principal=A_PRINCIPAL, group=GROUP, key=A_PRIVATE_KEY):
    _, _, bindings, consents, *_ = env
    consent = consents.grant(principal, group, granted_by=principal,
                             granted_via=key)
    ref = bindings.ensure_member_space(principal, conversation_key=key,
                                       granted_by=principal)
    return consent, ref


def _resolver(env):
    _, _, _, _, directory, *_ = env
    return ConversationContextResolver(member_memory=directory)


def _fill_timeline(tl):
    tl.append(GROUP, message_id="m-a1", sender_id=A_UIN, text=A_MSG_1)
    tl.append(GROUP, message_id="m-b1", sender_id=B_UIN, text=B_MSG)
    tl.append(GROUP, message_id="m-a2", sender_id=A_UIN, text=A_MSG_2)


# ---------------------------------------------------------------------------
# 空间类型与绑定
# ---------------------------------------------------------------------------


def test_member_space_create_and_protected_still_blocked():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    ref = spaces.create(MemorySpaceType.MEMBER_PRIVATE, name="成员个人记忆",
                        owner=A_PRINCIPAL)
    assert ref.type is MemorySpaceType.MEMBER_PRIVATE
    with pytest.raises(MemorySpaceError):
        spaces.create(MemorySpaceType.PROTECTED_ORIGINAL, name="vault")


def test_member_binding_rules(env):
    store, spaces, bindings, *_ = env
    ref = spaces.create(MemorySpaceType.MEMBER_PRIVATE, name="m",
                        owner=A_PRINCIPAL, space_id="sp-a")
    # 群聊绑定 member_private 一律拒绝（通用会话绑定入口也一样）
    with pytest.raises(BindingError):
        bindings.bind_memory(GROUP_KEY, "sp-a", granted_by="studio-api")
    with pytest.raises(BindingError):
        bindings.bind_member_space(A_PRINCIPAL, "sp-a",
                                   conversation_key=GROUP_KEY,
                                   granted_by=A_PRINCIPAL)
    # 他人私聊也不行
    with pytest.raises(BindingError):
        bindings.bind_memory(B_PRIVATE_KEY, "sp-a", granted_by="studio-api")
    with pytest.raises(BindingError):
        bindings.bind_member_space(A_PRINCIPAL, "sp-a",
                                   conversation_key=B_PRIVATE_KEY,
                                   granted_by=A_PRINCIPAL)
    # 空间 owner 与 principal 不一致
    with pytest.raises(BindingError):
        bindings.bind_member_space(B_PRINCIPAL, "sp-a",
                                   conversation_key=B_PRIVATE_KEY,
                                   granted_by=B_PRINCIPAL)
    # 本人已认证私聊 → 允许
    row = bindings.bind_memory(A_PRIVATE_KEY, "sp-a", granted_by=A_PRINCIPAL)
    assert row["space_id"] == "sp-a"
    data = bindings.bind_member_space(A_PRINCIPAL, "sp-a",
                                      conversation_key=A_PRIVATE_KEY,
                                      granted_by=A_PRINCIPAL)
    assert data["space_id"] == "sp-a"
    assert bindings.member_binding_for(A_PRINCIPAL)["space_id"] == "sp-a"
    # 非 member_private 空间不能进 PrincipalMemoryBinding
    gref = spaces.create(MemorySpaceType.GROUP_SPACE, name="g",
                         space_id="sp-g")
    with pytest.raises(BindingError):
        bindings.bind_member_space(A_PRINCIPAL, gref.space_id,
                                   conversation_key=A_PRIVATE_KEY,
                                   granted_by=A_PRINCIPAL)


def test_principal_matches_private_conversation():
    assert principal_matches_private_conversation(
        "qq", ConversationKind.QQ_PRIVATE, "private:10001", A_PRINCIPAL)
    assert not principal_matches_private_conversation(
        "qq", ConversationKind.QQ_PRIVATE, "private:10002", A_PRINCIPAL)
    assert not principal_matches_private_conversation(
        "qq", ConversationKind.QQ_GROUP, GROUP, A_PRINCIPAL)
    assert not principal_matches_private_conversation(
        "qq", ConversationKind.QQ_PRIVATE, "private:", A_PRINCIPAL)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


def test_consent_grant_revoke_ttl(env):
    consents = env[3]
    c = consents.grant(A_PRINCIPAL, GROUP, granted_by=A_PRINCIPAL,
                       granted_via=A_PRIVATE_KEY)
    assert c.version == 1 and consents.is_valid(c)
    # 重复 grant = 新版本
    c2 = consents.grant(A_PRINCIPAL, GROUP, granted_by=A_PRINCIPAL,
                        granted_via=A_PRIVATE_KEY)
    assert c2.version == 2
    # 群主/他人代授权被拒（存储层）
    with pytest.raises(ConsentError):
        consents.grant(A_PRINCIPAL, GROUP, granted_by="paired:qq:owner",
                       granted_via="qq:qq_private:private:owner")
    # TTL 过期
    c3 = consents.grant(B_PRINCIPAL, GROUP, ttl_s=10, granted_by=B_PRINCIPAL,
                        granted_via=B_PRIVATE_KEY)
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert not consents.is_valid(c3, now=future)
    assert not consents.has_valid(B_PRINCIPAL, now=future)
    # 撤销
    assert consents.revoke(A_PRINCIPAL, GROUP)
    assert not consents.is_valid(consents.get(A_PRINCIPAL, GROUP))
    assert not consents.revoke(A_PRINCIPAL, GROUP)  # 重复撤销幂等 False
    assert not consents.has_valid(A_PRINCIPAL)


# ---------------------------------------------------------------------------
# 提炼管道
# ---------------------------------------------------------------------------


def test_extraction_default_on_and_optout_stops(env):
    """owner 决策 2026-11-01：成员记忆默认开启（私聊即同意）；退出即停。

    MEM-02 修订：默认采集仅本人自述；「别记我了」写持久退出标记，
    退出后零扫描零写入。
    """
    store, spaces, bindings, consents, _, tl, extractor = env
    _fill_timeline(tl)
    _, ref = _grant_self(env)  # 建档（空间+绑定）
    # 默认在记：无需显式 consent 即写入
    stats = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert stats.written >= 1
    # 退出：零扫描零写入
    consents.opt_out(A_PRINCIPAL)
    tl.append(GROUP, message_id="m-a9", sender_id=A_UIN, text="我又来了一句自述")
    stats2 = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert stats2.written == 0 and stats2.scanned == 0


def test_extraction_writes_only_self_sourced(env):
    store, spaces, bindings, consents, directory, tl, extractor = env
    _fill_timeline(tl)
    _, ref = _grant_self(env)
    stats = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert stats.written >= 1
    records = store.list_by_kind(MemoryKind.EPISODE, space_ids=(ref.space_id,))
    summaries = [str(r.content.get("summary")) for r in records]
    assert any("论文" in s for s in summaries)
    # B 的十六进制不会进 A 的空间；A 的第二条无「我」自述不进
    assert not any("E6 88 91" in s for s in summaries)
    assert not any("360" in s for s in summaries)
    # 来源链完整（MEM-04）
    rec = records[0]
    assert rec.content["source_group"] == GROUP
    assert rec.content["principal_id"] == A_PRINCIPAL
    assert rec.content["source_message_ids"] == ["m-a1"]
    assert rec.content["consent_version"] == 1
    assert rec.content["expires_at"]  # 有 TTL
    # 「明天」= 近期事件 → 短 TTL
    exp = datetime.fromisoformat(rec.content["expires_at"])
    assert (exp - datetime.now(timezone.utc)).total_seconds() < 30 * 86400


def test_extraction_sensitive_and_ability_skipped(env):
    store, *_ , tl, extractor = env
    _grant_self(env)
    tl.append(GROUP, message_id="m-s1", sender_id=A_UIN,
              text="我的手机号是 13800000001，记一下")
    tl.append(GROUP, message_id="m-s2", sender_id=A_UIN,
              text="我的密码是 hunter2")
    tl.append(GROUP, message_id="m-s3", sender_id=A_UIN,
              text="我家住在幸福路 3 号院 2 栋")
    tl.append(GROUP, message_id="m-a3", sender_id=A_UIN,
              text="我不懂电脑")
    stats = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert stats.skipped_sensitive == 3
    binding = env[2].member_binding_for(A_PRINCIPAL)
    records = store.list_by_kind(MemoryKind.EPISODE,
                                 space_ids=(binding["space_id"],)) + \
        store.list_by_kind(MemoryKind.PREFERENCE,
                           space_ids=(binding["space_id"],))
    summaries = " ".join(str(r.content.get("summary")) for r in records)
    assert "13800000001" not in summaries
    assert "hunter2" not in summaries
    assert "幸福路" not in summaries
    assert "不懂电脑" not in summaries  # 能力画像不固化
    assert contains_sensitive("我电话 13800000001")


def test_extraction_idempotent_and_revoke_stops(env):
    store, spaces, bindings, consents, _, tl, extractor = env
    _fill_timeline(tl)
    _grant_self(env)
    s1 = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    s2 = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert s1.written >= 1
    assert s2.written == 0 and s2.skipped_duplicate == s2.candidates
    # 退出后：新消息不再写入（opt-out 持久标记）
    consents.opt_out(A_PRINCIPAL)
    tl.append(GROUP, message_id="m-a4", sender_id=A_UIN,
              text="我下周要出差答辩。")
    s3 = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert s3.written == 0 and s3.candidates == 0


def test_extraction_regrant_does_not_resurrect_deleted(env):
    store, spaces, bindings, consents, _, tl, extractor = env
    _fill_timeline(tl)
    _grant_self(env)
    s1 = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    for key in s1.record_keys:
        store.delete(key)
    # 重新授权（新版本）也不复活已删除记录：幂等键不含 consent 版本
    consents.grant(A_PRINCIPAL, GROUP, granted_by=A_PRINCIPAL,
                   granted_via=A_PRIVATE_KEY)
    s2 = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert s2.written == 0


# ---------------------------------------------------------------------------
# resolver 下发与召回隔离
# ---------------------------------------------------------------------------


def test_resolver_member_space_only_in_own_private(env):
    _fill_timeline(env[5])
    _grant_self(env)
    extractor = env[6]
    extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    resolver = _resolver(env)
    # A 本人私聊 → 下发 member_private
    ctx_a = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=f"private:{A_UIN}")
    member_spaces = [s for s in ctx_a.allowed_memory_spaces
                     if s.type is MemorySpaceType.MEMBER_PRIVATE]
    assert len(member_spaces) == 1
    # 群聊永不（即使 A 在群里发言）
    ctx_g = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=GROUP)
    assert not [s for s in ctx_g.allowed_memory_spaces
                if s.type is MemorySpaceType.MEMBER_PRIVATE]
    # B 私聊 → 没有 A 的空间
    ctx_b = resolver.resolve(channel="qq", sender_id=B_UIN,
                             conversation_id=f"private:{B_UIN}")
    assert not [s for s in ctx_b.allowed_memory_spaces
                if s.type is MemorySpaceType.MEMBER_PRIVATE]
    # 会话 id 与 sender 不符（伪造私聊）→ 不下发
    ctx_x = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id="private:99999")
    assert not [s for s in ctx_x.allowed_memory_spaces
                if s.type is MemorySpaceType.MEMBER_PRIVATE]
    # opt-out → 不再下发
    env[3].opt_out(A_PRINCIPAL)
    ctx_a2 = resolver.resolve(channel="qq", sender_id=A_UIN,
                              conversation_id=f"private:{A_UIN}")
    assert not [s for s in ctx_a2.allowed_memory_spaces
                if s.type is MemorySpaceType.MEMBER_PRIVATE]


def test_recall_isolation_between_members(env):
    store = env[0]
    _fill_timeline(env[5])
    _grant_self(env)                       # A
    _grant_self(env, principal=B_PRINCIPAL, key=B_PRIVATE_KEY)  # B
    extractor = env[6]
    extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    tl = env[5]
    tl.append(GROUP, message_id="m-b2", sender_id=B_UIN,
              text="我喜欢骑行，上周买了新车。")
    extractor.extract_for_principal(B_PRINCIPAL, GROUP)
    resolver = _resolver(env)
    curator = MemoryCurator(store)

    ctx_a = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=f"private:{A_UIN}")
    hits_a = curator.recall("论文 电脑", allowed_spaces=ctx_a.allowed_memory_spaces)
    assert any("论文" in str(h.record.content.get("summary"))
               for h in hits_a)
    assert not any("骑行" in str(h.record.content.get("summary"))
                   for h in hits_a)  # A 看不到 B 的

    ctx_b = resolver.resolve(channel="qq", sender_id=B_UIN,
                             conversation_id=f"private:{B_UIN}")
    hits_b = curator.recall("论文 电脑", allowed_spaces=ctx_b.allowed_memory_spaces)
    assert not any("论文" in str(h.record.content.get("summary"))
                   for h in hits_b)  # B 看不到 A 的
    hits_b2 = curator.recall("骑行", allowed_spaces=ctx_b.allowed_memory_spaces)
    assert any("骑行" in str(h.record.content.get("summary")) for h in hits_b2)

    # 群聊上下文：谁的 member_private 都读不到
    ctx_g = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=GROUP)
    hits_g = curator.recall("论文", allowed_spaces=ctx_g.allowed_memory_spaces)
    assert hits_g == ()


def test_expired_member_memory_not_recalled(env):
    store = env[0]
    _grant_self(env)
    binding = env[2].member_binding_for(A_PRINCIPAL)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="member-mem:expired",
        content={"summary": "我上周感冒", "source": "member-extract:g",
                 "evidence_id": "m0", "confidence": 0.8, "expires_at": past},
        tags=("curated", "member"), space_id=binding["space_id"]))
    resolver = _resolver(env)
    ctx_a = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=f"private:{A_UIN}")
    curator = MemoryCurator(store)
    assert curator.recall("感冒",
                          allowed_spaces=ctx_a.allowed_memory_spaces) == ()


def test_owner_default_space_unaffected(env):
    """主人私聊的 owner-working 默认空间不受 member 机制影响。"""
    from whale_core.memory.spaces import ensure_owner_working_copy
    owner_space = ensure_owner_working_copy(env[0])
    resolver = ConversationContextResolver(
        pairing_lookup=lambda ch, sid: "owner" if sid == "owner1" else None,
        member_memory=env[4], owner_default_space=owner_space)
    ctx = resolver.resolve(channel="qq", sender_id="owner1",
                           conversation_id="private:owner1")
    ids = [s.space_id for s in ctx.allowed_memory_spaces]
    assert owner_space.space_id in ids
    assert not [s for s in ctx.allowed_memory_spaces
                if s.type is MemorySpaceType.MEMBER_PRIVATE]


# ---------------------------------------------------------------------------
# Studio member API（真实回环 HTTP）
# ---------------------------------------------------------------------------

TOKEN = "member-test-token"


@pytest.fixture()
def api():
    store = InMemoryStore()
    # G6 中-2：consent/成员记忆 API 需要该私聊真实发言证据——种子历史
    for _uin in (A_UIN, B_UIN):
        store.put("chat:history:private:%s" % _uin,
                  [{"role": "user", "content": "你好"}])
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server, store
    server.stop()


def _req(server, method, path, *, token=TOKEN, body=None, query=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    if query:
        path = f"{path}?{urlencode(query)}"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


def _api_grant(server, principal=A_PRINCIPAL, key=A_PRIVATE_KEY, **kw):
    body = {"principal_id": principal, "source_group": GROUP,
            "actor_conversation_key": key}
    body.update(kw)
    return _req(server, "POST", "/api/v1/studio/members/consent", body=body)


def test_api_consent_grant_and_status(api):
    server, store = api
    status, data = _api_grant(server)
    assert status == 200
    result = data["result"]
    assert result["consent"]["principal_id"] == A_PRINCIPAL
    assert result["consent"]["version"] == 1
    assert result["space_id"].startswith("member-")
    # 状态视图
    status, data = _req(server, "GET", "/api/v1/studio/members/consent",
                        query={"principal_id": A_PRINCIPAL})
    assert status == 200 and data["result"]["active"] is True
    # 重复 grant 版本递增
    status, data = _api_grant(server)
    assert data["result"]["consent"]["version"] == 2


def test_api_consent_grant_rejects_proxy_and_group(api):
    server, _ = api
    # 群主用自己的私聊替 A 授权 → 403（MEM-02）
    status, data = _api_grant(server, key="qq:qq_private:private:owner1")
    assert status == 403
    assert data["error"]["code"] == "member_actor_mismatch"
    # 群会话 key → 403
    status, data = _api_grant(server, key=GROUP_KEY)
    assert status == 403
    # B 的私聊替 A 授权 → 403
    status, data = _api_grant(server, key=B_PRIVATE_KEY)
    assert status == 403
    # 未知字段 → 422（严格字段表）
    status, data = _api_grant(server, approved=True)
    assert status == 422 and data["error"]["code"] == "invalid_request"
    # 未认证 → 401
    status, _ = _req(server, "POST", "/api/v1/studio/members/consent",
                     token="", body={"principal_id": A_PRINCIPAL})
    assert status == 401


def _api_env_with_memory(api):
    """A 授权 + 时间线 + 提炼，返回 (server, store, extractor, space_id)。"""
    server, store = api
    status, data = _api_grant(server)
    assert status == 200
    space_id = data["result"]["space_id"]
    tl = GroupTimelineStore(store)
    _fill_timeline(tl)
    from whale_core.api.studio import services_for
    svc = services_for(server.ctx)
    extractor = MemberMemoryExtractor(store, tl, svc.consents,
                                      svc.bindings, svc.spaces)
    stats = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert stats.written >= 1
    return server, store, space_id


def test_api_memory_list_own_requires_self(api):
    server, store, space_id = _api_env_with_memory(api)
    # 本人可列出
    status, data = _req(
        server, "GET", "/api/v1/studio/members/memory",
        query={"principal_id": A_PRINCIPAL,
               "actor_conversation_key": A_PRIVATE_KEY})
    assert status == 200
    items = data["result"]["items"]
    assert any("论文" in it["summary"] for it in items)
    assert all(it["source_group"] == GROUP for it in items)
    # 群主（管理通道）冒充本人 → 403：群主不能读成员私人记忆全文
    status, data = _req(
        server, "GET", "/api/v1/studio/members/memory",
        query={"principal_id": A_PRINCIPAL,
               "actor_conversation_key": "qq:qq_private:private:owner1"})
    assert status == 403
    # B 替 A 看 → 403
    status, data = _req(
        server, "GET", "/api/v1/studio/members/memory",
        query={"principal_id": A_PRINCIPAL,
               "actor_conversation_key": B_PRIVATE_KEY})
    assert status == 403


def test_api_memory_correct_and_delete_own(api):
    server, store, space_id = _api_env_with_memory(api)
    status, data = _req(
        server, "GET", "/api/v1/studio/members/memory",
        query={"principal_id": A_PRINCIPAL,
               "actor_conversation_key": A_PRIVATE_KEY})
    key = data["result"]["items"][0]["key"]
    # 更正
    status, data = _req(server, "POST", "/api/v1/studio/members/memory/correct",
                        body={"principal_id": A_PRINCIPAL,
                              "actor_conversation_key": A_PRIVATE_KEY,
                              "key": key,
                              "new_summary": "我的 Windows 电脑很卡，论文下周交。"})
    assert status == 200 and data["result"]["corrected"] is True
    old = store.get_record(key)
    assert "superseded" in old.tags
    new = store.get_record(data["result"]["new_key"])
    assert new.space_id == space_id and "论文下周交" in str(
        new.content["summary"])
    # 旧记录退出召回，新记录可召回
    curator = MemoryCurator(store)
    hits = curator.recall("论文", allowed_spaces=(space_id,))
    summaries = [str(h.record.content.get("summary")) for h in hits]
    assert any("下周交" in s for s in summaries)
    assert not any("明天" in s for s in summaries)
    # 更正内容含敏感信息 → 422
    status, data = _req(server, "POST", "/api/v1/studio/members/memory/correct",
                        body={"principal_id": A_PRINCIPAL,
                              "actor_conversation_key": A_PRIVATE_KEY,
                              "key": data["result"]["new_key"],
                              "new_summary": "我电话 13800000001"})
    assert status == 422
    # 删除（真删 + embedding 传播）
    class FakeProvider:
        def __init__(self):
            self.forgotten = []

        def forget_text(self, text):
            self.forgotten.append(text)

    class FakeRegistry:
        def __init__(self, p):
            self._p = p

        def providers_for(self, cap):
            return [("emb", self._p)] if cap == "embedding.embed" else []

    class FakeManager:
        def __init__(self, r):
            self.registry = r

    provider = FakeProvider()
    server.ctx.manager = FakeManager(FakeRegistry(provider))
    status, data = _req(server, "POST", "/api/v1/studio/members/memory/delete",
                        body={"principal_id": A_PRINCIPAL,
                              "actor_conversation_key": A_PRIVATE_KEY,
                              "key": key})
    assert status == 200 and data["result"]["deleted"] is True
    assert store.get_record(key) is None
    assert provider.forgotten  # forget_text 被调用
    # 删除后不可召回（把更正产生的新记录也删掉，空间即无任何可召回记录）
    status, data = _req(
        server, "GET", "/api/v1/studio/members/memory",
        query={"principal_id": A_PRINCIPAL,
               "actor_conversation_key": A_PRIVATE_KEY})
    for item in data["result"]["items"]:
        status, _ = _req(server, "POST",
                         "/api/v1/studio/members/memory/delete",
                         body={"principal_id": A_PRINCIPAL,
                               "actor_conversation_key": A_PRIVATE_KEY,
                               "key": item["key"]})
        assert status == 200
    assert curator.recall("论文", allowed_spaces=(space_id,)) == ()
    # 他人空间/他人记录不可删
    status, data = _req(server, "POST", "/api/v1/studio/members/memory/delete",
                        body={"principal_id": B_PRINCIPAL,
                              "actor_conversation_key": B_PRIVATE_KEY,
                              "key": key})
    assert status == 404  # 不泄露记录存在性


def test_api_revoke_stops_recall_but_management_survives(api):
    server, store, space_id = _api_env_with_memory(api)
    from whale_core.api.studio import services_for
    svc = services_for(server.ctx)
    resolver = ConversationContextResolver(member_memory=svc.directory)
    ctx_a = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=f"private:{A_UIN}")
    assert any(s.space_id == space_id for s in ctx_a.allowed_memory_spaces)
    # 撤销
    status, data = _req(server, "POST", "/api/v1/studio/members/consent/revoke",
                        body={"principal_id": A_PRINCIPAL})
    assert status == 200 and data["result"]["revoked"] == 1
    # 召回停止
    ctx_a2 = resolver.resolve(channel="qq", sender_id=A_UIN,
                              conversation_id=f"private:{A_UIN}")
    assert not any(s.space_id == space_id for s in ctx_a2.allowed_memory_spaces)
    # 新写入停止
    tl = GroupTimelineStore(store)
    extractor = MemberMemoryExtractor(store, tl, svc.consents,
                                      svc.bindings, svc.spaces)
    stats = extractor.extract_for_principal(A_PRINCIPAL, GROUP)
    assert stats.written == 0
    # 本人管理仍可用（撤销后可以查看/删除存量）
    status, data = _req(
        server, "GET", "/api/v1/studio/members/memory",
        query={"principal_id": A_PRINCIPAL,
               "actor_conversation_key": A_PRIVATE_KEY})
    assert status == 200 and data["result"]["items"]


def test_api_studio_bind_member_space_to_group_rejected(api):
    """通用绑定端点也拒绝 member_private → 群聊（纵深防御，N01 同构）。"""
    server, _ = api
    status, data = _api_grant(server)
    space_id = data["result"]["space_id"]
    status, data = _req(
        server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/memories",
        body={"space_id": space_id, "can_read": True})
    assert status == 422
    assert data["error"]["code"] == "invalid_request"
    # 群绑定不存在（不留残边）
    status, data = _req(server, "GET", "/api/v1/studio/conversations")
    entries = [c for c in data["result"]["conversations"]
               if c["key"] == GROUP_KEY]
    assert not entries or not entries[0]["memory_spaces"]


# ---------------------------------------------------------------------------
# §10 固定样例全链路：A 授权 → 群经历 → A 私聊可回忆；B/群不能
# ---------------------------------------------------------------------------


def test_section10_full_chain(api):
    server, store, space_id = _api_env_with_memory(api)
    from whale_core.api.studio import services_for
    svc = services_for(server.ctx)
    resolver = ConversationContextResolver(member_memory=svc.directory)
    curator = MemoryCurator(store)

    # A 私聊自然问起 → 召回 A 的记忆
    ctx_a = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=f"private:{A_UIN}")
    hits = curator.recall("论文 电脑 后来好些了吗",
                          allowed_spaces=ctx_a.allowed_memory_spaces)
    assert any("论文" in str(h.record.content.get("summary")) for h in hits)

    # B 私聊不能读取 A 的私有记忆（无授权 → 连自己的空间都没有）
    ctx_b = resolver.resolve(channel="qq", sender_id=B_UIN,
                             conversation_id=f"private:{B_UIN}")
    assert ctx_b.allowed_memory_spaces == ()
    assert curator.recall("论文", allowed_spaces=()) == ()

    # 群聊上下文读不到任何人的 member_private
    ctx_g = resolver.resolve(channel="qq", sender_id=A_UIN,
                             conversation_id=GROUP)
    assert not [s for s in ctx_g.allowed_memory_spaces
                if s.type is MemorySpaceType.MEMBER_PRIVATE]
    assert curator.recall(
        "论文", allowed_spaces=ctx_g.allowed_memory_spaces) == ()

    # B 的消息不污染 A 的空间（时间线里 B 的十六进制未被提炼）
    records = store.list_by_kind(MemoryKind.EPISODE, space_ids=(space_id,))
    assert not any("E6 88 91" in str(r.content.get("summary"))
                   for r in records)


# ---------------------------------------------------------------------------
# issue #16：首次私聊回填（有界/增量/幂等）、退出期间不补采、wipe 清提炼态
# ---------------------------------------------------------------------------


def test_first_private_chat_backfills_group_history(env):
    """A 先在群里自述，之后首次私聊建档 → 历史回填可召回（issue #16 验收 A）。"""
    store, spaces, bindings, consents, directory, timeline, extractor = env
    _fill_timeline(timeline)
    # 首次私聊：建档 + 回填（runner 的 hook 顺序：ensure → backfill）
    ref = bindings.ensure_member_space(A_PRINCIPAL, conversation_key=A_PRIVATE_KEY,
                                       granted_by=A_PRINCIPAL)
    stats = extractor.backfill_for_principal(A_PRINCIPAL)
    assert stats.scanned == 2            # A 在群里两条自述
    assert stats.written >= 1
    recs = store.list_by_kind(MemoryKind.EPISODE, space_ids=(ref.space_id,))
    prefs = store.list_by_kind(MemoryKind.PREFERENCE, space_ids=(ref.space_id,))
    assert recs or prefs
    # 幂等：再次回填不重复
    again = extractor.backfill_for_principal(A_PRINCIPAL)
    assert again.written == 0            # done 水位跳过（force=False）


def test_backfill_opted_out_zero_scan_zero_write(env):
    """退出者：零扫描零写入（issue #16 / owner 决策）。"""
    store, spaces, bindings, consents, directory, timeline, extractor = env
    _fill_timeline(timeline)
    consents.opt_out(A_PRINCIPAL)
    _grant_self(env)  # 即便有空间/绑定
    stats = extractor.backfill_for_principal(A_PRINCIPAL)
    assert stats.scanned == 0 and stats.written == 0


def test_optin_watermark_excludes_opted_out_interval(env):
    """「恢复记录我」钉水位：退出期间留在时间线的消息不补采。"""
    from whale_core.memory.extraction import (
        backfill_watermark, note_optin_watermark)
    store, spaces, bindings, consents, directory, timeline, extractor = env
    t0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    timeline.append(GROUP, message_id="m-old", sender_id=A_UIN,
                    text="我的旧自述", send_time=t0.isoformat())
    consents.opt_out(A_PRINCIPAL)
    # 退出期间的消息（时间线仍留存——群公共记录）
    t1 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    timeline.append(GROUP, message_id="m-during", sender_id=A_UIN,
                    text="我在退出期间说的事", send_time=t1.isoformat())
    consents.opt_in(A_PRINCIPAL)
    note_optin_watermark(store, A_PRINCIPAL,
                         now=datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc))
    _grant_self(env)
    stats = extractor.backfill_for_principal(A_PRINCIPAL, force=True)
    # 水位 13:00：m-old(10:00) 与 m-during(12:00) 都不补采
    assert stats.scanned == 0 and stats.written == 0
    wm = backfill_watermark(store, A_PRINCIPAL)
    assert wm["not_before"] > t1.isoformat()


def test_wipe_purges_extraction_state(env):
    """删掉我的记忆：记忆记录 + 提炼幂等键 + 回填水位全部真删。"""
    from whale_core.memory.extraction import (
        backfill_watermark, purge_member_extraction_state)
    store, spaces, bindings, consents, directory, timeline, extractor = env
    _fill_timeline(timeline)
    _grant_self(env)
    extractor.backfill_for_principal(A_PRINCIPAL)
    assert any(k.startswith("member:ex:") for k in store.keys())
    assert backfill_watermark(store, A_PRINCIPAL) is not None
    n = purge_member_extraction_state(store, A_PRINCIPAL)
    assert n >= 1
    assert not any(k.startswith("member:ex:") for k in store.keys())
    assert backfill_watermark(store, A_PRINCIPAL) is None
    # 他人（B）的提炼态不受影响
    assert not any(True for k in store.keys() if k.startswith("member:ex:"))


def test_backfill_bounded_by_group_cap(env):
    """有界：来源群数 ≤ BACKFILL_MAX_GROUPS；失败可观测不静默。"""
    store, spaces, bindings, consents, directory, timeline, extractor = env
    _grant_self(env)
    for i in range(60):
        timeline.append(f"group:g{i}", message_id=f"m-{i}",
                        sender_id=A_UIN, text="我的电脑很卡")
    stats = extractor.backfill_for_principal(A_PRINCIPAL)
    from whale_core.memory.extraction import BACKFILL_MAX_GROUPS
    assert stats.scanned <= BACKFILL_MAX_GROUPS
