"""Agent Run 录制模块（contracts/v2 §12）。

录制与轨迹共用 run_id：屏幕录像（scrcpy 优先、screenrecord 兜底）与
RunTraceEvent JSONL 轨迹落盘到 ``runs/<run_id>/``，收尾产出符合
``objects/recording-manifest.schema.json`` 的 RecordingManifest，并可按
``phone_only`` / ``demo`` 两个模板用 FFmpeg 合成导出 MP4。

设计约束（规划 §9）：
- 默认不录制；录制由上层显式 ``recording.control start`` 触发。
- 轨迹只记录可理解的操作说明，绝不记录模型思维链。
- scrcpy 源不支持真暂停：pause/resume 只把暂停区间写入 ``recorder_state.json``
  的 ``paused_segments``，时间轴对齐与导出时跳过对应时间段。

子模块：
- ``scrcpy_source``：纯 Python 极简 scrcpy 客户端（push 服务端 jar、
  tunnel_forward、解析流头与 packet 元数据、落盘 raw H.264 + 每帧 pts）。
- ``screenrecord_source``：``adb shell screenrecord`` 兜底（≤180s/段，多段
  用 ffmpeg concat 拼接）。
- ``run_recorder``：RunRecorder 编排（start/record_event/pause/resume/stop/export）。
- ``trace_align``：trace 墙钟时间戳 ↔ 视频时间轴对齐（处理暂停区间）。
- ``video_composer``：FFmpeg 合成（phone_only / demo 两个模板）。
- ``store``：录制产物目录管理与删除。
"""

from __future__ import annotations

from ._types import RawRecording, RecordingOptions
from .run_recorder import RunRecorder
from .store import RecordingStore

__all__ = [
    "RawRecording",
    "RecordingOptions",
    "RecordingStore",
    "RunRecorder",
]
