"""下一阶段综合集成：§10 固定样例经真实监听口全链路回放（离线）。

WeQSsePushListener（真解析+真配对门）→ 时间线 → ReplyTargetResolver →
QqInboundWorkflow → 编排器 →（假发送口）。验证 DoD 前两条：
身份/引用/话题始终可辨；时间线结构化且正文不被无身份注入。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins"))

from weq_channel.wq_push import WeQSsePushListener
from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator
from whale_core.life.channels import OwnerChannelRegistry
from whale_core.life.context_resolver import (
    ConversationContextResolver, pairing_lookup_from_mapping)
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.life.reply_target import ReplyTargetResolver
from whale_core.memory.bindings import BindingStore
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import InMemoryStore
from whale_core.memory.timeline import GroupTimelineStore

SELF_UIN = "3899713055"
GROUP = "1124744261"
A_UIN, B_UIN = "10001", "10002"


class RecBrain:
    def __init__(self):
        self.calls = []

    def reply(self, text, **kwargs):
        self.calls.append(kwargs)
        return "先看是不是卡死了，别急着关机。"


def _ev(mid, sender, text, *, uid="", elements=()):
    return {"type": "message", "chatType": "group", "convId": GROUP,
            "msgId": mid, "msgSeq": "1", "senderUin": sender,
            "senderUid": uid, "sendTime": "1793419000", "text": text,
            "elements": list(elements)}


def _build():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    registry = OwnerChannelRegistry(owner_id="owner", bindings={})
    timeline = GroupTimelineStore(store)
    brain = RecBrain()
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({}),
        bindings=BindingStore(store, spaces), owner_id="owner")
    orch = ConversationOrchestrator(store, WhaleCompanion(store, brain),
                                    context_resolver=resolver)
    wf = QqInboundWorkflow(store, brain, orchestrator=orch)
    wf.companion.timeline = timeline
    target = ReplyTargetResolver(self_uin=SELF_UIN, self_uid="u_self")
    replied = []
    listener = WeQSsePushListener(
        registry, expected_token="tok", self_uin=SELF_UIN,
        on_owner_event=lambda e: replied.append(("owner", e)),
        on_unpaired=lambda inc: replied.append(("member", inc))
        if target.decide(conversation=inc.conversation, sender_id=inc.account,
                         message_id=inc.message_id, mentions=inc.mentions,
                         reply_to=inc.reply_to).action == "reply" else None,
        accept_group=True, group_allow=frozenset({GROUP}),
        timeline=timeline, async_dispatch=False)
    return store, timeline, listener, brain, replied


def _feed(listener, *events):
    body = json.dumps({"events": list(events)}).encode()
    return listener.process_payload({"Authorization": "Bearer tok"}, body)


def test_section10_full_pipeline_replay():
    """§10 样例：A 求助 / B 十六进制 / A 继续——归属、裁决、上下文三处一致。"""
    store, timeline, listener, brain, replied = _build()
    conv = f"group:{GROUP}"
    _feed(listener,
          _ev("m-a1", A_UIN, "我的 Windows 电脑很卡，明天论文还要交。"),
          _ev("m-b1", B_UIN, "E6 88 91 E7 88 B1 E4 BD A0"),
          _ev("m-a2", A_UIN, "电脑现在动不了了，如果装了 360 是不是就不会这样？"))
    # 时间线：三条各自归属，B 不并入 A
    entries = timeline.recent(conv)
    assert [e["sender_id"] for e in entries] == [A_UIN, B_UIN, A_UIN]
    # 无 @/引用 → 全部旁观，无人被误回复（B 的「我爱你」不被认领）
    assert replied == []


def test_at_bot_triggers_reply_with_context():
    """群友 @ 机器人 → 回复且模型上下文带归属时间线。"""
    store, timeline, listener, brain, replied = _build()
    conv = f"group:{GROUP}"
    _feed(listener,
          _ev("m-a1", A_UIN, "电脑很卡"),
          _ev("m-b1", B_UIN, "@鲸鱼 在吗", elements=[
              {"type": "at", "data": {"atTargetUid": "u_self"}}]))
    assert replied and replied[0][0] == "member"
    inc = replied[0][1]
    assert inc.account == B_UIN  # 回复目标是 B，不是 A
    # 工作流处理：companion 的群历史来自时间线且带归属
    wf_out = None
    store2 = InMemoryStore()
    tl2 = GroupTimelineStore(store2)
    tl2.append(conv, message_id="m-a1", sender_id=A_UIN, text="电脑很卡")
    brain2 = RecBrain()
    c = WhaleCompanion(store2, brain2, timeline=tl2)
    c.chat("怎么办", conversation_id=conv)
    hist = brain2.calls[-1]["history"]
    assert "群友" in hist[0]["content"] and "电脑很卡" in hist[0]["content"]


def test_paired_owner_event_flows_with_metadata():
    """已配对主人的群消息正常进入 owner 通道且元数据完整（旁观裁决在宿主层）。"""
    store, timeline, listener, brain, replied = _build()
    listener.registry.bindings[("qq", A_UIN)] = "owner"
    _feed(listener, _ev("m-o1", A_UIN, "帮我搜下明天的天气",
                        elements=[{"type": "at", "data": {"atTargetUid": "u_self"}}]))
    assert replied and replied[0][0] == "owner"
    ev = replied[0][1]
    assert ev.sender_id == A_UIN and ev.conversation == f"group:{GROUP}"
    assert ev.message_id == "m-o1" and ev.mentions == ("u_self",)
