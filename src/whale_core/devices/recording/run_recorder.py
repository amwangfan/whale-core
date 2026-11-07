"""RunRecorder：一次 Agent Run 的录制编排（规划 §9.4 接口）。

职责：
- ``start``：选源（默认 scrcpy，失败自动降级 screenrecord 并在 manifest 的
  ``recorder`` 字段记录实际使用的源），创建 run 目录与 trace.jsonl；
- ``record_event``：追加 RunTraceEvent（每行一个 JSON，seq 自 1 递增，
  只记录可理解的操作说明，绝不记录模型思维链）；
- ``pause`` / ``resume``：scrcpy 源不支持真暂停——诚实实现为把暂停区间
  （墙钟毫秒）记入 ``recorder_state.json`` 的 ``paused_segments``，时间轴
  对齐与导出时跳过对应时间段，画面采集本身不中断；
- ``stop``：停录、收尾 ``manifest.json``（contracts/v2
  objects/recording-manifest.schema.json，不含合同外字段）；
- ``export``：调 video_composer 产出最终 MP4。

状态机对齐 recording.state（§12.2）：
idle → recording ⇄ paused → stopped / failed。
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from pathlib import Path

from ..transports.base import new_id, now_ms
from ._adb import DEFAULT_ADB_PATH
from ._types import RawRecording, RecordingOptions, SourceRecorder
from .scrcpy_source import DEFAULT_SERVER_JAR, KNOWN_SERVER_SHA256, ScrcpySourceRecorder
from .screenrecord_source import ScreenRecordSourceRecorder
from .store import RecordingStore

# recording.state 枚举（§12.2）
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"


class RecorderStateError(Exception):
    """状态机非法转移（如未 start 就 record_event）。"""


def device_id_from_serial(serial: str) -> str:
    """由 adb serial 派生合同 device_id（^[0-9a-f]{32}$，md5 十六进制）。

    真实接入 Device Gateway 时应传入 registry 里的正式 device_id；此函数
    仅保证录制模块自包含时产出的 manifest 过 schema。
    """
    return hashlib.md5(serial.encode("utf-8")).hexdigest()


class RunRecorder:
    """一次 run 的录制编排器（一个实例对应一个 run，不可复用）。"""

    def __init__(
        self,
        store: RecordingStore | None = None,
        *,
        adb_path: str = DEFAULT_ADB_PATH,
        server_jar: str = DEFAULT_SERVER_JAR,
        server_sha256: str = KNOWN_SERVER_SHA256,
    ) -> None:
        self._store = store or RecordingStore()
        self._adb_path = adb_path
        self._server_jar = server_jar
        self._server_sha256 = server_sha256

        self._state = STATE_IDLE
        self._run_id = ""
        self._device_serial = ""
        self._device_id = ""
        self._options = RecordingOptions()
        self._run_dir: Path | None = None
        self._source: SourceRecorder | None = None
        self._source_name_actual: str | None = None
        self._raw: RawRecording | None = None
        self._seq = 0
        self._lock = asyncio.Lock()
        self._paused_segments: list[list[int | None]] = []
        self._trace_file = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_dir(self) -> Path | None:
        return self._run_dir

    @property
    def raw_recording(self) -> RawRecording | None:
        return self._raw

    @property
    def paused_segments(self) -> list[tuple[int, int]]:
        """已闭合的暂停区间（墙钟毫秒）；未闭合区间以 now 补。"""
        result: list[tuple[int, int]] = []
        for start, end in self._paused_segments:
            result.append((int(start), int(end) if end is not None else now_ms()))
        return result

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------

    async def start(
        self,
        device_serial: str,
        *,
        run_id: str | None = None,
        device_id: str | None = None,
        options: RecordingOptions | None = None,
    ) -> str:
        """启动录制，返回 run_id。源选择：prefer_source 失败自动降级另一源。"""
        if self._state != STATE_IDLE:
            raise RecorderStateError(f"当前状态 {self._state}，不能重复 start")
        self._run_id = run_id or new_id("run")
        self._device_serial = device_serial
        self._device_id = device_id or device_id_from_serial(device_serial)
        self._options = options or RecordingOptions()
        self._run_dir = self._store.ensure_run(self._run_id)
        self._trace_file = open(self._run_dir / "trace.jsonl", "a", encoding="utf-8")

        order = (
            ["scrcpy", "screenrecord"]
            if self._options.prefer_source == "scrcpy"
            else ["screenrecord", "scrcpy"]
        )
        last_error: BaseException | None = None
        for source_name in order:
            source = self._make_source(source_name)
            try:
                await source.start(
                    self._run_id, device_serial, self._options, self._run_dir
                )
            except Exception as e:
                last_error = e
                continue
            self._source = source
            self._source_name_actual = source_name
            self._state = STATE_RECORDING
            self._write_state(source_actual=source_name)
            return self._run_id
        self._state = STATE_FAILED
        self._write_state(source_actual=None, error=str(last_error))
        raise RecorderStateError(f"两个采集源均启动失败，最后错误: {last_error}")

    def _make_source(self, name: str) -> SourceRecorder:
        if name == "scrcpy":
            return ScrcpySourceRecorder(
                adb_path=self._adb_path,
                server_jar=self._server_jar,
                server_sha256=self._server_sha256,
            )
        return ScreenRecordSourceRecorder(adb_path=self._adb_path)

    async def stop(self) -> RawRecording:
        """停止录制并收尾 manifest.json，返回 RawRecording。"""
        if self._state not in (STATE_RECORDING, STATE_PAUSED):
            raise RecorderStateError(f"当前状态 {self._state}，不能 stop")
        if self._state == STATE_PAUSED:
            self._close_pause_segment()
        assert self._source is not None
        source_name = self._source_name_actual or "screenrecord"
        status = "completed"
        try:
            self._raw = await self._source.stop()
        except Exception:
            # 源收尾失败：已有内容标记 partial（可能部分可放）
            status = "partial"
            self._raw = RawRecording(
                file_path=(self._run_dir or Path(".")) / "video.h264",
                started_at_ms=0,
                stopped_at_ms=now_ms(),
                source=source_name,
            )
        self._state = STATE_STOPPED
        if self._trace_file is not None:
            self._trace_file.close()
            self._trace_file = None
        self._write_manifest(status=status, recorder=source_name)
        self._write_state(source_actual=source_name)
        return self._raw

    # ------------------------------------------------------------------
    # pause / resume（诚实实现：只记区间，不中断采流）
    # ------------------------------------------------------------------

    async def pause(self) -> None:
        if self._state != STATE_RECORDING:
            raise RecorderStateError(f"当前状态 {self._state}，不能 pause")
        self._paused_segments.append([now_ms(), None])
        self._state = STATE_PAUSED
        self._write_state(source_actual=self._source_name())

    async def resume(self) -> None:
        if self._state != STATE_PAUSED:
            raise RecorderStateError(f"当前状态 {self._state}，不能 resume")
        self._close_pause_segment()
        self._state = STATE_RECORDING
        self._write_state(source_actual=self._source_name())

    def _close_pause_segment(self) -> None:
        if self._paused_segments and self._paused_segments[-1][1] is None:
            self._paused_segments[-1][1] = now_ms()

    # ------------------------------------------------------------------
    # 轨迹
    # ------------------------------------------------------------------

    async def record_event(
        self,
        kind: str,
        *,
        task_summary: str | None = None,
        action: dict | None = None,
        result: str | None = None,
        verification: dict | None = None,
        frame_ref: str | None = None,
        timestamp_ms: int | None = None,
    ) -> dict:
        """追加一条 RunTraceEvent 到 <run_id>.jsonl，返回事件字典。

        只接受可理解的操作说明字段；调用方不得传入思维链内容。
        """
        if self._state not in (STATE_RECORDING, STATE_PAUSED):
            raise RecorderStateError(f"当前状态 {self._state}，不能 record_event")
        async with self._lock:
            self._seq += 1
            event: dict = {
                "run_id": self._run_id,
                "seq": self._seq,
                "timestamp_ms": timestamp_ms if timestamp_ms is not None else now_ms(),
                "kind": kind,
            }
            if task_summary is not None:
                event["task_summary"] = task_summary
            if action is not None:
                event["action"] = action
            if result is not None:
                event["result"] = result
            if verification is not None:
                event["verification"] = verification
            if frame_ref is not None:
                event["frame_ref"] = frame_ref
            assert self._trace_file is not None
            self._trace_file.write(
                json.dumps(event, ensure_ascii=False) + "\n"
            )
            self._trace_file.flush()
            return event

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    async def export(self, profile: str | None = None, *, title: str = "") -> Path:
        """按模板合成最终 MP4，返回导出文件路径。"""
        if self._state != STATE_STOPPED or self._raw is None:
            raise RecorderStateError("只有 stop 之后才能 export")
        from . import video_composer  # 延迟导入：仅导出时需要 ffmpeg
        from .trace_align import align

        assert self._run_dir is not None
        profile = profile or self._options.profile
        duration_ms = self._raw.stopped_at_ms - self._raw.started_at_ms
        aligned = align(
            self._run_dir / "trace.jsonl",
            first_frame_wall_ms=self._raw.first_frame_wall_ms,
            paused_segments=self.paused_segments,
            duration_ms=duration_ms,
        )
        out_path = self._run_dir / f"export_{profile}.mp4"
        await video_composer.compose(
            video_path=self._raw.file_path,
            events=aligned,
            profile=profile,
            out_path=out_path,
            title=title or self._run_id,
            measured_fps=self._raw.measured_fps,
            duration_ms=duration_ms,
        )
        return out_path

    # ------------------------------------------------------------------
    # manifest / state 落盘
    # ------------------------------------------------------------------

    def _source_name(self) -> str | None:
        return self._source_name_actual

    def _video_ref(self) -> str:
        assert self._raw is not None
        suffix = self._raw.file_path.suffix.lstrip(".") or "h264"
        return f"recording:{self._run_id}/video.{suffix}"

    def _write_manifest(self, *, status: str, recorder: str) -> Path:
        """产出 RecordingManifest（严格符合 schema，不含合同外字段）。"""
        assert self._run_dir is not None and self._raw is not None
        manifest: dict = {
            "run_id": self._run_id,
            "device_id": self._device_id,
            "recorder": recorder,
            "started_at": self._raw.started_at_ms,
            "stopped_at": self._raw.stopped_at_ms,
            "video_ref": self._video_ref(),
            "trace_ref": f"recording:{self._run_id}/trace.jsonl",
            "profile": self._options.profile,
            "status": status,
        }
        if self._raw.width and self._raw.height:
            manifest["resolution"] = {
                "width": self._raw.width,
                "height": self._raw.height,
            }
        if self._raw.measured_fps:
            fps = round(self._raw.measured_fps)
            if 1 <= fps <= 120:
                manifest["frame_rate"] = fps
        path = self._run_dir / "manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def _write_state(
        self, *, source_actual: str | None, error: str | None = None
    ) -> None:
        """运行期状态（非合同产物）：paused_segments、实际源、错误等。"""
        if self._run_dir is None:
            return
        state = {
            "run_id": self._run_id,
            "device_serial": self._device_serial,
            "state": self._state,
            "source": source_actual,
            "paused_segments": [
                [int(s), int(e) if e is not None else None]
                for s, e in self._paused_segments
            ],
            "error": error,
        }
        (self._run_dir / "recorder_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
