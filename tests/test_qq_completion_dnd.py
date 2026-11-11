"""DND applies to every completion payload; all ports and artifacts are fakes."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from whale_core.life.rhythm import ConversationRhythm, RhythmStore, rhythm_key_for
from whale_core.memory.store import InMemoryStore


RUNNER = Path(__file__).resolve().parents[1] / "examples" / "qq_live_workflow.py"


def _completion(namespace):
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_send_completion")
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(RUNNER), "exec"),
         namespace)
    return namespace["_send_completion"]


@pytest.mark.parametrize("conversation", ["private:synthetic", "group:synthetic"])
@pytest.mark.parametrize("payload", ["text", "audio", "image", "file", "all"])
def test_dnd_blocks_completion_text_and_media(conversation, payload):
    store = InMemoryStore()
    RhythmStore(store).put(rhythm_key_for("qq", conversation), ConversationRhythm(dnd=True))
    sent = []

    def send(kind):
        def fake_send(*args, **kwargs):
            sent.append(kind)
            return True
        return fake_send

    artifacts = SimpleNamespace(
        directory=Path("/synthetic-not-accessed"),
        get=lambda aid: SimpleNamespace(content_type="text/markdown", artifact_id=aid),
        read=lambda aid: b"synthetic image")
    namespace = {
        "live_auto": True, "store": store, "self_uin": "synthetic-bot",
        "_await_turn": lambda mid: None,
        "_send_audio_artifacts": lambda ids, **kw: (
            sent.extend(["audio"] * len(ids)) or len(ids)),
        "send_port": SimpleNamespace(send_file_to=send("file"), send_image_to=send("image")),
        "cap": SimpleNamespace(artifacts=artifacts),
        "timeline": SimpleNamespace(append=lambda *a, **kw: None),
        "workflow": SimpleNamespace(outbox=SimpleNamespace(
            propose=lambda draft: None, close=lambda *a, **kw: None)),
    }
    complete = _completion(namespace)
    event = SimpleNamespace(message_id="synthetic-message", conversation=conversation)
    complete(event, "synthetic completion",
             audio_ids=("audio-id",) if payload in ("audio", "all") else (),
             image_ids=("image-id",) if payload in ("image", "all") else (),
             file_ids=("file-id",) if payload in ("file", "all") else ())
    assert sent == [], "DND must suppress media as well as completion text"
