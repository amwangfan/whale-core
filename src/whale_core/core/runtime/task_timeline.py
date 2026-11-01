"""UX-08（issue #43）：任务执行时间线聚合——把 task 台账变成可读执行过程。

纪律：
- 状态语义真实：succeeded（已执行）与 delivered（已送达）分开；
  unknown 永不显示成成功；planned/pending_approval 不误标 executed。
- 重试建议保守：failed 可 resume（runtime 幂等键保护）；delivery_failed
  可 redeliver（不重跑工具，只重投递）；unknown 先查诊断不盲目重试。
- 时间线来自台账事件流水（_append_event 持久化），外加 phase/送达状态
  的自然语言化；无事件时也能从当前 phase 给出「当前在哪一步」。
"""

from __future__ import annotations

from typing import Any, Optional

#: phase → (用户可读标签, 解释, 下一步建议)
_PHASE_VIEW = {
    "pending": ("已记录", "任务已创建，等待执行。", "等待自动执行或手动开始。"),
    "acknowledged": ("已受理", "任务已被执行器受理。", "等待执行完成。"),
    "running": ("执行中", "任务正在执行。", "等待执行完成；页面刷新后仍可回来查看。"),
    "pending_approval": ("等待审批", "需要你确认后才能继续执行。",
                        "在任务页选择批准或拒绝。"),
    "succeeded": ("已执行成功", "执行器返回成功回执。",
                 "如未收到通知，可检查送达状态。"),
    "failed": ("执行失败", "执行器报告失败。", "可以重试；若反复失败请查看诊断。"),
    "unknown": ("状态未知", "执行结果未能确认（进程中断或渠道无回执）。",
               "先在诊断/Trace 里核实实际状态，再决定是否重试——"
               "unknown 不代表失败，更不能当成成功。"),
    "cancelled": ("已取消", "任务被手动取消。", "无需操作。"),
    "denied": ("已拒绝", "审批被拒绝，任务未执行。", "无需操作。"),
    "delivered": ("已送达", "结果已送达到目标渠道。", "无需操作。"),
    "delivery_failed": ("送达失败", "执行成功，但结果未能送达。",
                       "可安全重发（只重投递，不重复执行工具）。"),
}

#: 事件流水 kind → 用户可读行
_EVENT_TEXT = {
    "pending": "任务已创建",
    "acknowledged": "执行器已受理",
    "running": "开始执行",
    "pending_approval": "等待审批",
    "succeeded": "执行成功",
    "failed": "执行失败",
    "unknown": "状态未能确认",
    "cancelled": "已取消",
    "denied": "审批被拒绝",
    "delivered": "结果已送达",
    "redelivered": "人工重发成功",
}


def _fmt_at(at: float) -> str:
    import time as _time
    return _time.strftime("%m-%d %H:%M:%S", _time.localtime(at))


def build_task_timeline(store, task_id: str,
                        trace_ledger=None) -> "dict | None":
    """聚合单个任务的可读时间线；任务不存在返回 None。"""
    record = store.get(f"task:{task_id}")
    if not isinstance(record, dict):
        return None
    phase = str(record.get("phase") or record.get("status") or "unknown")
    label, explanation, next_step = _PHASE_VIEW.get(
        phase, ("未知阶段", "台账里没有这个阶段的说明。", "请查看诊断。"))

    timeline: list[dict] = []
    events = record.get("events") or []
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "")
        timeline.append({
            "at": float(e.get("at") or 0),
            "at_text": _fmt_at(float(e.get("at") or 0)),
            "kind": kind,
            "text": _EVENT_TEXT.get(kind, kind or "事件"),
            "detail": {k: v for k, v in e.items() if k not in ("at", "kind")},
        })
    if not timeline:
        timeline.append({
            "at": float(record.get("at") or 0),
            "at_text": _fmt_at(float(record.get("at") or 0)),
            "kind": phase,
            "text": _EVENT_TEXT.get(phase, label),
            "detail": {},
        })

    # 重试/恢复建议（保守：不盲目重复外部动作）
    retry: dict
    if phase == "failed":
        retry = {"allowed": True, "kind": "resume",
                 "reason": "失败任务可重试；重试走同一任务 ID（幂等键），"
                           "不会产生重复外部动作。"}
    elif phase == "delivery_failed":
        retry = {"allowed": True, "kind": "redeliver",
                 "reason": "只重新投递结果，不重复执行工具。"}
    elif phase == "unknown":
        retry = {"allowed": False, "kind": "inspect",
                 "reason": "状态未知时不建议直接重试：先到诊断/Trace 核实。"}
    elif phase in ("succeeded", "delivered"):
        retry = {"allowed": False, "kind": "none", "reason": "已成功，无需重试。"}
    else:
        retry = {"allowed": False, "kind": "none",
                 "reason": "当前阶段无需重试。"}

    delivery = record.get("delivery") or {}
    receipts = [str(r) for r in (record.get("receipts") or [])]

    # 关联 Trace（可观测链路；账本缺位时为空列表）
    trace_ids: list[str] = []
    if trace_ledger is not None:
        try:
            rows, _ = trace_ledger.query(limit=200)
            for t in rows:
                if t.task_id == task_id:
                    trace_ids.append(t.trace_id)
        except Exception:
            trace_ids = []

    return {
        "task_id": task_id,
        "goal": str(record.get("goal") or ""),
        "phase": phase,
        "status": str(record.get("status") or ""),
        "current": {"label": label, "explanation": explanation,
                    "next_step": next_step},
        "timeline": timeline,
        "receipts": receipts,
        "delivery": {
            "state": str(delivery.get("state") or ""),
            "attempts": int(delivery.get("attempts") or 0),
            "last_error": str(delivery.get("last_error") or ""),
            "channel": str(delivery.get("channel") or ""),
        },
        "retry": retry,
        "trace_ids": trace_ids,
        "capabilities": list(record.get("capabilities") or []),
    }
