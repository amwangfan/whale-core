"""人物卡管理 API 测试（owner 决策 2026-11-01：工作台可添加/修改人物卡）。

真实回环 HTTP（127.0.0.1 + 临时端口），不触外网。覆盖：
- POST /studio/personas 新建自定义卡（自动/指定 card_id）；
- PUT /studio/personas/{id} 修改自定义卡；内置卡不可改（403）；
- 严格字段表：未知字段 422；类型/取值校验（speaking_rate 区间等）；
- 卡可立即绑定到会话；活跃卡被修改时连续性快照当场重渲；
- 列表视图不回显 external_prompt（仅 has_external_prompt 标记），
  详情视图（owner 管理面）含完整编辑字段。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.store import InMemoryStore
from whale_core.ui.persona_cards import (
    CARD_KEY_PREFIX,
    MAX_EXTERNAL_PROMPT_CHARS,
    switch_card,
)

TOKEN = "persona-admin-test-token"
GROUP_KEY = "qq:qq_group:group:1001"


@contextmanager
def _server():
    store = InMemoryStore()
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        yield server, store
    finally:
        server.stop()


def _req(server, method, path, *, token=TOKEN, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    data = json.loads(raw)
    # 成功包络 {"result": ...}；错误包络 {"error": {...}} 保持原样。
    return resp.status, data.get("result", data)


def _create(server, **fields):
    body = {"title": "测试卡", **fields}
    return _req(server, "POST", "/api/v1/studio/personas", body=body)


# ---------------------------------------------------------------------------
# 新建
# ---------------------------------------------------------------------------


def test_create_persona_minimal():
    with _server() as (server, store):
        status, data = _create(server, summary="安静的卡。")
        assert status == 201, data
        persona = data["persona"]
        assert persona["title"] == "测试卡"
        assert persona["builtin"] is False
        assert persona["id"].startswith("custom-")
        assert persona["summary"] == "安静的卡。"
        # 持久化到 persona:card:{id}
        assert store.get(f"{CARD_KEY_PREFIX}{persona['id']}")


def test_create_persona_with_explicit_id_and_full_fields():
    with _server() as (server, _):
        status, data = _create(
            server, card_id="my-card", name="小鲸", tone="温柔",
            address_user_as="主人", style_rules=["句尾加波浪号"],
            extra_taboos=["不谈论政治"], speaking_rate=1.2,
            temperament="慢热", relationship_style="熟人亲近",
            voice_hint="soft", external_prompt="原文……")
        assert status == 201, data
        p = data["persona"]
        assert p["id"] == "my-card"
        assert p["name"] == "小鲸"
        assert p["style_rules"] == ["句尾加波浪号"]
        assert p["extra_taboos"] == ["不谈论政治"]
        assert p["speaking_rate"] == 1.2
        assert p["external_prompt"] == "原文……"


def test_create_persona_appears_in_listing():
    with _server() as (server, _):
        _create(server, card_id="listed-card")
        status, data = _req(server, "GET", "/api/v1/studio/personas")
        assert status == 200
        ids = [p["id"] for p in data["personas"]]
        assert "listed-card" in ids
        listed = next(p for p in data["personas"] if p["id"] == "listed-card")
        assert listed["builtin"] is False


def test_create_persona_rejects_builtin_id():
    with _server() as (server, _):
        status, data = _create(server, card_id="default")
        assert status == 409
        assert data["error"]["code"] == "builtin_immutable"


def test_create_persona_rejects_duplicate_id():
    with _server() as (server, _):
        status, _ = _create(server, card_id="dup-card")
        assert status == 201
        status, data = _create(server, card_id="dup-card")
        assert status == 409
        assert data["error"]["code"] == "duplicate_card"


def test_create_persona_requires_title():
    with _server() as (server, _):
        status, _ = _req(server, "POST", "/api/v1/studio/personas",
                         body={"title": "  "})
        assert status == 422


def test_create_persona_rejects_unknown_field():
    with _server() as (server, _):
        status, data = _create(server, allowed_scopes=["*"])
        assert status == 422
        assert "未知字段" in data["error"]["message"]


def test_create_persona_rejects_bad_card_id():
    with _server() as (server, _):
        status, _ = _create(server, card_id="Bad_ID!")
        assert status == 422


def test_create_persona_rejects_bad_speaking_rate():
    with _server() as (server, _):
        status, _ = _create(server, speaking_rate=3.0)
        assert status == 422
        status, _ = _create(server, speaking_rate="fast")
        assert status == 422


def test_create_persona_truncates_external_prompt():
    with _server() as (server, _):
        status, data = _create(
            server, external_prompt="x" * (MAX_EXTERNAL_PROMPT_CHARS + 100))
        assert status == 201
        assert len(data["persona"]["external_prompt"]) == MAX_EXTERNAL_PROMPT_CHARS


# ---------------------------------------------------------------------------
# 详情 / 列表视图纪律
# ---------------------------------------------------------------------------


def test_detail_view_contains_edit_fields_list_view_does_not():
    with _server() as (server, _):
        _create(server, card_id="detail-card", external_prompt="内部原文")
        status, data = _req(
            server, "GET", "/api/v1/studio/personas/detail-card")
        assert status == 200
        assert data["persona"]["external_prompt"] == "内部原文"
        assert data["persona"]["builtin"] is False

        status, data = _req(server, "GET", "/api/v1/studio/personas")
        listed = next(p for p in data["personas"] if p["id"] == "detail-card")
        assert "external_prompt" not in listed
        assert listed["has_external_prompt"] is True


def test_detail_404_for_unknown_card():
    with _server() as (server, _):
        status, _ = _req(server, "GET", "/api/v1/studio/personas/ghost")
        assert status == 404


# ---------------------------------------------------------------------------
# 修改
# ---------------------------------------------------------------------------


def test_update_persona_partial_fields():
    with _server() as (server, store):
        _create(server, card_id="edit-card", summary="旧设定", name="旧名")
        status, data = _req(server, "PUT", "/api/v1/studio/personas/edit-card",
                            body={"summary": "新设定"})
        assert status == 200, data
        assert data["persona"]["summary"] == "新设定"
        assert data["persona"]["name"] == "旧名"  # 未提交字段保持不变
        stored = store.get(f"{CARD_KEY_PREFIX}edit-card")
        assert stored["summary"] == "新设定" and stored["name"] == "旧名"


def test_update_persona_rejects_builtin():
    with _server() as (server, _):
        status, data = _req(server, "PUT", "/api/v1/studio/personas/default",
                            body={"summary": "x"})
        assert status == 403
        assert data["error"]["code"] == "builtin_immutable"


def test_update_persona_404_for_unknown():
    with _server() as (server, _):
        status, _ = _req(server, "PUT", "/api/v1/studio/personas/ghost",
                         body={"summary": "x"})
        assert status == 404


def test_update_persona_rejects_empty_fields_and_blank_title():
    with _server() as (server, _):
        _create(server, card_id="edit2")
        status, _ = _req(server, "PUT", "/api/v1/studio/personas/edit2",
                         body={})
        assert status == 422
        status, _ = _req(server, "PUT", "/api/v1/studio/personas/edit2",
                         body={"title": "  "})
        assert status == 422


def test_update_persona_rejects_unknown_field():
    with _server() as (server, _):
        _create(server, card_id="edit3")
        status, data = _req(server, "PUT", "/api/v1/studio/personas/edit3",
                            body={"permissions": ["all"]})
        assert status == 422
        assert "未知字段" in data["error"]["message"]


# ---------------------------------------------------------------------------
# 与既有链路联动：新卡可绑定；活跃卡修改当场重渲快照
# ---------------------------------------------------------------------------


def test_created_card_is_bindable():
    with _server() as (server, _):
        _create(server, card_id="bindable-card")
        status, data = _req(
            server, "PUT",
            f"/api/v1/studio/conversations/{GROUP_KEY}/persona",
            body={"persona_id": "bindable-card"})
        assert status == 200, data
        assert data["persona_id"] == "bindable-card"


def test_updating_active_card_rerenders_continuity_snapshot():
    with _server() as (server, store):
        _create(server, card_id="active-card", tone="旧语气", name="小鲸")
        switch_card(store, "active-card")
        before = store.get_persona_continuity()
        assert before is not None and "旧语气" in str(
            before.traits.get("system_preamble", ""))

        status, _ = _req(server, "PUT", "/api/v1/studio/personas/active-card",
                         body={"tone": "新语气"})
        assert status == 200
        after = store.get_persona_continuity()
        assert "新语气" in str(after.traits.get("system_preamble", ""))
        assert "旧语气" not in str(after.traits.get("system_preamble", ""))
