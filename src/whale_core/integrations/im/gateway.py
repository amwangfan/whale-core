"""IM Gateway：provider 注册、入站路由、命令分发、主动投递（规划 §7）。

职责边界：
- 入站：provider 回调 → 统一 ImMessage → 命令/审批回复/普通消息三分类路由；
- 命令：/stop /steer /status /new /model 解析为结构化 Command，**不实现**
  任务执行本体——通过注入的 ``TaskControlPort`` 回调出去（返回的字符串
  作为给用户的结果回复）；
- 审批回复优先于普通消息，命令优先于审批回复（``/`` 开头不可能是审批词）；
- 群聊：可接收通知（出站不限），但命令与审批核销一律拒绝并提示私聊
  （contracts/v2 §13：群聊不作审批入口；控制面整体只认私聊，与
  life/temporary_auth「群里不 sudo」同款纪律）；
- 出站：``send`` 按 target_id 反查所属 provider 后交给 DeliveryService
  （有界重试 + 死信）。
"""
from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from whale_core.integrations.im.approval_bridge import ApprovalBridge
from whale_core.integrations.im.delivery import DeliveryError, DeliveryService
from whale_core.integrations.im.models import (
    KNOWN_COMMANDS,
    Command,
    ImMessage,
    OutboundMessage,
    parse_command,
)
from whale_core.integrations.im.router import SessionRouter

#: 普通消息回调：(消息, 绑定的 conversation_id) -> None
MessageHandlerFn = Callable[[ImMessage, str], None]


@runtime_checkable
class ImProvider(Protocol):
    """IM provider 契约（QQ dry-run、未来的 Telegram 等都实现这个形状）。"""

    channel: str
    bot_id: str

    def set_inbound_handler(self, handler: Callable[[ImMessage], None]) -> None: ...
    def owns(self, target_id: str) -> bool:
        """该 target_id 是否由本 provider 生成（出站寻址用）。"""
        ...
    def send(self, message: OutboundMessage) -> object:
        """发一条出站消息；失败抛异常（DeliveryService 负责重试/死信）。"""
        ...


@runtime_checkable
class TaskControlPort(Protocol):
    """core 任务控制回调口。方法返回给用户看的答复文本；空串=不回复。

    Gateway 只做解析与转发，不实现任何任务执行语义。
    """

    def stop(self, conversation_id: str) -> str: ...
    def steer(self, conversation_id: str, instruction: str) -> str: ...
    def status(self, conversation_id: str) -> str: ...
    def new_session(self, target_id: str, conversation_id: str) -> str: ...
    def set_model(self, conversation_id: str, model: str) -> str: ...


class ImGateway:
    """IM Gateway 本体。单实例可挂多个 provider（按 target_id 归属寻址）。"""

    def __init__(self, *, router: SessionRouter | None = None,
                 delivery: DeliveryService | None = None,
                 approval_bridge: ApprovalBridge | None = None,
                 task_control: TaskControlPort | None = None,
                 message_handler: MessageHandlerFn | None = None) -> None:
        self.router = router if router is not None else SessionRouter()
        self._delivery = delivery
        self.approval_bridge = approval_bridge
        self.task_control = task_control
        self.message_handler = message_handler
        self._providers: list[ImProvider] = []
        # 未被任何处理器消费的消息落这里（观测/测试；默认不产生自动回复）
        self.unhandled: list[ImMessage] = []

    # -- provider 与投递装配 ------------------------------------------------

    def register_provider(self, provider: ImProvider) -> None:
        """挂接一个 provider；其入站回调进 Gateway 的统一路由。"""
        self._providers.append(provider)
        provider.set_inbound_handler(self._handle_inbound)

    @property
    def delivery(self) -> DeliveryService:
        """投递服务；未显式注入时按已注册 provider 懒装配。"""
        if self._delivery is None:
            self._delivery = DeliveryService(self._dispatch_send)
        return self._delivery

    def _dispatch_send(self, message: OutboundMessage) -> object:
        """按 target_id 找所属 provider 发出；找不到即投递失败（进重试/死信）。"""
        for provider in self._providers:
            if provider.owns(message.target_id):
                return provider.send(message)
        raise DeliveryError(f"no provider owns target {message.target_id!r}")

    # -- 主动投递（对外 API） -------------------------------------------------

    def send(self, message: OutboundMessage) -> bool:
        return self.delivery.send(message)

    def send_text(self, target_id: str, text: str, *,
                  reply_to: str | None = None) -> bool:
        return self.delivery.send_text(target_id, text, reply_to=reply_to)

    # -- 入站路由 ------------------------------------------------------------

    def _handle_inbound(self, msg: ImMessage) -> None:
        command = parse_command(msg.text)
        if command is not None:
            self._handle_command(msg, command)
            return
        if self.approval_bridge is not None and self.approval_bridge.handle_reply(msg):
            return
        conversation_id = self.router.resolve(msg.target_id)
        if self.message_handler is not None:
            self.message_handler(msg, conversation_id)
        else:
            self.unhandled.append(msg)

    def _handle_command(self, msg: ImMessage, command: Command) -> None:
        # 控制面只认私聊（群里的命令一律拒绝并提示，审批纪律的延伸）
        if msg.is_group:
            self.send_text(msg.target_id, "命令请私聊我处理（群里只接收通知）。")
            return
        port = self.task_control
        name = command.name
        if name not in KNOWN_COMMANDS:
            self.send_text(msg.target_id,
                           f"未知命令 /{name}。可用：/stop /steer /status /new /model")
            return
        if name == "new":
            # /new 重建会话绑定是 Gateway/路由层自身的语义，不依赖任务层
            conversation_id = self.router.reset(msg.target_id)
            reply = port.new_session(msg.target_id, conversation_id) if port else ""
            self.send_text(msg.target_id, reply or "已开始新会话。")
            return
        conversation_id = self.router.resolve(msg.target_id)
        if port is None:
            self.send_text(msg.target_id, "任务控制未接入，命令暂不可用。")
            return
        if name == "steer" and not command.args:
            self.send_text(msg.target_id, "用法：/steer <给任务的指示>")
            return
        if name == "model" and not command.args:
            self.send_text(msg.target_id, "用法：/model <模型名>")
            return
        handler = {
            "stop": lambda: port.stop(conversation_id),
            "steer": lambda: port.steer(conversation_id, command.args),
            "status": lambda: port.status(conversation_id),
            "model": lambda: port.set_model(conversation_id, command.args),
        }[name]
        reply = handler()
        if reply:
            self.send_text(msg.target_id, reply)
