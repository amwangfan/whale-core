"""Exercise the runner's real video callbacks without starting its service/threads."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "examples" / "qq_live_workflow.py"


class _StopPolling(Exception):
    pass


def _callbacks():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"), filename=str(RUNNER))
    names = {"_track_video", "_video_poller"}
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name in names]
    queue = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AnnAssign)
                 and isinstance(n.target, ast.Name)
                 and n.target.id == "_pending_videos")
    assert {n.name for n in functions} == names
    manager = Mock()
    port = Mock()
    deliver = Mock()
    # Let the real loop complete exactly one iteration; no sleep or thread.
    clock = SimpleNamespace(time=lambda: 1000.0,
                            sleep=Mock(side_effect=[None, _StopPolling()]))
    namespace = {"cap": SimpleNamespace(video_manager=manager),
                 "time": clock, "send_port": port, "_deliver_video": deliver}
    module = ast.Module(body=[queue, *functions], type_ignores=[])
    exec(compile(module, str(RUNNER), "exec"), namespace)
    return namespace, manager, port, deliver


def test_pending_video_hook_registers_task_for_real_poller():
    ns, manager, _, deliver = _callbacks()
    event = SimpleNamespace(conversation="private:owner", message_id="request-1")
    ns["_track_video"](event, "video-1")
    assert ns["_pending_videos"] == {
        "video-1": ("private:owner", "request-1", 4600.0)}
    manager.poll.return_value = SimpleNamespace(
        status="succeeded", url="https://example.invalid/video.mp4")
    with pytest.raises(_StopPolling):
        ns["_video_poller"]()
    manager.poll.assert_called_once_with("video-1")
    deliver.assert_called_once_with("private:owner", "https://example.invalid/video.mp4")
    assert ns["_pending_videos"] == {}


@pytest.mark.parametrize("conversation,has_manager", [("", True), ("private:owner", False)])
def test_pending_video_hook_ignores_untrackable_event(conversation, has_manager):
    ns, _, _, _ = _callbacks()
    if not has_manager:
        ns["cap"].video_manager = None
    ns["_track_video"](SimpleNamespace(conversation=conversation), "video-1")
    assert ns["_pending_videos"] == {}


def test_failed_video_poller_reports_failure_and_removes_task():
    ns, manager, port, deliver = _callbacks()
    ns["_pending_videos"]["video-1"] = ("group:test", "request-1", 4600.0)
    manager.poll.return_value = SimpleNamespace(status="failed", error="provider", url="")
    with pytest.raises(_StopPolling):
        ns["_video_poller"]()
    manager.poll.assert_called_once_with("video-1")
    port.send_group_text.assert_called_once()
    assert port.send_group_text.call_args.args[0] == "test"
    deliver.assert_not_called()
    assert ns["_pending_videos"] == {}
