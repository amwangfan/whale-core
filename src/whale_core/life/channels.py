"""Identity-aware inbound contract. Actual QQ/OneBot adapter remains external."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Incoming:
    channel: str
    account: str
    conversation: str
    message_id: str
    text: str
    transport_authenticated: bool = False


@dataclass(frozen=True)
class OwnerEvent:
    owner_id: str
    channel: str
    conversation: str
    message_id: str
    text: str


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
                          message_id=message.message_id, text=message.text)
