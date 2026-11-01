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
                 context_resolver=None,
                 on_member_provisioned=None,
                 stranger_daily_limit: int | None = None) -> None:
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
        # issue #16：成员建档/恢复后的回调（宿主接回填管道）；
        # callable(principal_id, reason)——reason ∈ {"auto", "optin"}。
        self._on_member_provisioned = on_member_provisioned
        # 陌生人/成员被动回复的日额度（防滥用保险丝）；
        # None = 类缺省 20。
        if stranger_daily_limit is not None and int(stranger_daily_limit) < 1:
            raise ValueError("stranger_daily_limit 须为正整数")
        self._stranger_daily_limit = (int(stranger_daily_limit)
                                      if stranger_daily_limit is not None
                                      else self.STRANGER_DAILY_LIMIT)
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

    def _effective_prefs(self) -> ProactivePrefs:
        """issue #42：kv 覆盖生效视图（单一配置来源——Studio 写入即生效，
        无需重启）；kv 缺失时回落构造期的部署侧 prefs。"""
        from whale_core.life.initiative import load_prefs
        return load_prefs(self.store, self.prefs)

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
            from whale_core.memory.spaces import default_write_space
            space_id = default_write_space(ctx.allowed_memory_spaces)
            allowed = ctx.allowed_memory_spaces
            # A group with no writable memory must NEVER fall back to the
            # owner's protected vault. Forget is also a write operation.
            from whale_core.core.memory_commands import REMEMBER_PREFIXES, FORGET_PREFIXES
            if not space_id and text.strip().startswith(REMEMBER_PREFIXES + FORGET_PREFIXES):
                return "当前会话没有可写记忆库，请先在工作台绑定有写权限的记忆空间。"
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
        eff_prefs = self._effective_prefs()  # issue #42：kv 覆盖生效

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
            target_conversation=event.conversation,
        )
        proposed = self.outbox.propose(draft)
        # QQ 入站回复是**被动回复**（owner 先说话）：走被动策略——静默时段是否
        # 豁免由 prefs.passive_reply_in_quiet 显式决定，配额走独立保险丝（issue #13）。
        verdict = self.outbox.inspect(draft_id, eff_prefs, now=self._now(),
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

    #: G6 红队高-1：陌生人/群成员回复的独立日额度（每 sender+会话）
    STRANGER_DAILY_LIMIT = 20  # 缺省值；宿主可经构造参数覆盖（见 __init__）

    def _stranger_gate(self, sender: str, conversation: str) -> str | None:
        """陌生人/群成员被动回复的滥用闸门（直发路径不经 Outbox，必须自带）。

        返回 None 放行；否则返回跳过原因。

        issue #22（owner 决策 2026-11-01）：本路径处理的都是「人类先发起」
        的入站消息，属于被动回复——**静默时段不拦截**；静默只约束无请求
        触发的主动打扰（ProactiveOutbox 的 unsolicited 判定）。这里保留的
        是防滥用额度（每人每会话每日上限），与渠道白名单/去重/工具权限
        各司其职。
        注意：不查 prefs.enabled——那是主动外发总开关，与本路径无关。
        """
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        key = f"rl:stranger:{day}:{conversation}:{sender}"
        count = self.store.get(key)
        count = int(count) if isinstance(count, (int, float)) else 0
        if count >= self._stranger_daily_limit:
            return "stranger_budget"
        self.store.put(key, count + 1)
        return None

    # -- 成员自助记忆管理（私聊指令；G2.4 的渠道内流程） --------------------

    #: 指令 → 处理器（在 handle_stranger_event 中优先于闲聊路径）
    _MEMBER_CMDS = (
        (("同意记录我", "同意记忆同步", "同意记住我", "恢复记录我"), "grant"),
        (("撤销记忆同步", "不同意记录我", "取消记忆同步", "撤销记录授权",
          "别记我了", "不要记录我"), "revoke"),
        (("我的记忆", "我的记忆有什么", "你记得我什么"), "list"),
        (("删掉我的记忆", "删除我的记忆", "忘掉我的一切"), "wipe"),
    )

    def _member_command_reply(self, text: str, *, principal_id: str,
                              conversation_key: str) -> str | None:
        """成员本人私聊的授权/记忆管理指令。非指令返回 None。

        身份证据 = 当前私聊会话本身（只有本人能在这个会话里发消息）。
        全部确定性处理，不调模型。
        """
        from whale_core.memory.consent import MemberConsentStore
        from whale_core.memory.spaces import SpaceStore

        matched = None
        for prefixes, action in self._MEMBER_CMDS:
            if text.strip() in prefixes:
                matched = action
                break
        if matched is None:
            return None

        consents = MemberConsentStore(self.store)
        spaces = SpaceStore(self.store)
        from whale_core.memory.bindings import BindingStore
        bindings = BindingStore(self.store, spaces)
        timeline = getattr(self.companion, "timeline", None)

        if matched == "grant":
            # owner 决策 2026-11-01：私聊即同意，默认在记；此指令用于
            # 「别记我了」之后的恢复。
            if consents.is_opted_out(principal_id):
                consents.opt_in(principal_id)
                bindings.ensure_member_space(
                    principal_id, conversation_key=conversation_key,
                    granted_by=principal_id)
                if self._on_member_provisioned is not None:
                    try:
                        self._on_member_provisioned(principal_id, "optin")
                    except Exception:
                        pass  # 回填失败不阻断指令回复
                return "好，重新开始记啦。"
            bindings.ensure_member_space(
                principal_id, conversation_key=conversation_key,
                granted_by=principal_id)
            return ("我一直在记着呢（你说的和我聊的）。想停就说「别记我了」，"
                    "想清光说「删掉我的记忆」。")

        if matched == "revoke":
            consents.opt_out(principal_id)
            return ("好，我以后不记了。之前记下的还在你小本本里，"
                    "说「删掉我的记忆」可以一并清掉。")

        if matched == "list":
            binding = bindings.member_binding_for(principal_id)
            if not binding:
                return "还没有你的个人记忆空间——你还没授权过我记录。"
            ref = spaces.ref(str(binding["space_id"]))
            if ref is None:
                return "还没有你的个人记忆空间——你还没授权过我记录。"
            from whale_core.memory.store import MemoryKind
            items = []
            for kind in (MemoryKind.EPISODE, MemoryKind.PREFERENCE):
                for rec in self.store.list_by_kind(kind, space_ids=(ref.space_id,)):
                    if "member" in rec.tags and "superseded" not in rec.tags:
                        items.append(str(rec.content.get("summary", ""))[:60])
            if not items:
                return "你的小本本现在还是空的。"
            return "我现在记着你的这些：\n" + "\n".join(f"· {s}" for s in items[:10])

        if matched == "wipe":
            binding = bindings.member_binding_for(principal_id)
            n_del = 0
            if binding:
                ref = spaces.ref(str(binding["space_id"]))
                if ref is not None:
                    from whale_core.memory.store import MemoryKind
                    for kind in (MemoryKind.EPISODE, MemoryKind.PREFERENCE):
                        for rec in list(self.store.list_by_kind(
                                kind, space_ids=(ref.space_id,))):
                            if "member" in rec.tags:
                                self.store.delete(rec.key)  # 真删
                                n_del += 1
            from whale_core.memory.extraction import (
                purge_member_extraction_state)
            n_del += purge_member_extraction_state(self.store, principal_id)
            # issue #21：经验（个性化建议权重）随记忆一并真删
            from whale_core.memory.experience import ExperienceStore
            n_del += ExperienceStore(self.store).purge_principal(principal_id)
            consents.opt_out(principal_id)
            return (f"清掉了 {n_del} 条（含提炼进度记录），以后我也不再记了。"
                    "（已经发出去的聊天记录和群里的公共时间线收不回。）"
                    "想说重新开始就说「恢复记录我」。")
        return None

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

        # owner 决策 2026-11-01：成员私聊即默认记录其个人记忆（自动建档；
        # 「别记我了」持久退出）。先建档再进 resolver，当轮即可回忆。
        if is_private and sender:
            try:
                from whale_core.memory.consent import MemberConsentStore
                consents = MemberConsentStore(self.store)
                principal_id = f"anon:{channel}:{sender}"
                if not consents.is_opted_out(principal_id):
                    from whale_core.memory.spaces import SpaceStore
                    from whale_core.memory.bindings import BindingStore
                    spaces = SpaceStore(self.store)
                    BindingStore(self.store, spaces).ensure_member_space(
                        principal_id,
                        conversation_key=f"qq:qq_private:{event.conversation}",
                        granted_by=principal_id)
                    if self._on_member_provisioned is not None:
                        self._on_member_provisioned(principal_id, "auto")
            except Exception:
                pass  # 建档/回填失败不阻断对话（fail soft；记忆缺席非致命）

        # G2.4：成员本人私聊的授权/记忆管理指令（先于预算闸门与闲聊——
        # 隐私管理权不消耗对话额度、不被静默时段拦）。
        if is_private:
            principal_id = f"anon:{channel}:{sender}"
            member_reply = self._member_command_reply(
                text, principal_id=principal_id,
                conversation_key=f"qq:qq_private:{event.conversation}")
            if member_reply is not None:
                return {"reply": member_reply}

        # G6 红队高-1：陌生人/群成员回复自带预算与静默闸门（直发路径不经
        # Outbox，必须前置）。sudo 验证回复不计额度（那是安全功能）。
        gate = self._stranger_gate(sender, str(event.conversation))
        if gate is not None:
            return {"skipped": gate}

        if self.orchestrator is None:
            return {"skipped": "no_orchestrator"}
        # 附件图片与 owner 路径同一可信边界（白名单/嗅探/SSRF 门控）；
        # 陌生人发的图可读，但不写入任何记忆（写作仍受空间纪律约束）。
        images = self._attachment_images(event) if getattr(
            event, "attachments", None) else ()
        envelope = InputEnvelope(
            conversation_id=event.conversation, text=text, source="qq:stranger",
            channel=channel, sender_id=sender, images=images)
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
