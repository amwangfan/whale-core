"""S5 Studio 关系图 API 测试（Whale Studio）。

真实回环 HTTP（127.0.0.1 + 临时端口），不触外网。覆盖 goal-chain S5 验收：
- 绑定 API 的版本乐观锁 / 幂等重放 / 稳定机器码拒绝；
- N01：protected_original 直连 403 且不留边；
- Fork preview→fork 全流程（不自动连线），副本端到 resolver 生效；
- 能力配置不超上限（scope_ceiling）；一次性口令 / 租约管理面；
- 审计事件只含元数据（INV-10）。
"""

from __future__ import annotations

import json
from http.client import HTTPConnection

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.life.context_resolver import ConversationContextResolver
from whale_core.life.conversation_context import (
    ConversationKind,
    MemorySpaceType,
)
from whale_core.life.temporary_auth import TemporaryAuthService
from whale_core.memory.bindings import BindingStore
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.ui.persona_cards import import_external_card

TOKEN = "studio-test-token"

GROUP_KEY = "qq:qq_group:group:1001"
PRIVATE_KEY = "qq:qq_private:private:u_owner"


@pytest.fixture()
def api():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    # 迁移管道建 vault（protected_original 只能这样来）
    store.put("space:vault-1", {
        "space_id": "vault-1", "type": "protected_original",
        "name": "默认私人原始库", "owner": "owner", "version": 1,
        "created_at": "t", "retired_at": None})
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="v-secret-1",
        content={"summary": "私密：电话 13800000001，邮箱 whale@example.com",
                 "importance": 0.9, "confidence": 1.0},
        tags=("curated",), space_id="vault-1"))
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="v-secret-2",
        content={"summary": "课程笔记：深度学习", "importance": 0.8,
                 "confidence": 1.0},
        tags=("curated",), space_id="vault-1"))
    # 一个普通群空间
    spaces.create(MemorySpaceType.GROUP_SPACE, name="群记忆A", space_id="g-1")
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="g-rec-1",
        content={"summary": "群A的记录", "importance": 0.5, "confidence": 1.0},
        tags=("curated",), space_id="g-1"))
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server, store
    server.stop()


def _req(server, method, path, *, token=TOKEN, body=None, content_type=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = content_type or "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def _json(raw):
    return json.loads(raw.decode("utf-8"))


def _err_code(raw):
    return _json(raw)["error"]["code"]


# ---------------------------------------------------------------------------
# 鉴权 / 路由骨架
# ---------------------------------------------------------------------------


def test_no_token_401(api):
    server, _ = api
    status, raw = _req(server, "GET", "/api/v1/studio/graph", token="")
    assert status == 401
    assert _json(raw)["error"]["code"] == "unauthorized"


def test_unknown_studio_route_404(api):
    server, _ = api
    status, raw = _req(server, "GET", "/api/v1/studio/nope")
    assert status == 404
    assert _json(raw)["error"]["code"] == "not_found"


def test_method_not_allowed_405(api):
    server, _ = api
    status, raw = _req(server, "GET", "/api/v1/studio/forks")
    assert status == 405
    assert _json(raw)["error"]["code"] == "method_not_allowed"


def test_write_requires_json_content_type(api):
    server, _ = api
    # 表单式 CSRF：有 body 但 Content-Type 不是 JSON → 415
    status, raw = _req(
        server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/persona",
        body={"persona_id": "default"}, content_type="text/plain")
    assert status == 415
    assert _json(raw)["error"]["code"] == "invalid_request"


def test_invalid_conversation_key_422(api):
    server, _ = api
    for bad in ("nocolon", "qq:qq_group:", "qq:nope_kind:g:1", "!!:qq_group:g:1"):
        status, raw = _req(
            server, "PUT", f"/api/v1/studio/conversations/{bad}/persona",
            body={"persona_id": "default"})
        assert status == 422, bad
        assert _json(raw)["error"]["code"] == "invalid_request", bad


# ---------------------------------------------------------------------------
# 角色卡列表 / 会话角色绑定
# ---------------------------------------------------------------------------


def test_personas_list_minimal_fields(api):
    server, store = api
    import_external_card(store, card_id="ext-1", title="外部卡",
                         prompt_text="原文 prompt 不得回显")
    status, raw = _req(server, "GET", "/api/v1/studio/personas")
    assert status == 200
    cards = _json(raw)["result"]["personas"]
    ids = {c["id"] for c in cards}
    assert {"default", "dafeiyu", "ext-1"} <= ids
    for card in cards:
        # 极简字段；内部 prompt / 规则文本不回显
        assert "external_prompt" not in card
        assert "style_rules" not in card
        assert "extra_taboos" not in card
    ext = next(c for c in cards if c["id"] == "ext-1")
    assert ext["builtin"] is False and ext["has_external_prompt"] is True


def test_persona_bind_version_and_idempotent_replay(api):
    server, _ = api
    path = f"/api/v1/studio/conversations/{GROUP_KEY}/persona"
    status, raw = _req(server, "PUT", path, body={"persona_id": "default"})
    assert status == 200
    result = _json(raw)["result"]
    assert result["version"] == 1 and result["changed"] is True
    # 幂等重放：同值（即使带自己首次的版本号）不产生变化
    status, raw = _req(server, "PUT", path,
                       body={"persona_id": "default", "expected_version": 1})
    result = _json(raw)["result"]
    assert status == 200 and result["changed"] is False
    assert result["version"] == 1
    # 乐观锁：带正确版本换卡 → v2
    status, raw = _req(server, "PUT", path,
                       body={"persona_id": "dafeiyu", "expected_version": 1})
    assert status == 200 and _json(raw)["result"]["version"] == 2
    # 过期版本换卡 → 409 + 回显当前版本
    status, raw = _req(server, "PUT", path,
                       body={"persona_id": "default", "expected_version": 1})
    assert status == 409
    err = _json(raw)["error"]
    assert err["code"] == "version_conflict" and err["current_version"] == 2


def test_persona_bind_unknown_card_422(api):
    server, _ = api
    status, raw = _req(
        server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/persona",
        body={"persona_id": "ghost-card"})
    assert status == 422
    assert _json(raw)["error"]["code"] == "invalid_request"


def test_persona_unbind(api):
    server, _ = api
    path = f"/api/v1/studio/conversations/{GROUP_KEY}/persona"
    _req(server, "PUT", path, body={"persona_id": "default"})
    status, raw = _req(server, "PUT", path, body={"persona_id": None})
    assert status == 200 and _json(raw)["result"]["changed"] is True
    # 重复解绑 = 幂等 no-op
    status, raw = _req(server, "PUT", path, body={"persona_id": None})
    assert status == 200 and _json(raw)["result"]["changed"] is False
    # 会话列表里不再有 persona 绑定（无任何绑定的会话可整项消失）
    status, raw = _req(server, "GET", "/api/v1/studio/conversations")
    convs = {c["key"]: c for c in _json(raw)["result"]["conversations"]}
    assert convs.get(GROUP_KEY) is None or convs[GROUP_KEY]["persona_id"] is None


# ---------------------------------------------------------------------------
# 记忆空间列表 / vault 绑定拒绝（N01）
# ---------------------------------------------------------------------------


def test_memory_spaces_vault_redacted(api):
    server, _ = api
    status, raw = _req(server, "GET", "/api/v1/studio/memory-spaces")
    assert status == 200
    spaces = {s["space_id"]: s for s in _json(raw)["result"]["memory_spaces"]}
    vault = spaces["vault-1"]
    # INV-08：只暴露「存在 + 名称」，无记录数/摘要/owner
    assert vault["restricted"] is True and vault["bindable"] is False
    assert vault["name"] == "默认私人原始库"
    assert "record_count" not in vault and "owner" not in vault
    group = spaces["g-1"]
    assert group["record_count"] == 1 and group["bindable"] is True


def test_bind_vault_403_leaves_no_edge(api):
    server, _ = api
    path = f"/api/v1/studio/conversations/{GROUP_KEY}/memories"
    status, raw = _req(server, "PUT", path, body={"space_id": "vault-1"})
    assert status == 403
    assert _json(raw)["error"]["code"] == "vault_not_bindable"
    # 重复提交 / 换会话同样拒绝
    status, _ = _req(server, "PUT", path, body={"space_id": "vault-1"})
    assert status == 403
    status, _ = _req(
        server, "PUT", f"/api/v1/studio/conversations/{PRIVATE_KEY}/memories",
        body={"space_id": "vault-1"})
    assert status == 403
    # 图上无边（INV-01）
    status, raw = _req(server, "GET", "/api/v1/studio/graph")
    graph = _json(raw)["result"]
    assert not any(e.get("to") == "space:vault-1" for e in graph["edges"])
    # vault 节点带解释且不泄露计数
    vault_node = next(n for n in graph["nodes"] if n["id"] == "space:vault-1")
    assert vault_node.get("restricted") is True
    assert "record_count" not in vault_node
    assert graph["explanations"]["space:vault-1"]["code"] == "vault_not_bindable"


def test_bind_unknown_space_404(api):
    server, _ = api
    status, raw = _req(
        server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/memories",
        body={"space_id": "sp-ghost"})
    assert status == 404
    assert _json(raw)["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Fork：preview → fork → 绑定 → resolver 端到端
# ---------------------------------------------------------------------------


def test_fork_preview_redacts_and_fork_does_not_autowire(api):
    server, store = api
    # 预览：内置脱敏回调掩码手机号/邮箱
    status, raw = _req(server, "POST", "/api/v1/studio/forks/preview",
                       body={"source_space": "vault-1",
                             "record_keys": ["v-secret-1"]})
    assert status == 200
    result = _json(raw)["result"]
    assert result["redact"] == "builtin" and result["count"] == 1
    summary = result["items"][0]["content"]["summary"]
    assert "13800000001" not in summary and "138****0001" in summary
    assert "whale@example.com" not in summary and "wh***@example.com" in summary
    # 未确认 → 422，不产生空间
    status, raw = _req(server, "POST", "/api/v1/studio/forks",
                       body={"source_space": "vault-1",
                             "record_keys": ["v-secret-1"],
                             "target_type": "shareable_fork"})
    assert status == 422
    # 目标为 protected_original → 422（永不产生新 vault）
    status, raw = _req(server, "POST", "/api/v1/studio/forks",
                       body={"source_space": "vault-1",
                             "record_keys": ["v-secret-1"],
                             "target_type": "protected_original",
                             "confirmed": True})
    assert status == 422
    # 确认后 Fork：副本出现在空间列表，不自动连线
    status, raw = _req(server, "POST", "/api/v1/studio/forks",
                       body={"source_space": "vault-1",
                             "record_keys": ["v-secret-1", "v-secret-2"],
                             "target_type": "shareable_fork",
                             "target_name": "课程副本",
                             "confirmed": True})
    assert status == 200
    fork_info = _json(raw)["result"]["fork"]
    new_space = fork_info["space_id"]
    assert fork_info["space_type"] == "shareable_fork"
    assert fork_info["name"] == "课程副本"
    assert fork_info["record_count"] == 2
    assert fork_info["auto_bound"] is False
    # 副本记录物理独立且已脱敏
    copies = store.list_by_kind(MemoryKind.EPISODE, space_ids=(new_space,))
    assert len(copies) == 2
    assert all("13800000001" not in str(c.content) for c in copies)
    # 不自动连线：graph 无指向新空间的边
    status, raw = _req(server, "GET", "/api/v1/studio/graph")
    graph = _json(raw)["result"]
    assert not any(e.get("to") == f"space:{new_space}" for e in graph["edges"])


def test_fork_unknown_source_404(api):
    server, _ = api
    status, raw = _req(server, "POST", "/api/v1/studio/forks/preview",
                       body={"source_space": "nope", "record_keys": ["k"]})
    assert status == 404


def test_bound_copy_reachable_by_resolver(api):
    """绑定副本到会话后，S1 resolver 端到端解析出该空间（不经 API 作弊）。"""
    server, store = api
    # Fork 出副本
    _, raw = _req(server, "POST", "/api/v1/studio/forks",
                  body={"source_space": "vault-1", "record_keys": ["v-secret-2"],
                        "target_type": "shareable_fork", "confirmed": True})
    new_space = _json(raw)["result"]["fork"]["space_id"]
    # 绑定副本 + 角色卡到群会话
    status, _ = _req(
        server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/memories",
        body={"space_id": new_space, "can_read": True})
    assert status == 200
    _req(server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/persona",
         body={"persona_id": "dafeiyu"})
    # 同一 store 上的 resolver（与 profiles 装配同构）读出绑定
    resolver = ConversationContextResolver(
        bindings=BindingStore(store, SpaceStore(store)))
    ctx = resolver.resolve(channel="qq", sender_id="10001",
                           conversation_id="group:1001")
    assert ctx.persona_id == "dafeiyu"
    space_ids = [s.space_id for s in ctx.allowed_memory_spaces]
    assert new_space in space_ids
    # vault 依然不在（MEM-HARD-01）
    assert "vault-1" not in space_ids


def test_memory_bind_version_conflict_and_idempotent_replay(api):
    server, _ = api
    path = f"/api/v1/studio/conversations/{GROUP_KEY}/memories"
    status, raw = _req(server, "PUT", path, body={"space_id": "g-1"})
    assert status == 200
    assert _json(raw)["result"]["version"] == 1
    # 幂等重放（同值，带首次版本号）→ no-op
    status, raw = _req(server, "PUT", path,
                       body={"space_id": "g-1", "expected_version": 1})
    result = _json(raw)["result"]
    assert status == 200 and result["changed"] is False
    assert result["version"] == 1
    # 过期版本改配置 → 409 + 当前版本
    status, raw = _req(server, "PUT", path,
                       body={"space_id": "g-1", "can_write": True,
                             "expected_version": 0})
    assert status == 409
    err = _json(raw)["error"]
    assert err["code"] == "version_conflict" and err["current_version"] == 1
    # 正确版本可改
    status, raw = _req(server, "PUT", path,
                       body={"space_id": "g-1", "can_write": True,
                             "expected_version": 1})
    assert status == 200 and _json(raw)["result"]["version"] == 2
    # 解绑 + 重复解绑幂等
    status, raw = _req(server, "PUT", path,
                       body={"space_id": "g-1", "remove": True})
    assert _json(raw)["result"]["removed"] is True
    status, raw = _req(server, "PUT", path,
                       body={"space_id": "g-1", "remove": True})
    assert _json(raw)["result"]["changed"] is False


# ---------------------------------------------------------------------------
# 能力配置
# ---------------------------------------------------------------------------


def test_capabilities_ceiling_enforced(api):
    server, _ = api
    group_path = f"/api/v1/studio/conversations/{GROUP_KEY}/capabilities"
    # 群上限 GROUP_CEILING=(chat.basic, tool.web.search)：outbox.submit 超限
    status, raw = _req(server, "PUT", group_path,
                       body={"capabilities": ["chat.basic", "outbox.submit"]})
    assert status == 403
    err = _json(raw)["error"]
    assert err["code"] == "scope_ceiling" and err["rejected"] == ["outbox.submit"]
    # 上限内允许
    status, raw = _req(server, "PUT", group_path,
                       body={"capabilities": ["chat.basic", "tool.web.search"]})
    assert status == 200
    # 私聊上限更宽：outbox.submit 允许
    priv_path = f"/api/v1/studio/conversations/{PRIVATE_KEY}/capabilities"
    status, raw = _req(server, "PUT", priv_path,
                       body={"capabilities": ["chat.basic", "outbox.submit"]})
    assert status == 200
    # memory.* 不属于本端点（走 memories 绑定）→ 422
    status, raw = _req(server, "PUT", priv_path,
                       body={"capabilities": ["memory.read:g-1"]})
    assert status == 422
    # 版本冲突
    status, raw = _req(server, "PUT", priv_path,
                       body={"capabilities": ["chat.basic"],
                             "expected_version": 99})
    assert status == 409
    assert _json(raw)["error"]["code"] == "version_conflict"


# ---------------------------------------------------------------------------
# 一次性口令 / 租约管理面
# ---------------------------------------------------------------------------


def test_codes_lifecycle(api):
    server, store = api
    # 创建：明文只在响应出现一次
    status, raw = _req(server, "POST", "/api/v1/auth/temporary/codes",
                       body={"scopes": ["task.read:self"], "ttl_s": 3600,
                             "grant_max_uses": 3})
    assert status == 200
    created = _json(raw)["result"]
    assert created["code"] and created["code_id"].startswith("oc-")
    assert created["proof_strength"] == "high"
    # 列表：无 verifier / 明文
    status, raw = _req(server, "GET", "/api/v1/auth/temporary/codes")
    assert status == 200
    codes = _json(raw)["result"]["codes"]
    mine = next(c for c in codes if c["code_id"] == created["code_id"])
    assert "verifier" not in mine and "code" not in mine
    assert mine["allowed_scopes"] == ["task.read:self"]
    assert created["code"] not in json.dumps(codes, ensure_ascii=False)
    # 撤销 → 200；重复撤销幂等仍 200；不存在的 id → 404
    status, _ = _req(server, "POST",
                     f"/api/v1/auth/temporary/codes/{created['code_id']}/revoke")
    assert status == 200
    status, _ = _req(server, "POST",
                     f"/api/v1/auth/temporary/codes/{created['code_id']}/revoke")
    assert status == 200  # 幂等：重复撤销不产生变化
    status, raw = _req(server, "POST",
                       "/api/v1/auth/temporary/codes/oc-nonexistent/revoke")
    assert status == 404
    assert _json(raw)["error"]["code"] == "not_found"
    # 撤销后的口令不能再验证签租约
    auth = TemporaryAuthService(store)
    assert auth.verify(created["code"], channel="qq", sender_id="u9",
                       conversation_kind=ConversationKind.QQ_PRIVATE,
                       conversation_id="private:u9") is None
    # 已核销的口令不可撤销 → 404
    code2 = auth.create_code()
    grant = auth.verify(code2, channel="qq", sender_id="u9",
                        conversation_kind=ConversationKind.QQ_PRIVATE,
                        conversation_id="private:u9")
    assert grant is not None
    code2_id = next(c["code_id"] for c in auth.list_codes()
                    if c.get("consumed_at"))
    status, _ = _req(server, "POST",
                     f"/api/v1/auth/temporary/codes/{code2_id}/revoke")
    assert status == 404


def test_code_create_rejects_ungrantable_scope(api):
    server, _ = api
    status, raw = _req(server, "POST", "/api/v1/auth/temporary/codes",
                       body={"scopes": ["tool.shell.safe"]})
    assert status == 422
    assert _json(raw)["error"]["code"] == "invalid_request"


def test_code_custom_phrase_strength_gate(api):
    server, _ = api
    # 低熵自定义短语 + 低风险 scope：允许，proof_strength=low
    status, raw = _req(server, "POST", "/api/v1/auth/temporary/codes",
                       body={"custom_phrase": "明月几时有把酒问青天",
                             "scopes": ["task.read:self"]})
    assert status == 200
    assert _json(raw)["result"]["proof_strength"] == "low"
    # 低熵短语 + 超低风险 scope：S4 服务拒绝 → 422
    status, raw = _req(server, "POST", "/api/v1/auth/temporary/codes",
                       body={"custom_phrase": "明月几时有把酒问青天",
                             "scopes": ["tool.web.search"]})
    assert status == 422


def test_grants_list_and_revoke(api):
    server, store = api
    auth = TemporaryAuthService(store)
    _, raw = _req(server, "POST", "/api/v1/auth/temporary/codes",
                  body={"scopes": ["task.read:self"], "grant_max_uses": 2})
    code = _json(raw)["result"]["code"]
    grant = auth.verify(code, channel="qq", sender_id="u1",
                        conversation_kind=ConversationKind.QQ_PRIVATE,
                        conversation_id="private:u1")
    assert grant is not None
    status, raw = _req(server, "GET", "/api/v1/auth/temporary/grants")
    assert status == 200
    grants = _json(raw)["result"]["grants"]
    mine = next(g for g in grants if g["grant_id"] == grant.grant_id)
    assert mine["scopes"] == ["task.read:self"]
    assert mine["conversation_id"] == "private:u1"
    assert mine["remaining_uses"] == 2 and mine["revoked"] is False
    # 撤销 → 立即失效
    status, _ = _req(server, "POST",
                     f"/api/v1/auth/temporary/grants/{grant.grant_id}/revoke")
    assert status == 200
    import time as _t
    assert auth.active_grants("anon:qq:u1", "qq:qq_private:private:u1",
                              now=_t.time()) == ()
    # 重复撤销幂等仍 200；不存在的租约 → 404
    status, _ = _req(server, "POST",
                     f"/api/v1/auth/temporary/grants/{grant.grant_id}/revoke")
    assert status == 200
    status, _ = _req(server, "POST",
                     "/api/v1/auth/temporary/grants/tg-nonexistent/revoke")
    assert status == 404


# ---------------------------------------------------------------------------
# 审计（INV-10）
# ---------------------------------------------------------------------------


def test_audit_trail_metadata_only(api):
    server, store = api
    phrase = "明月几时有把酒问青天"
    _req(server, "POST", "/api/v1/auth/temporary/codes",
         body={"custom_phrase": phrase, "scopes": ["task.read:self"]})
    _req(server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/memories",
         body={"space_id": "vault-1"})  # 拒绝也要留痕
    _req(server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/capabilities",
         body={"capabilities": ["outbox.submit"]})  # scope_ceiling 拒绝
    _req(server, "PUT", f"/api/v1/studio/conversations/{GROUP_KEY}/persona",
         body={"persona_id": "default"})
    audit_keys = [k for k in store.keys() if k.startswith("audit:studio:")]
    assert len(audit_keys) >= 4
    events = [store.get(k) for k in audit_keys]
    blob = json.dumps(events, ensure_ascii=False)
    # 审计不含秘密：口令明文 / 记录正文一律不出现
    assert phrase not in blob
    assert "私密：电话" not in blob
    actions = {e["action"]: e for e in events}
    assert actions["auth.code.create"]["outcome"] == "ok"
    denied = [e for e in events if e["outcome"] == "denied"]
    assert {e["reason"] for e in denied} >= {"vault_not_bindable",
                                             "scope_ceiling"}
    assert actions["persona.bind"]["conversation_key"] == GROUP_KEY


# ---------------------------------------------------------------------------
# Vault 管理管道：记录浏览（Fork 向导数据源）
# ---------------------------------------------------------------------------


def test_vault_records_listing_redacted(api):
    server, store = api
    status, raw = _req(server, "GET",
                       "/api/v1/studio/vault/records?space_id=vault-1")
    assert status == 200
    result = _json(raw)["result"]
    assert result["count"] == 2
    by_key = {i["key"]: i for i in result["items"]}
    assert set(by_key) == {"v-secret-1", "v-secret-2"}
    # 脱敏生效：手机号/邮箱不现原形
    assert "13800000001" not in json.dumps(result, ensure_ascii=False)
    assert "whale@example.com" not in json.dumps(result, ensure_ascii=False)
    assert "****" in by_key["v-secret-1"]["summary"]


def test_vault_records_rejects_non_vault_space(api):
    server, _ = api
    status, raw = _req(server, "GET",
                       "/api/v1/studio/vault/records?space_id=g-1")
    assert status == 404  # 普通空间不走管理管道（也防形状探测）


def test_vault_records_default_space_and_audit(api):
    server, store = api
    status, raw = _req(server, "GET", "/api/v1/studio/vault/records")
    assert status == 404  # 默认 vault-default 在本 fixture 不存在
    status, raw = _req(server, "GET",
                       "/api/v1/studio/vault/records?space_id=vault-1")
    assert status == 200
    audit = [store.get(k) for k in store.keys()
             if k.startswith("audit:studio:")]
    assert any(e["action"] == "vault.records" for e in audit)


def test_vault_records_require_token(api):
    server, _ = api
    status, _ = _req(server, "GET",
                     "/api/v1/studio/vault/records?space_id=vault-1", token="")
    assert status == 401
