"""RunTraceEvent 墙钟时间戳 ↔ 视频时间轴对齐（contracts/v2 §12.1）。

- trace 的 ``timestamp_ms`` 是墙钟毫秒（epoch ms）；
- 视频时间轴以录制首帧为 0（scrcpy 的 pts 相对录制起点，raw H.264 remux
  时按实测 fps 重建时间轴，误差在一帧以内）；
- 暂停区间（paused_segments，墙钟毫秒）内的画面不存在于视频中，对齐时
  把事件时间减去之前所有暂停区间的总时长。

对齐公式（事件墙钟 t，首帧墙钟 t0）::

    offset_ms(t) = (t - t0) - Σ overlap((pause_start, pause_end), (t0, t))

落在暂停区间内的事件，对齐到该区间结束时刻对应的视频位置（画面续上的
第一帧）；超出视频时长的事件截断到片尾。
"""

from __future__ import annotations

import dataclasses
import json

from pathlib import Path

# 暂停区间类型：墙钟毫秒 (start, end)，end 为 None 表示尚未 resume
PausedSegment = tuple[int, int]


@dataclasses.dataclass(frozen=True)
class AlignedEvent:
    """一个对齐到视频时间轴的轨迹事件。"""

    seq: int
    timestamp_ms: int             # 原始墙钟
    video_offset_ms: float        # 视频内偏移（毫秒，已扣暂停区间）
    event: dict                   # 原始 RunTraceEvent JSON


class AlignError(Exception):
    """轨迹文件解析失败。"""


def load_trace(trace_path: Path | str) -> list[dict]:
    """读取 trace.jsonl（每行一个 RunTraceEvent JSON），按 seq 排序返回。"""
    events: list[dict] = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise AlignError(f"{trace_path}:{lineno} JSON 解析失败: {e}") from e
    events.sort(key=lambda e: e.get("seq", 0))
    return events


def _paused_before(
    paused_segments: list[PausedSegment], t0: int, t: int
) -> int:
    """(t0, t) 与所有暂停区间的重叠总毫秒数。"""
    total = 0
    for start, end in paused_segments:
        lo = max(start, t0)
        hi = min(end, t)
        if hi > lo:
            total += hi - lo
    return total


def wall_to_video_offset_ms(
    timestamp_ms: int,
    first_frame_wall_ms: int,
    paused_segments: list[PausedSegment],
    duration_ms: int | None = None,
) -> float:
    """墙钟时间戳 → 视频内偏移毫秒（纯函数，对齐数学的唯一实现）。"""
    t0 = first_frame_wall_ms
    t = max(timestamp_ms, t0)
    # 事件若落在某个暂停区间内，吸附到该区间结束（画面恢复时刻）
    for start, end in paused_segments:
        if start <= t < end:
            t = end
            break
    offset = (t - t0) - _paused_before(paused_segments, t0, t)
    offset = max(0, offset)
    if duration_ms is not None:
        offset = min(offset, duration_ms)
    return float(offset)


def align(
    trace_path: Path | str,
    *,
    first_frame_wall_ms: int,
    paused_segments: list[PausedSegment] | None = None,
    duration_ms: int | None = None,
) -> list[AlignedEvent]:
    """对齐整条轨迹，返回每事件的视频内偏移列表（按 seq 升序）。

    ``first_frame_wall_ms`` 取 RawRecording.first_frame_wall_ms；
    ``duration_ms`` 取 stopped_at - started_at 与视频实际时长的较小者更稳，
    传 None 则不截断。
    """
    segments = list(paused_segments or [])
    result: list[AlignedEvent] = []
    for event in load_trace(trace_path):
        offset = wall_to_video_offset_ms(
            int(event["timestamp_ms"]), first_frame_wall_ms, segments, duration_ms
        )
        result.append(AlignedEvent(
            seq=int(event.get("seq", 0)),
            timestamp_ms=int(event["timestamp_ms"]),
            video_offset_ms=offset,
            event=event,
        ))
    return result
