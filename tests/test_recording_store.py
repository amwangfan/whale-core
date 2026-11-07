"""RecordingStore 目录管理与删除测试。"""

from __future__ import annotations

import pytest

from whale_core.devices.recording.store import RecordingStore, RecordingStoreError

RUN_ID = "run_01J9XK3EM8TQ2ZP0VN6W4RA7YC"


@pytest.fixture()
def store(tmp_path):
    return RecordingStore(tmp_path / "recordings")


def test_ensure_run_creates_layout(store):
    run_dir = store.ensure_run(RUN_ID)
    assert run_dir.is_dir()
    assert run_dir.name == RUN_ID
    assert run_dir.parent.name == "runs"
    # 幂等
    assert store.ensure_run(RUN_ID) == run_dir


def test_invalid_run_id_rejected(store):
    with pytest.raises(RecordingStoreError):
        store.run_dir("not-a-run-id")
    with pytest.raises(RecordingStoreError):
        store.run_dir("run_" + "A" * 25)          # 长度不足
    with pytest.raises(RecordingStoreError):
        store.run_dir("run_01J9XK3EM8TQ2ZP0VN6W4RA7YU")  # 含合同外字符 U
    with pytest.raises(RecordingStoreError):
        store.run_dir("../etc")


def test_list_runs_and_artifacts(store):
    assert store.list_runs() == []
    run_dir = store.ensure_run(RUN_ID)
    (run_dir / "video.h264").write_bytes(b"x")
    (run_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    assert store.list_runs() == [RUN_ID]
    assert store.list_artifacts(RUN_ID) == ["trace.jsonl", "video.h264"]
    with pytest.raises(RecordingStoreError):
        store.list_artifacts("run_01J9XK3EM8TQ2ZP0VN6W4RA8YC")


def test_delete_artifact(store):
    run_dir = store.ensure_run(RUN_ID)
    (run_dir / "video.h264").write_bytes(b"x")
    assert store.delete_artifact(RUN_ID, "video.h264") is True
    assert not (run_dir / "video.h264").exists()
    assert store.delete_artifact(RUN_ID, "video.h264") is False  # 幂等
    # 白名单外拒绝（防路径穿越）
    with pytest.raises(RecordingStoreError):
        store.delete_artifact(RUN_ID, "../../etc/passwd")
    with pytest.raises(RecordingStoreError):
        store.delete_artifact(RUN_ID, "evil.sh")


def test_delete_run(store):
    run_dir = store.ensure_run(RUN_ID)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    assert store.delete_run(RUN_ID) is True
    assert not run_dir.exists()
    assert store.delete_run(RUN_ID) is False
    assert store.list_runs() == []
