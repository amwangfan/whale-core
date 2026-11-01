"""持久化行为 Trace（issue #39）：一次回复/沉默/任务可重建的结构化账本。

目标：给定一次回复、沉默、工具执行或任务 ID，能重建「接收 → 策略判断 →
模型生成 → 渠道回执」的关键阶段与最终状态；重启后仍可查询。

纪律：

- **默认不保存私聊正文、原始 Prompt、密钥、Token**：span detail 只放非内容
  信息（阶段、耗时、段数、reason_code）；所有写入再经 ``_redact`` 兜底脱敏
  （token/key/密码类模式 + 长度截断）。
- 状态语义真实：``planned/accepted/executed/failed/silent`` 按事实记录，
  planned 永不显示成 executed（产品原则 4）。
- 容量与保留：``max_rows`` 硬上限 + ``max_age_days`` 保留期，写入时摊销清理；
  进程重启后仍可查询（sqlite 持久化，WAL）。
- 全部 stdlib；时钟可注入，测试离线。

概念对齐 Langfuse 的 Trace/Observation，但只实现轻量账本：traces 表一行
一条因果链，spans 表逐阶段。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from uuid import uuid4

# ---------------------------------------------------------------- 脱敏

_SECRET_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[-_]?key|authorization|"
    r"cookie|session|凭据|密钥|密码)\s*[=:：]\s*[^\s,;，；]{4,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[^\s,;，；]{4,}")
_MAX_DETAIL = 240


def _redact(text: str) -> str:
    """span detail 兜底脱敏：疑似键值对与 Bearer 凭据打码 + 截断。"""
    text = str(text or "")
    # 先掩 Bearer 凭据（否则 Authorization: Bearer xxx 的 xxx 会残留）
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SECRET_RE.sub(lambda m: m.group(1) + "=***", text)
    return text[:_MAX_DETAIL]

# ---------------------------------------------------------------- reason_code 用户可读映射

#: 稳定 reason_code → 用户可读说明（「为什么没有回复/为什么这样做」）。
REASON_TEXT: dict[str, str] = {
    # 群聊指向裁决（reply_target）
    "private": "私聊消息，直接回复",
    "at_bot": "有人在群里 @ 了我",
    "reply_to_bot": "有人引用了我的消息",
    "group_observe": "群消息没有指向我，按规则旁观",
    # 群聊主动参与（group_turn）
    "dnd": "该群开启了免打扰",
    "quiet_hours": "当前处于静默时段，非定向插话被压制",
    "quiet_mode": "该群是安静档，只对点名接话",
    "not_directed": "消息没有点名或明确提问，按当前档位旁观",
    "consecutive": "上一条群消息已经是我发的，避免连续刷屏",
    "cooldown": "距离上次主动接话还在冷却时间内",
    "daily_quota": "该群今日主动接话次数已用完",
    "join:lively": "活跃档：检测到点名/提问信号，主动接话",
    "join:balanced": "均衡档：检测到点名信号，主动接话",
    # 陌生人/私聊闸门（qq_workflow）
    "disabled": "陌生人对话开关未开启",
    "channel_denied": "该渠道不允许此类会话",
    "empty_text": "消息没有可回复的文本内容",
    "sudo_in_group_ignored": "sudo 验证在群聊中不受理（保护口令）",
    "stranger_budget": "今日陌生人回复额度已用完",
    "no_orchestrator": "运行时未装配编排器，无法生成回复",
    "quiet_hours_passive_exempt": "静默时段：被动回复不受拦截",
    # 发送/规划（runner）
    "silent_planned": "模型规划本轮沉默（[[沉默]]），不发送",
    "epoch_stale": "回复生成期间会话已有新消息，剩余片段作废",
    "safety_review": "安全审核有顾虑，留待人工终审",
    "model_error": "模型调用失败",
    "send_failed": "渠道发送失败",
    "replied": "已回复",
    "unpaired_no_consent": "未配对的发送者：陌生人对话未开启，仅记录不回复",
    "empty_reply": "没有生成有效回复内容",
}

# ---------------------------------------------------------------- 数据模型

TRACE_STATUSES = ("open", "replied", "silent", "failed", "partial")


@dataclass(frozen=True)
class Span:
    span_id: str
    trace_id: str
    phase: str            # received / decide / workflow / model / send / ...
    status: str           # ok / skipped / failed / planned / executed
    reason_code: str = ""
    detail: str = ""      # 非内容信息（已脱敏、截断）
    started_at: float = 0.0
    duration_ms: float = 0.0
    attempt: int = 0


@dataclass(frozen=True)
class Trace:
    trace_id: str
    kind: str             # qq_inbound / task / action / device_event ...
    conversation_id: str = ""
    principal_id: str = ""
    message_id: str = ""
    task_id: str = ""
    action_id: str = ""
    receipt_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    status: str = "open"
    summary: str = ""     # 非内容摘要（已脱敏）
    spans: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------- 账本


class TraceLedger:
    """sqlite 持久化 Trace 账本（线程安全；保留期与容量上限）。"""

    def __init__(
        self,
        path: str,
        *,
        max_rows: int = 20000,
        max_age_days: float = 14.0,
        now: Callable[[], float] = time.time,
        prune_every: int = 50,
    ) -> None:
        self.path = path
        self.max_rows = max_rows
        self.max_age_days = max_age_days
        self._now = now
        self._prune_every = max(1, prune_every)
        self._writes = 0
        # RLock：begin 持锁时摊销 prune 会再次进入（重入安全）。
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS traces ("
            " trace_id TEXT PRIMARY KEY, kind TEXT, conversation_id TEXT,"
            " principal_id TEXT, message_id TEXT, task_id TEXT,"
            " action_id TEXT, receipt_id TEXT,"
            " started_at REAL, ended_at REAL, status TEXT, summary TEXT)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS spans ("
            " span_id TEXT PRIMARY KEY, trace_id TEXT, phase TEXT,"
            " status TEXT, reason_code TEXT, detail TEXT,"
            " started_at REAL, duration_ms REAL, attempt INTEGER)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_started"
            " ON traces(started_at DESC)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_conv ON traces(conversation_id)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id)")
        self._db.commit()

    # ------------------------------------------------------------ 写入

    def begin(self, kind: str, *, conversation_id: str = "",
              principal_id: str = "", message_id: str = "",
              task_id: str = "", action_id: str = "",
              trace_id: str = "") -> str:
        tid = trace_id or uuid4().hex[:16]
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO traces (trace_id, kind,"
                " conversation_id, principal_id, message_id, task_id,"
                " action_id, started_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, kind, conversation_id, principal_id, message_id,
                 task_id, action_id, self._now(), "open"))
            self._db.commit()
            self._amortized_prune()
        return tid

    def span(self, trace_id: str, phase: str, status: str, *,
             reason_code: str = "", detail: str = "",
             duration_ms: float = 0.0, attempt: int = 0) -> str:
        sid = uuid4().hex[:16]
        with self._lock:
            self._db.execute(
                "INSERT INTO spans (span_id, trace_id, phase, status,"
                " reason_code, detail, started_at, duration_ms, attempt)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, trace_id, phase, status, reason_code,
                 _redact(detail), self._now(), duration_ms, attempt))
            self._db.commit()
        return sid

    def finish(self, trace_id: str, status: str, *, summary: str = "",
               receipt_id: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE traces SET ended_at=?, status=?,"
                " summary=COALESCE(NULLIF(?,''), summary),"
                " receipt_id=COALESCE(NULLIF(?,''), receipt_id)"
                " WHERE trace_id=?",
                (self._now(), status, _redact(summary), receipt_id, trace_id))
            self._db.commit()

    # ------------------------------------------------------------ 查询

    def get(self, trace_id: str) -> Optional[Trace]:
        with self._lock:
            row = self._db.execute(
                "SELECT trace_id, kind, conversation_id, principal_id,"
                " message_id, task_id, action_id, receipt_id, started_at,"
                " ended_at, status, summary FROM traces WHERE trace_id=?",
                (trace_id,)).fetchone()
            if row is None:
                return None
            spans = self._db.execute(
                "SELECT span_id, trace_id, phase, status, reason_code,"
                " detail, started_at, duration_ms, attempt FROM spans"
                " WHERE trace_id=? ORDER BY started_at, rowid",
                (trace_id,)).fetchall()
        return Trace(*row[:12], spans=tuple(Span(*s) for s in spans))

    def query(self, *, kind: str = "", conversation_id: str = "",
              principal_id: str = "", status: str = "",
              since: float = 0.0, until: float = 0.0,
              limit: int = 50, offset: int = 0) -> "tuple[list[Trace], int]":
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if conversation_id:
            where.append("conversation_id=?")
            params.append(conversation_id)
        if principal_id:
            where.append("principal_id=?")
            params.append(principal_id)
        if status:
            where.append("status=?")
            params.append(status)
        if since:
            where.append("started_at>=?")
            params.append(since)
        if until:
            where.append("started_at<=?")
            params.append(until)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self._lock:
            total = self._db.execute(
                f"SELECT COUNT(*) FROM traces{clause}", params).fetchone()[0]
            rows = self._db.execute(
                "SELECT trace_id, kind, conversation_id, principal_id,"
                " message_id, task_id, action_id, receipt_id, started_at,"
                " ended_at, status, summary FROM traces"
                f"{clause} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        traces = [Trace(*r[:12]) for r in rows]
        return traces, total

    # ------------------------------------------------------------ 保留与导出

    def prune(self) -> int:
        """按保留期与容量上限清理；返回删除的 trace 数。"""
        cutoff = self._now() - self.max_age_days * 86400
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM traces WHERE started_at<?", (cutoff,))
            n = cur.rowcount if cur.rowcount else 0
            over = self._db.execute(
                "SELECT COUNT(*) - ? FROM traces", (self.max_rows,)
            ).fetchone()[0]
            if over and over > 0:
                cur = self._db.execute(
                    "DELETE FROM traces WHERE trace_id IN ("
                    " SELECT trace_id FROM traces ORDER BY started_at ASC"
                    " LIMIT ?)", (int(over),))
                n += cur.rowcount if cur.rowcount else 0
            self._db.execute(
                "DELETE FROM spans WHERE trace_id NOT IN"
                " (SELECT trace_id FROM traces)")
            self._db.commit()
        return n

    def export(self, trace_id: str) -> str:
        trace = self.get(trace_id)
        if trace is None:
            return ""
        return json.dumps(_trace_dict(trace), ensure_ascii=False, indent=2)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ 内部

    def _amortized_prune(self) -> None:
        self._writes += 1
        if self._writes % self._prune_every == 0:
            self.prune()


# ---------------------------------------------------------------- 用户视图


def explain(trace: Trace) -> "list[str]":
    """用户活动记录视图：自然语言解释「发生了什么/为什么/结果如何」。"""
    lines: list[str] = []
    when = time.strftime("%H:%M:%S", time.localtime(trace.started_at))
    where = ("群聊" if trace.conversation_id.startswith("group:")
             else "私聊") if trace.conversation_id else "会话"
    lines.append(f"{when} · {where}里来了一条消息。")
    for span in trace.spans:
        text = REASON_TEXT.get(span.reason_code, "")
        if span.phase == "decide" and span.reason_code:
            if span.status in ("skipped", "observe"):
                lines.append(f"判断：{text or span.reason_code}。")
            else:
                lines.append(f"判断：{text or '需要回复'}。")
        elif span.phase == "model" and span.status == "failed":
            lines.append("生成回复时模型调用失败。")
        elif span.phase == "send":
            if span.status == "executed":
                lines.append("回复已发送到聊天窗口。")
            elif span.status == "failed":
                lines.append(f"发送失败：{span.detail or '渠道异常'}。")
        elif span.phase == "plan" and span.reason_code == "silent_planned":
            lines.append("这轮规划为不说，保持安静。")
    if trace.status == "replied":
        lines.append("结果：已回复。")
    elif trace.status == "silent":
        lines.append("结果：没有回复（以上为原因）。")
    elif trace.status == "failed":
        lines.append("结果：处理失败。")
    elif trace.status == "partial":
        lines.append("结果：部分完成（有的片段未送达）。")
    return lines


def _trace_dict(t: Trace) -> dict:
    return {
        "trace_id": t.trace_id, "kind": t.kind,
        "conversation_id": t.conversation_id,
        "principal_id": t.principal_id, "message_id": t.message_id,
        "task_id": t.task_id, "action_id": t.action_id,
        "receipt_id": t.receipt_id,
        "started_at": t.started_at, "ended_at": t.ended_at,
        "status": t.status, "summary": t.summary,
        "spans": [
            {"span_id": s.span_id, "phase": s.phase, "status": s.status,
             "reason_code": s.reason_code, "detail": s.detail,
             "started_at": s.started_at, "duration_ms": s.duration_ms,
             "attempt": s.attempt}
            for s in t.spans
        ],
    }


__all__ = [
    "REASON_TEXT",
    "Span",
    "Trace",
    "TraceLedger",
    "explain",
]
