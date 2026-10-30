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
from whale_core.core.memory_commands import handle_memory_command
from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import MemoryStore

REPLY_CHANNEL = "direct_message"


def _default_media_roots() -> tuple[str, ...]:
    """QQ 本机媒体缓存白名单根目录（G0-B）。

    - 显式配置优先：``WHALE_QQ_MEDIA_ROOTS``（os.pathsep 分隔）；
    - 否则自动收敛到 QQ NT 客户端媒体缓存 ``~/.config/QQ/nt_qq_*/nt_data``
      （仅收录当前真实存在的目录，fail-closed：一个都不存在就拒绝本地读取）。
    """
    import glob
    import os

    configured = os.environ.get("WHALE_QQ_MEDIA_ROOTS", "").strip()
    if configured:
        return tuple(
            root for root in configured.split(os.pathsep) if root.strip()
        )
    return tuple(
        path
        for path in glob.glob(
            os.path.expanduser("~/.config/QQ/nt_qq_*/nt_data")
        )
        if os.path.isdir(path)
    )


class QqInboundWorkflow:
    """OwnerEvent → 回复草稿 + 审批裁决。单实例串行使用（监听层已保证按序派发）。"""

    def __init__(self, store: MemoryStore, brain: TextCompanion, *,
                 prefs: ProactivePrefs | None = None,
                 now_fn: Callable[[], datetime] | None = None,
                 orchestrator=None,
                 safety_reviewer=None,
                 completion_sender=None,
                 media_validator=None) -> None:
        self.store = store
        self.companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
        self.outbox = ProactiveOutbox(store)
        # Safe default: a caller must explicitly opt in to processing QQ messages.
        # The live runner passes owner-bound, operator-configured prefs explicitly.
        self.prefs = prefs if prefs is not None else ProactivePrefs()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._curator = MemoryCurator(store)
        # 可选的对话编排器（意图路由 + 工具）：设置后回复改由编排器产出
        # （多轮/记忆/指令在编排器内部同款共享），本草稿仍走 Outbox 裁决。
        self.orchestrator = orchestrator
        # 可选安全审核（llm.safety 角色）：callable(reply) -> None=通过 | str=顾虑原因；
        # 有顾虑的草稿强制 needs_approval（不自动发送），由人工终审。
        self._safety_reviewer = safety_reviewer
        # 可选补发回调（两段式慢任务）：callable(event, reply) -> None。
        # 由宿主注入真实发送路径（草稿+审批+网关）；慢任务 ack 后的结果经此补发。
        self._completion_sender = completion_sender
        # 媒体输入可信边界（G0-B）：附件路径白名单/SSRF/格式与尺寸校验统一走
        # core.media_input.ImageInputValidator；未注入时构造默认实例
        # （本地路径 fail-closed——无白名单即拒绝本地读取，仅走受门控的 https）。
        if media_validator is None:
            from whale_core.core.media_input import ImageInputValidator

            media_validator = ImageInputValidator(
                allowed_roots=_default_media_roots()
            )
        self._media_validator = media_validator

    def _attachment_images(self, event: OwnerEvent) -> tuple:
        """把消息附件读成已验证图片 ``(bytes, mime)``：本机缓存路径优先
        （白名单内），公网 URL 兜底（https + SSRF 门控）。最多取 4 张；
        单张校验失败只跳过该附件，不阻断整轮对话。"""
        from whale_core.core.media_input import MediaInputError

        out: list = []
        for att in (event.attachments or ())[:4]:
            if not isinstance(att, dict) or att.get("kind") != "pic":
                continue
            image = None
            path = str(att.get("file_path") or "")
            if path:
                try:
                    image = self._media_validator.from_local_path(path)
                except MediaInputError as exc:
                    print(f"  [附件] 本地图片被拒：{exc}")
            if image is None:
                url = str(att.get("url") or "")
                if url.startswith("https://"):
                    try:
                        image = self._media_validator.from_url(url)
                    except MediaInputError as exc:
                        print(f"  [附件] 远端图片被拒：{exc}")
            if image is not None:
                out.append((image.data, image.mime))
        return tuple(out)

    def _on_async_complete(self, event: OwnerEvent, result) -> None:
        """两段式慢任务的完成回调：审核通过后交宿主补发真实结果。"""
        reply = getattr(result, "reply", "") or ""
        if not reply.strip():
            return
        audio_ids = _extract_audio_artifacts(getattr(result, "receipts", ()))
        if self._safety_reviewer is not None:
            concern = self._safety_reviewer(reply)
            if concern:
                # 审核有顾虑不静默吞掉：留痕 + 给用户一句兜底交代（不发被拦内容）
                print(f"  [审核] 补发被拦：{concern[:80]}")
                fallback = "你要的东西做好了，但我对里面的链接不太放心，先没直接发——你在控制台任务页能看到，或者跟我说一声我再想办法给你。"
                if self._completion_sender is not None:
                    self._completion_sender(event, fallback)
                return
        if self._completion_sender is not None:
            self._completion_sender(event, reply, audio_ids)

    def _memory_command_reply(self, text: str, event: OwnerEvent) -> str | None:
        """委托给 core.memory_commands 的共享实现（QQ 侧只提供来源与证据链）。"""
        return handle_memory_command(
            self.store, text, source="qq:owner",
            evidence_id=f"qq:{event.conversation}:{event.message_id}",
            curator=self._curator,
        )

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

        turn_receipts: list = []
        # owner 显式记忆指令（记住/忘掉）：不调模型，确定性处理（省 token、可测试）。
        # owner 直聊的显式指令即 consent（单 owner 陪伴场景，docs/gap-analysis §4 R4）；
        # 证据链指向触发消息，记忆可经「忘掉」或删除语义移除。
        command_reply = self._memory_command_reply(text, event)
        if command_reply is not None:
            reply = command_reply
        elif self.orchestrator is not None:
            images = self._attachment_images(event) if event.attachments else ()
            if self._completion_sender is not None:
                result = self.orchestrator.handle_message_async(
                    event.conversation, text, source="qq:owner",
                    on_complete=lambda r, ev=event: self._on_async_complete(ev, r),
                    images=images)
            else:
                result = self.orchestrator.handle_message(
                    event.conversation, text, source="qq:owner", images=images)
            reply = result.reply
            turn_receipts = list(getattr(result, "receipts", []) or [])
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
        result = {
            "draft_id": draft_id,
            "reply": reply,
            "proposed": proposed,
            "verdict": verdict.status,
            "reason": verdict.reason,
            "receipts": turn_receipts,
        }
        # 安全审核（可选）：审核模型有顾虑时打标——runner 据此不自动发送，
        # 留人工终审；不改变账本/配额语义。
        if self._safety_reviewer is not None:
            concern = self._safety_reviewer(reply)
            if concern:
                result["safety_concern"] = str(concern)[:200]
                result["reason"] = f"{result['reason']}｜安全审核：{result['safety_concern']}"
        return result


def _extract_audio_artifacts(receipts) -> tuple:
    """从回执里提音频产物 id（TTS 等工具的 JSON 回执含 artifact_id +
    content_type=audio/*）。"""
    import json as _json
    import re as _re
    out: list[str] = []
    for r in receipts or ():
        s = str(r)
        if "artifact_id" not in s or "audio" not in s:
            continue
        for m in _re.finditer(r"\{[^{}]*\}", s):
            try:
                obj = _json.loads(m.group(0))
            except ValueError:
                continue
            aid = obj.get("artifact_id")
            ctype = str(obj.get("content_type") or "")
            if aid and ctype.startswith("audio/"):
                out.append(str(aid))
    return tuple(out)
