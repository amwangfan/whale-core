"""RunRecorder 编排 + RecordingManifest / RunTraceEvent schema 校验测试。

不依赖真机：采集源用内存 FakeSource 替换（monkeypatch ``_make_source``）。
schema 校验使用 whale-mobile 仓 tests/_vendor 的 jsonschema 与
contracts/v2 的 schema 文件；缺依赖时 skip 而非失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from pathlib import Path

import pytest

from whale_core.devices.recording import RecordingOptions, RecordingStore, RunRecorder
from whale_core.devices.recording._types import RawRecording
from whale_core.devices.schema import contracts_dir
from whale_core.devices.recording.run_recorder import (
    RecorderStateError,
    device_id_from_serial,
)

# whale-mobile 仓的 vendor jsonschema + contracts/v2 schema
WHALE_MOBILE = Path(os.environ.get("WHALE_MOBILE_ROOT", Path(__file__).resolve().parents[2] / "whale-mobile"))
VENDOR_DIR = WHALE_MOBILE / "tests" / "_vendor"
SCHEMA_DIR = WHALE_MOBILE / "contracts" / "v2" if "WHALE_MOBILE_ROOT" in os.environ else contracts_dir().parent

if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

jsonschema = pytest.importorskip("jsonschema", reason="需要 whale-mobile tests/_vendor 的 jsonschema")

RUN_ID = "run_01J9XK3EM8TQ2ZP0VN6W4RA7YC"


def run(coro):
    return asyncio.run(coro)


class FakeSource:
    """内存采集源：不落视频以外的任何东西，可控失败。"""

    def __init__(self, name: str, fail_on_start: bool = False) -> None:
        self.name = name
        self.fail_on_start = fail_on_start
        self.started = False

    async def start(self, run_id, device_serial, options, dest_dir):
        if self.fail_on_start:
            raise RuntimeError(f"{self.name} 启动失败（模拟）")
        self.started = True
        self._dest = dest_dir
        self._t0 = 1_700_000_000_000
        (dest_dir / "video.h264").write_bytes(b"\x00\x00\x00\x01\x67fake")

    async def stop(self):
        return RawRecording(
            file_path=self._dest / "video.h264",
            started_at_ms=self._t0,
            stopped_at_ms=self._t0 + 8000,
            source=self.name,
            width=1264,
            height=1680,
            frame_count=240,
            first_frame_wall_ms=self._t0 + 500,
            measured_fps=30.0,
        )


@pytest.fixture()
def store(tmp_path):
    return RecordingStore(tmp_path / "recordings")


def _make_recorder(store, source: FakeSource) -> RunRecorder:
    recorder = RunRecorder(store)
    recorder._make_source = lambda name: source  # type: ignore[method-assign]
    return recorder


async def _drive_full_run(recorder: RunRecorder) -> None:
    await recorder.start("100.92.91.85:5555", run_id=RUN_ID)
    await recorder.record_event(
        "task_started", task_summary="在视频应用中搜索指定内容"
    )
    await recorder.record_event(
        "action_completed",
        action={"type": "device.tap", "label": "搜索按钮", "target": "element:23"},
        result="succeeded",
        verification={"status": "passed", "summary": "搜索页面已打开"},
    )
    await recorder.pause()
    await recorder.resume()
    await recorder.record_event(
        "task_completed", result="succeeded",
        frame_ref=f"recording:{RUN_ID}/frame_000003",
    )
    await recorder.stop()


def test_full_run_produces_schema_valid_manifest_and_trace(store):
    recorder = RunRecorder(store)
    fake = FakeSource("scrcpy")
    recorder._make_source = lambda name: fake  # type: ignore[method-assign]
    run(_drive_full_run(recorder))

    run_dir = store.run_dir(RUN_ID)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    trace = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    # manifest 过 contracts/v2 objects/recording-manifest.schema.json
    manifest_schema = json.loads(
        (SCHEMA_DIR / "objects" / "recording-manifest.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(manifest_schema).validate(manifest)
    assert manifest["run_id"] == RUN_ID
    assert manifest["recorder"] == "scrcpy"
    assert manifest["status"] == "completed"
    assert manifest["resolution"] == {"width": 1264, "height": 1680}
    assert manifest["frame_rate"] == 30
    assert manifest["video_ref"] == f"recording:{RUN_ID}/video.h264"
    assert manifest["trace_ref"] == f"recording:{RUN_ID}/trace.jsonl"

    # 每条 trace 事件过 objects/run-trace-event.schema.json
    event_schema = json.loads(
        (SCHEMA_DIR / "objects" / "run-trace-event.schema.json").read_text()
    )
    validator = jsonschema.Draft202012Validator(event_schema)
    assert [e["seq"] for e in trace] == [1, 2, 3]
    for event in trace:
        validator.validate(event)

    # 暂停区间写入 recorder_state.json（合同外字段不进 manifest）
    state = json.loads((run_dir / "recorder_state.json").read_text(encoding="utf-8"))
    assert len(state["paused_segments"]) == 1
    start, end = state["paused_segments"][0]
    assert end >= start
    assert "paused_segments" not in manifest


def test_scrcpy_failure_falls_back_to_screenrecord(store):
    recorder = RunRecorder(store)
    calls: list[str] = []

    def make_source(name):
        calls.append(name)
        return FakeSource(name, fail_on_start=(name == "scrcpy"))

    recorder._make_source = make_source  # type: ignore[method-assign]

    async def drive():
        await recorder.start("serial-x", run_id=RUN_ID)
        await recorder.stop()

    run(drive())
    assert calls == ["scrcpy", "screenrecord"]
    manifest = json.loads(
        (store.run_dir(RUN_ID) / "manifest.json").read_text(encoding="utf-8")
    )
    # manifest 记录实际使用的源
    assert manifest["recorder"] == "screenrecord"


def test_both_sources_fail_raises(store):
    recorder = RunRecorder(store)
    recorder._make_source = lambda name: FakeSource(name, fail_on_start=True)  # type: ignore[method-assign]
    with pytest.raises(RecorderStateError):
        run(recorder.start("serial-x", run_id=RUN_ID))
    assert recorder.state == "failed"


def test_state_machine_rejects_illegal_ops(store):
    recorder = _make_recorder(store, FakeSource("scrcpy"))
    with pytest.raises(RecorderStateError):
        run(recorder.record_event("note"))
    with pytest.raises(RecorderStateError):
        run(recorder.stop())
    with pytest.raises(RecorderStateError):
        run(recorder.pause())

    run(recorder.start("serial-x", run_id=RUN_ID))
    with pytest.raises(RecorderStateError):
        run(recorder.start("serial-x", run_id=RUN_ID))  # 重复 start
    with pytest.raises(RecorderStateError):
        run(recorder.resume())  # 未 pause
    run(recorder.pause())
    with pytest.raises(RecorderStateError):
        run(recorder.pause())   # 重复 pause
    run(recorder.resume())
    run(recorder.stop())
    with pytest.raises(RecorderStateError):
        run(recorder.record_event("note"))  # stop 后不能再写事件


def test_device_id_from_serial_matches_contract_pattern():
    device_id = device_id_from_serial("100.92.91.85:5555")
    assert len(device_id) == 32
    assert all(c in "0123456789abcdef" for c in device_id)
    # 同 serial 幂等
    assert device_id == device_id_from_serial("100.92.91.85:5555")


def test_record_event_rejects_after_failed_start(store):
    recorder = RunRecorder(store)
    recorder._make_source = lambda name: FakeSource(name, fail_on_start=True)  # type: ignore[method-assign]
    with pytest.raises(RecorderStateError):
        run(recorder.start("serial-x", run_id=RUN_ID))
    with pytest.raises(RecorderStateError):
        run(recorder.record_event("note"))


def test_options_default_no_recording_profile(store):
    # 默认 profile = phone_only；偏好源 = scrcpy
    opts = RecordingOptions()
    assert opts.profile == "phone_only"
    assert opts.prefer_source == "scrcpy"
