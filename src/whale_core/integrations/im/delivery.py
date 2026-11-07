"""主动投递层：send_text / send_attachment / send_approval 统一入口。

纪律（规划 §7）：
- **有界重试**：默认最多 3 次尝试，指数退避（base * 2^n）；测试可注入
  ``sleep_fn`` 避免真实等待。
- **死信可查询**：重试耗尽后落 ``dead_letters()``，可人工查看/重发；
  delivery 层**只负责投递**，绝不因为通知失败去重放/重执行原任务
  （任务是否重试是 core 任务层自己的决策）。
- sender 是一个「发一条 OutboundMessage，失败抛异常」的可调用；由
  ImGateway 用 provider 出站能力装配，测试可注入 stub。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable

from whale_core.integrations.im.models import (
    ApprovalRequest,
    DeadLetter,
    OutboundMessage,
    utc_now,
)

MAX_ATTEMPTS = 3
BASE_DELAY_S = 0.5

#: sender 契约：发出去或抛异常；返回值忽略（回执语义归 provider 层）
SenderFn = Callable[[OutboundMessage], object]


class DeliveryError(RuntimeError):
    """投递层错误（如无可用 sender）。"""


def format_approval_text(request: ApprovalRequest) -> str:
    """把审批请求渲染为主人可读的私聊文本（含一次性/有效期提示）。"""
    options = "/".join(str(o) for o in request.reply_options) or "批准/拒绝"
    expires = request.expires_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return (
        "【审批请求】\n"
        f"{request.summary}\n"
        f"动作：{request.action}\n"
        f"任务：{request.task_id}\n"
        f"审批号：{request.approval_id}\n"
        f"有效期至：{expires}\n"
        f"请私聊回复「{options}」；一次性核销，过期自动失效。"
    )


class DeliveryService:
    """统一投递入口。线程安全（死信列表与计数有锁）。"""

    def __init__(self, sender: SenderFn, *,
                 max_attempts: int = MAX_ATTEMPTS,
                 base_delay_s: float = BASE_DELAY_S,
                 sleep_fn: Callable[[float], None] | None = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        if sender is None:
            raise ValueError("sender 必填（可注入 stub）")
        if max_attempts <= 0:
            raise ValueError("max_attempts 须为正整数")
        self._sender = sender
        self.max_attempts = int(max_attempts)
        self.base_delay_s = float(base_delay_s)
        self._sleep = sleep_fn or time.sleep
        self._now = now_fn or utc_now
        self._lock = threading.Lock()
        self._dead_letters: list[DeadLetter] = []
        self.sent_count = 0  # 成功投递计数（观测用）

    # -- 统一入口 ----------------------------------------------------------

    def send_text(self, target_id: str, text: str, *,
                  reply_to: str | None = None) -> bool:
        return self.send(OutboundMessage(target_id=target_id, text=text,
                                         reply_to=reply_to))

    def send_attachment(self, target_id: str, attachment: dict, *,
                        text: str = "") -> bool:
        return self.send(OutboundMessage(target_id=target_id, text=text,
                                         attachments=(attachment,)))

    def send_approval(self, target_id: str, request: ApprovalRequest) -> bool:
        """审批卡片投递（文本形式；卡片 UI 由真实 provider 后续扩展）。"""
        return self.send_text(target_id, format_approval_text(request))

    # -- 重试与死信 --------------------------------------------------------

    def send(self, message: OutboundMessage) -> bool:
        """有界重试投递；最终失败落死信并返回 False（不抛给调用方）。

        注意：失败只影响「这条通知」的送达；原任务不会因通知失败而被
        重新执行——本层没有、也不应该有任务语义。
        """
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                self._sender(message)
            except Exception as exc:  # noqa: BLE001 —— 投递失败一律转为重试/死信
                last_error = type(exc).__name__
                if attempt < self.max_attempts:
                    self._sleep(self.base_delay_s * (2 ** (attempt - 1)))
                continue
            with self._lock:
                self.sent_count += 1
            return True
        with self._lock:
            self._dead_letters.append(DeadLetter(
                message=message, error=last_error or "unknown",
                attempts=self.max_attempts, failed_at=self._now()))
        return False

    def dead_letters(self) -> list[DeadLetter]:
        """死信快照（新在前？不——按失败先后排序，便于审计）。"""
        with self._lock:
            return list(self._dead_letters)

    def clear_dead_letters(self) -> int:
        """清空死信（人工处理后调用），返回清除条数。"""
        with self._lock:
            n = len(self._dead_letters)
            self._dead_letters.clear()
            return n
