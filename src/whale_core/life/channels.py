"""Identity-aware inbound contract. Actual QQ/OneBot adapter remains external."""
from __future__ import annotations
from dataclasses import dataclass


def record_uid_mapping(store, uin: str, uid: str) -> None:
    """G2：传输层已验证的 uin↔uid 映射（同一事件里两者同源出现）。

    QQ 私聊会话 id 用 u_ 形态 uid，配对表用数字 uin——不做映射，「本人
    私聊」结构校验永远对不上。只记录非空且形状合法的映射。
    """
    uin, uid = str(uin or ""), str(uid or "")
    if not uin or not uid or uin == uid:
        return
    if not uid.startswith("u_"):
        return
    store.put(f"uidmap:uin:{uin}", uid)
    store.put(f"uidmap:uid:{uid}", uin)


def uid_to_uin(store, uid: str) -> str:
    val = store.get(f"uidmap:uid:{uid}")
    return str(val) if isinstance(val, str) else ""


@dataclass(frozen=True)
class Incoming:
    channel: str
    account: str
    conversation: str
    message_id: str
    text: str
    transport_authenticated: bool = False
    # 附件（图片等）：{"kind": "pic", "file_path": "...", "url": "..."} 元组
    attachments: tuple = ()
    # G1：完整入站元数据（TrustedInboundEvent 子集）。
    # mentions：被 @ 的目标 uid 元组；reply_to：引用消息的来源
    # {"sender_uin","sender_uid","msg_id","msg_seq"}；均来自平台事件元数据，
    # 不从正文猜测（ID-01/02）。
    sender_uid: str = ""
    send_time: str = ""
    mentions: tuple = ()
    reply_to: tuple = ()


@dataclass(frozen=True)
class OwnerEvent:
    owner_id: str
    channel: str
    conversation: str
    message_id: str
    text: str
    # 附件（图片等）：{"kind": "pic", "file_path"|"url"} 元组——宿主负责取字节
    attachments: tuple = ()
    # S1（Whale Studio）：保留真实发送者。群聊中 sender 是说话成员本人，
    # 不等于全群身份；下游禁止用 owner_id 推断群里其他成员的权限。
    sender_id: str = ""
    # G1：@/引用/uid/时间元数据随事件穿透（含持久化 inbox 路径）。
    sender_uid: str = ""
    send_time: str = ""
    mentions: tuple = ()
    reply_to: tuple = ()


class OwnerChannelRegistry:
    """Explicitly paired identities only; never infer ownership from nickname."""
    def __init__(self, *, owner_id: str, bindings: dict[tuple[str, str], str]):
        self.owner_id = owner_id
        self.bindings = dict(bindings)

    def accept(self, message: Incoming) -> OwnerEvent | None:
        if not message.transport_authenticated:
            return None
        matched = self.bindings.get((message.channel, message.account))
        if matched != self.owner_id or not message.message_id:
            return None
        return OwnerEvent(owner_id=self.owner_id, channel=message.channel,
                          conversation=message.conversation,
                          message_id=message.message_id, text=message.text,
                          attachments=tuple(message.attachments or ()),
                          sender_id=message.account,
                          sender_uid=message.sender_uid,
                          send_time=message.send_time,
                          mentions=tuple(message.mentions or ()),
                          reply_to=tuple(message.reply_to or ()))
