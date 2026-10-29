import tempfile
from pathlib import Path
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.continuity import remember_episode
from whale_core.memory.store import PersonaContinuity, MemoryKind
from whale_core.core.companion import WhaleCompanion, RealtimeTranscriptRecorder
from whale_core.core.deepseek import DeepSeekCompanion
from whale_core.core.agent import WhaleAgent
from whale_core.core.types import Decision, ActionIntent
from whale_core.perception.events import ScheduleTick

class FakeDeepSeek:
    def reply(self, text, *, persona="", memories=(), history=()):
        assert "鲸鱼" in persona
        assert "上次" in "".join(memories)
        return "记得，我们继续聊。"

def test_persistent_memory_roundtrip():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "memory.db"
        s = SqliteMemoryStore(path)
        s.put("foo", {"x": 2})
        s.put_persona_continuity(PersonaContinuity(persona_name="鲸鱼"))
        remember_episode(s, summary="上次聊了音乐", key="ep:test")
        s.close()
        s = SqliteMemoryStore(path)
        assert s.get("foo") == {"x": 2}
        assert s.get_persona_continuity().persona_name == "鲸鱼"
        assert s.get_record("ep:test").content["summary"] == "上次聊了音乐"
        assert len(s.list_by_kind(MemoryKind.EPISODE)) == 1
        s.delete("ep:test")
        assert s.get_record("ep:test") is None
        s.close()

def test_companion_uses_shared_memory():
    with tempfile.TemporaryDirectory() as root:
        s = SqliteMemoryStore(Path(root) / "memory.db")
        s.put_persona_continuity(PersonaContinuity())
        remember_episode(s, summary="上次聊过一本书", key="ep:old")
        bot = WhaleCompanion(s, FakeDeepSeek())
        assert "记得" in bot.chat("接着聊")
        assert len(s.list_by_kind(MemoryKind.EPISODE)) == 2
        s.close()

def test_realtime_transcripts_persist_without_llm():
    with tempfile.TemporaryDirectory() as root:
        s = SqliteMemoryStore(Path(root) / "memory.db")
        recorder = RealtimeTranscriptRecorder(s)
        assert recorder.offer("call-1", "user", "你好")
        assert recorder.offer("call-1", "assistant", "我在")
        assert not recorder.offer("call-1", "system", "secret")
        recorder.close()
        records = s.list_by_kind(MemoryKind.EPISODE)
        assert len(records) == 2
        assert {r.content["role"] for r in records} == {"user", "assistant"}
        s.close()

def test_deepseek_requires_key_and_https():
    try:
        DeepSeekCompanion(base_url="http://example.com")
    except ValueError:
        pass
    else:
        assert False
    d = DeepSeekCompanion(api_key="")
    try:
        d.reply("hi")
    except RuntimeError:
        pass
    else:
        assert False

class SensitiveRouter:
    def route(self, observation):
        return Decision(intents=(ActionIntent(kind="place_call"),), rationale="test")

def test_default_policy_never_calls_without_opt_in():
    a = WhaleAgent(router=SensitiveRouter())
    decision, receipts = a.step([ScheduleTick(title="Hi")])
    assert decision.suppress
    assert receipts == []


def test_recorder_disk_errors_do_not_deadlock_close():
    from whale_core.memory.store import InMemoryStore
    class RejectWrites(InMemoryStore):
        def put_record(self, record):
            raise OSError("disk unavailable")
    recorder = RealtimeTranscriptRecorder(RejectWrites())
    assert recorder.offer("s1", "user", "first")
    assert recorder.offer("s1", "assistant", "second")
    recorder.close()
    assert recorder.failed_count == 2
    assert recorder.offer("s1", "user", "after-close") is False
