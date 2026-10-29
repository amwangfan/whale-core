"""QQ 入站 → AI 工作流（记忆召回 + DeepSeek 回复草稿 + 审批闸门）。

与 life/initiative 的保守默认对齐：
- 策略前置闸门：prefs 未启用或 direct_message 渠道未授权时**不调用 AI**（省 token，
  也不产生草稿）；静默时段/每日额度等仍由 ProactiveOutbox.inspect 在草稿后裁决。
- 回复只进 ProactiveOutbox 成为待审草稿（direct_message 渠道强制逐条人工批准），
  本模块**不发送任何消息**。真实发送是 Phase 2（NapCat 小号或 WeQ 受控发送接口，
  见 docs/research-weq-deepseek-qq.md §6）。
- AI 走 core.companion.WhaleCompanion：人格连续性上下文 + MemoryCurator 召回 +
  DeepSeek 生成 + 情节记忆回写，一条入站消息触发完整闭环。
- 同一 message_id 的草稿幂等（draft_id=qq-reply:<msgId>；监听层另有 msgId 去重）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from whale_core.core.companion import TextCompanion, WhaleCompanion
from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import MemoryStore

REPLY_CHANNEL = "direct_message"


class QqInboundWorkflow:
    """OwnerEvent → 回复草稿 + 审批裁决。单实例串行使用（监听层已保证按序派发）。"""

    def __init__(self, store: MemoryStore, brain: TextCompanion, *,
                 prefs: ProactivePrefs | None = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
        self.outbox = ProactiveOutbox(store)
        # Safe default: a caller must explicitly opt in to processing QQ messages.
        # The live runner passes owner-bound, operator-configured prefs explicitly.
        self.prefs = prefs if prefs is not None else ProactivePrefs()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._curator = MemoryCurator(store)

    _REMEMBER_PREFIXES = ("记住：", "记住:", "记住 ")
    _FORGET_PREFIXES = ("忘掉：", "忘掉:", "忘记：", "忘记:", "忘掉 ", "忘记 ")

    def _memory_command_reply(self, text: str, event: OwnerEvent) -> str | None:
        """owner 显式记忆指令（记住/忘掉）；非指令返回 None。

        - 记住：<事实> → curated 稳定事实（consent=True——owner 显式指令即同意；
          evidence 指向触发消息；stable_fact=True 不衰减）；
        - 忘掉/忘记：<线索> → 词项召回最相关的一条 curated 记忆并删除；
          只删 curated 记录（curator.forget 语义），聊天情节不受影响。
        """
        for prefix in self._REMEMBER_PREFIXES:
            if text.startswith(prefix):
                fact = text[len(prefix):].strip()
                if not fact:
                    return "想让我记住什么？"
                self._curator.remember(
                    fact, source="qq:owner",
                    evidence_id=f"qq:{event.conversation}:{event.message_id}",
                    importance=0.8, confidence=1.0, stable_fact=True, consent=True,
                )
                return f"好，我记住啦：{fact[:50]}"
        for prefix in self._FORGET_PREFIXES:
            if text.startswith(prefix):
                clue = text[len(prefix):].strip()
                if not clue:
                    return "想让我忘掉什么？"
                hits = self._curator.recall(clue, limit=1)
                if hits and self._curator.forget(hits[0].record.key):
                    summary = str(hits[0].record.content.get("summary", ""))
                    return f"已忘掉：{summary[:50]}"
                return "没有找到相关的记忆诶"
        return None

    def handle_owner_event(self, event: OwnerEvent) -> dict:
        """处理一条已配对的所有者消息，返回结构化结果（不含发送行为）。

        返回键：
        - skipped: 策略闸门未过（disabled/channel_denied），未调用 AI；
        - reply / draft_id / proposed / verdict / reason: 正常路径；
        - empty_reply: AI 返回空文本，不产生草稿。
        """
        # 前置闸门（便宜的先做）：整体开关与渠道授权。
        if not self.prefs.enabled:
            return {"skipped": "disabled"}
        if REPLY_CHANNEL not in self.prefs.allowed_channels:
            return {"skipped": "channel_denied"}

        text = event.text.strip()
        if not text:
            return {"skipped": "empty_text"}

        # owner 显式记忆指令（记住/忘掉）：不调模型，确定性处理（省 token、可测试）。
        # owner 直聊的显式指令即 consent（单 owner 陪伴场景，docs/gap-analysis §4 R4）；
        # 证据链指向触发消息，记忆可经「忘掉」或删除语义移除。
        command_reply = self._memory_command_reply(text, event)
        if command_reply is not None:
            reply = command_reply
        else:
            reply = self.companion.chat(text, conversation_id=event.conversation)
        if not reply or not reply.strip():
            return {"empty_reply": True, "draft_id": None}

        draft_id = f"qq-reply:{event.message_id}"
        draft = ProactiveDraft(
            draft_id=draft_id, text=reply,
            reason="QQ 入站消息的回复草稿（owner 已配对）",
            evidence=(f"qq:{event.conversation}:{event.message_id}",),
            channel=REPLY_CHANNEL,
        )
        proposed = self.outbox.propose(draft)
        # QQ 入站回复是**被动回复**（owner 先说话）：走被动策略——静默时段是否
        # 豁免由 prefs.passive_reply_in_quiet 显式决定，配额走独立保险丝（issue #13）。
        verdict = self.outbox.inspect(draft_id, self.prefs, now=self._now(),
                                      passive_reply=True)
        return {
            "draft_id": draft_id,
            "reply": reply,
            "proposed": proposed,
            "verdict": verdict.status,
            "reason": verdict.reason,
        }
