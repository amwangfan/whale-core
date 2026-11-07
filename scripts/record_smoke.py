#!/usr/bin/env python3
"""Agent Run 录制真机冒烟（contracts/v2 §12 链路端到端）。

流程：
1. RunRecorder.start（默认 scrcpy 源，失败自动降级 screenrecord）；
2. 录制 N 秒，期间 ``adb shell input swipe`` 几次产生画面变化，并经
   ``record_event`` 写入 3 条假 RunTraceEvent；
3. stop → 用 frames.json/ffprobe 检查视频帧数/分辨率/时长；
4. export(phone_only) → ffprobe 验证导出时长 ≈ 原始时长；
5. manifest.json 过 contracts/v2 recording-manifest schema（whale-mobile
   tests/_vendor 的 jsonschema）；
6. 打印 JSON 结果汇总。

用法：
    python3 scripts/record_smoke.py [--serial 100.92.91.85:5555] [--duration 8]

环境：WHALE_ADB 可指定 adb 路径；默认把 /opt/android-sdk/platform-tools
加入 PATH。WHALE_MOBILE_ROOT 指定 whale-mobile 仓位置（schema 校验用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from pathlib import Path

# adb 不在默认 PATH 时兜底（N100 的 SDK 位置）
_SDK_PLATFORM_TOOLS = "/opt/android-sdk/platform-tools"
if os.path.isdir(_SDK_PLATFORM_TOOLS):
    os.environ["PATH"] = _SDK_PLATFORM_TOOLS + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from whale_core.devices.recording import RecordingOptions, RecordingStore, RunRecorder  # noqa: E402
from whale_core.devices.recording.video_composer import probe_video  # noqa: E402

WHALE_MOBILE = Path(os.environ.get("WHALE_MOBILE_ROOT", "/opt/whale/whale-mobile"))
FAKE_EVENTS = [
    ("task_started", {"task_summary": "冒烟：滑动屏幕并验证录制"}),
    ("action_completed", {
        "action": {"type": "device.swipe", "label": "上滑桌面", "target": "screen"},
        "result": "succeeded",
        "verification": {"status": "skipped", "summary": "冒烟不做验证"},
    }),
    ("task_completed", {"result": "succeeded"}),
]


async def adb_shell(serial: str, adb: str, command: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        adb, "-s", serial, "shell", command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def wake_device(serial: str, adb: str) -> None:
    """录制前唤醒设备并上滑解锁（灭屏设备上 scrcpy 只能采到黑屏/首帧）。"""
    await adb_shell(serial, adb, "input keyevent KEYCODE_WAKEUP")
    await asyncio.sleep(0.5)
    await adb_shell(serial, adb, "wm dismiss-keyguard")
    await asyncio.sleep(0.5)
    await adb_shell(serial, adb, "input swipe 300 1000 300 300 300")
    await asyncio.sleep(1.0)


async def adb_swipe(serial: str, adb: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        adb, "-s", serial, "shell",
        "input swipe 500 1200 500 400 300",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def validate_manifest(manifest: dict) -> list[str]:
    vendor = WHALE_MOBILE / "tests" / "_vendor"
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["SKIP: jsonschema 不可用"]
    schema = json.loads(
        (WHALE_MOBILE / "contracts/v2/objects/recording-manifest.schema.json")
        .read_text(encoding="utf-8")
    )
    errors = [
        f"{list(e.path)}: {e.message}"
        for e in Draft202012Validator(schema).iter_errors(manifest)
    ]
    return errors or ["PASS"]


async def smoke(serial: str, duration_s: int, adb: str, workdir: Path) -> dict:
    store = RecordingStore(workdir / "recordings")
    recorder = RunRecorder(store, adb_path=adb)
    result: dict = {"serial": serial}

    await wake_device(serial, adb)
    run_id = await recorder.start(
        serial,
        options=RecordingOptions(profile="phone_only", max_size=1280),
    )
    result["run_id"] = run_id
    result["run_dir"] = str(store.run_dir(run_id))

    # 录制期间：3 次滑动 + 3 条假 trace 事件
    event_count = 0
    interval = duration_s / (len(FAKE_EVENTS) + 1)
    for kind, extra in FAKE_EVENTS:
        await asyncio.sleep(interval)
        await adb_swipe(serial, adb)
        await recorder.record_event(kind, **extra)
        event_count += 1
    await asyncio.sleep(interval)

    raw = await recorder.stop()
    run_dir = store.run_dir(run_id)
    result["source"] = raw.source
    result["raw_video"] = str(raw.file_path)
    result["raw_video_bytes"] = raw.file_path.stat().st_size

    # --- 检查原始录像：帧数 / 分辨率 / 时长 ---
    frames_sidecar = run_dir / "frames.json"
    if frames_sidecar.is_file():
        sidecar = json.loads(frames_sidecar.read_text(encoding="utf-8"))
        frames = [f for f in sidecar["frames"] if not f["config"]]
        last_pts_ms = max(
            (f["pts_rel_ms"] or 0 for f in frames), default=0
        )
        result["frames"] = len(frames)
        result["resolution"] = [sidecar["width"], sidecar["height"]]
        result["raw_duration_s"] = round(last_pts_ms / 1000, 2)
        result["measured_fps"] = round(raw.measured_fps, 2)
    else:
        probe = await probe_video(raw.file_path)
        result["frames"] = "n/a(screenrecord)"
        result["resolution"] = [probe["width"], probe["height"]]
        result["raw_duration_s"] = round(probe["duration_s"], 2)

    # --- 导出 phone_only 并校验时长 ---
    export_path = await recorder.export("phone_only", title=f"smoke {run_id}")
    export_probe = await probe_video(export_path)
    result["export"] = str(export_path)
    result["export_duration_s"] = round(export_probe["duration_s"], 2)
    result["export_resolution"] = [export_probe["width"], export_probe["height"]]
    raw_dur = float(result["raw_duration_s"])
    result["duration_match"] = abs(export_probe["duration_s"] - raw_dur) < 1.5

    # --- manifest schema 校验 ---
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    result["manifest"] = manifest
    result["manifest_schema"] = validate_manifest(manifest)
    result["events_recorded"] = event_count
    result["ok"] = (
        result["manifest_schema"] == ["PASS"]
        and result["duration_match"]
        and (result["frames"] == "n/a(screenrecord)" or result["frames"] > duration_s * 2)
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="100.92.91.85:5555")
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--adb", default=os.environ.get("WHALE_ADB", "adb"))
    parser.add_argument(
        "--workdir", type=Path,
        default=Path(os.environ.get("WHALE_RECORDINGS_DIR", "/tmp/whale-recordings-smoke")),
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(smoke(args.serial, args.duration, args.adb, args.workdir))
    except Exception as e:
        print(json.dumps({"serial": args.serial, "ok": False, "error": str(e)},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
