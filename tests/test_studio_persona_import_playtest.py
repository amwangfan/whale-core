"""UX-02（issue #37）：人物卡导入预览与即时试玩 API 测试。

- 导入：SillyTavern v2/v1 JSON、纯文本启发式；字段映射/缺失/冲突/未识别
  字段全部如实展示；预览不写任何数据。
- 试玩：真实有效人格快照（同一渲染链路）+ 注入假模型；默认人格 vs 试用卡
  同输入对比；模型失败/未装配如实报错；不写记忆不进历史。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection
from types import SimpleNamespace

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.store import InMemoryStore
from whale_core.ui.persona_cards import CARD_KEY_PREFIX, PersonaCard

TOKEN = "import-playtest-test-token"

V2_CARD = json.dumps({
    "spec": "chara_card_v2",
    "data": {
        "name": "Miko",
        "description": "一只来自深海的安静鲸鱼，喜欢倾听。",
        "personality": "安静、细腻、慢热",
        "scenario": "深夜的书房",
        "first_mes": "晚上好，今晚想聊点什么？",
        "mes_example": "user: 你好\nchar: 嗯，我在。慢慢说。",
        "custom_unknown_field": "某某",
    },
}, ensure_ascii=False)


class _FakeModel:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append({"text": text, "persona": persona})
        if self.fail:
            raise RuntimeError("boom")
        marker = "miko" if "Miko" in persona else "default"
        return f"[{marker}] 收到。"


@contextmanager
def _server(model=None):
    store = InMemoryStore()
    ctx = ApiContext(store=store)
    if model is not None:
        ctx.orchestrator = SimpleNamespace(
            store=store,
            companion=SimpleNamespace(model=model))
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        yield server, store
    finally:
        server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


class TestImportPreview:
    def test_v2_card_mapping(self):
        with _server() as (server, store):
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/import-preview",
                                body={"text": V2_CARD})
            assert status == 200
            r = body["result"]
            assert r["parsed"]["name"] == "Miko"
            assert r["parsed"]["temperament"] == "安静、细腻、慢热"
            assert "慢慢说" in r["parsed"]["example_dialogue"]
            assert "深海" in r["parsed"]["summary"]
            assert "深夜的书房" in r["parsed"]["external_prompt"]
            assert r["mapping"]["personality"] == "temperament"
            # 未识别字段不静默丢弃
            assert any(u["field"] == "custom_unknown_field"
                       for u in r["unmapped"])
            # first_mes 明示不进运行
            assert any(u["field"] == "first_mes" for u in r["unmapped"])
            assert r["requires_confirmation"] is True
            # 预览不落库
            assert not [k for k in store.keys() if k.startswith(CARD_KEY_PREFIX)]

    def test_conflict_detected(self):
        with _server() as (server, store):
            store.put(f"{CARD_KEY_PREFIX}miko",
                      PersonaCard(card_id="miko", title="已有").as_dict())
            _, body = _req(server, "POST",
                           "/api/v1/studio/personas/import-preview",
                           body={"text": V2_CARD})
            assert any("miko" in c for c in body["result"]["conflicts"])

    def test_plain_text_heuristic(self):
        text = "一只稳重的老鲸鱼\n性格：慢条斯理，先听后说\n相处：像长辈一样"
        with _server() as (server, _):
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/import-preview",
                                body={"text": text})
            assert status == 200
            r = body["result"]
            assert r["parsed"]["summary"] == "一只稳重的老鲸鱼"
            assert "慢条斯理" in r["parsed"]["temperament"]
            assert r["warnings"]  # 纯文本启发式有提示
            assert r["parsed"]["external_prompt"] == text

    def test_invalid_input_errors_clearly(self):
        with _server() as (server, _):
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/import-preview",
                                body={"text": ""})
            assert status == 422
            assert "text" in body["error"]["message"]


class TestPlaytest:
    def test_compare_baseline_and_saved_card(self):
        model = _FakeModel()
        with _server(model) as (server, store):
            store.put(f"{CARD_KEY_PREFIX}miko", PersonaCard(
                card_id="miko", title="Miko", name="Miko",
                summary="安静的深海鲸鱼", example_dialogue="char: 嗯，我在。",
            ).as_dict())
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/playtest",
                                body={"persona_id": "miko"})
            assert status == 200
            r = body["result"]
            assert r["input"]  # 固定场景输入
            assert "[default]" in r["baseline"]["reply"]
            assert "[miko]" in r["trial"]["reply"]
            assert r["trial"]["content_sha"] != r["baseline"]["content_sha"]
            assert "example_dialogue" in r["trial"]["fields_used"]
            # 两次模型调用人格文本不同，输入文本一致（同输入对比）
            assert len(model.calls) == 2
            assert model.calls[0]["text"] == model.calls[1]["text"]
            assert "Miko" not in model.calls[0]["persona"]
            assert "Miko" in model.calls[1]["persona"]

    def test_unsaved_draft_card(self):
        model = _FakeModel()
        with _server(model) as (server, store):
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/playtest",
                                body={"card": {"name": "Miko",
                                               "summary": "草稿卡"}})
            assert status == 200
            r = body["result"]
            assert r["trial"]["source"] == "unsaved_draft"
            assert "[miko]" in r["trial"]["reply"]
            # 草稿试玩不落库
            assert not [k for k in store.keys() if k.startswith(CARD_KEY_PREFIX)]

    def test_model_failure_explicit(self):
        model = _FakeModel(fail=True)
        with _server(model) as (server, store):
            store.put(f"{CARD_KEY_PREFIX}miko",
                      PersonaCard(card_id="miko", title="M").as_dict())
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/playtest",
                                body={"persona_id": "miko"})
            assert status == 200
            r = body["result"]
            assert "error" in r["trial"] and r["trial"]["error"]
            assert "reply" not in r["trial"]

    def test_no_orchestrator_503(self):
        with _server() as (server, _):
            status, body = _req(server, "POST",
                                "/api/v1/studio/personas/playtest",
                                body={"persona_id": "miko"})
            assert status == 503
            assert "模型未装配" in body["error"]["message"]
