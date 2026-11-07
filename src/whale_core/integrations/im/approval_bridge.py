"""IM 审批桥（contracts/v2 §13 task.approval 的 core 侧实现）。

生命周期：core 创建审批（approval_id=new_id("aprv")、默认 300s 有效期、
落内存 + 可选 JSONL 审计）→ 投递到主人私聊 target_id → 主人在私聊回复
「批准/拒绝」→ 按 (target_id + 未过期 + pending) 匹配 → 一次性核销 →
回调 core 侧 resolver。

硬约束（§13）：
- 一次性：核销后状态落 resolved，重复回复返回「已处理」；
- 旧 approval_id 不得批准新动作：每条请求独立 approval_id，已核销/已过期的
  id 永远匹配不到新请求（显式引用旧 id 只得到「已处理/已过期」答复）；
- 过期自动失效：懒过期（回复时检查）+ ``expire_overdue`` 主动清理，两种
  路径都会产生 decision=expired 的 Resolution 回调；
- 群聊不作审批入口：群里的批准/拒绝一律拒绝并提示私聊，**不消耗**审批。

本模块与 life/autonomy.ApprovalBook 的关系：ApprovalBook 是「设备动作
指纹级」的当次审批台账（kv 事务核销），面向 L3 自主动作；本模块面向
IM 人机对话审批（人对自然语言摘要说「批准」），粒度不同，故不复用其
存储，但「一次性 + 绑定确定对象 + fail closed」的语义与之一致。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from whale_core.integrations.im.delivery import DeliveryService
from whale_core.integrations.im.models import (
    CHAT_PRIVATE,
    DECISION_APPROVED,
    DECISION_EXPIRED,
    DECISION_REJECTED,
    DEFAULT_APPROVAL_TTL_S,
    DEFAULT_REPLY_OPTIONS,
    ApprovalRequest,
    ApprovalResolution,
    ImMessage,
    utc_now,
)

#: 内置批准/拒绝同义词（reply_options 之外始终接受）
_APPROVE_WORDS = frozenset({"批准", "同意", "通过", "approve", "yes"})
_REJECT_WORDS = frozenset({"拒绝", "驳回", "否决", "reject", "no"})

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_EXPIRED = "expired"

ResolverFn = Callable[[ApprovalResolution], None]


@dataclass
class _ApprovalRecord:
    """一条审批的内部状态（可变；对外只暴露冻结的 Request/Resolution）。"""

    request: ApprovalRequest
    seq: int = 0               # 桥内单调序号：同毫秒内「最新一条」也可判定
    status: str = STATUS_PENDING
    resolution: ApprovalResolution | None = None
    on_resolved: ResolverFn | None = None


def _parse_approval_reply(text: str, reply_options: tuple) -> tuple[str, str] | None:
    """把回复文本解析为 (decision, explicit_approval_id_or_empty)；不像审批回复返回 None。

    形式：``批准`` / ``拒绝`` / 自定义 reply_options 标签 / 内置同义词，
    可跟第二个 token 显式引用 approval_id（``批准 aprv_xxx``）。
    """
    tokens = (text or "").split()
    if not tokens:
        return None
    word = tokens[0].strip().lower()
    options = tuple(str(o) for o in (reply_options or ()))
    approve_label = options[0].lower() if len(options) >= 1 else None
    reject_label = options[1].lower() if len(options) >= 2 else None
    if word in _APPROVE_WORDS or (approve_label and word == approve_label):
        decision = DECISION_APPROVED
    elif word in _REJECT_WORDS or (reject_label and word == reject_label):
        decision = DECISION_REJECTED
    else:
        return None
    explicit_id = ""
    if len(tokens) >= 2 and tokens[1].startswith("aprv_"):
        explicit_id = tokens[1].strip()
    return decision, explicit_id


class ApprovalBridge:
    """core ↔ IM 的审批桥。线程安全（核销在锁内完成）。"""

    def __init__(self, delivery: DeliveryService, *, owner_target_id: str,
                 ttl_s: float = DEFAULT_APPROVAL_TTL_S,
                 jsonl_path: str | Path | None = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        if not owner_target_id:
            raise ValueError("owner_target_id 必填（审批只投主人私聊）")
        if ttl_s <= 0:
            raise ValueError("ttl_s 须为正数")
        self.delivery = delivery
        self.owner_target_id = owner_target_id
        self.ttl_s = float(ttl_s)
        self._jsonl_path = Path(jsonl_path) if jsonl_path else None
        self._now = now_fn or utc_now
        self._lock = threading.Lock()
        self._records: dict[str, _ApprovalRecord] = {}
        self._listeners: list[ResolverFn] = []
        self._seq = 0

    # -- core 侧：创建与查询 ------------------------------------------------

    def add_listener(self, listener: ResolverFn) -> None:
        """注册全局 resolver 回调（所有审批结局都会通知）。"""
        with self._lock:
            self._listeners.append(listener)

    def create_request(self, *, task_id: str, device_id: str, action: str,
                       summary: str, target_id: str | None = None,
                       reply_options: tuple = DEFAULT_REPLY_OPTIONS,
                       ttl_s: float | None = None,
                       on_resolved: ResolverFn | None = None) -> ApprovalRequest:
        """创建并投递一条审批请求，返回冻结的 ApprovalRequest。"""
        if not summary.strip():
            raise ValueError("summary 必填（审批必须对人可解释）")
        now = self._now()
        ttl = self.ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            raise ValueError("ttl_s 须为正数")
        request = ApprovalRequest(
            task_id=str(task_id), device_id=str(device_id), action=str(action),
            summary=summary, target_id=target_id or self.owner_target_id,
            expires_at=now + timedelta(seconds=ttl),
            reply_options=tuple(reply_options) or DEFAULT_REPLY_OPTIONS,
            created_at=now)
        with self._lock:
            self._seq += 1
            self._records[request.approval_id] = _ApprovalRecord(
                request=request, seq=self._seq, on_resolved=on_resolved)
        self._audit("created", request)
        self.delivery.send_approval(request.target_id, request)
        return request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            record = self._records.get(approval_id)
            return record.request if record else None

    def pending_for(self, target_id: str) -> list[ApprovalRequest]:
        """某 target 当前未过期、未核销的待审批列表（观测/调试用）。"""
        now = self._now()
        with self._lock:
            return [r.request for r in self._records.values()
                    if r.request.target_id == target_id
                    and r.status == STATUS_PENDING
                    and r.request.expires_at > now]

    # -- IM 侧：回复核销 ----------------------------------------------------

    def handle_reply(self, msg: ImMessage) -> bool:
        """尝试把一条入站消息当作审批回复处理；是审批回复（无论成败）返回 True。"""
        parsed = _parse_approval_reply(msg.text, DEFAULT_REPLY_OPTIONS)
        if parsed is None:
            return False
        decision, explicit_id = parsed

        # 群聊不作审批入口：拒绝并提示私聊，绝不消耗审批。
        if msg.chat_type != CHAT_PRIVATE:
            self.delivery.send_text(
                msg.target_id, "审批请私聊我处理（群里不核销任何审批）。")
            return True

        target = msg.target_id
        resolution: ApprovalResolution | None = None
        with self._lock:
            record = self._locate(target, explicit_id)
            if record is None:
                # 没有可核销记录：区分「已处理/已过期/不存在」给用户明确答复
                reply = self._explain_miss(target, explicit_id)
            elif record.request.target_id != target:
                # 显式引用了别的会话的审批号：不允许跨会话核销
                reply = "该审批不属于当前会话，无法处理。"
            else:
                resolution, reply = self._settle_locked(record, decision, msg)
        if reply:
            self.delivery.send_text(target, reply, reply_to=msg.message_id or None)
        if resolution is not None:
            self._fire(record, resolution)
        return True

    def expire_overdue(self) -> list[str]:
        """主动把所有已过期 pending 审批标记失效（可被定时器调用）。

        返回本次失效的 approval_id 列表；每条都会触发 expired 回调。
        """
        now = self._now()
        fired: list[tuple[_ApprovalRecord, ApprovalResolution]] = []
        with self._lock:
            for record in self._records.values():
                if record.status == STATUS_PENDING and record.request.expires_at <= now:
                    resolution = ApprovalResolution(
                        approval_id=record.request.approval_id,
                        task_id=record.request.task_id,
                        decision=DECISION_EXPIRED, resolved_at=now)
                    record.status = STATUS_EXPIRED
                    record.resolution = resolution
                    fired.append((record, resolution))
                    self._audit_locked("expired", record.request)
        for record, resolution in fired:
            self._fire(record, resolution)
        return [r.request.approval_id for r, _ in fired]

    # -- 内部 ----------------------------------------------------------------

    def _locate(self, target: str, explicit_id: str) -> _ApprovalRecord | None:
        """锁内定位要核销的记录：显式 id 优先，否则取该 target 最新一条 pending。"""
        if explicit_id:
            return self._records.get(explicit_id)
        candidates = [r for r in self._records.values()
                      if r.request.target_id == target and r.status == STATUS_PENDING]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.seq)

    def _explain_miss(self, target: str, explicit_id: str) -> str:
        """锁内：没有可核销记录时的解释文案（区分「已处理」与「没有待办」）。

        显式 id 走到这里说明台账中根本不存在（存在但已核销/已过期的
        由 ``_settle_locked`` 回答）。
        """
        if explicit_id:
            return "未找到该审批（审批号无效）。"
        for record in self._records.values():
            if record.request.target_id == target and record.status in (
                    STATUS_RESOLVED, STATUS_EXPIRED):
                return "审批已处理或已过期，无需重复操作。"
        return "当前没有待处理的审批。"

    def _settle_locked(self, record: _ApprovalRecord, decision: str,
                       msg: ImMessage) -> tuple[ApprovalResolution | None, str]:
        """锁内核销。返回 (resolution 或 None, 给用户的回复文案)。"""
        request = record.request
        if record.status == STATUS_RESOLVED:
            assert record.resolution is not None
            past = "批准" if record.resolution.decision == DECISION_APPROVED else "拒绝"
            return None, f"该审批已处理（{past}），旧审批号不能重复使用。"
        now = self._now()
        if record.status == STATUS_EXPIRED or request.expires_at <= now:
            # 懒过期：回复时才发现过期，补一个 expired 结局
            resolution = ApprovalResolution(
                approval_id=request.approval_id, task_id=request.task_id,
                decision=DECISION_EXPIRED, resolved_at=now)
            record.status = STATUS_EXPIRED
            record.resolution = resolution
            self._audit_locked("expired", request)
            return resolution, "该审批已过期，请让任务方重新发起。"
        resolution = ApprovalResolution(
            approval_id=request.approval_id, task_id=request.task_id,
            decision=decision, resolved_by=msg.target_id, resolved_at=now)
        record.status = STATUS_RESOLVED
        record.resolution = resolution
        self._audit_locked("resolved", request,
                           extra={"decision": decision, "by": msg.sender_id})
        verb = "已批准" if decision == DECISION_APPROVED else "已拒绝"
        return resolution, f"{verb}：{request.summary}（{request.approval_id}）"

    def _fire(self, record: _ApprovalRecord, resolution: ApprovalResolution) -> None:
        """锁外触发回调；回调异常吞掉（审批结局已落定，通知失败不改变事实）。"""
        callbacks: list[ResolverFn] = []
        if record.on_resolved is not None:
            callbacks.append(record.on_resolved)
        with self._lock:
            callbacks.extend(self._listeners)
        for callback in callbacks:
            try:
                callback(resolution)
            except Exception:
                pass  # fail soft：resolver 异常不反噬审批台账

    def _audit(self, event: str, request: ApprovalRequest,
               extra: dict | None = None) -> None:
        with self._lock:
            self._audit_locked(event, request, extra=extra)

    def _audit_locked(self, event: str, request: ApprovalRequest,
                      extra: dict | None = None) -> None:
        """JSONL 审计（可选）：每条状态迁移一行，绝不写回复正文之外的敏感信息。"""
        if self._jsonl_path is None:
            return
        row = {
            "event": event,
            "approval_id": request.approval_id,
            "task_id": request.task_id,
            "device_id": request.device_id,
            "action": request.action,
            "target_id": request.target_id,
            "expires_at": request.expires_at.isoformat(),
            "at": self._now().isoformat(),
        }
        if extra:
            row.update(extra)
        try:
            with self._jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # fail soft：审计写入失败不阻断审批流程
