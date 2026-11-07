"""trace_align 时间轴对齐数学测试（含 pause 区间）。"""

from __future__ import annotations

import json

import pytest

from whale_core.devices.recording.trace_align import (
    AlignError,
    align,
    load_trace,
    wall_to_video_offset_ms,
)

T0 = 1_000_000  # 首帧墙钟基准（任意起点，ms）


def _write_trace(tmp_path, events):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return path


def test_no_pause_linear():
    # 无暂停：offset = t - t0
    assert wall_to_video_offset_ms(T0 + 5000, T0, []) == 5000
    assert wall_to_video_offset_ms(T0, T0, []) == 0


def test_before_first_frame_clamped():
    assert wall_to_video_offset_ms(T0 - 3000, T0, []) == 0


def test_pause_segment_subtracted():
    # 暂停区间 [T0+2000, T0+5000)：其后的事件前移 3000ms
    segments = [(T0 + 2000, T0 + 5000)]
    assert wall_to_video_offset_ms(T0 + 1000, T0, segments) == 1000
    assert wall_to_video_offset_ms(T0 + 8000, T0, segments) == 5000


def test_event_inside_pause_snaps_to_resume():
    # 落在暂停区间内的事件 → 吸附到恢复时刻对应的视频位置（2000ms 处）
    segments = [(T0 + 2000, T0 + 5000)]
    assert wall_to_video_offset_ms(T0 + 3000, T0, segments) == 2000


def test_multiple_pause_segments():
    segments = [(T0 + 1000, T0 + 2000), (T0 + 5000, T0 + 8000)]
    # 事件在 T0+9000：减去两段暂停共 4000ms → 5000
    assert wall_to_video_offset_ms(T0 + 9000, T0, segments) == 5000


def test_duration_clamp():
    assert wall_to_video_offset_ms(T0 + 99999, T0, [], duration_ms=8000) == 8000


def test_align_end_to_end(tmp_path):
    trace = _write_trace(tmp_path, [
        {"run_id": "r", "seq": 2, "timestamp_ms": T0 + 7000, "kind": "note"},
        {"run_id": "r", "seq": 1, "timestamp_ms": T0 + 1000, "kind": "task_started"},
        {"run_id": "r", "seq": 3, "timestamp_ms": T0 + 4000, "kind": "note"},
    ])
    segments = [(T0 + 2000, T0 + 5000)]
    result = align(trace, first_frame_wall_ms=T0, paused_segments=segments)
    # 按 seq 排序返回
    assert [e.seq for e in result] == [1, 2, 3]
    assert result[0].video_offset_ms == 1000
    assert result[1].video_offset_ms == 4000   # 7000 - 3000 暂停
    assert result[2].video_offset_ms == 2000   # 区间内 → 吸附到 2000


def test_load_trace_bad_json(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"seq": 1}\nnot-json\n', encoding="utf-8")
    with pytest.raises(AlignError):
        load_trace(path)
