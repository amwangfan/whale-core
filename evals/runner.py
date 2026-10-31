"""回放执行器（issue #19）：InMemoryStore + 假 Brain 驱动真实代码路径。

回放纪律：

- 存储用 ``InMemoryStore``；上下文解析、成员记忆目录、关系视图、表达策略、
  群时间线、回复目标裁决、companion 注入链**全部是真实实现**（不 mock 被测
  对象）；只有模型层是 ``FakeBrain``——录制调用并返回固定文本，不调真实
  API；
- 群消息先落 ``GroupTimelineStore``（真实监听器的落点），裁决为「回复」的
  消息才走 ``WhaleCompanion.chat``；旁观消息只进时间线、不触发模型调用；
- 鲸鱼的群回复以 ``is_self=True`` 回写时间线（``{message_id}-r``），
  供归属断言核查「我的发言」与群友发言不串；
- 关系视图 / 表达策略对每条消息都计算（纯函数），与是否回复无关；
  私聊逐条走 ``chat``，历史由真实 ``append_turn`` 逐轮累积。

每个场景独立建仓（store/timeline/brain 全新），场景间零共享。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from whale_core.core.companion import WhaleCompanion
from whale_core.life.context_resolver import (
    ConversationContextResolver,
    derive_kind,
    pairing_lookup_from_mapping,
)
from whale_core.life.conversation_context import ConversationKind
from whale_core.life.expression import ExpressionPolicy, derive_policy
from whale_core.life.relationship import RelationshipResolver, RelationshipState
from whale_core.life.reply_target import ReplyTargetResolver
from whale_core.memory.bindings import BindingStore
from whale_core.memory.consent import MemberConsentStore, MemberMemoryDirectory
from whale_core.memory.history import append_turn
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord
from whale_core.memory.timeline import GroupTimelineStore

from evals.scenarios import (
    CHANNEL,
    GROUP,
    OWNER_UIN,
    SELF_UID,
    SELF_UIN,
    ReplayMessage,
    Scenario,
    principal_of,
    private_conv,
    private_conv_key,
)

#: 时间线渲染行首的「[消息尾4|归属标签]」解析式（与 render_for_model 格式一致）。
_LABEL_RE = re.compile(r"^\[([^\]|]+)\|([^\]]+)\]")


# ---------------------------------------------------------------------------
# 假 Brain：确定性、录制全部调用、不调真实模型 API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrainCall:
    text: str
    persona: str
    memories: Tuple[str, ...]
    history: Tuple[dict, ...]


class FakeBrain:
    """``reply(text, **kwargs)`` 录制调用并返回固定文本（纯确定性）。"""

    def __init__(self, reply_text: str = "收到，我看看。") -> None:
        self.reply_text = reply_text
        self.calls: list[BrainCall] = []

    def reply(self, text: str, *, persona: str = "", memories=(), history=(),
              images=()) -> str:
        self.calls.append(BrainCall(
            text=text, persona=persona,
            memories=tuple(str(m) for m in memories),
            history=tuple(dict(h) for h in history),
        ))
        return self.reply_text


# ---------------------------------------------------------------------------
# 回放产物
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRecord:
    """一条消息的回放观测（全部来自真实代码路径的输出）。"""

    message_id: str
    sender_id: str
    conversation_id: str
    text: str
    send_time: str
    is_group: bool
    # 回复目标裁决
    reply_action: str  # "reply" | "observe"
    reply_reason: str
    reply_target_sender: str
    # 关系视图（裁决时刻快照）
    familiarity: str
    interaction_count: int
    self_facts: Tuple[str, ...]
    relationship_render: str
    # 表达策略
    policy: ExpressionPolicy
    policy_render: str
    # 模型侧观测（旁观消息为空）
    persona_injected: str
    history_texts: Tuple[str, ...]  # 注入模型的 history 各条 content
    reply_text: str  # 鲸鱼回复（旁观为 ""）
    # 群时间线（该消息处理完毕后的渲染；私聊为 ""）
    timeline_render: str = ""


@dataclass(frozen=True)
class AttributionRecord:
    """时间线渲染中一条消息的归属观测。"""

    message_id: str
    sender_id: str
    is_self: bool
    tail: str  # message_id 尾 4（渲染行首的消息标签）
    label: str  # 渲染出的归属标签（「我」或「群友<hash>」）；缺失为 ""
    conversation_id: str


@dataclass(frozen=True)
class ProbeRecord:
    """陌生人探针观测（未发言主体的关系视图）。"""

    sender_id: str
    conversation_id: str
    familiarity: str
    interaction_count: int
    relationship_render: str


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    turns: Tuple[TurnRecord, ...]
    attributions: Tuple[AttributionRecord, ...]
    probes: Tuple[ProbeRecord, ...]
    brain_calls: Tuple[BrainCall, ...] = field(default_factory=tuple)

    def turn(self, message_id: str) -> Optional[TurnRecord]:
        for t in self.turns:
            if t.message_id == message_id:
                return t
        return None


# ---------------------------------------------------------------------------
# 回放世界（每场景独立）
# ---------------------------------------------------------------------------


class _World:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.store = InMemoryStore()
        self.spaces = SpaceStore(self.store)
        self.bindings = BindingStore(self.store, self.spaces)
        self.consents = MemberConsentStore(self.store)
        self.directory = MemberMemoryDirectory(self.bindings, self.spaces, self.consents)
        self.timeline = GroupTimelineStore(self.store)
        self.resolver = ConversationContextResolver(
            pairing_lookup=pairing_lookup_from_mapping({(CHANNEL, OWNER_UIN): "owner"}),
            bindings=self.bindings,
            member_memory=self.directory,
        )
        self.relationships = RelationshipResolver(self.store, timeline=self.timeline)
        self.reply_targets = ReplyTargetResolver(self_uin=SELF_UIN, self_uid=SELF_UID)
        self.brain = FakeBrain(scenario.reply_text)
        self.companion = WhaleCompanion(
            self.store, self.brain, timeline=self.timeline, self_uid=SELF_UID)
        #: (message_id, sender_id, is_self, conversation_id) —— 含鲸鱼回写
        self.group_messages: list[Tuple[str, str, bool, str]] = []

    # -- 预置（授权自述 / 私聊历史） -----------------------------------------

    def seed(self) -> None:
        for uin, fact in self.scenario.member_facts:
            principal = principal_of(uin)
            # 本人授权链（与 tests/test_persona_relationship.py 同口径）
            self.consents.grant(principal, GROUP, granted_by=principal,
                                granted_via=private_conv_key(uin))
            ref = self.bindings.ensure_member_space(
                principal, conversation_key=private_conv_key(uin),
                granted_by=principal)
            self.store.put_record(MemoryRecord(
                kind=MemoryKind.PREFERENCE,
                key="member-mem:eval-" + hashlib.sha1(
                    f"{principal}|{fact}".encode("utf-8")).hexdigest()[:12],
                content={"summary": fact, "confidence": 0.7},
                tags=("curated", "member"),
                space_id=ref.space_id,
            ))
        for uin, user_text, assistant_text in self.scenario.prior_private_turns:
            append_turn(self.store, private_conv(uin), user_text, assistant_text)

    # -- 单条消息回放 ----------------------------------------------------------

    def step(self, msg: ReplayMessage) -> TurnRecord:
        cid = msg.conversation_id
        is_group = derive_kind(CHANNEL, cid) is ConversationKind.QQ_GROUP
        if is_group:
            self.timeline.append(
                cid, message_id=msg.message_id, sender_id=msg.sender_id,
                text=msg.text, send_time=msg.send_time,
                mentions=msg.mentions, reply_to=msg.reply_to,
                is_self=msg.is_self)
            self.group_messages.append(
                (msg.message_id, msg.sender_id, msg.is_self, cid))

        ctx = self.resolver.resolve(channel=CHANNEL, sender_id=msg.sender_id,
                                    conversation_id=cid)
        decision = self.reply_targets.decide(
            conversation=cid, sender_id=msg.sender_id, message_id=msg.message_id,
            mentions=msg.mentions, reply_to=msg.reply_to)
        state: RelationshipState = self.relationships.state_for(ctx, conversation_id=cid)
        policy = derive_policy(msg.text, is_group=is_group,
                               self_facts=state.self_facts)

        persona_injected = ""
        history_texts: Tuple[str, ...] = ()
        reply_text = ""
        if decision.action == "reply":
            reply_text = self.companion.chat(
                msg.text, conversation_id=cid, context=ctx, episodic=False)
            call = self.brain.calls[-1]
            persona_injected = call.persona
            history_texts = tuple(str(h.get("content") or "")
                                  for h in call.history)
            if is_group:
                # 鲸鱼的回复回写时间线（归属「我」，并标注回复关系）
                self.timeline.append(
                    cid, message_id=f"{msg.message_id}-r", sender_id=SELF_UIN,
                    text=reply_text, send_time=msg.send_time,
                    reply_to=(msg.message_id,), is_self=True)
                self.group_messages.append(
                    (f"{msg.message_id}-r", SELF_UIN, True, cid))

        timeline_render = (
            self.timeline.render_for_model(cid, limit=200, self_uid=SELF_UID)
            if is_group else "")
        return TurnRecord(
            message_id=msg.message_id, sender_id=msg.sender_id,
            conversation_id=cid, text=msg.text, send_time=msg.send_time,
            is_group=is_group,
            reply_action=decision.action, reply_reason=decision.reason_code,
            reply_target_sender=decision.target_sender_id,
            familiarity=state.familiarity,
            interaction_count=state.interaction_count,
            self_facts=state.self_facts,
            relationship_render=state.render(),
            policy=policy, policy_render=policy.render(),
            persona_injected=persona_injected, history_texts=history_texts,
            reply_text=reply_text, timeline_render=timeline_render,
        )

    # -- 归属观测（场景结束后从最终时间线渲染提取） -----------------------------

    def extract_attributions(self) -> Tuple[AttributionRecord, ...]:
        records: list[AttributionRecord] = []
        by_conv: Dict[str, list[Tuple[str, str, bool]]] = {}
        for message_id, sender_id, is_self, cid in self.group_messages:
            by_conv.setdefault(cid, []).append((message_id, sender_id, is_self))
        for cid, messages in by_conv.items():
            rendered = self.timeline.render_for_model(cid, limit=200,
                                                      self_uid=SELF_UID)
            labels: Dict[str, str] = {}
            for line in rendered.splitlines():
                m = _LABEL_RE.match(line)
                if m:
                    labels.setdefault(m.group(1), m.group(2))
            for message_id, sender_id, is_self in messages:
                tail = message_id[-4:]
                records.append(AttributionRecord(
                    message_id=message_id, sender_id=sender_id, is_self=is_self,
                    tail=tail, label=labels.get(tail, ""), conversation_id=cid))
        return tuple(records)

    # -- 陌生人探针 --------------------------------------------------------------

    def run_probes(self) -> Tuple[ProbeRecord, ...]:
        out: list[ProbeRecord] = []
        for probe in self.scenario.probes:
            ctx = self.resolver.resolve(channel=CHANNEL,
                                        sender_id=probe.sender_id,
                                        conversation_id=probe.conversation_id)
            state = self.relationships.state_for(
                ctx, conversation_id=probe.conversation_id)
            out.append(ProbeRecord(
                sender_id=probe.sender_id,
                conversation_id=probe.conversation_id,
                familiarity=state.familiarity,
                interaction_count=state.interaction_count,
                relationship_render=state.render()))
        return tuple(out)


def run_scenario(scenario: Scenario) -> ScenarioResult:
    """回放一个场景并返回全部观测（确定性：同一代码同一输入同一结果）。"""
    world = _World(scenario)
    world.seed()
    turns = tuple(world.step(msg) for msg in scenario.messages)
    return ScenarioResult(
        scenario=scenario,
        turns=turns,
        attributions=world.extract_attributions(),
        probes=world.run_probes(),
        brain_calls=tuple(world.brain.calls),
    )
