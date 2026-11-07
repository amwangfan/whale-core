"""IM Gateway 统一消息模型（whale-mobile-core-development-plan §7）。

设计要点：
- ``target_id`` 是 core 内部的**稳定会话别名**，由 provider 生成并维护映射；
  平台原生 chat id（QQ 号、群号、Telegram chat_id）不进入统一模型，
  下游（路由/审批/投递）只认 target_id。
- ``ApprovalRequest`` / ``ApprovalResolution`` 对齐 contracts/v2 §13 的
  task.approval 对象模型：approval_id / task_id / device_id / action /
  summary / expires_at / reply_options；一次性、有有效期、旧 approval_id
  不得批准新动作、群聊不作审批入口。
- approval_id 用 ``devices/transports/base.py`` 的 ``new_id("aprv")``
  （前缀 + 26 字符 ULID，contracts/v2 §3）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from whale_core.devices.transports.base import new_id

#: 聊天类型词表
CHAT_PRIVATE = "private"
CHAT_GROUP = "group"

#: 审批决定词表（contracts/v2 §13：一次性核销）
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_EXPIRED = "expired"

#: 审批默认回复选项（第一个=批准，第二个=拒绝）
DEFAULT_REPLY_OPTIONS: tuple[str, ...] = ("批准", "拒绝")

#: 审批默认有效期（秒）
DEFAULT_APPROVAL_TTL_S = 300.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ImMessage:
    """统一入站消息。provider 负责把平台事件解析为本模型。"""

    channel: str                 # 渠道名，如 "qq" / "telegram"
    bot_id: str                  # 接收消息的 bot 身份（provider 实例标识）
    target_id: str               # 内部稳定会话别名（不暴露平台 chat id）
    sender_id: str               # 发送者平台账号（str 化）
    chat_type: str               # CHAT_PRIVATE | CHAT_GROUP
    message_id: str              # 平台消息 id（str 化）
    text: str                    # 纯文本（provider 已截断/清洗）
    attachments: tuple = ()      # 附件元数据 tuple[dict]，不携带字节
    received_at: datetime = field(default_factory=utc_now)

    @property
    def is_group(self) -> bool:
        return self.chat_type == CHAT_GROUP


@dataclass(frozen=True)
class OutboundMessage:
    """统一出站消息。target_id 由 provider 反向解析为平台地址。"""

    target_id: str
    text: str = ""
    attachments: tuple = ()      # AttachmentRef 风格 dict（§11），不含字节
    reply_to: str | None = None  # 引用的平台 message_id（可选）


@dataclass(frozen=True)
class ApprovalRequest:
    """core 发起的一次性审批（contracts/v2 §13 task.approval）。"""

    task_id: str
    device_id: str
    action: str                  # 待批准的能力/动作，如 "device.shell"
    summary: str                 # 给人看的一句话说明
    target_id: str               # 投递目标（主人私聊别名）
    approval_id: str = field(default_factory=lambda: new_id("aprv"))
    expires_at: datetime = field(default_factory=utc_now)  # 由 bridge 按 ttl 设置
    reply_options: tuple = DEFAULT_REPLY_OPTIONS
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ApprovalResolution:
    """审批结局。decision ∈ approved / rejected / expired。"""

    approval_id: str
    task_id: str
    decision: str
    resolved_by: str = ""        # 核销者的 target_id（主人私聊）；过期为 ""
    resolved_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Command:
    """斜杠命令的结构化形式。name 不带斜杠、小写；args 为剩余原文。"""

    name: str                    # stop | steer | status | new | model | 其他（未识别）
    args: str
    raw: str


#: 规划 §7 要求的命令集合
KNOWN_COMMANDS = ("stop", "steer", "status", "new", "model")


def parse_command(text: str) -> Command | None:
    """把以 ``/`` 开头的文本解析为 Command；非命令返回 None。

    ``/steer 改成分三步`` → Command(name="steer", args="改成分三步")。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    name = head.strip().lower()
    if not name:
        return None
    return Command(name=name, args=rest.strip(), raw=stripped)


@dataclass(frozen=True)
class DeadLetter:
    """投递最终失败（重试耗尽）的死信记录。delivery 层只投递、不重放任务。"""

    message: OutboundMessage
    error: str                   # 末次失败原因（异常类型名，不含正文敏感信息）
    attempts: int
    failed_at: datetime = field(default_factory=utc_now)


def attachment_dict(attachment_id: str = "", type: str = "file",
                    mime: str = "", **extra: Any) -> dict:
    """构造 §11 AttachmentRef 风格附件 dict 的小助手（只组装元数据）。"""
    data: dict[str, Any] = {"type": type}
    if attachment_id:
        data["attachment_id"] = attachment_id
    if mime:
        data["mime"] = mime
    data.update(extra)
    return data
