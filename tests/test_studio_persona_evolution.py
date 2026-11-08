"""Studio REST: proposal, effective persona, approval and rollback."""
from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.store import InMemoryStore

TOKEN = "persona-evolution-test-token"


@contextmanager
def api():
    store = InMemoryStore()
    server = WhaleApiServer(ApiContext(store=store), port=0, token=TOKEN)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def req(server, method, path, body=None, *, token=TOKEN):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": "Bearer " + token} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = json.loads(response.read())
    conn.close()
    return response.status, data.get("result", data)


def test_propose_approve_and_rollback_via_studio():
    with api() as server:
        status, data = req(server, "POST", "/api/v1/studio/personas", {
            "card_id": "evolving", "title": "鲸鱼", "name": "鲸",
            "tone": "冷静", "style_rules": ["简短"],
        })
        assert status == 201, data
        path = "/api/v1/studio/personas/evolving/evolution"
        status, first = req(server, "GET", path)
        assert status == 200 and first["version"] == 0
        status, draft = req(server, "POST", path + "/proposals", {
            "patch": {"tone": "温柔", "style_rules": ["自然一些"]},
            "evidence_ids": ["chat:explicit-feedback:1"],
            "rationale": "明确反馈后调整表达",
        })
        assert status == 201, draft
        pid = draft["proposal"]["proposal_id"]

        status, before = req(server, "GET",
                             "/api/v1/studio/personas/evolving/effective")
        assert status == 200 and before["effective"]["evolution_version"] == 0
        assert "冷静" in before["effective"]["model_context"]

        status, approved = req(server, "POST", path + "/" + pid + "/approve", {})
        assert status == 200, approved
        assert approved["proposal"]["state"] == "approved"
        status, after = req(server, "GET",
                            "/api/v1/studio/personas/evolving/effective")
        assert status == 200 and after["effective"]["evolution_version"] == 1
        assert "温柔" in after["effective"]["model_context"]
        assert "自然一些" in after["effective"]["model_context"]

        status, rolled = req(server, "POST", path + "/rollback", {"to_version": 0})
        assert status == 200, rolled
        assert rolled["active"]["version"] == 2
        status, restored = req(server, "GET",
                               "/api/v1/studio/personas/evolving/effective")
        assert status == 200 and restored["effective"]["evolution_version"] == 2
        assert "冷静" in restored["effective"]["model_context"]


def test_evolution_rejects_invalid_fields_and_cross_persona_approval():
    with api() as server:
        for name in ("one", "two"):
            assert req(server, "POST", "/api/v1/studio/personas", {
                "card_id": name, "title": name,
            })[0] == 201
        path = "/api/v1/studio/personas/one/evolution"
        status, data = req(server, "POST", path + "/proposals", {
            "patch": {"device_scopes": ["device.shell"]},
            "evidence_ids": ["message:1"], "rationale": "no",
        })
        assert status == 422, data
        status, data = req(server, "POST", path + "/proposals", {
            "patch": {"tone": "活泼"}, "evidence_ids": ["message:2"],
            "rationale": "owner requested",
        })
        assert status == 201, data
        pid = data["proposal"]["proposal_id"]
        status, _ = req(server, "POST",
                        "/api/v1/studio/personas/two/evolution/" + pid + "/approve",
                        {})
        assert status == 404


def test_evolution_endpoints_require_admin_bearer():
    with api() as server:
        status, _ = req(server, "GET",
                        "/api/v1/studio/personas/default/evolution", token="")
        assert status == 401
