"""Real loopback HTTP integration for the assembled, persistent console host.

The endpoint below is a protocol fixture, never a replacement production model.
It proves wire format and host integration, not external vendor/phone access.
"""

import base64
import io
import json
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.core.artifacts import ArtifactStore
from whale_core.core.jobs import JobService
from whale_core.memory.backup import create_backup, restore_backup, validate_backup
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.profiles import assemble_runtime

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(
                {
                    "status": "succeeded",
                    "url": "data:video/mp4;base64,"
                    + base64.b64encode(b"video-wire-fixture").decode(),
                }
            )

        def respond(self, value, media="application/json"):
            raw = json.dumps(value).encode() if media == "application/json" else value
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.path, raw))
            if self.path.endswith("/chat/completions"):
                value = json.loads(raw)
                content = value["messages"][-1]["content"]
                self.respond(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "wire:"
                                    + value["model"]
                                    + ":"
                                    + content[:40]
                                }
                            }
                        ]
                    }
                )
            elif self.path.endswith("/images/generations"):
                self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
            elif self.path.endswith("/audio/speech"):
                self.respond(b"ID3-wire-audio", "audio/mpeg")
            elif self.path.endswith("/audio/transcriptions"):
                assert b"Content-Disposition" in raw
                self.respond({"text": "回环语音识别"})
            elif self.path.endswith("/jobs"):
                self.respond({"task_id": "external-1"})
            elif self.path.endswith("/cancel"):
                self.respond({"cancelled": True})
            elif self.path.endswith("/embeddings"):
                texts = json.loads(raw)["input"]
                self.respond(
                    {
                        "data": [
                            {"index": i, "embedding": [1.0, 0.0]}
                            for i, _ in enumerate(texts)
                        ]
                    }
                )
            elif self.path.endswith("/sessions"):
                self.respond(
                    {"url": "wss://example.com/session/test", "session_id": "sid"}
                )
            elif self.path.endswith("/execute"):
                self.respond({"ok": True, "receipt": "external action accepted"})
            elif self.path.endswith("/messages"):
                self.respond({"accepted": True})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", calls
    server.shutdown()
    server.server_close()
    thread.join()


def call(server, path, method="GET", value=None, token="test-console"):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=8)
    raw = None if value is None else json.dumps(value).encode()
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    conn.request(method, "/api/v1" + path, body=raw, headers=headers)
    response = conn.getresponse()
    data = response.read()
    status = response.status
    conn.close()
    return status, json.loads(data) if data.startswith((b"{", b"[")) else data


@pytest.fixture
def console(tmp_path, provider):
    url, _ = provider
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"profile": "local", "model": {"base_url": url, "model": "first-model"}}
        )
    )
    bundle = assemble_runtime("local", config_dir=str(tmp_path), env={})
    ctx = ApiContext(
        store=bundle.store,
        manager=bundle.manager,
        orchestrator=bundle.orchestrator,
        curator=bundle.companion.curator,
        bundle=bundle,
        config_dir=str(tmp_path),
    )
    server = WhaleApiServer(ctx, port=0, token="test-console")
    server.start()
    yield server, bundle, tmp_path
    server.stop()
    bundle.store.close()


def wait_job(server, jid):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        status, body = call(server, "/jobs/" + jid)
        assert status == 200
        job = body["result"]
        if job["status"] in ("succeeded", "failed", "unknown", "denied", "cancelled"):
            return job
        time.sleep(0.03)
    raise AssertionError("job did not reach terminal status")


def test_config_hot_reload_disable_restart(console, provider):
    server, bundle, path = console
    assert call(server, "/chat", "POST", {"text": "hello"})[1]["result"][
        "reply"
    ].startswith("wire:first-model:")
    status, data = call(
        server,
        "/plugins/local_model/config",
        "POST",
        {"config": {"model": "changed", "api_key": "private-api-key"}},
    )
    assert status == 200
    assert (
        "wire:changed:"
        in call(server, "/chat", "POST", {"text": "next"})[1]["result"]["reply"]
    )
    assert (
        call(server, "/plugins/local_model")[1]["result"]["config"]["api_key"] == "***"
    )
    assert "private-api-key" not in json.dumps(
        bundle.store.get("plugin:config:local_model")
    )
    assert "private-api-key" not in json.dumps(call(server, "/diagnostics")[1])
    call(server, "/plugins/local_model/disable", "POST", {})
    assert call(server, "/chat", "POST", {"text": "disabled"})[0] == 502
    other = assemble_runtime("local", config_dir=str(path), env={})
    try:
        assert other.registry.provider_of("local_model") is None
        assert other.manager.runtime_config("local_model")["model"] == "changed"
        other.manager.enable("local_model")
        assert other.companion.model.reply("after restart").startswith("wire:changed:")
    finally:
        other.store.close()


def test_secret_schema_and_missing_credentials(console):
    server, _, _ = console
    data = call(server, "/plugins/speech_asr")[1]["result"]
    assert data["config_schema"]["properties"]["api_key"]["format"] == "secret"
    assert (
        call(
            server,
            "/plugins/intent_model/config",
            "POST",
            {"config": {"timeout": True}},
        )[0]
        == 422
    )
    assert (
        call(
            server,
            "/plugins/web_search/config",
            "POST",
            {"config": {"provider": "typo"}},
        )[0]
        == 422
    )
    assert call(server, "/status", token="bad")[0] == 401
    assert (
        call(server, "/jobs", "POST", {"kind": "shell", "args": {}}, token="bad")[0]
        == 401
    )


@pytest.mark.parametrize(
    "kind,pid,args",
    [
        ("image", "media_image", {"prompt": "sea"}),
        ("tts", "speech_tts", {"text": "hello"}),
        ("asr", "speech_asr", {"audio": base64.b64encode(b"RIFF-fixture").decode()}),
        ("video", "media_video", {"prompt": "sea"}),
        ("search", "web_search", {"query": "anything"}),
    ],
)
def test_media_wire_artifacts(console, provider, kind, pid, args, monkeypatch):
    server, bundle, _ = console
    if kind == "search":
        # Search vendor's fixed public endpoint is intentionally not contacted.
        from whale_core.plugins.web_search import TavilyProvider, SearchResult

        monkeypatch.setattr(
            TavilyProvider,
            "search",
            lambda *a, **k: [
                SearchResult("title", "https://example.com", "snippet", "tavily")
            ],
        )
        config = {"provider": "tavily", "api_key": "search-key"}
    else:
        config = {"base_url": provider[0]}
        if kind == "video":
            config["protocol"] = "bridge"
    assert call(server, f"/plugins/{pid}/config", "POST", {"config": config})[0] == 200
    assert call(server, f"/plugins/{pid}/enable", "POST", {})[0] == 200
    status, data = call(
        server,
        "/jobs",
        "POST",
        {"kind": kind, "args": args, "request_id": "fixture-" + kind},
    )
    assert status == 202
    job = wait_job(server, data["result"]["task_id"])
    assert job["status"] == "succeeded", job
    for artifact in job["result"].get("artifacts", []):
        assert call(server, "/artifacts/" + artifact["artifact_id"])[0] == 200
        assert (
            call(server, "/artifacts/" + artifact["artifact_id"], token="wrong")[0]
            == 401
        )
    if kind == "asr":
        assert job["result"]["text"] == "回环语音识别"
        assert job["args"] == {"audio": "[transient]"}
    repeated = call(
        server,
        "/jobs",
        "POST",
        {"kind": kind, "args": args, "request_id": "fixture-" + kind},
    )[1]["result"]
    assert repeated["task_id"] == job["task_id"]
    assert (
        call(
            server,
            "/jobs",
            "POST",
            {
                "kind": kind,
                "args": {"different": True},
                "request_id": "fixture-" + kind,
            },
        )[0]
        == 422
    )
    call(server, f"/plugins/{pid}/disable", "POST", {})
    assert bundle.registry.manifest(pid) is None


def test_approval_exact_request_and_duplicate(console, monkeypatch):
    server, _, _ = console
    from whale_core.cloud.container_executor import ContainerShellExecutor

    ran = []
    monkeypatch.setattr(
        ContainerShellExecutor,
        "run",
        lambda self, command, **kw: (
            ran.append(command) or {"status": "success", "stdout": "safe"}
        ),
    )
    job = call(
        server, "/jobs", "POST", {"kind": "shell", "args": {"command": "echo reviewed"}}
    )[1]["result"]
    assert job["status"] == "pending_approval" and not ran
    approvals = call(server, "/approvals")[1]["result"]["approvals"]
    assert approvals[0]["args"]["command"] == "echo reviewed"
    assert (
        call(
            server,
            "/approvals/" + job["task_id"] + "/decision",
            "POST",
            {"decision": "approve", "args": {"command": "other"}},
        )[0]
        == 200
    )
    result = wait_job(server, job["task_id"])
    assert result["status"] == "succeeded" and ran == ["echo reviewed"]
    assert (
        call(
            server,
            "/approvals/" + job["task_id"] + "/decision",
            "POST",
            {"decision": "approve"},
        )[0]
        == 200
    )
    assert (
        call(
            server,
            "/approvals/" + job["task_id"] + "/decision",
            "POST",
            {"decision": "reject"},
        )[0]
        == 422
    )
    assert ran == ["echo reviewed"]


def test_cancel_pending_prevents_approval(console):
    server, _, _ = console
    job = call(
        server, "/jobs", "POST", {"kind": "shell", "args": {"command": "echo hi"}}
    )[1]["result"]
    assert (
        call(server, "/jobs/" + job["task_id"] + "/cancel", "POST", {})[1]["result"][
            "status"
        ]
        == "cancelled"
    )
    assert (
        call(
            server,
            "/approvals/" + job["task_id"] + "/decision",
            "POST",
            {"decision": "approve"},
        )[0]
        == 422
    )


def test_pairing_transcripts_memory_backup(console, provider):
    server, bundle, path = console
    event = {
        "channel": "phone",
        "account": "owner-device",
        "conversation": "1",
        "message_id": "msg-1",
        "text": "hello",
    }
    assert call(server, "/channels/inbound", "POST", event)[0] == 403
    assert (
        call(
            server,
            "/settings",
            "POST",
            {"bindings": [{"channel": "phone", "account": "owner-device"}]},
        )[0]
        == 200
    )
    job = call(server, "/channels/inbound", "POST", event)[1]["result"]
    assert wait_job(server, job["task_id"])["status"] == "succeeded"
    assert (
        call(server, "/channels/inbound", "POST", event)[1]["result"]["task_id"]
        == job["task_id"]
    )
    assert (
        call(
            server,
            "/transcripts",
            "POST",
            {"session_id": "s1", "role": "user", "text": "我喜欢鲸鱼"},
        )[0]
        == 200
    )
    assert call(server, "/memory")[1]["result"]["total"] >= 2
    backup = path / "snapshot.sqlite3"
    create_backup(bundle.store, backup)
    validate_backup(backup)
    restored = path / "restored.sqlite3"
    restore_backup(backup, restored)
    check = SqliteMemoryStore(restored)
    try:
        assert check.keys() == bundle.store.keys()
    finally:
        check.close()
    assert call(server, "/backup", "POST", {})[1].startswith(b"SQLite format 3")


def test_restart_marks_running_unknown(tmp_path):
    from whale_core.memory.store import InMemoryStore

    bundle = assemble_runtime("local", store=InMemoryStore(), env={})
    bundle.store.put(
        "job:job-old",
        {"task_id": "job-old", "kind": "shell", "status": "running", "at": 0},
    )
    jobs = JobService(bundle, ArtifactStore(bundle.store, tmp_path))
    try:
        assert jobs.get("job-old")["status"] == "unknown"
    finally:
        jobs.close()


def test_artifact_limits_path_expiry(tmp_path):
    from whale_core.memory.store import InMemoryStore

    artifacts = ArtifactStore(InMemoryStore(), tmp_path)
    with pytest.raises(KeyError):
        artifacts.read("../outside")
    with pytest.raises(ValueError):
        artifacts.save(b"<script>", "text/html")
    with pytest.raises(Exception):
        artifacts.capture("http://127.0.0.1/private")
    meta = artifacts.save(PNG, "image/png", ttl_s=-1)
    with pytest.raises(KeyError):
        artifacts.read(meta["artifact_id"])
    assert artifacts.cleanup() == 1


def test_external_http_plugin_approval_and_denied_action(console, provider):
    server, bundle, path = console
    manifests = path / "manifests"
    manifests.mkdir(exist_ok=True)
    (manifests / "external.json").write_text(
        json.dumps(
            {
                "plugin_id": "external_test",
                "name": "External fixture",
                "version": "1",
                "transport": "http",
                "kind": "output",
                "actions": ["notify"],
                "action_risk": {"notify": "communicate"},
                "config_schema": {
                    "properties": {
                        "base_url": {"type": "string"},
                        "api_key": {"type": "string", "format": "secret"},
                    }
                },
            }
        )
    )
    assert bundle.manager.load_dir(manifests) == ["external_test"]
    bundle.manager.patch_config(
        "external_test", {"base_url": provider[0], "api_key": "external-secret"}
    )
    bundle.manager.enable("external_test")
    job = call(
        server,
        "/jobs",
        "POST",
        {
            "kind": "plugin",
            "args": {
                "plugin_id": "external_test",
                "action": "notify",
                "target": "owner",
                "args": {"text": "reviewed"},
            },
        },
    )[1]["result"]
    assert not any(p.endswith("/execute") for p, _ in provider[1])
    call(
        server,
        "/approvals/" + job["task_id"] + "/decision",
        "POST",
        {"decision": "approve"},
    )
    result = wait_job(server, job["task_id"])
    assert result["status"] == "succeeded", result
    assert len([p for p, _ in provider[1] if p.endswith("/execute")]) == 1
    denied = call(
        server,
        "/jobs",
        "POST",
        {
            "kind": "plugin",
            "args": {"plugin_id": "external_test", "action": "undeclared"},
        },
    )[1]["result"]
    call(
        server,
        "/approvals/" + denied["task_id"] + "/decision",
        "POST",
        {"decision": "approve"},
    )
    assert wait_job(server, denied["task_id"])["status"] == "denied"
    assert len([p for p, _ in provider[1] if p.endswith("/execute")]) == 1


def test_bad_hot_config_fails_closed(console):
    server, bundle, _ = console
    assert (
        call(
            server,
            "/plugins/local_model/config",
            "POST",
            {"config": {"base_url": "http://public.example.com"}},
        )[0]
        == 422
    )
    assert bundle.registry.provider_of("local_model") is None
    assert (
        call(server, "/chat", "POST", {"text": "must not use stale provider"})[0] == 502
    )


def test_daily_budgets_persist_and_can_be_adjusted(console):
    server, bundle, _ = console
    assert (
        call(
            server, "/settings", "POST", {"budget": {"model_calls": 0, "media_jobs": 0}}
        )[0]
        == 200
    )
    assert (
        call(server, "/chat", "POST", {"text": "over budget"})[1]["error"]["code"]
        == "budget"
    )
    assert (
        call(
            server, "/settings", "POST", {"budget": {"model_calls": 5, "media_jobs": 2}}
        )[0]
        == 200
    )
    assert call(server, "/chat", "POST", {"text": "now admitted"})[0] == 200
    assert any(key.startswith("budget:") for key in bundle.store.keys())


def test_cancel_running_http_call_waits_for_receipt(console, monkeypatch):
    server, bundle, _ = console
    started, release = threading.Event(), threading.Event()

    class TTS:
        def synthesize(self, *a, **k):
            started.set()
            release.wait(3)
            return b"ID3-fixture"

    bundle.registry.unregister("speech_tts")
    from whale_core.plugins.manifest import PluginManifest

    bundle.registry.register(
        PluginManifest(
            plugin_id="speech_tts",
            name="fixture",
            version="1",
            kind="model",
            provides=("tts.synthesize",),
        ),
        provider=TTS(),
    )
    job = call(server, "/jobs", "POST", {"kind": "tts", "args": {"text": "hello"}})[1][
        "result"
    ]
    assert started.wait(2)
    assert (
        call(server, "/jobs/" + job["task_id"] + "/cancel", "POST", {})[1]["result"][
            "status"
        ]
        == "cancelling"
    )
    release.set()
    assert wait_job(server, job["task_id"])["status"] == "cancelled"


def test_provider_redirect_never_forwards_credentials(provider):
    from whale_core.core.deepseek import DeepSeekCompanion, ModelError
    from whale_core.plugins.http_provider import JsonProvider, ProviderError

    sink = []

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", provider[0] + "/messages")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = len(provider[1])
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(ModelError):
            DeepSeekCompanion(api_key="must-not-forward", base_url=url).reply("test")
        with pytest.raises(ProviderError):
            JsonProvider(api_key="must-not-forward", base_url=url).request("/test", {})
        assert len(provider[1]) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_browser_voice_relay_pcm_auth_and_one_use_ticket(console):
    import socketserver
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed
    from whale_core.plugins.pcm_voice import pack_frame, read_frame

    server, bundle, _ = console
    received = []

    class PCM(socketserver.BaseRequestHandler):
        def handle(self):
            kind, auth = read_frame(self.request)
            received.append((kind, auth))
            self.request.sendall(pack_frame(1, b"HELLO box mode=echo sample=16000"))
            kind, audio = read_frame(self.request)
            received.append((kind, audio))
            self.request.sendall(pack_frame(0, audio))
            # Keep the transport open until the browser ends its call.
            try:
                self.request.recv(1)
            except OSError:
                pass

    tcp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), PCM)
    tcp.daemon_threads = True
    thread = threading.Thread(target=tcp.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            call(
                server,
                "/plugins/voice_bridge/config",
                "POST",
                {
                    "config": {
                        "host": "127.0.0.1",
                        "port": tcp.server_address[1],
                        "bridge_token": "fixture-bridge-secret",
                    }
                },
            )[0]
            == 200
        )
        assert call(server, "/plugins/voice_bridge/enable", "POST", {})[0] == 200
        status, data = call(server, "/voice/session", "POST", {})
        assert status == 200, data
        session = data["result"]
        assert "fixture-bridge-secret" not in json.dumps(session)
        with connect(
            session["url"], subprotocols=["whale", session["ticket"]], proxy=None
        ) as ws:
            assert json.loads(ws.recv())["type"] == "ready"
            sample = b"\x01\x00\xff\x7f"
            ws.send(sample)
            assert ws.recv() == sample
            ws.send("stop")
        assert received == [(1, b"AUTH fixture-bridge-secret"), (0, sample)]
        with connect(
            session["url"], subprotocols=["whale", session["ticket"]], proxy=None
        ) as ws:
            with pytest.raises(ConnectionClosed):
                ws.recv()
        # Raw microphone bytes and the upstream permanent token aren't in jobs.
        assert all(
            j.get("kind") != "voice" for j in bundle.store.get("tasks:recent") or []
        )
    finally:
        tcp.shutdown()
        tcp.server_close()
        thread.join()


def test_dashscope_video_wire_submit_poll_cancel(provider):
    from whale_core.plugins.media_video import DashScopeVideoProvider
    from whale_core.plugins.http_provider import JsonProvider

    # Use the real HTTP transport with a local vendor-protocol fixture.
    class Vendor(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, data):
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.endswith("/cancel"):
                self.send_json({"request_id": "cancelled"})
                return
            assert self.headers["X-DashScope-Async"] == "enable"
            data = json.loads(raw)
            assert data["parameters"] == {
                "size": "1280*720",
                "duration": 5,
                "prompt_extend": True,
            }
            self.send_json({"output": {"task_id": "wan-fixture"}})

        def do_GET(self):
            assert self.path == "/api/v1/tasks/wan-fixture"
            self.send_json(
                {
                    "output": {
                        "task_status": "SUCCEEDED",
                        "video_url": "https://example.com/video.mp4",
                    }
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        video = DashScopeVideoProvider(
            base_url=f"http://127.0.0.1:{server.server_port}/api/v1",
            api_key="fixture",
            model="wan2.6-t2v",
        )
        tid = video.submit("sea")
        assert video.poll(tid) == ("succeeded", "https://example.com/video.mp4", "")
        assert video.cancel(tid) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_native_whale_io_dashboard_and_ring_wire():
    from whale_core.plugins.whale_io import WhaleIOChannelProvider

    calls = []

    class Native(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append(
                (
                    self.path,
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            raw = b'{"ok":true,"queued":true,"delivered":false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Native)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        native = WhaleIOChannelProvider(
            dashboard_url=root + "/card",
            dashboard_token="d",
            ring_url=root,
            ring_token="r",
        )
        assert native.send("hud", "fixed-owner", "hello", idempotency_key="1")[
            "accepted"
        ]
        assert native.send("call", "fixed-device", "whale", idempotency_key="2")[
            "accepted"
        ]
        assert calls == [
            ("/card", {"title": "鲸鱼", "body": "hello", "layout_hint": "default"}),
            (
                "/v1/ring",
                {"device_id": "fixed-device", "name": "whale", "number": "***"},
            ),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_catalog_schema_never_contains_env_token_defaults(tmp_path):
    bundle = assemble_runtime(
        "local",
        config_dir=str(tmp_path),
        env={
            "BRIDGE_TOKEN": "private-bridge-token",
            "SIGNAL_TOKEN": "private-ring-token",
            "WHALE_DASHBOARD_TOKEN": "private-dashboard-token",
            "WEQ_WEB_TOKEN": "private-weq-token",
        },
    )
    try:
        for p in bundle.manager.status():
            data = json.dumps(bundle.manager.schema(p["plugin_id"]))
            assert "private-" not in data
        assert bundle.manager.config_view("voice_bridge")["bridge_token"] == "***"
    finally:
        bundle.store.close()


def test_second_job_host_and_restore_are_blocked_by_runtime_lease(console):
    from whale_core.core.lease import RuntimeBusy

    server, bundle, path = console
    with pytest.raises(RuntimeBusy):
        WhaleApiServer(
            ApiContext(store=bundle.store, config_dir=str(path)), port=0, token="second"
        )
    from whale_core.cli.main import main

    backup = path / "while-running.sqlite3"
    create_backup(bundle.store, backup)
    assert main(["restore", str(backup), "--offline", "--config-dir", str(path)]) == 2


def test_cli_side_changes_are_observed_at_provider_boundary(console):
    server, bundle, path = console
    other = assemble_runtime("local", config_dir=str(path), env={})
    try:
        other.manager.patch_config("local_model", {"model": "from-cli-process"})
        assert (
            "wire:from-cli-process:"
            in call(server, "/chat", "POST", {"text": "hello"})[1]["result"]["reply"]
        )
        other.manager.disable("local_model")
        assert call(server, "/chat", "POST", {"text": "blocked"})[0] == 502
    finally:
        other.store.close()


def test_cancel_chat_waiting_for_conversation_lock_makes_no_provider_call(
    console, provider
):
    server, bundle, _ = console
    before = len(provider[1])
    with bundle.orchestrator._turn_lock:
        job = call(
            server, "/jobs", "POST", {"kind": "chat", "args": {"text": "do not call"}}
        )[1]["result"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (
                call(server, "/jobs/" + job["task_id"])[1]["result"]["status"]
                == "running"
            ):
                break
            time.sleep(0.01)
        call(server, "/jobs/" + job["task_id"] + "/cancel", "POST", {})
    assert wait_job(server, job["task_id"])["status"] == "cancelled"
    assert len(provider[1]) == before


def test_reconcile_interrupted_video_never_resubmits(console, provider):
    server, bundle, _ = console
    from whale_core.plugins.media_video import VideoTaskManager

    bundle.manager.patch_config(
        "media_video", {"protocol": "bridge", "base_url": provider[0]}
    )
    bundle.manager.enable("media_video")
    manager = VideoTaskManager(bundle.store, bundle.registry.provider_of("media_video"))
    task = manager.submit("sea")
    before = len(provider[1])
    bundle.store.put(
        "job:job-interrupted",
        {
            "task_id": "job-interrupted",
            "kind": "video",
            "status": "unknown",
            "external_task_id": task.task_id,
            "at": time.time(),
            "goal": "sea",
            "args": {},
            "receipts": [],
            "capabilities": ["video"],
            "result": {},
        },
    )
    status, result = call(server, "/jobs/job-interrupted/refresh", "POST", {})
    assert status == 200, result
    assert result["result"]["status"] == "succeeded"
    assert len(provider[1]) == before
