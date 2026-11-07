"""screenrecord 兜底采集源（scrcpy 不可用时的降级路径）。

``adb shell screenrecord`` 单段硬上限 180s（Android 平台限制）：超过时
顺序启动多段，停止后 ``adb pull`` 拉回并用 ffmpeg concat（-c copy，
无需重编码）拼成单个 ``video.mp4``。

停止方式：对设备端 screenrecord 进程发 SIGINT（``kill -INT``），让其
正常收尾 MP4（直接 kill 会丢 moov，文件无法解码）。
"""

from __future__ import annotations

import asyncio

from pathlib import Path

from ..transports.base import now_ms
from ._adb import DEFAULT_ADB_PATH, AdbError, adb, adb_shell_text
from ._types import RawRecording, RecordingOptions

SEGMENT_LIMIT_S = 180                  # Android screenrecord 单段上限
REMOTE_DIR = "/data/local/tmp"
STOP_GRACE_S = 10.0                    # SIGINT 后等待收尾的时间


class ScreenRecordError(Exception):
    """screenrecord 源错误。"""


async def _concat_segments(
    segment_paths: list[Path], out_path: Path, *, ffmpeg_path: str = "ffmpeg"
) -> None:
    """多段 MP4 用 ffmpeg concat demuxer 无损拼接（-c copy）。"""
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text(
        "".join(f"file '{p}'\n" for p in segment_paths), encoding="utf-8"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", "-movflags", "+faststart",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise ScreenRecordError(
                "ffmpeg concat 失败: " + stderr.decode(errors="replace")[-500:]
            )
    finally:
        list_file.unlink(missing_ok=True)


class ScreenRecordSourceRecorder:
    """screenrecord 采集源（SourceRecorder 实现，scrcpy 的降级路径）。"""

    def __init__(self, *, adb_path: str = DEFAULT_ADB_PATH) -> None:
        self._adb_path = adb_path
        self._serial = ""
        self._run_id = ""
        self._dest_dir: Path | None = None
        self._options = RecordingOptions(prefer_source="screenrecord")
        self._segment_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._current_proc: asyncio.subprocess.Process | None = None
        self._started_at_ms = 0
        self._segment_count = 0
        self._segment_error: BaseException | None = None

    # ------------------------------------------------------------------
    # SourceRecorder 接口
    # ------------------------------------------------------------------

    async def start(
        self,
        run_id: str,
        device_serial: str,
        options: RecordingOptions,
        dest_dir: Path,
    ) -> None:
        self._run_id = run_id
        self._serial = device_serial
        self._options = options
        self._dest_dir = dest_dir
        self._started_at_ms = now_ms()
        self._stop_event.clear()
        self._segment_task = asyncio.create_task(self._segment_loop())
        # 启动失败快速失败：给第一段 1s 时间暴露错误（如设备不支持）
        await asyncio.sleep(1.0)
        if self._segment_error is not None:
            raise ScreenRecordError(f"screenrecord 启动失败: {self._segment_error}")

    async def stop(self) -> RawRecording:
        self._stop_event.set()
        await self._interrupt_current()
        if self._segment_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._segment_task), timeout=STOP_GRACE_S * 2
                )
            except (asyncio.TimeoutError, Exception):
                self._segment_task.cancel()
                try:
                    await self._segment_task
                except BaseException:
                    pass
            self._segment_task = None
        stopped_at_ms = now_ms()
        if self._segment_error is not None and self._segment_count == 0:
            raise ScreenRecordError(f"screenrecord 失败: {self._segment_error}")
        video_path = await self._pull_and_concat()
        await self._cleanup_remote()
        return RawRecording(
            file_path=video_path,
            started_at_ms=self._started_at_ms,
            stopped_at_ms=stopped_at_ms,
            source="screenrecord",
            first_frame_wall_ms=self._started_at_ms,  # 无帧级信息，近似取启动时刻
        )

    # ------------------------------------------------------------------
    # 内部：分段录制
    # ------------------------------------------------------------------

    async def _segment_loop(self) -> None:
        """顺序录制多段（每段 ≤180s），直到 stop_event 或达到 max_duration。"""
        elapsed_s = 0
        while not self._stop_event.is_set():
            remaining = self._options.max_duration_s - elapsed_s
            if remaining <= 0:
                break
            limit = min(SEGMENT_LIMIT_S, remaining)
            index = self._segment_count
            remote = f"{REMOTE_DIR}/rec_{self._run_id}_{index}.mp4"
            try:
                await self._record_segment(remote, limit)
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                self._segment_error = e
                break
            else:
                self._segment_count = index + 1
                elapsed_s += limit

    async def _record_segment(self, remote: str, limit_s: int) -> None:
        cmd = f"screenrecord --time-limit {limit_s} {remote}"
        self._current_proc = await asyncio.create_subprocess_exec(
            self._adb_path, "-s", self._serial, "shell", cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = self._current_proc
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=limit_s + 30
            )
            if proc.returncode not in (0, None) and not self._stop_event.is_set():
                raise ScreenRecordError(
                    f"screenrecord 段失败（rc={proc.returncode}）: "
                    + stderr.decode(errors="replace")[-300:]
                )
        finally:
            self._current_proc = None

    async def _interrupt_current(self) -> None:
        """SIGINT 设备端 screenrecord，让 MP4 正常收尾。"""
        try:
            await adb_shell_text(
                self._serial, "kill -INT $(pidof screenrecord)",
                timeout_s=5, adb_path=self._adb_path,
            )
        except AdbError:
            pass
        proc = self._current_proc
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_S)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    # ------------------------------------------------------------------
    # 内部：回收产物
    # ------------------------------------------------------------------

    async def _pull_and_concat(self) -> Path:
        assert self._dest_dir is not None
        local_segments: list[Path] = []
        for i in range(self._segment_count):
            remote = f"{REMOTE_DIR}/rec_{self._run_id}_{i}.mp4"
            local = self._dest_dir / f"segment_{i:03d}.mp4"
            await adb(
                self._serial, "pull", remote, str(local),
                timeout_s=120, adb_path=self._adb_path,
            )
            local_segments.append(local)
        if not local_segments:
            raise ScreenRecordError("没有任何录制分段可取回")
        out_path = self._dest_dir / "video.mp4"
        if len(local_segments) == 1:
            local_segments[0].rename(out_path)
        else:
            await _concat_segments(local_segments, out_path)
            for seg in local_segments:
                seg.unlink(missing_ok=True)
        return out_path

    async def _cleanup_remote(self) -> None:
        try:
            await adb_shell_text(
                self._serial, f"rm -f {REMOTE_DIR}/rec_{self._run_id}_*.mp4",
                timeout_s=10, adb_path=self._adb_path,
            )
        except AdbError:
            pass
