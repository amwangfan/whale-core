"""issue #45：流式回复（DeepSeekCompanion SSE）与 /chat/stream 阶段反馈测试。

全部离线：流式模型端点用本机回环 HTTP 假服务；/chat/stream 用注入假
orchestrator 的回环服务。零外网。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection

from whale_core.core.deepseek import DeepSeekCompanion

SSE_BODY = (
    'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
    'data: {"choices":[{"delta":{}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"呀"}}]}\n\n'
    "data: [DONE]\n\n"
)


class _StreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["stream"] is True
        data = SSE_BODY.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def log_message(self, *a):
        pass


@contextmanager
def _fake_model_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()


class TestReplyStream:
    def test_deltas_and_metrics(self):
        with _fake_model_server() as server:
            model = DeepSeekCompanion(
                base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=5)
            pieces = []
            metrics: dict = {}
            reply = model.reply("hi", on_delta=pieces.append,
                                metrics=metrics)
            assert reply == "你好呀"
            assert pieces == ["你", "好", "呀"]
            assert metrics["chunks"] == 3
            assert metrics["stopped"] is False
            assert metrics["ttft_ms"] is not None
            assert metrics["total_ms"] >= metrics["ttft_ms"]

    def test_stop_aborts_stream(self):
        with _fake_model_server() as server:
            model = DeepSeekCompanion(
                base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=5)
            pieces = []
            metrics: dict = {}
            reply = model.reply("hi", on_delta=pieces.append,
                                should_stop=lambda: len(pieces) >= 1,
                                metrics=metrics)
            assert reply == "你"  # 只保留已收到的部分，不假装完整
            assert metrics["stopped"] is True

    def test_non_stream_unchanged(self):
        """不传 on_delta 时仍走原非流式路径（stream=False）。"""
        with _fake_model_server() as server:
            model = DeepSeekCompanion(
                base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=5)
            # 假服务要求 stream=True——非流式请求应 500；用异常证明没走流式
            try:
                model.reply("hi")
            except Exception:
                pass  # 关键是请求体 stream=False（假服务端断言）
            else:
                raise AssertionError("fake server should reject non-stream")


class _FakeOrchestrator:
    class _R:
        reply = "流式回答"
        status = "answered"
        task_id = ""
        receipts = ()

    def __init__(self, store):
        self.store = store
        self.calls: list[dict] = []

    def handle_message(self, conversation_id, text, *, source="runtime",
                       images=(), sender_id="", on_delta=None,
                       should_stop=None, metrics=None, on_phase=None):
        self.calls.append({"on_delta": bool(on_delta)})
        if on_phase:
            on_phase("context")
        if metrics is not None:
            metrics.update({"ttft_ms": 12, "total_ms": 34, "chunks": 2,
                            "stopped": False})
        if on_delta:
            on_delta("流式")
            on_delta("回答")
        return self._R()


class TestChatStreamApi:
    @contextmanager
    def _server(self):
        from whale_core.api.server import ApiContext, WhaleApiServer
        from whale_core.memory.store import InMemoryStore
        store = InMemoryStore()
        ctx = ApiContext(store=store)
        ctx.orchestrator = _FakeOrchestrator(store)
        server = WhaleApiServer(ctx, port=0, token="t0k")
        server.start()
        try:
            yield server
        finally:
            server.stop()

    def test_sse_sequence(self):
        with self._server() as server:
            conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("POST", "/api/v1/chat/stream",
                         json.dumps({"text": "你好"}),
                         headers={"Authorization": "Bearer t0k",
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 200
            raw = resp.read().decode("utf-8")
            conn.close()
            events = [json.loads(line[5:]) for line in raw.splitlines()
                      if line.startswith("data: ")]
            types = [e["type"] for e in events]
            assert types[0] == "phase" and events[0]["phase"] == "received"
            assert "context" in [e.get("phase") for e in events]
            deltas = [e["text"] for e in events if e["type"] == "delta"]
            assert deltas == ["流式", "回答"]
            done = events[-1]
            assert done["type"] == "done"
            assert done["reply"] == "流式回答"
            assert done["metrics"]["ttft_ms"] == 12
            assert done["stopped"] is False
