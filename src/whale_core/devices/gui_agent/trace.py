"""RunTrace 写入（contracts/v2 §12.1）。

JSONL 每行一个 RunTraceEvent；run_id 与录屏共用；label 是可理解操作说明，
不记录模型思维链。文件 sink 与内存 sink 并存。
"""
from __future__ import annotations

import json
import os
from typing import Any

from whale_core.devices.transports.base import now_ms

_KINDS = frozenset({
    "task_started", "observation", "action_started", "action_completed",
    "action_failed", "verification", "approval_requested", "approval_resolved",
    "task_completed", "task_failed", "note",
})


class RunTraceWriter:
    """JSONL 轨迹写入器。sink 为目录路径（落盘）或 None（仅内存）。"""

    def __init__(self, run_id: str, sink_dir: str | None = None) -> None:
        self.run_id = run_id
        self._seq = 0
        self.events: list[dict[str, Any]] = []
        self._path: str | None = None
        if sink_dir is not None:
            os.makedirs(sink_dir, exist_ok=True)
            self._path = os.path.join(sink_dir, f"{run_id}.jsonl")

    @property
    def path(self) -> str | None:
        return self._path

    def emit(self, kind: str, *, task_summary: str | None = None,
             action: dict | None = None, result: str | None = None,
             verification: dict | None = None, frame_ref: str | None = None,
             timestamp_ms: int | None = None) -> dict:
        if kind not in _KINDS:
            raise ValueError(f"未知轨迹 kind: {kind}")
        self._seq += 1
        ev: dict[str, Any] = {
            "run_id": self.run_id,
            "seq": self._seq,
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else now_ms(),
            "kind": kind,
        }
        if task_summary:
            ev["task_summary"] = task_summary[:200]
        if action:
            ev["action"] = {k: str(v)[:200] for k, v in action.items() if v}
        if result:
            ev["result"] = str(result)[:500]
        if verification:
            ev["verification"] = {
                "status": str(verification.get("status", "skipped")),
                "summary": str(verification.get("summary", ""))[:500],
            }
        if frame_ref:
            ev["frame_ref"] = frame_ref
        self.events.append(ev)
        if self._path:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev
