"""统一对话编排的入站/出站信封与任务相位表（issue #30 拆分）。

- ``InputEnvelope``：跨渠道统一入站信封（G1-A）；
- ``TurnResult``：一轮对话的结果（status 如实标注结局）；
- ``TASK_PHASES`` / ``_PHASE_STATUS``：任务生命周期（G1-B）相位机与
  兼容 status 映射；
- ``_Decision``：一次请求至多一次规划的结果（模型提议、核心裁决后）。
"""
from __future__ import annotations

from dataclasses import dataclass

from whale_core.core.intent_plan import IntentPlan


@dataclass(frozen=True)
class TurnResult:
    """一轮对话的结果。status 如实标注结局：

    - answered：正常回答（含纯聊天与工具任务完成）；
    - clarify：需要用户澄清（未执行任何工具）；
    - pending_approval：动作已提交待审批（**未执行**）；
    - denied：被策略拒绝（未执行）；
    - failed：执行失败（部分工具可能已跑完——看 receipts）。
    """

    reply: str
    status: str
    plan: IntentPlan | None = None
    receipts: tuple[str, ...] = ()
    task_id: str = ""


@dataclass(frozen=True)
class InputEnvelope:
    """跨渠道统一入站信封（G1-A）：所有渠道的消息经同一结构进入编排器。

    - ``source``：宿主侧认证后的来源标签（api/cli/qq/...），**永不来自
      用户或模型输入**；
    - ``channel``：逻辑渠道（qq/web/voice/...），供投递与隐私域判定；
    - ``owner_id``：逻辑 owner 域（G2 跨设备连续性以此归并）；
    - ``images``：已过可信边界验证的图片 ``((bytes, mime), ...)``。
    """

    conversation_id: str
    text: str
    source: str = "runtime"
    channel: str = ""
    owner_id: str = ""
    images: tuple = ()
    request_id: str = ""
    # S1（Whale Studio）：真实发送者与服务端授权上下文。
    # sender_id 由可信渠道适配器填写；context 只接受服务端解析产物，
    # 编排器注入 resolver 后会自行重解析并覆盖任何调用方提供的值。
    sender_id: str = ""
    context: object | None = None


# 任务生命周期（G1-B）：pending → acknowledged → running(leased) →
# waiting_approval(pending_approval) → succeeded/failed/unknown → delivered；
# cancelled/denied 为旁路终态；delivery_failed 是「送达失败可见、可人工重发」。
TASK_PHASES = frozenset({
    "pending", "acknowledged", "running", "pending_approval",
    "succeeded", "failed", "unknown", "cancelled", "denied",
    "delivered", "delivery_failed",
})

# phase → 兼容 status（旧消费方读 status；新逻辑一律读 phase）
_PHASE_STATUS = {"succeeded": "answered", "delivered": "answered",
                 "delivery_failed": "answered"}


@dataclass
class _Decision:
    """G1-A：一次请求至多一次规划的结果（模型提议、核心裁决后）。"""

    route: str  # "empty" | "memory" | "chat" | "clarify" | "task"
    plan: IntentPlan | None = None
    receipt: str = ""  # 规划异常留痕（如 intent:parse-failed→plain-chat）
    memory_reply: str = ""
    slow: bool = False
