"""G1：ReplyTargetResolver —— 群聊中「该不该回、回谁」的确定性裁决。

优先级（PRD G1.3）：显式 @ 机器人 > 引用机器人的消息 > 私聊 > 其他。
不确定时**保守不认领**（observe）。所有判断只基于平台事件元数据
（mentions/reply_to），不让 LLM 从对话正文猜（G1.1/ID-02）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnDecision:
    action: str            # "reply" | "observe"
    reason_code: str       # at_bot / reply_to_bot / private / group_observe
    target_sender_id: str = ""
    reply_to_message_id: str = ""


class ReplyTargetResolver:
    def __init__(self, *, self_uin: str = "", self_uid: str = ""):
        self.self_uin = str(self_uin or "")
        self.self_uid = str(self_uid or "")

    def decide(self, *, conversation: str, sender_id: str, message_id: str,
               mentions=(), reply_to=(), sender_uid: str = "") -> TurnDecision:
        if conversation.startswith("private:"):
            return TurnDecision("reply", "private", target_sender_id=sender_id,
                                reply_to_message_id=message_id)
        # 群聊：只有明确指向机器人才回复
        mentions = tuple(str(m) for m in mentions or ())
        if self.self_uid and self.self_uid in mentions:
            return TurnDecision("reply", "at_bot", target_sender_id=sender_id,
                                reply_to_message_id=message_id)
        if reply_to and len(reply_to) >= 2:
            orig_uin, orig_uid = str(reply_to[0]), str(reply_to[1])
            if (self.self_uin and orig_uin == self.self_uin) or \
               (self.self_uid and orig_uid == self.self_uid):
                return TurnDecision("reply", "reply_to_bot",
                                    target_sender_id=sender_id,
                                    reply_to_message_id=message_id)
        return TurnDecision("observe", "group_observe")
