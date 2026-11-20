"""deepseek.py 恒流式重构测试（2026-11-20 治本令）：SSE 逐 chunk 读、usage/
finish_reason 解析、无回调路径、首 token 打点、故障分类不回归。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from whale_core.core.deepseek import DeepSeekCompanion, ModelError


class _SSEHandler(BaseHTTPRequestHandler):
    chunks = ["你好", "，", "我在"]

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(body)
        assert payload.get("stream") is True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i, piece in enumerate(self.chunks):
            fr = "stop" if i == len(self.chunks) - 1 else None
            data = {"choices": [{"delta": {"content": piece},
                                  "finish_reason": fr}]}
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        self.wfile.write(f"data: {json.dumps({'usage': usage})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):  # noqa: D401 — 静默
        pass


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown(); srv.server_close(); t.join(5)


def _companion(url):
    return DeepSeekCompanion(api_key="k", base_url=url, model="m")


def test_reply_always_streams_without_callback(server):
    m = {}
    out = _companion(server).reply("hi", metrics=m)
    assert out == "你好，我在"
    assert m["ttft_ms"] is not None and m["total_ms"] >= m["ttft_ms"]
    assert m["chunks"] == 3
    assert m["finish_reason"] == "stop"
    assert m["usage"]["total_tokens"] == 8


def test_reply_streams_with_callback(server):
    got = []
    out = _companion(server).reply("hi", on_delta=got.append, metrics={})
    assert out == "你好，我在"
    assert got == ["你好", "，", "我在"]


class _ErrHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(429)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_error_classification_preserved_streaming():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ErrHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        comp = DeepSeekCompanion(api_key="k",
                                 base_url=f"http://127.0.0.1:{srv.server_port}",
                                 model="m")
        with pytest.raises(ModelError) as ei:
            comp.reply("hi")
        assert ei.value.kind == "rate_limited" and ei.value.retryable
    finally:
        srv.shutdown(); srv.server_close()
