"""录制模块共享类型（源录制器接口与数据对象）。"""

from __future__ import annotations

import dataclasses

from pathlib import Path
from typing import Protocol


@dataclasses.dataclass(frozen=True)
class RecordingOptions:
    """一次录制的选项（对应 recording.control options，§12.2）。"""

    max_size: int = 1280            # scrcpy max_size（限制长边，服务端对齐到 8 的倍数）
    video_bit_rate: int = 4_000_000
    max_fps: int = 30
    profile: str = "phone_only"     # 导出模板：phone_only / demo
    prefer_source: str = "scrcpy"   # 首选源：scrcpy / screenrecord
    max_duration_s: int = 1800      # 录制的硬上限（screenrecord 分段依据）


@dataclasses.dataclass(frozen=True)
class RawRecording:
    """源录制器 stop() 的返回：原始录像文件与时间轴基准。"""

    file_path: Path
    started_at_ms: int              # 墙钟：录制启动（start 被接受时刻）
    stopped_at_ms: int              # 墙钟：录制收尾完成
    source: str                     # scrcpy / screenrecord
    width: int = 0
    height: int = 0
    frame_count: int = 0
    # 首帧到达的墙钟（trace_align 对齐基准；screenrecord 近似为进程启动时刻）
    first_frame_wall_ms: int = 0
    # 实测帧率（raw H.264 无时间戳，remux 时按此 fps 生成时间轴）
    measured_fps: float = 0.0


class SourceRecorder(Protocol):
    """屏幕采集源统一接口（scrcpy / screenrecord 两实现）。"""

    async def start(
        self,
        run_id: str,
        device_serial: str,
        options: RecordingOptions,
        dest_dir: Path,
    ) -> None:
        """启动采集，立即开始落盘；失败抛异常由上层降级。"""
        ...

    async def stop(self) -> RawRecording:
        """停止采集并收尾文件；幂等。"""
        ...
