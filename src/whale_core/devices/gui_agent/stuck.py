"""卡住检测（借鉴 dsh-mobile-gui-agent src/agent/stuck-detector.ts）。

保留最近 12 条 {action 归一化, before 签名, after 签名, success, error_code}。
判据按序命中，结果是**提示**（进决策上下文），不强制终止。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

_HISTORY_MAX = 12


def normalize_action(kind: str, arguments: Mapping[str, Any]) -> str:
    """归一化动作（忽略 element_id/observation_id 等易变字段）。"""
    stable = {k: v for k, v in arguments.items()
              if k not in ("observation_id",)}
    return f"{kind}:{json.dumps(stable, sort_keys=True, ensure_ascii=False)}"


@dataclass(frozen=True)
class StuckHint:
    reason: str     # repeated-action / unchanged-failures / state-cycle /
                    # stale-observation / device-errors
    recovery: str   # choose-another-element / observe / back / wait

    def render(self) -> str:
        return f"Stuck: {self.reason}; try {self.recovery}"


class StuckDetector:
    def __init__(self) -> None:
        self._history: list[dict[str, Any]] = []

    def record(self, *, action_key: str, state_before: str, state_after: str,
               success: bool, error_code: str | None) -> StuckHint | None:
        self._history.append({
            "action": action_key, "before": state_before, "after": state_after,
            "success": success, "error": error_code,
        })
        if len(self._history) > _HISTORY_MAX:
            self._history.pop(0)
        return self.detect()

    def detect(self) -> StuckHint | None:
        h = self._history
        if len(h) >= 3:
            last3 = h[-3:]
            # 同一动作且状态不变
            if (all(x["action"] == last3[-1]["action"] for x in last3)
                    and all(x["before"] == x["after"] for x in last3)):
                return StuckHint("repeated-action", "choose-another-element")
            # 连续失败且状态不变
            if (all(not x["success"] for x in last3)
                    and all(x["before"] == x["after"] for x in last3)):
                return StuckHint("unchanged-failures", "observe")
            # 连续 stale
            if all(x["error"] == "stale_observation" for x in last3):
                return StuckHint("stale-observation", "observe")
            # 连续设备错误
            if all((x["error"] or "").startswith(("device_", "adb_", "unavailable",
                                                  "internal"))
                   and x["error"] for x in last3):
                return StuckHint("device-errors", "wait")
        # ABAB 状态环
        if len(h) >= 4:
            a = [x["after"] for x in h[-4:]]
            if a[0] == a[2] and a[1] == a[3] and a[0] != a[1]:
                return StuckHint("state-cycle", "back")
        return None
