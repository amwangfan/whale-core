"""QQ 模拟 provider（dry-run）：复用 OneBot v11 解析，出站只记录不发送。

- 入站：从注入队列取 OneBot 风格事件 dict，用
  ``whale_core.life.qq_onebot.parse_onebot_message`` 解析（与线上同一套
  截断/过滤/私群判定），再映射为统一 ImMessage；
- 出站：``RecordingOneBotTransport`` 把每次 call 追加到内存列表（可选
  JSONL 落盘），**绝不发起任何网络请求**——本模块不 import 任何 socket/
  urllib，真发送路径（HttpOneBotTransport）在这里根本不被实例化；
- target_id：``sha256(salt|channel|conversation)`` 生成的稳定不透明别名，
  平台原生 QQ 号/群号不进入统一模型；salt 默认每次实例化随机（进程内
  稳定），测试可注入固定 salt 复现。
"""
from __future__ import annotations

import hashlib
import json
import queue as queue_mod
import secrets
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from whale_core.integrations.im.models import (
    CHAT_GROUP,
    CHAT_PRIVATE,
    ImMessage,
    OutboundMessage,
)
from whale_core.life.qq_onebot import (
    CHANNEL_QQ,
    OneBotTransportError,
    parse_onebot_message,
)

ALIAS_PREFIX = "tgt_"


class RecordingOneBotTransport:
    """OneBotTransport 记录桩：追加记录、返回拟真回执，永不触网。

    - ``calls``：每次调用的 {action, params, at} 快照（内存）；
    - 可选 ``jsonl_path`` 同步落盘（演示/审计）；
    - 可选 ``responder`` 自定义回执（测试中抛异常即可模拟发送失败）；
      默认返回 OneBot 受理成功形状 ``{"retcode": 0, "status": "ok", ...}``。
    """

    def __init__(self, *, jsonl_path: str | Path | None = None,
                 responder: Callable[[str, dict], dict] | None = None) -> None:
        self.calls: list[dict] = []
        self._jsonl_path = Path(jsonl_path) if jsonl_path else None
        self._responder = responder
        self._seq = 0

    def call(self, action: str, params: dict) -> dict:
        # 深拷贝一层参数，防调用方事后改内容污染记录
        record = {
            "seq": self._seq,
            "action": str(action),
            "params": json.loads(json.dumps(params, ensure_ascii=False)),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self._seq += 1
        self.calls.append(record)
        if self._jsonl_path is not None:
            try:
                with self._jsonl_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass  # fail soft：记录落盘失败不影响主流程
        if self._responder is not None:
            return self._responder(action, params)
        return {"retcode": 0, "status": "ok",
                "data": {"message_id": record["seq"] + 1}}

    def drain(self) -> list[dict]:
        """取出并清空已记录的调用（「本会真实会发送的内容」）。"""
        drained = list(self.calls)
        self.calls.clear()
        return drained


class QqDryRunProvider:
    """QQ dry-run provider：队列进、记录出，全程零网络。

    用法：``feed(onebot_event_dict)`` 注入事件 → ``pump()`` 逐条解析并
    回调 Gateway；``send`` 经 RecordingOneBotTransport 记录；
    ``drain_sent()`` 查看本会「真实会发送」的内容。
    """

    channel = CHANNEL_QQ

    def __init__(self, *, bot_id: str, inbound=None,
                 transport: RecordingOneBotTransport | None = None,
                 salt: str | None = None) -> None:
        if not str(bot_id):
            raise ValueError("bot_id 必填")
        self.bot_id = str(bot_id)
        # 入站队列：支持 deque / list / queue.Queue 三种注入形状
        self._inbound = inbound if inbound is not None else deque()
        self.transport = transport if transport is not None else RecordingOneBotTransport()
        self._salt = salt if salt is not None else secrets.token_hex(16)
        self._targets: dict[str, str] = {}   # target_id -> 平台 conversation
        self._handler: Callable[[ImMessage], None] | None = None

    # -- 入站 ---------------------------------------------------------------

    def set_inbound_handler(self, handler: Callable[[ImMessage], None]) -> None:
        self._handler = handler

    def feed(self, event: dict) -> None:
        """便捷注入一条 OneBot 事件（等价于宿主把事件塞进注入队列）。"""
        if hasattr(self._inbound, "put_nowait"):
            self._inbound.put_nowait(event)
        elif hasattr(self._inbound, "append"):
            self._inbound.append(event)
        else:
            raise TypeError("注入队列须支持 append 或 put_nowait")

    def _pop_event(self):
        """从注入队列取一条；空了返回 None。"""
        if isinstance(self._inbound, queue_mod.Queue):
            try:
                return self._inbound.get_nowait()
            except queue_mod.Empty:
                return None
        if isinstance(self._inbound, deque):
            return self._inbound.popleft() if self._inbound else None
        if isinstance(self._inbound, list):
            return self._inbound.pop(0) if self._inbound else None
        raise TypeError("注入队列须为 deque / list / queue.Queue")

    def pump(self) -> int:
        """把队列里积压的事件全部解析派发，返回产出的 ImMessage 数。"""
        count = 0
        while True:
            event = self._pop_event()
            if event is None:
                return count
            msg = self._convert(event)
            if msg is None:
                continue
            count += 1
            if self._handler is not None:
                self._handler(msg)

    def _convert(self, event: object) -> ImMessage | None:
        """OneBot 事件 → ImMessage；解析复用 life.qq_onebot（线上同款）。"""
        incoming = parse_onebot_message(event, authenticated=True, accept_group=True)
        if incoming is None:
            return None
        conversation = incoming.conversation
        target_id = self._alias(conversation)
        if conversation.startswith("private:"):
            chat_type = CHAT_PRIVATE
        elif conversation.startswith("group:"):
            chat_type = CHAT_GROUP
        else:
            return None  # 频道/临时会话等未接入形态
        received_at = datetime.now(timezone.utc)
        if isinstance(event, Mapping) and isinstance(event.get("time"), (int, float)):
            try:
                received_at = datetime.fromtimestamp(float(event["time"]),
                                                     tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                pass
        return ImMessage(
            channel=self.channel, bot_id=self.bot_id, target_id=target_id,
            sender_id=incoming.account, chat_type=chat_type,
            message_id=incoming.message_id, text=incoming.text,
            attachments=_extract_attachments(event), received_at=received_at)

    # -- target_id 别名 -------------------------------------------------------

    def _alias(self, conversation: str) -> str:
        """平台 conversation → 稳定不透明 target_id（进程内稳定，可复现）。"""
        digest = hashlib.sha256(
            f"{self._salt}|{self.channel}|{conversation}".encode("utf-8")
        ).hexdigest()
        target_id = f"{ALIAS_PREFIX}{digest[:20]}"
        self._targets.setdefault(target_id, conversation)
        return target_id

    def owns(self, target_id: str) -> bool:
        return target_id in self._targets

    def resolve(self, target_id: str) -> str | None:
        """反查平台 conversation（观测/调试用；统一模型之外不应依赖它）。"""
        return self._targets.get(target_id)

    # -- 出站（记录而不发送） --------------------------------------------------

    def send(self, message: OutboundMessage) -> dict:
        """把统一出站消息翻译成 OneBot action 并**记录**（不发送）。"""
        conversation = self._targets.get(message.target_id)
        if conversation is None:
            raise OneBotTransportError("unknown target_id (no alias mapping)")
        if conversation.startswith("private:"):
            action = "send_private_msg"
            address = {"user_id": _maybe_int(conversation[len("private:"):])}
        elif conversation.startswith("group:"):
            action = "send_group_msg"
            address = {"group_id": _maybe_int(conversation[len("group:"):])}
        else:
            raise OneBotTransportError(f"unsupported conversation {conversation!r}")
        params = {**address, "message": _build_segments(message)}
        receipt = self.transport.call(action, params)
        if not (receipt.get("retcode") == 0 or receipt.get("status") == "ok"):
            raise OneBotTransportError("onebot rejected the send (recorded)")
        return receipt

    def drain_sent(self) -> list[dict]:
        """查看本会「真实会发送」的全部内容（并清空记录）。"""
        return self.transport.drain()


def _maybe_int(value: str):
    """QQ 号/群号是数字就转 int（OneBot 习惯），否则原样保留。"""
    text = str(value)
    return int(text) if text.isdigit() else text


def _build_segments(message: OutboundMessage) -> list[dict]:
    """统一出站消息 → OneBot message 段数组。"""
    segments: list[dict] = []
    if message.reply_to:
        segments.append({"type": "reply", "data": {"id": str(message.reply_to)}})
    if message.text:
        segments.append({"type": "text", "data": {"text": message.text}})
    for attachment in message.attachments or ():
        if not isinstance(attachment, Mapping):
            continue
        att_type = str(attachment.get("type") or "file")
        data = {str(k): v for k, v in attachment.items() if k != "type"}
        segments.append({"type": att_type, "data": data})
    return segments


def _extract_attachments(event: object) -> tuple:
    """从原始 OneBot 事件提取非文本段元数据（只记 url/file 引用，不取字节）。"""
    if not isinstance(event, Mapping):
        return ()
    raw = event.get("message")
    if not isinstance(raw, list):
        return ()
    out: list[dict] = []
    for segment in raw:
        if not isinstance(segment, Mapping):
            continue
        seg_type = segment.get("type")
        if seg_type in (None, "text"):
            continue
        data = segment.get("data")
        data = data if isinstance(data, Mapping) else {}
        out.append({"type": str(seg_type),
                    "url": str(data.get("url") or ""),
                    "file": str(data.get("file") or "")})
    return tuple(out)
