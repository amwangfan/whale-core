"""Phone contract integration using local HTTP and temporary SQLite."""
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from whale_core.api.glasses import build_delegates, make_handler
from whale_core.api.glasses_context import SHANGHAI
from whale_core.memory.history import load_history


@contextmanager
def server(tmp_path):
    calls = []
    class Brain:
        def reply(self, text, **kwargs):
            calls.append((text, kwargs))
            return "收到"
    delegates = build_delegates(
        {"WHALE_GLASSES_STORE": str(tmp_path / "memory.sqlite"), "OWNER_UID": "owner"},
        brain_factory=Brain, quick_brain_factory=Brain, send_port_factory=lambda: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(delegates))
    srv.glasses_token = "test-phone"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    def post(path, body, token="test-phone"):
        req = Request(f"http://127.0.0.1:{srv.server_port}" + path,
                      data=json.dumps(body).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)
    try:
        yield post, delegates, calls
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(5)
        if delegates["rayneo_store"]:
            delegates["rayneo_store"].close()
        delegates["store"].close()


def test_sync_then_chat_queries_dated_snapshot_without_changing_user_history(tmp_path):
    tomorrow = (datetime.now(SHANGHAI) + timedelta(days=1)).date().isoformat()
    with server(tmp_path) as (post, delegates, calls):
        assert post("/v1/context/sync", {"calendar": {tomorrow: []}}, token="wrong")[0] == 401
        assert post("/v1/context/sync", {"calendar": {"invalid": []}})[0] == 400
        status, saved = post("/v1/context/sync", {
            "source": "android.calendar", "calendar": {tomorrow: [{"title": "真实考试", "start": "09:00"}]}})
        assert status == 200 and saved["ok"] and saved["dates"] == [tomorrow]
        status, reply = post("/v1/chat", {"text": "明天有什么安排", "sessionId": "one"})
        assert status == 200 and reply["reply"] == "收到"
        assert "真实考试" in calls[-1][1]["persona"]
        history = load_history(delegates["store"], "glasses:one")
        assert history[0]["content"] == "明天有什么安排"
        assert "真实考试" not in json.dumps(history, ensure_ascii=False)
        assert post("/v1/context/sync", {"calendar": {}})[1]["ok"]
        post("/v1/chat", {"text": "还有吗", "sessionId": "two"})
        assert "真实考试" not in calls[-1][1]["persona"]


def test_legacy_prefix_is_split_before_http_delegate_and_not_persisted(tmp_path):
    today = datetime.now(SHANGHAI).date().isoformat()
    with server(tmp_path) as (post, delegates, calls):
        code, body = post("/v1/chat", {"sessionId": "legacy", "text":
            f"[现在] {today} 08:00\n[明日日程] 09:00 不落库的考试\n[用户] 明天有什么日程"})
        assert code == 200 and body["reply"]
        assert calls[-1][0] == "明天有什么日程"
        assert "不落库的考试" in calls[-1][1]["persona"]
        values = [str(delegates["store"].get(key)) for key in delegates["store"].keys()]
        assert all("不落库的考试" not in value for value in values)


def test_nested_user_text_is_not_parsed_twice(tmp_path):
    with server(tmp_path) as (post, delegates, calls):
        actual_speech = "[现在] 这是一段我要讨论的格式\n[用户] 请解释这个协议"
        text = "[今日日程] （无）\n[用户] " + actual_speech
        code, _ = post("/v1/chat", {"text": text, "sessionId": "nested"})
        assert code == 200
        assert calls[-1][0] == actual_speech
        assert load_history(delegates["store"], "glasses:nested")[0]["content"] == actual_speech
