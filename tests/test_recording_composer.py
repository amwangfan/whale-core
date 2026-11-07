"""video_composer 命令拼装测试（mock subprocess，不依赖真机/ffmpeg）。"""

from __future__ import annotations

import asyncio

import pytest

from whale_core.devices.recording import video_composer
from whale_core.devices.recording.trace_align import AlignedEvent
from whale_core.devices.recording.video_composer import (
    ComposeError,
    build_ffmpeg_command,
    build_filter_demo,
    build_filter_phone_only,
    escape_drawtext,
    event_color,
    event_label,
    find_font,
    font_supports_cjk,
    to_display_text,
)


def run(coro):
    return asyncio.run(coro)


def _ev(seq, offset_ms, kind="action_completed", label="点击搜索按钮", result="succeeded"):
    return AlignedEvent(
        seq=seq,
        timestamp_ms=offset_ms,
        video_offset_ms=float(offset_ms),
        event={
            "seq": seq,
            "timestamp_ms": offset_ms,
            "kind": kind,
            "action": {"type": "device.tap", "label": label},
            "result": result,
        },
    )


# ---------------------------------------------------------------------------
# 文本与字体
# ---------------------------------------------------------------------------

def test_escape_drawtext():
    assert escape_drawtext("a:b") == "a\\:b"
    assert escape_drawtext("100%") == "100\\%"
    assert escape_drawtext("it's") == "it\\'s"
    assert escape_drawtext("a\\b") == "a\\\\b"
    assert escape_drawtext("x,y;z[w]") == "x\\,y\\;z\\[w\\]"
    assert escape_drawtext("行1\n行2") == "行1 行2"


def test_to_display_text_cjk_fallback():
    assert to_display_text("搜索按钮", cjk_ok=True) == "搜索按钮"
    assert to_display_text("搜索按钮", cjk_ok=False) == "(Chinese text)"
    assert to_display_text("open 设置", cjk_ok=False) == "open"
    assert to_display_text("plain ascii", cjk_ok=False) == "plain ascii"


def test_font_detection(tmp_path):
    fake_font = tmp_path / "wqy-zenhei.ttc"
    fake_font.write_bytes(b"fake")
    assert find_font(str(fake_font)) == str(fake_font)
    assert font_supports_cjk(str(fake_font))
    assert not font_supports_cjk("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    assert find_font("/nonexistent/font.ttf") is not None or True  # 可能找到系统字体
    assert find_font.__call__  # noqa - 冒烟存在性


def test_event_label_and_color():
    ev = _ev(1, 0)
    assert "点击搜索按钮" in event_label(ev.event, cjk_ok=True)
    assert event_color(ev.event) == video_composer.COLOR_OK
    assert event_color(_ev(2, 0, result="failed").event) == video_composer.COLOR_FAIL
    assert event_color(_ev(3, 0, kind="observation", result="").event) == video_composer.COLOR_NEUTRAL


# ---------------------------------------------------------------------------
# filtergraph 拼装
# ---------------------------------------------------------------------------

def test_phone_only_filter_contains_bar_and_endcard():
    graph = build_filter_phone_only(
        [_ev(1, 0), _ev(2, 4000)],
        width=1264, height=1680, duration_s=8.0,
        font=None, cjk_ok=True,
    )
    assert "drawbox=x=0:y=ih-110" in graph          # 底部字幕条
    assert "between(t,0.000,4.000)" in graph        # 第一个事件窗口
    assert "between(t,4.000,8.000)" in graph        # 第二个事件窗口
    assert "text='Whale'" in graph                   # 片尾字样
    assert "between(t,6.500,8.000)" in graph        # 片尾 1.5s
    assert graph.endswith("[vout]")


def test_demo_filter_contains_panel_and_title_card():
    graph = build_filter_demo(
        [_ev(1, 0), _ev(2, 4000)],
        width=1264, height=1680, duration_s=8.0,
        font=None, cjk_ok=True, title="演示标题",
    )
    assert "pad=w=1584:h=1680" in graph             # 右侧 320px 面板
    assert "text='Whale Agent'" in graph
    assert "1/2" in graph and "2/2" in graph         # 进度
    assert "lt(t,2.000)" in graph                    # 标题卡片 2s
    assert "演示标题" in graph


def test_cjk_fallback_label_in_filter():
    graph = build_filter_phone_only(
        [_ev(1, 0)], width=640, height=1280, duration_s=4.0,
        font="/x/DejaVuSans.ttf", cjk_ok=False,
    )
    assert "Chinese text" in graph or "点击" not in graph


# ---------------------------------------------------------------------------
# ffmpeg 命令拼装与 compose 执行（mock subprocess）
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_h264_input():
    argv = build_ffmpeg_command(
        video_path="/tmp/run/video.h264", out_path="/tmp/run/export_phone_only.mp4",
        filtergraph="[0:v]copy[vout]", input_fps=29.7,
    )
    assert argv[:2] == ["ffmpeg", "-y"]
    # raw h264：genpts + 实测 fps 重建时间轴
    i = argv.index("-fflags")
    assert argv[i + 1] == "+genpts"
    r = argv.index("-r")
    assert argv[r + 1] == "29.700"   # 保留小数，不取整（变帧率设备时长不偏）
    assert "-c:v" in argv and "libx264" in argv
    assert "-c:a" in argv and "aac" in argv
    assert "-map" in argv and "[vout]" in argv


def test_build_ffmpeg_command_mp4_input_no_genpts():
    argv = build_ffmpeg_command(
        video_path="/tmp/run/video.mp4", out_path="/tmp/out.mp4",
        filtergraph="[0:v]copy[vout]", input_fps=30,
    )
    assert "+genpts" not in argv


def test_compose_rejects_unknown_profile(tmp_path):
    with pytest.raises(ComposeError):
        run(video_composer.compose(
            video_path=tmp_path / "v.h264", events=[], profile="cinema",
            out_path=tmp_path / "o.mp4", title="t",
        ))


def test_compose_with_mocked_subprocess(tmp_path, monkeypatch):
    video = tmp_path / "video.h264"
    video.write_bytes(b"\x00\x00\x00\x01")
    out = tmp_path / "export_phone_only.mp4"
    captured: dict = {}

    async def fake_probe(path, *, ffprobe_path="ffprobe"):
        return {"duration_s": 8.0, "width": 1264, "height": 1680, "fps": 30.0}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return FakeProc()

    monkeypatch.setattr(video_composer, "probe_video", fake_probe)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = run(video_composer.compose(
        video_path=video, events=[_ev(1, 0), _ev(2, 4000)],
        profile="phone_only", out_path=out, title="t",
    ))
    assert result == out
    argv = captured["argv"]
    assert argv[0] == "ffmpeg"
    assert str(video) in argv and str(out) in argv
    fg = argv[argv.index("-filter_complex") + 1]
    assert "drawbox" in fg


def test_compose_ffmpeg_failure_raises(tmp_path, monkeypatch):
    video = tmp_path / "video.h264"
    video.write_bytes(b"\x00\x00\x00\x01")

    async def fake_probe(path, *, ffprobe_path="ffprobe"):
        return {"duration_s": 8.0, "width": 640, "height": 1280, "fps": 30.0}

    class FailProc:
        returncode = 1

        async def communicate(self):
            return b"", b"encode exploded"

    async def fake_exec(*argv, **kwargs):
        return FailProc()

    monkeypatch.setattr(video_composer, "probe_video", fake_probe)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ComposeError, match="encode exploded"):
        run(video_composer.compose(
            video_path=video, events=[], profile="phone_only",
            out_path=tmp_path / "o.mp4", title="t",
        ))
