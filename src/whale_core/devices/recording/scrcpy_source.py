"""纯 Python 极简 scrcpy 客户端（scrcpy-server v3.3.1，video-only）。

不依赖 scrcpy desktop：直接 push 服务端 jar、``adb forward localabstract:``
建隧道、解析服务端写出的元数据与 packet 流，落盘 raw H.264 Annex-B 及
每帧 pts 侧车文件。

协议依据（v3.3.1 源码，github.com/Genymobile/scrcpy）：
- 启动参数：``Options.parse``（server/.../Options.java）——第一个参数必须是
  客户端版本号 "3.3.1"，其余为 ``key=value``；本模块用到
  ``scid``（十六进制 int）/ ``tunnel_forward`` / ``video`` / ``audio`` /
  ``control`` / ``max_size`` / ``video_bit_rate`` / ``max_fps`` /
  ``cleanup`` / ``log_level``。
- socket 名：``DesktopConnection.getSocketName``（device/DesktopConnection.java）
  —— scid == -1 时为 ``scrcpy``，否则 ``scrcpy_%08x``。
- 流布局（video socket，tunnel_forward）：
  1. 1 字节 dummy byte（``send_dummy_byte`` 默认 true，供客户端探测连接失败）；
  2. 64 字节设备名（``DEVICE_NAME_FIELD_LENGTH = 64``）；
  3. 12 字节 codec meta（``Streamer.writeVideoHeader``：codec_id u32 BE +
     width u32 BE + height u32 BE；H.264 的 codec_id 为 ASCII "h264"）；
  4. 连续 packet（``Streamer.writeFrameMeta``）：12 字节头
     ``[pts_and_flags u64 BE][packet_size u32 BE]`` 后跟 payload；
     bit63 = PACKET_FLAG_CONFIG（SPS/PPS，pts 无意义），
     bit62 = PACKET_FLAG_KEY_FRAME；pts 单位微秒。
- payload 即 MediaCodec 输出的 raw H.264 Annex-B NAL 流，可直接拼成 .h264。

停止：杀本地 adb shell 进程 + 设备端 ``pkill -f com.genymobile.scrcpy.Server``，
remove forward；``cleanup=true`` 让服务端退出时恢复设备显示状态。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import struct

from pathlib import Path

from ..transports.base import now_ms
from ._adb import DEFAULT_ADB_PATH, AdbError, adb, adb_shell_text, local_sha256, remote_sha256
from ._types import RawRecording, RecordingOptions

# ---------------------------------------------------------------------------
# 协议常量（v3.3.1 源码逐字对应，见模块 docstring）
# ---------------------------------------------------------------------------

SCRCPY_VERSION = "3.3.1"
DEVICE_NAME_FIELD_LENGTH = 64          # DesktopConnection.DEVICE_NAME_FIELD_LENGTH
CODEC_META_LENGTH = 12                 # Streamer.writeVideoHeader
PACKET_META_LENGTH = 12                # Streamer.writeFrameMeta
PACKET_FLAG_CONFIG = 1 << 63           # Streamer.PACKET_FLAG_CONFIG
PACKET_FLAG_KEY_FRAME = 1 << 62        # Streamer.PACKET_FLAG_KEY_FRAME
CODEC_ID_H264 = 0x68323634             # "h264"（util/Codec.java）

REMOTE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
# 随仓外下发的 scrcpy-server v3.3.1（dex）。sha256 由 scripts/record_smoke.py
# 实测固定，变更服务端文件时必须同步更新。
DEFAULT_SERVER_JAR = "/tmp/scrcpy-server-v3.3.1"
KNOWN_SERVER_SHA256 = "a0f70b20aa4998fbf658c94118cd6c8dab6abbb0647a3bdab344d70bc1ebcbb8"

CONNECT_ATTEMPTS = 50                  # 等服务端 listen 的 connect 重试次数
CONNECT_RETRY_DELAY_S = 0.1
DUMMY_BYTE_TIMEOUT_S = 5.0
HEADER_TIMEOUT_S = 10.0
READ_CHUNK = 64 * 1024
SERVER_STDERR_MAX = 64 * 1024          # 服务端 stderr 诊断有界缓冲


class ScrcpyError(Exception):
    """scrcpy 源错误（push/启动/流解析失败），上层据此降级 screenrecord。"""


# ---------------------------------------------------------------------------
# 流头 / packet 解析（纯函数 + StreamReader 版本，便于测试）
# ---------------------------------------------------------------------------

def parse_device_meta(data: bytes) -> str:
    """解析 64 字节设备名（0 填充，UTF-8）。"""
    if len(data) != DEVICE_NAME_FIELD_LENGTH:
        raise ScrcpyError(f"设备元数据长度错误: {len(data)}")
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def parse_codec_meta(data: bytes) -> tuple[int, int, int]:
    """解析 12 字节 codec meta，返回 (codec_id, width, height)。"""
    if len(data) != CODEC_META_LENGTH:
        raise ScrcpyError(f"codec 元数据长度错误: {len(data)}")
    codec_id, width, height = struct.unpack(">III", data)
    if width == 0 or height == 0:
        raise ScrcpyError(f"codec 元数据分辨率非法: {width}x{height}")
    return codec_id, width, height


def parse_packet_meta(data: bytes) -> tuple[int | None, int, bool, bool]:
    """解析 12 字节 packet 头，返回 (pts_us, size, is_config, is_key_frame)。

    config 包 pts 无意义，返回 None。
    """
    if len(data) != PACKET_META_LENGTH:
        raise ScrcpyError(f"packet 元数据长度错误: {len(data)}")
    pts_and_flags, size = struct.unpack(">QI", data)
    is_config = bool(pts_and_flags & PACKET_FLAG_CONFIG)
    is_key_frame = bool(pts_and_flags & PACKET_FLAG_KEY_FRAME)
    pts_us = None if is_config else (pts_and_flags & ~(1 << 63) & ~(1 << 62))
    return pts_us, size, is_config, is_key_frame


async def _read_exactly(
    reader: asyncio.StreamReader, n: int, timeout_s: float
) -> bytes:
    try:
        return await asyncio.wait_for(reader.readexactly(n), timeout=timeout_s)
    except asyncio.IncompleteReadError as e:
        raise ScrcpyError(f"流提前结束（期望 {n} 字节，实得 {len(e.partial)}）") from e
    except asyncio.TimeoutError as e:
        raise ScrcpyError(f"读流超时（{n} 字节 / {timeout_s}s）") from e


# ---------------------------------------------------------------------------
# scrcpy 源录制器
# ---------------------------------------------------------------------------

class ScrcpySourceRecorder:
    """scrcpy-server 采集源（SourceRecorder 实现）。"""

    def __init__(
        self,
        *,
        adb_path: str = DEFAULT_ADB_PATH,
        server_jar: str = DEFAULT_SERVER_JAR,
        server_sha256: str = KNOWN_SERVER_SHA256,
    ) -> None:
        self._adb_path = adb_path
        self._server_jar = server_jar
        self._server_sha256 = server_sha256

        self._serial = ""
        self._scid = -1
        self._forward_port = 0
        self._server_proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._dest_dir: Path | None = None
        self._video_file = None  # type: ignore[assignment]
        self._frames: list[dict] = []
        self._started_at_ms = 0
        self._first_frame_wall_ms = 0
        self._width = 0
        self._height = 0
        self._device_name = ""
        self._reader_error: BaseException | None = None

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
        self._serial = device_serial
        self._dest_dir = dest_dir
        try:
            await self._push_server()
            await self._setup_forward()
            await self._launch_server(options)
            await self._connect_and_spawn_reader()
        except BaseException:
            await self._cleanup_after_failed_start()
            raise
        self._started_at_ms = now_ms()

    async def stop(self) -> RawRecording:
        stopped_at_ms = now_ms()
        # 先停读循环，再杀服务端，最后拆隧道；全部幂等。
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        await self._kill_server()
        await self._remove_forward()
        if self._video_file is not None:
            self._video_file.close()
            self._video_file = None
        self._write_frames_sidecar()
        # 读循环曾出错：录制不完整但仍产出已有内容，由上层标记 partial。
        if self._reader_error is not None:
            raise ScrcpyError(f"采集中途失败: {self._reader_error}")

        video_path = (self._dest_dir or Path(".")) / "video.h264"
        media_frames = [f for f in self._frames if not f["config"]]
        duration_ms = max(1, stopped_at_ms - self._started_at_ms)
        # 优先按 pts 跨度算 fps（变帧率设备如墨水屏只有画面变化才出帧，
        # 用墙钟跨度会把 remux 时长拉偏）；无 pts 信息时退回墙钟跨度。
        pts_span_ms = max(
            (f["pts_rel_ms"] or 0 for f in media_frames), default=0
        )
        if pts_span_ms > 0:
            measured_fps = len(media_frames) / (pts_span_ms / 1000)
        else:
            measured_fps = len(media_frames) / (duration_ms / 1000) if media_frames else 0.0
        return RawRecording(
            file_path=video_path,
            started_at_ms=self._started_at_ms,
            stopped_at_ms=stopped_at_ms,
            source="scrcpy",
            width=self._width,
            height=self._height,
            frame_count=len(media_frames),
            first_frame_wall_ms=self._first_frame_wall_ms or self._started_at_ms,
            measured_fps=measured_fps,
        )

    # ------------------------------------------------------------------
    # 内部：部署与隧道
    # ------------------------------------------------------------------

    async def _push_server(self) -> None:
        """幂等 push：远端 sha256 一致则跳过。"""
        local_digest = local_sha256(self._server_jar)
        if self._server_sha256 and local_digest != self._server_sha256:
            raise ScrcpyError(
                f"本地 scrcpy-server sha256 不符: {local_digest} "
                f"!= {self._server_sha256}"
            )
        remote_digest = await remote_sha256(
            self._serial, REMOTE_SERVER_PATH, adb_path=self._adb_path
        )
        if remote_digest == local_digest:
            return
        await adb(
            self._serial, "push", self._server_jar, REMOTE_SERVER_PATH,
            timeout_s=60, adb_path=self._adb_path,
        )

    async def _setup_forward(self) -> None:
        # scid 随机 31bit；socket 名 scrcpy_%08x（DesktopConnection.getSocketName）
        self._scid = secrets.randbits(31)
        # 让 adb 分配空闲本地端口：tcp:0 语法（adb ≥ 1.0.41 支持）
        out = await adb(
            self._serial, "forward", "tcp:0",
            f"localabstract:scrcpy_{self._scid:08x}",
            adb_path=self._adb_path,
        )
        try:
            self._forward_port = int(out.stdout.decode().strip())
        except ValueError as e:
            raise ScrcpyError(f"adb forward 未返回端口号: {out.stdout!r}") from e

    async def _remove_forward(self) -> None:
        if not self._forward_port:
            return
        try:
            await adb(
                self._serial, "forward", "--remove", f"tcp:{self._forward_port}",
                adb_path=self._adb_path,
            )
        except AdbError:
            pass
        self._forward_port = 0

    # ------------------------------------------------------------------
    # 内部：服务端进程
    # ------------------------------------------------------------------

    def _server_command(self, options: RecordingOptions) -> str:
        # 参数拼法依据 Options.parse（v3.3.1）：版本号首位 + key=value
        args = [
            SCRCPY_VERSION,
            f"scid={self._scid:x}",
            "log_level=info",
            "tunnel_forward=true",
            "video=true",
            "audio=false",
            "control=false",
            f"max_size={options.max_size}",
            f"video_bit_rate={options.video_bit_rate}",
            f"max_fps={options.max_fps}",
            "cleanup=true",
            "send_dummy_byte=true",
        ]
        return (
            f"CLASSPATH={REMOTE_SERVER_PATH} app_process / "
            f"com.genymobile.scrcpy.Server {' '.join(args)}"
        )

    async def _launch_server(self, options: RecordingOptions) -> None:
        self._server_proc = await asyncio.create_subprocess_exec(
            self._adb_path, "-s", self._serial, "shell",
            self._server_command(options),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _kill_server(self) -> None:
        proc, self._server_proc = self._server_proc, None
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        # 设备端兜底：app_process 可能在 adb 断开后残留
        if self._serial:
            try:
                await adb_shell_text(
                    self._serial, "pkill -f com.genymobile.scrcpy.Server",
                    timeout_s=5, adb_path=self._adb_path,
                )
            except AdbError:
                pass

    # ------------------------------------------------------------------
    # 内部：连接与读流
    # ------------------------------------------------------------------

    async def _connect_and_spawn_reader(self) -> None:
        reader, writer = await self._connect_with_retry()
        self._reader_task = asyncio.create_task(self._read_loop(reader, writer))

    async def _connect_with_retry(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """连接本地 forward 端口。adb forward 在服务未 listen 时立即 EOF，
        因此可安全重试；服务端一旦 accept 会立刻发 dummy byte。"""
        last_error: BaseException | None = None
        for _ in range(CONNECT_ATTEMPTS):
            if self._server_proc is not None and self._server_proc.returncode is not None:
                stderr = b""
                if self._server_proc.stderr is not None:
                    stderr = await self._server_proc.stderr.read(SERVER_STDERR_MAX)
                raise ScrcpyError(
                    "scrcpy-server 提前退出: "
                    + stderr.decode(errors="replace")[:500]
                )
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", self._forward_port
                )
            except OSError as e:
                last_error = e
                await asyncio.sleep(CONNECT_RETRY_DELAY_S)
                continue
            try:
                dummy = await asyncio.wait_for(
                    reader.readexactly(1), timeout=DUMMY_BYTE_TIMEOUT_S
                )
                return reader, writer
            except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                # EOF：服务端尚未 listen，可重试；超时：服务端已 accept 但没发
                # dummy byte（异常状态），放弃本次连接再试
                last_error = e
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                await asyncio.sleep(CONNECT_RETRY_DELAY_S)
        raise ScrcpyError(f"连接 scrcpy-server 失败: {last_error}")

    async def _read_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            device_meta = await _read_exactly(
                reader, DEVICE_NAME_FIELD_LENGTH, HEADER_TIMEOUT_S
            )
            self._device_name = parse_device_meta(device_meta)
            codec_meta = await _read_exactly(
                reader, CODEC_META_LENGTH, HEADER_TIMEOUT_S
            )
            codec_id, self._width, self._height = parse_codec_meta(codec_meta)
            if codec_id != CODEC_ID_H264:
                raise ScrcpyError(f"非 H.264 流（codec_id={codec_id:#x}）")

            assert self._dest_dir is not None
            self._video_file = open(self._dest_dir / "video.h264", "wb")
            first_pts_us: int | None = None
            while True:
                try:
                    meta = await asyncio.wait_for(
                        reader.readexactly(PACKET_META_LENGTH), timeout=3600
                    )
                except asyncio.IncompleteReadError as e:
                    if not e.partial:
                        break  # 服务端正常关闭（stop 杀进程后的 EOF）
                    raise ScrcpyError(
                        f"packet 头读了一半（{len(e.partial)}/12 字节）流断了"
                    ) from e
                except asyncio.TimeoutError as e:
                    raise ScrcpyError("读 packet 头超时") from e
                pts_us, size, is_config, is_key = parse_packet_meta(meta)
                # 有界落盘：payload 分块写文件，不在内存累积
                remaining = size
                while remaining > 0:
                    chunk = await _read_exactly(
                        reader, min(READ_CHUNK, remaining), timeout_s=3600
                    )
                    self._video_file.write(chunk)
                    remaining -= len(chunk)
                self._video_file.flush()  # 每 packet 落盘，进程被杀也不丢已收帧
                wall = now_ms()
                if self._first_frame_wall_ms == 0 and not is_config:
                    self._first_frame_wall_ms = wall
                if pts_us is not None and first_pts_us is None:
                    first_pts_us = pts_us
                self._frames.append({
                    "pts_us": pts_us,
                    "pts_rel_ms": (
                        round((pts_us - first_pts_us) / 1000, 3)
                        if pts_us is not None and first_pts_us is not None else None
                    ),
                    "size": size,
                    "config": is_config,
                    "keyframe": is_key,
                    "wall_ms": wall,
                })
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # 记录错误，stop() 时上报
            self._reader_error = e
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _write_frames_sidecar(self) -> None:
        if self._dest_dir is None:
            return
        sidecar = {
            "device_name": self._device_name,
            "width": self._width,
            "height": self._height,
            "started_at_ms": self._started_at_ms,
            "first_frame_wall_ms": self._first_frame_wall_ms,
            "frames": self._frames,
        }
        (self._dest_dir / "frames.json").write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )

    async def _cleanup_after_failed_start(self) -> None:
        await self._kill_server()
        await self._remove_forward()
        if self._video_file is not None:
            self._video_file.close()
            self._video_file = None
