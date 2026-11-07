"""录制产物目录管理（contracts/v2 §12 落地侧）。

目录布局（``root`` 默认 ``~/.whale/recordings``，可用环境变量
``WHALE_RECORDINGS_DIR`` 覆盖）::

    <root>/runs/<run_id>/
        video.h264          # scrcpy 源：raw H.264 Annex-B 流
        video.mp4           # screenrecord 源：拼接后的 MP4（或 remux 产物）
        frames.json         # scrcpy 源：每帧 pts/size/keyframe 侧车文件
        trace.jsonl         # RunTraceEvent，每行一个 JSON
        manifest.json       # RecordingManifest（contracts/v2 objects/recording-manifest）
        recorder_state.json # 运行期状态（paused_segments 等，非合同产物）
        export_phone_only.mp4 / export_demo.mp4

隐私：用户可删除原始录像、轨迹、导出件或整个 run 目录。
"""

from __future__ import annotations

import os
import re
import shutil

from pathlib import Path

# run_id 合同格式（contracts/v2 §3/§12）：run_ + 26 字符 Crockford Base32
_RUN_ID_RE = re.compile(r"^run_[0-9A-HJKMNP-TV-Z]{26}$")

# 允许单独删除的产物名（防止路径穿越，只允许白名单内文件名）
_DELETABLE_ARTIFACTS = frozenset({
    "video.h264",
    "video.mp4",
    "frames.json",
    "trace.jsonl",
    "manifest.json",
    "recorder_state.json",
    "export_phone_only.mp4",
    "export_demo.mp4",
})

DEFAULT_ROOT = Path(
    os.environ.get("WHALE_RECORDINGS_DIR", Path.home() / ".whale" / "recordings")
)


class RecordingStoreError(Exception):
    """目录/产物操作错误（未知 run、非法产物名等）。"""


class RecordingStore:
    """录制产物目录管理。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else DEFAULT_ROOT

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        """返回 run 目录路径（不创建）。run_id 非法时抛 RecordingStoreError。"""
        if not _RUN_ID_RE.match(run_id):
            raise RecordingStoreError(f"非法 run_id: {run_id!r}")
        return self._root / "runs" / run_id

    def ensure_run(self, run_id: str) -> Path:
        """创建（幂等）并返回 run 目录。"""
        path = self.run_dir(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_path(self, run_id: str, name: str) -> Path:
        """返回 run 目录内产物路径（不检查存在性）。"""
        return self.run_dir(run_id) / name

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_runs(self) -> list[str]:
        """列出磁盘上所有 run_id（按目录名排序）。"""
        runs_root = self._root / "runs"
        if not runs_root.is_dir():
            return []
        return sorted(
            p.name
            for p in runs_root.iterdir()
            if p.is_dir() and _RUN_ID_RE.match(p.name)
        )

    def list_artifacts(self, run_id: str) -> list[str]:
        """列出某 run 目录内的产物文件名。"""
        path = self.run_dir(run_id)
        if not path.is_dir():
            raise RecordingStoreError(f"run 不存在: {run_id}")
        return sorted(p.name for p in path.iterdir() if p.is_file())

    # ------------------------------------------------------------------
    # 删除（用户隐私出口）
    # ------------------------------------------------------------------

    def delete_artifact(self, run_id: str, name: str) -> bool:
        """删除单个产物（原始录像/轨迹/导出件）。返回是否真的删掉了文件。

        产物名必须在白名单内，防止路径穿越。
        """
        if name not in _DELETABLE_ARTIFACTS:
            raise RecordingStoreError(f"不允许删除的产物名: {name!r}")
        path = self.artifact_path(run_id, name)
        if path.is_file():
            path.unlink()
            return True
        return False

    def delete_run(self, run_id: str) -> bool:
        """删除整个 run 目录（录像 + 轨迹 + manifest + 导出件）。"""
        path = self.run_dir(run_id)
        if not path.is_dir():
            return False
        shutil.rmtree(path)
        return True
