"""Independent regressions for credential generations and orphaned sessions."""
from __future__ import annotations
import pytest

from whale_core.configuration import SettingsController, configured_env
from whale_core.plugins.realtime_voice import OpenAICompatibleRealtime
from whale_core.plugins.speech import SpeechError
from whale_core.profiles import assemble_runtime


@pytest.mark.parametrize("close_fails", [False, True])
def test_failed_realtime_session_update_closes_untracked_connection(close_fails):
    class Connection:
        close_calls = 0
        def send(self, _):
            raise OSError("PRIVATE-UPSTREAM-CREDENTIAL")
        def close(self):
            self.close_calls += 1
            if close_fails:
                raise OSError("PRIVATE-CLOSE-ERROR")
    connection = Connection()
    provider = OpenAICompatibleRealtime(url="wss://voice.example/realtime", api_key="PRIVATE-KEY",
        model="omni", connector=lambda *args, **kwargs: connection)
    with pytest.raises(SpeechError) as caught:
        provider.open_session()
    assert connection.close_calls == 1
    assert caught.value.kind == "network"
    assert "PRIVATE" not in str(caught.value)


def test_failed_public_projection_never_mixes_endpoint_and_key_generations(tmp_path, monkeypatch):
    import whale_core.configuration as configuration
    bundle = assemble_runtime("local", config_dir=str(tmp_path), env={})
    controller = SettingsController(tmp_path, bundle, env={})
    try:
        controller.update({"profile": "cloud", "model": {
            "base_url": "https://old.example/v1", "model": "old-model", "api_key": "OLD-KEY"}})
        original = configuration.atomic_json
        def fail_projection(path, value):
            if path.name == "config.json":
                raise OSError("simulated failed public projection")
            return original(path, value)
        monkeypatch.setattr(configuration, "atomic_json", fail_projection)
        try:
            controller.update({"model": {
                "base_url": "https://new.example/v1", "model": "new-model", "api_key": "NEW-KEY"}})
        except OSError:
            pass
        effective = configured_env("cloud", tmp_path, {})
        assert (effective.get("DEEPSEEK_BASE_URL"), effective.get("DEEPSEEK_API_KEY")) in {
            ("https://old.example/v1", "OLD-KEY"), ("https://new.example/v1", "NEW-KEY")}
    finally:
        bundle.store.close()


def test_malformed_voice_open_request_cannot_start_provider(tmp_path):
    from http.client import HTTPConnection
    from whale_core.api.server import ApiContext, WhaleApiServer
    from whale_core.plugins.manifest import PluginManifest
    class Session:
        def close(self): pass
    class Provider:
        opens = 0
        def open_session(self, **kwargs):
            self.opens += 1
            return Session()
    bundle = assemble_runtime("local", config_dir=str(tmp_path), env={})
    provider = Provider()
    bundle.registry.register(PluginManifest(plugin_id="voice_realtime", name="Voice", version="1",
        kind="model", provides=("voice.realtime",)), provider=provider)
    bundle.manager.adopt_registered()
    context = ApiContext(bundle.store, manager=bundle.manager, orchestrator=bundle.orchestrator)
    server = WhaleApiServer(context, port=0, token="test")
    server.start()
    connection = HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.request("POST", "/api/v1/voice/sessions", b"{invalid-json", {
            "Authorization": "Bearer test", "Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        assert response.status == 400
        assert provider.opens == 0
    finally:
        connection.close()
        server.stop()
        bundle.store.close()


def test_raw_audio_does_not_reach_capability_task_or_memory_storage(tmp_path, monkeypatch):
    import base64
    import json
    from http.client import HTTPConnection
    from whale_core.api.server import ApiContext, WhaleApiServer
    from whale_core.core.deepseek import DeepSeekCompanion
    from whale_core.core.runtime import ToolSpec
    monkeypatch.setattr(DeepSeekCompanion, "reply", lambda *a, **kw: "Heard text")
    bundle = assemble_runtime("local", config_dir=str(tmp_path), env={})
    audio = base64.b64encode(b"PRIVATE-RAW-AUDIO-SENTINEL").decode()
    received = []
    bundle.tools.register(ToolSpec("test_transcribe", "asr.transcribe",
        handler=lambda params: received.append(params["audio_base64"]) or "Transcribed text"))
    context = ApiContext(bundle.store, manager=bundle.manager, orchestrator=bundle.orchestrator)
    server = WhaleApiServer(context, port=0, token="test")
    server.start()
    connection = HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.request("POST", "/api/v1/capabilities/asr.transcribe",
            json.dumps({"params": {"audio_base64": audio, "format": "wav"}}), {
            "Authorization": "Bearer test", "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read().decode()
        assert response.status == 200 and received == [audio]
        assert "Transcribed text" in payload and audio not in payload
        stored = json.dumps({key: bundle.store.get(key) for key in bundle.store.keys()})
        assert audio not in stored and "PRIVATE-RAW-AUDIO-SENTINEL" not in stored
        assert not bundle.orchestrator._transient_args
    finally:
        connection.close()
        server.stop()
        bundle.store.close()


def test_global_pause_prevents_actual_outbound_send_from_another_outbox():
    from datetime import datetime, time, timezone
    from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
    from whale_core.memory.store import InMemoryStore
    store = InMemoryStore()
    first, second = ProactiveOutbox(store), ProactiveOutbox(store)
    first.propose(ProactiveDraft("pause-test", "hello", "test", ("evidence",)))
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"hud"}), auto_send=True,
                          quiet_start=time(0), quiet_end=time(0))
    class Port:
        calls = 0
        def send(self, draft):
            self.calls += 1
            return True
    port = Port()
    store.put("workflows:settings", {"paused": True})
    result = second.dispatch("pause-test", prefs, now=datetime.now(timezone.utc), port=port)
    assert result.status == "paused" and port.calls == 0
    assert second.ledger("pause-test")["attempts"] == 0


def test_effective_environment_reads_one_credential_snapshot(tmp_path, monkeypatch):
    import whale_core.configuration as configuration
    old = {"schema_version": 1, "config": {"profile": "cloud", "model": {
        "base_url": "https://old.example/v1"}}, "private": {"model_api_key": "OLD-KEY"}}
    new = {"schema_version": 1, "config": {"profile": "cloud", "model": {
        "base_url": "https://new.example/v1"}}, "private": {"model_api_key": "NEW-KEY"}}
    reads = []
    def snapshot(_):
        reads.append(1)
        return old if len(reads) == 1 else new
    monkeypatch.setattr(configuration, "_private_snapshot", snapshot)
    effective = configured_env("cloud", tmp_path, {})
    assert (effective.get("DEEPSEEK_BASE_URL"), effective.get("DEEPSEEK_API_KEY")) in {
        ("https://old.example/v1", "OLD-KEY"), ("https://new.example/v1", "NEW-KEY")}


def test_voice_api_inherits_persona_records_only_final_text_and_never_calls_text_llm(tmp_path, monkeypatch):
    import base64
    import json
    from collections import deque
    from http.client import HTTPConnection
    from whale_core.api.server import ApiContext, WhaleApiServer
    from whale_core.core.deepseek import DeepSeekCompanion
    from whale_core.memory.continuity import remember_episode
    from whale_core.memory.store import MemoryKind, PersonaContinuity
    from whale_core.plugins.manifest import PluginManifest
    calls = []
    def forbidden_text_call(*args, **kwargs):
        calls.append(1)
        raise AssertionError("realtime must not wait for text LLM")
    monkeypatch.setattr(DeepSeekCompanion, "reply", forbidden_text_call)
    audio = base64.b64encode(b"PRIVATE-VOICE-AUDIO-SENTINEL").decode()
    final_user = {"type": "conversation.item.input_audio_transcription.completed", "event_id": "u1",
                  "transcript": "今天完成了阅读"}
    class Session:
        def __init__(self):
            self.events = deque([final_user, final_user,
                {"type": "response.audio_transcript.done", "event_id": "a1", "transcript": "记得带水"},
                {"type": "response.audio.delta", "delta": audio}])
            self.audio = []
        def receive(self, **kwargs): return self.events.popleft()
        def append_audio(self, data): self.audio.append(data)
        def close(self): pass
    class Provider:
        instructions = ""
        def open_session(self, **kwargs):
            self.instructions = kwargs.get("instructions", "")
            self.session = Session()
            return self.session
    bundle = assemble_runtime("local", config_dir=str(tmp_path), env={})
    bundle.store.put_persona_continuity(PersonaContinuity(persona_name="测试鲸鱼人格", tone="温暖简短"))
    remember_episode(bundle.store, summary="昨天约定去图书馆", key="ep:test")
    provider = Provider()
    bundle.registry.register(PluginManifest(plugin_id="voice_realtime", name="Voice", version="1",
        kind="model", provides=("voice.realtime",)), provider=provider)
    bundle.manager.adopt_registered()
    context = ApiContext(bundle.store, manager=bundle.manager, orchestrator=bundle.orchestrator,
                         curator=bundle.companion.curator)
    server = WhaleApiServer(context, port=0, token="test")
    server.start()
    def post(route, body):
        connection = HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            connection.request("POST", "/api/v1/voice/" + route, json.dumps(body), {
                "Authorization": "Bearer test", "Content-Type": "application/json"})
            response = connection.getresponse()
            payload = json.loads(response.read())
            assert response.status == 200, payload
            return payload["result"]
        finally:
            connection.close()
    try:
        sid = post("sessions", {"instructions": "聊聊阅读计划"})["session_id"]
        assert "测试鲸鱼人格" in provider.instructions
        assert "昨天约定去图书馆" in provider.instructions
        assert "聊聊阅读计划" in provider.instructions
        post("sessions/" + sid + "/append", {"audio_base64": audio})
        for _ in range(4):
            post("sessions/" + sid + "/receive", {})
        context.transcript_recorder.events.join()
        episodes = [record for record in bundle.store.list_by_kind(MemoryKind.EPISODE) if "call" in record.tags]
        assert len(episodes) == 2
        assert {record.content["summary"] for record in episodes} == {"今天完成了阅读", "记得带水"}
        assert calls == []
        stored = json.dumps({key: bundle.store.get(key) for key in bundle.store.keys()})
        assert audio not in stored and "PRIVATE-VOICE-AUDIO-SENTINEL" not in stored
    finally:
        server.stop()
        bundle.store.close()
