"""scrcpy 源流头/packet 解析与读流循环测试（不依赖真机）。

流字节格式依据 scrcpy v3.3.1 源码构造：
DesktopConnection.sendDeviceMeta（64B 设备名）、Streamer.writeVideoHeader
（12B codec meta）、Streamer.writeFrameMeta（12B packet 头 + payload）。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from whale_core.devices.recording import scrcpy_source
from whale_core.devices.recording.scrcpy_source import (
    CODEC_ID_H264,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    ScrcpyError,
    ScrcpySourceRecorder,
    parse_codec_meta,
    parse_device_meta,
    parse_packet_meta,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 字节流构造助手
# ---------------------------------------------------------------------------

def make_device_meta(name: str = "Leaf5") -> bytes:
    data = name.encode()[:63]
    return data + b"\x00" * (64 - len(data))


def make_codec_meta(codec_id: int = CODEC_ID_H264, w: int = 1264, h: int = 1680) -> bytes:
    return struct.pack(">III", codec_id, w, h)


def make_packet(pts_us: int | None, payload: bytes, *, config=False, key=False) -> bytes:
    if config:
        pts_and_flags = PACKET_FLAG_CONFIG
    else:
        pts_and_flags = (pts_us or 0) | (PACKET_FLAG_KEY_FRAME if key else 0)
    return struct.pack(">QI", pts_and_flags, len(payload)) + payload


# ---------------------------------------------------------------------------
# 纯解析函数
# ---------------------------------------------------------------------------

def test_parse_device_meta():
    assert parse_device_meta(make_device_meta("Leaf5")) == "Leaf5"
    assert parse_device_meta(make_device_meta("")) == ""


def test_parse_device_meta_bad_length():
    with pytest.raises(ScrcpyError):
        parse_device_meta(b"\x00" * 10)


def test_parse_codec_meta():
    codec_id, w, h = parse_codec_meta(make_codec_meta())
    assert codec_id == CODEC_ID_H264
    assert (w, h) == (1264, 1680)


def test_parse_codec_meta_zero_size():
    with pytest.raises(ScrcpyError):
        parse_codec_meta(make_codec_meta(w=0, h=0))


def test_parse_packet_meta_plain_frame():
    pts, size, config, key = parse_packet_meta(
        struct.pack(">QI", 123456, 100)
    )
    assert (pts, size, config, key) == (123456, 100, False, False)


def test_parse_packet_meta_config_flag():
    pts, size, config, key = parse_packet_meta(
        struct.pack(">QI", PACKET_FLAG_CONFIG, 30)
    )
    assert pts is None and config and not key and size == 30


def test_parse_packet_meta_keyframe_flag():
    pts, size, config, key = parse_packet_meta(
        struct.pack(">QI", PACKET_FLAG_KEY_FRAME | 5000, 200)
    )
    assert (pts, size, config, key) == (5000, 200, False, True)


# ---------------------------------------------------------------------------
# 读流循环（构造完整字节流喂给 _read_loop）
# ---------------------------------------------------------------------------

class _FakeWriter:
    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_read_loop_parses_headers_and_packets(tmp_path):
    payload1 = b"\x00\x00\x00\x01\x67\x64" + b"\xaa" * 10   # config（SPS）
    payload2 = b"\x00\x00\x00\x01\x65" + b"\xbb" * 50        # keyframe
    payload3 = b"\x00\x00\x00\x01\x41" + b"\xcc" * 20        # 普通帧
    stream = (
        make_device_meta("Leaf5")
        + make_codec_meta()
        + make_packet(None, payload1, config=True)
        + make_packet(1_000_000, payload2, key=True)
        + make_packet(1_033_333, payload3)
    )

    async def drive():
        reader = asyncio.StreamReader()
        reader.feed_data(stream)
        reader.feed_eof()
        rec = ScrcpySourceRecorder()
        rec._dest_dir = tmp_path
        await rec._read_loop(reader, _FakeWriter())
        return rec

    rec = run(drive())
    assert rec._device_name == "Leaf5"
    assert (rec._width, rec._height) == (1264, 1680)
    assert rec._reader_error is None
    # 落盘内容 = 三个 payload 依序拼接（raw Annex-B）
    assert (tmp_path / "video.h264").read_bytes() == payload1 + payload2 + payload3
    # 帧记录：config + 2 帧；pts_rel_ms 相对首个非 config pts
    assert len(rec._frames) == 3
    assert rec._frames[0]["config"] is True
    assert rec._frames[1]["keyframe"] is True
    assert rec._frames[1]["pts_rel_ms"] == 0.0
    assert rec._frames[2]["pts_rel_ms"] == pytest.approx(33.333, abs=0.01)
    assert rec._first_frame_wall_ms != 0


def test_read_loop_rejects_non_h264(tmp_path):
    stream = make_device_meta() + make_codec_meta(codec_id=0x68657663)  # "hevc"

    async def drive():
        reader = asyncio.StreamReader()
        reader.feed_data(stream)
        reader.feed_eof()
        rec = ScrcpySourceRecorder()
        rec._dest_dir = tmp_path
        await rec._read_loop(reader, _FakeWriter())
        return rec

    rec = run(drive())
    assert isinstance(rec._reader_error, ScrcpyError)


def test_read_loop_truncated_stream_records_error(tmp_path):
    stream = make_device_meta() + make_codec_meta() + b"\x00" * 5  # 半个 packet 头

    async def drive():
        reader = asyncio.StreamReader()
        reader.feed_data(stream)
        reader.feed_eof()
        rec = ScrcpySourceRecorder()
        rec._dest_dir = tmp_path
        await rec._read_loop(reader, _FakeWriter())
        return rec

    rec = run(drive())
    assert isinstance(rec._reader_error, ScrcpyError)


def test_server_command_uses_v331_key_value_args():
    rec = ScrcpySourceRecorder()
    rec._scid = 0x0000ABCD
    from whale_core.devices.recording import RecordingOptions
    cmd = rec._server_command(RecordingOptions(max_size=1280))
    assert "com.genymobile.scrcpy.Server 3.3.1" in cmd
    assert "scid=abcd" in cmd
    assert "tunnel_forward=true" in cmd
    assert "audio=false" in cmd
    assert "control=false" in cmd
    assert "max_size=1280" in cmd
    assert "video_bit_rate=4000000" in cmd
    assert "max_fps=30" in cmd
    assert "cleanup=true" in cmd
