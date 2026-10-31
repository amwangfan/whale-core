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
                 media_validator=None,
                 auth_service=None,
                 context_resolver=None) -> None:
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
        # S4：临时授权服务（一次性口令 sudo）；None = 未启用，口令消息按
        # 普通文本处理。
        self._auth_service = auth_service
        # F-06：回落路径的上下文解析器（编排器缺位时使用）
        self._context_resolver = context_resolver
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
                # 候选下载链：attachments 可带 urls 有序列表（QQ CDN 相对路径
                # 已被渠道适配器补全为多个 rkey 候选），逐个尝试。
                candidates = att.get("urls") or ()
                if not candidates:
                    single = str(att.get("url") or "")
                    candidates = (single,) if single else ()
                for url in candidates[:4]:
                    if not str(url).startswith("https://"):
                        continue
                    try:
                        image = self._media_validator.from_url(str(url))
                        break
                    except MediaInputError as exc:
                        print(f"  [附件] 远端图片被拒：{exc}")
            if image is not None:
                out.append((image.data, image.mime))
        return tuple(out)

    def _on_async_complete(self, event: OwnerEvent, result) -> None:
        """两段式慢任务的完成回调：审核通过后交宿主补发真实结果。

        G1-B 送达回执：补发真实成功后 ``mark_delivered``；补发抛错
        ``mark_delivery_failed``（台账可见、可人工重发）。审核拦截不算
        送达失败——兜底说明发出即算已交付。
        """
        task_id = getattr(result, "task_id", "") or ""
        reply = getattr(result, "reply", "") or ""
        if not reply.strip():
            return
        receipts = getattr(result, "receipts", ())
        # 生图任务：模型未必把图片链接写进答复——产物 id 兜底注入，
        # 保证补发里必有图片段（本地字节直发，不经过网络/代理）。
        image_ids = _extract_image_artifacts(receipts)
        if image_ids and not _has_image_ref(reply):
            reply = reply.rstrip() + f"\n![图](/api/v1/artifacts/{image_ids[0]})"
        audio_ids = _extract_audio_artifacts(receipts)
        if self._safety_reviewer is not None:
            concern = self._safety_reviewer(reply)
            if concern:
                # 审核有顾虑不静默吞掉：留痕 + 给用户一句兜底交代（不发被拦内容）
                print(f"  [审核] 补发被拦：{concern[:80]}")
                fallback = "你要的东西做好了，但我对里面的链接不太放心，先没直接发——你在控制台任务页能看到，或者跟我说一声我再想办法给你。"
                if self._completion_sender is not None:
                    try:
                        self._completion_sender(event, fallback)
                        if self.orchestrator is not None and task_id:
                            self.orchestrator.mark_delivered(task_id, channel="qq")
                    except Exception:
                        if self.orchestrator is not None and task_id:
                            self.orchestrator.mark_delivery_failed(
                                task_id, error="completion-send-raised")
                return
        if self._completion_sender is not None:
            try:
                self._completion_sender(event, reply, audio_ids)
            except Exception:
                if self.orchestrator is not None and task_id:
                    self.orchestrator.mark_delivery_failed(
                        task_id, error="completion-send-raised")
                raise
            if self.orchestrator is not None and task_id:
                self.orchestrator.mark_delivered(task_id, channel="qq")

    def _memory_command_reply(self, text: str, event: OwnerEvent) -> str | None:
        """委托给 core.memory_commands 的共享实现（QQ 侧提供来源与证据链）。

        S2：编排器注入了 resolver 时按服务端上下文选择写入空间
        （工作副本优先，未绑定时落主人受保护库——安全但暂不进对话召回）。
        """
        space_id, allowed = "", None
        resolver = getattr(self.orchestrator, "context_resolver", None)
        if resolver is not None:
            ctx = resolver.resolve(channel=event.channel,
                                   sender_id=event.sender_id or event.owner_id,
                                   conversation_id=event.conversation)
            from whale_core.memory.spaces import (
                DEFAULT_VAULT_ID, default_write_space)
            space_id = default_write_space(ctx.allowed_memory_spaces) or DEFAULT_VAULT_ID
            allowed = ctx.allowed_memory_spaces
        return handle_memory_command(
            self.store, text, source="qq:owner",
            evidence_id=f"qq:{event.conversation}:{event.message_id}",
            curator=self._curator, space_id=space_id, allowed_spaces=allowed,
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

        # S4：sudo 验证尝试在进入 LLM/记忆/历史/日志之前截获（N17）。
        # 明文口令只用于验证，绝不向后续链路传递。
        if self._auth_service is not None:
            from whale_core.life.context_resolver import derive_kind
            from whale_core.life.conversation_context import ConversationKind
            from whale_core.life.temporary_auth import maybe_intercept_sudo
            attempt = maybe_intercept_sudo(text)
            if attempt is not None:
                kind = derive_kind(event.channel, event.conversation)
                if kind is ConversationKind.QQ_GROUP:
                    # 群里不开放 sudo，也不把验证回执发进群（观感+最小披露）
                    return {"skipped": "sudo_in_group_ignored"}
                grant = self._auth_service.verify(
                    attempt, channel=event.channel,
                    sender_id=event.sender_id or event.owner_id,
                    conversation_kind=kind,
                    conversation_id=event.conversation)
                if grant is not None:
                    mins = max(1, int((grant.expires_at - __import__("time").time()) / 60))
                    return {"reply": (
                        "验证通过，本次私聊已获得临时授权："
                        f"可用能力 {('、'.join(grant.scopes)) or '无'}，"
                        f"{mins} 分钟内最多 {grant.max_uses} 次操作。"
                        "到期或用尽自动恢复普通权限。"),
                        "draft_id": None, "auth_grant": grant.grant_id}
                return {"reply": "验证失败（口令无效、已使用或已过期）。",
                        "draft_id": None, "auth_grant": None}

        turn_receipts: list = []
        # owner 显式记忆指令（记住/忘掉）：不调模型，确定性处理（省 token、可测试）。
        # owner 直聊的显式指令即 consent（单 owner 陪伴场景，docs/gap-analysis §4 R4）；
        # 证据链指向触发消息，记忆可经「忘掉」或删除语义移除。
        command_reply = self._memory_command_reply(text, event)
        if command_reply is not None:
            reply = command_reply
        elif self.orchestrator is not None:
            from whale_core.core.runtime import InputEnvelope

            images = self._attachment_images(event) if event.attachments else ()
            envelope = InputEnvelope(
                conversation_id=event.conversation, text=text, source="qq:owner",
                channel="qq", owner_id=event.owner_id, images=images,
                sender_id=event.sender_id)
            if self._completion_sender is not None:
                result = self.orchestrator.handle_envelope_async(
                    envelope,
                    on_complete=lambda r, ev=event: self._on_async_complete(ev, r))
            else:
                result = self.orchestrator.handle_envelope(envelope)
            reply = result.reply
            turn_receipts = list(getattr(result, "receipts", []) or [])
        else:
            # F-06（红队）：无编排器的回落路径同样按服务端上下文限域，
            # 不走「全空间兼容召回」旁路。
            fallback_ctx = None
            resolver = getattr(self.orchestrator, "context_resolver", None) \
                or self._context_resolver
            if resolver is not None:
                fallback_ctx = resolver.resolve(
                    channel=event.channel,
                    sender_id=event.sender_id or event.owner_id,
                    conversation_id=event.conversation)
            reply = self.companion.chat(text, conversation_id=event.conversation,
                                        context=fallback_ctx)
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

    # -- S7：陌生账号私聊（受限入口） -----------------------------------------

    def handle_stranger_event(self, event) -> dict:
        """处理**未配对**账号的私聊消息（S7；宿主须显式开门）。

        纪律：
        - 仅私聊；群里的陌生人消息不进入（群门由适配器 prefs 控制）。
        - sudo 尝试优先截获（口令明文不进入 LLM/记忆/历史/日志）。
        - 普通对话走编排器，ResolvedContext = 匿名主体（chat.basic +
          可能的有效租约 scope）；无记忆空间、无写回、无工具提权。
        - 回复由宿主直接发送到该私聊（不经 owner 草稿/审批链）。
        """
        from whale_core.core.runtime import InputEnvelope
        from whale_core.life.context_resolver import derive_kind
        from whale_core.life.temporary_auth import maybe_intercept_sudo

        # 兼容 Incoming（account 字段）与 OwnerEvent（sender_id 字段）两种形状
        sender = str(getattr(event, "sender_id", "") or getattr(event, "account", ""))
        channel = str(getattr(event, "channel", "qq") or "qq")
        text = (event.text or "").strip()
        if not text:
            return {"skipped": "empty_text"}
        is_private = str(event.conversation).startswith("private:")
        # 群成员消息：不开放 sudo，直接走匿名群上下文对话（resolver 保证群上限）。
        # 私聊：sudo 验证尝试优先截获。
        if is_private and self._auth_service is not None:
            attempt = maybe_intercept_sudo(text)
            if attempt is not None:
                grant = self._auth_service.verify(
                    attempt, channel=channel, sender_id=sender,
                    conversation_kind=derive_kind(channel, event.conversation),
                    conversation_id=event.conversation)
                if grant is not None:
                    import time as _time
                    mins = max(1, int((grant.expires_at - _time.time()) / 60))
                    return {"reply": (
                        "验证通过，本次私聊已获得临时授权："
                        f"可用能力 {('、'.join(grant.scopes)) or '无'}，"
                        f"{mins} 分钟内最多 {grant.max_uses} 次操作。"
                        "到期或用尽自动恢复普通权限。"),
                        "auth_grant": grant.grant_id}
                return {"reply": "验证失败（口令无效、已使用或已过期）。",
                        "auth_grant": None}

        if not is_private and maybe_intercept_sudo(text) is not None:
            # 群里的 sudo 尝试：不验证、不回显、不落记忆——只提示私聊（N05/N17）
            return {"reply": "验证身份请私聊我（群里不做这个）。"}

        if self.orchestrator is None:
            return {"skipped": "no_orchestrator"}
        envelope = InputEnvelope(
            conversation_id=event.conversation, text=text, source="qq:stranger",
            channel=channel, sender_id=sender)
        result = self.orchestrator.handle_envelope(envelope)
        reply = (result.reply or "").strip()
        if not reply:
            return {"empty_reply": True}
        return {"reply": reply}


def _extract_image_artifacts(receipts) -> tuple:
    """从回执里提图片产物 id（生图工具 JSON 回执的 images 列表）。"""
    import json as _json
    ids: list = []
    for r in receipts or ():
        text = str(r)
        if ":ok:" not in text or "/api/v1/artifacts/" not in text:
            continue
        payload = text.split(":ok:", 1)[-1]
        try:
            data = _json.loads(payload)
        except Exception:
            continue
        for ref in (data.get("images") or [])[:2]:
            ref = str(ref)
            if "/api/v1/artifacts/" in ref:
                ids.append(ref.rsplit("/", 1)[-1])
    return tuple(ids)


_IMG_REF_RE = None


def _has_image_ref(text: str) -> bool:
    import re
    global _IMG_REF_RE
    if _IMG_REF_RE is None:
        _IMG_REF_RE = re.compile(
            r"https?://[^\s)\]<>'\"]+\.(?:png|jpe?g|webp|gif)|/api/v1/artifacts/",
            re.IGNORECASE)
    return bool(_IMG_REF_RE.search(text or ""))


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
