"""对话编排（issue #30 拆分，G1-A）：统一入口与唯一规划点。

一次请求至多一次规划；慢任务两段式（台账先于 ack，后台执行经同一
``_dispatch_step`` 门控）。模型输出只是提议，核心裁决后才派发。
"""
from __future__ import annotations

import threading
from dataclasses import replace
from uuid import uuid4

from whale_core.core.intent_plan import IntentPlanError
from whale_core.core.memory_commands import handle_memory_command
from whale_core.core.runtime.context_resolution import _ctx_spaces, _ctx_write_space
from whale_core.core.runtime.envelope import InputEnvelope, TurnResult, _Decision
from whale_core.memory.history import append_turn, conversation_lock


class TurnOrchestrationMixin:
    """对话编排职责（编排器 mixin；状态由 ConversationOrchestrator 提供）。"""

    # ---- G1-A：统一入口（InputEnvelope）与唯一规划点 ----

    def handle_message(self, conversation_id: str, text: str, *, source: str = "runtime",
                       images: Sequence = (), sender_id: str = "",
                       on_delta=None, should_stop=None, metrics=None,
                       on_phase=None) -> TurnResult:
        return self.handle_envelope(InputEnvelope(
            conversation_id=conversation_id, text=text, source=source,
            images=tuple(images), sender_id=sender_id, on_delta=on_delta,
            should_stop=should_stop, metrics=metrics, on_phase=on_phase))

    def handle_envelope(self, env: InputEnvelope) -> TurnResult:
        """同步入口：一次请求至多一次规划，Decision 直接消费。"""
        env = self._ensure_context(env)
        with conversation_lock(self.store, env.conversation_id):
            return self._finish_decision(env, self._decide(env))

    def handle_message_async(
        self, conversation_id: str, text: str, *, source: str = "runtime",
        on_complete=None, images: Sequence = (), sender_id: str = "",
    ) -> TurnResult:
        return self.handle_envelope_async(InputEnvelope(
            conversation_id=conversation_id, text=text, source=source,
            images=tuple(images), sender_id=sender_id), on_complete=on_complete)

    def handle_envelope_async(self, env: InputEnvelope, *, on_complete=None) -> TurnResult:
        """两段式慢任务回复（owner 决策 2026-10-31；G1-A/B 重构）：

        - **一次请求一次规划**：快路径（chat/clarify/无慢能力的任务/记忆指令）
          复用同一个 Decision 同步执行，不二次规划；慢任务（含慢能力前缀）
          复用同一个 IntentPlan 后台执行；
        - **台账先于 ack**（G1-B）：任务先持久化 phase=pending，再生成并返回
          人格语气占位 ack（status="ack"），随后标记 acknowledged；
        - 后台线程执行完成后调 ``on_complete(TurnResult)`` 把真实结果交给
          宿主补发；送达回执经 ``mark_delivered`` / ``mark_delivery_failed``
          落到任务台账（delivery 子状态）。
        """
        env = self._ensure_context(env)
        with conversation_lock(self.store, env.conversation_id):
            decision = self._decide(env)
            if decision.route != "task" or not decision.slow:
                return self._finish_decision(env, decision)
            # 两段式的后台执行不等对话方：ack 已发，用户没被阻塞——超时按
            # 供应商真实时长计（生图实测 60-180s），与同步路径的 60s 会话
            # 超时是两个语义。这不是「加长超时糊弄失败」：同步路径保持 60s，
            # 异步后台路径需要覆盖供应商真实执行时长才有意义。
            plan = replace(decision.plan,
                           timeout_s=max(decision.plan.timeout_s, 240.0))
            # G1-B：先持久化台账（pending）再 ack——ack 不得先于持久任务创建。
            task_id, plan = self._persist_task(
                plan, conversation_id=env.conversation_id, text=env.text,
                source=env.source, channel=env.channel, owner_id=env.owner_id,
                async_delivery=True, context=env.context)
            try:
                attach_note = (
                    "用户的消息带有图片附件且已成功读取（你在生成这句占位回复时"
                    "看不到图片内容，但**不要说没看到图**）。"
                    if env.images else "")
                ack = self._voiced(
                    env.conversation_id, env.text,
                    "用户的请求需要耗时工具处理（生图/搜索等，约半分钟到几分钟）。"
                    "请用你自己的人格语气先回一句你已经在做了、稍后给结果——简短自然，"
                    "不要假装已经完成。" + attach_note, turn_key=task_id,
                    allowed_spaces=_ctx_spaces(env.context),
                    write_space=_ctx_write_space(env.context))
            except Exception:
                ack = "好，我在处理了，稍后给你结果。"  # ack 模型失败不阻断任务
            self._save_task(task_id, plan, "acknowledged", ())

            images = env.images

            def _run() -> None:
                run_plan = plan
                # 参考图转文字：生图任务且带附件图 → 先用视觉能力把参考图描述
                # 成一句风格描述并进生图 prompt（意图模型是文本规划，看不见图）。
                if images and any(str(c).startswith("media.image") for c in plan.capabilities):
                    try:
                        desc = self.companion.chat(
                            "用户发来一张参考图，想用它的风格/角色再创作。"
                            "用一句话（≤80字）描述这张图的角色外观与画风要点，供生图模型参考。"
                            "只写描述，不要客套。",
                            conversation_id=env.conversation_id, images=images,
                            episodic=False, context=env.context)
                        desc = (desc or "").strip()[:200]
                        if desc:
                            slots = dict(plan.slots)
                            slots["prompt"] = (str(slots.get("prompt") or plan.goal)[:400]
                                               + "。参考图特征：" + desc)
                            run_plan = replace(plan, slots=slots)
                            self._update_journal_plan(task_id, run_plan)
                    except Exception:
                        pass  # 描述失败不阻塞生图（按原 prompt 画）
                try:
                    result = self._execute_task(task_id)
                except Exception:
                    # 执行层异常已在台账留痕（failed/unknown）；回调仍要触发，
                    # 让用户拿到诚实结局而不是沉默。
                    record = self.store.get(f"task:{task_id}") or {}
                    result = TurnResult(
                        reply="任务执行中断，结果未知。",
                        status=str(record.get("status", "unknown")),
                        plan=plan, task_id=task_id)
                if on_complete is not None:
                    try:
                        on_complete(result)
                    except Exception:
                        # 宿主补发异常：台账标记送达失败（可见、可人工重发）
                        self.mark_delivery_failed(task_id, error="host-callback-raised")

            threading.Thread(target=_run, daemon=True,
                             name=f"whale-task-{task_id}").start()
            return TurnResult(reply=ack, status="ack", plan=plan, task_id=task_id)

    def _decide(self, env: InputEnvelope) -> _Decision:
        """G1-A 唯一规划点：一次请求至多一次意图模型调用。

        顺序（与 roadmap §6.2 一致）：空消息 → 记忆指令捷径（不调模型）→
        无意图模型纯对话 → 意图规划（模型提议、核心裁决，失败回落纯对话）。
        """
        text = (env.text or "").strip()
        if not text:
            return _Decision(route="empty")
        # S2：显式记忆指令是写入操作——携带已解析上下文时仅限已认证主体，
        # 写入当前授权工作空间（无则落主人受保护库，绝不进会话召回路径）。
        # 本机宿主入口（cli/runtime 源且无渠道 sender）视为受信管理通道。
        ctx = env.context
        trusted_host = env.source in ("runtime", "cli") and not env.sender_id
        if ctx is None or getattr(ctx.principal, "is_owner", False) or trusted_host:
            write_space = _ctx_write_space(ctx)
            if ctx is not None and not write_space:
                from whale_core.core.memory_commands import REMEMBER_PREFIXES, FORGET_PREFIXES
                if text.strip().startswith(REMEMBER_PREFIXES + FORGET_PREFIXES):
                    return _Decision(route="memory", memory_reply=(
                        "当前会话没有可写记忆库，请先绑定有写权限的记忆空间。"))
            command = handle_memory_command(
                self.store, text, source=env.source, curator=self.companion.curator,
                evidence_id=f"{env.source}:{env.conversation_id}:{uuid4().hex}",
                space_id=write_space or "",
                allowed_spaces=_ctx_spaces(ctx),
            )
            if command is not None:
                return _Decision(route="memory", memory_reply=command)
        if self.intent_model is None:
            return _Decision(route="chat")
        # 意图模型需要上下文（近期历史+相关记忆）才能做合理默认；
        # 带附件时必须告知（它是文本规划看不见图，否则会误以为没图去澄清）。
        plan_text = text
        if env.images:
            plan_text = f"（用户消息附带 {len(env.images)} 张图片，已可读取）\n" + text
        # 经 facade 模块属性解析 plan_from_model_text：测试可在
        # whale_core.core.runtime 命名空间上 monkeypatch 规划裁决函数。
        from whale_core.core import runtime as _runtime_facade
        try:
            plan = _runtime_facade.plan_from_model_text(
                self.intent_model.plan(
                    plan_text, context=self._intent_context(
                        env.conversation_id, text,
                        allowed_spaces=_ctx_spaces(env.context))))
        except IntentPlanError:
            return _Decision(route="chat", receipt="intent:parse-failed→plain-chat")
        except Exception:
            return _Decision(route="chat", receipt="intent:model-error→plain-chat")
        if plan.kind == "chat":
            return _Decision(route="chat", plan=plan)
        if plan.kind == "clarify" or plan.needs_clarification:
            return _Decision(route="clarify", plan=plan)
        slow = any(str(c).startswith(self.SLOW_CAPABILITY_PREFIXES)
                   for c in plan.capabilities)
        return _Decision(route="task", plan=plan, slow=slow)

    def _finish_decision(self, env: InputEnvelope, decision: _Decision) -> TurnResult:
        """同步消费 Decision（handle_envelope 与 async 快路径共用）。"""
        text = (env.text or "").strip()
        if decision.route == "empty":
            return TurnResult(reply="（空消息）", status="answered")
        if decision.route == "memory":
            if env.conversation_id:
                append_turn(self.store, env.conversation_id, text, decision.memory_reply)
            return TurnResult(reply=decision.memory_reply, status="answered",
                              receipts=("memory-command",))
        if decision.route == "chat":
            receipts = (decision.receipt,) if decision.receipt else ()
            plan = decision.plan
            return TurnResult(
                reply=self.companion.chat(text, conversation_id=env.conversation_id,
                                          images=env.images, context=env.context,
                                          on_delta=env.on_delta,
                                          should_stop=env.should_stop,
                                          metrics=env.metrics,
                                          on_phase=env.on_phase),
                status="answered", plan=plan, receipts=receipts,
                task_id=(plan.task_id or plan.intent_id) if plan else "")
        if decision.route == "clarify":
            plan = decision.plan
            ask = plan.clarification_question or "还需要更多信息"
            reply = self._voiced(
                env.conversation_id, text,
                f"用户的请求缺少必要信息，需要澄清这一点：{ask}\n"
                "请用你自己的人格语气自然地追问，简短，不要复读上面的原文。",
                images=env.images,
                turn_key=plan.task_id or plan.intent_id,
                allowed_spaces=_ctx_spaces(env.context),
                write_space=_ctx_write_space(env.context),
            )
            return TurnResult(reply=reply, status="clarify", plan=plan,
                              task_id=plan.task_id or plan.intent_id)
        return self._start_plan(decision.plan, env.conversation_id, text,
                                source=env.source, channel=env.channel,
                                owner_id=env.owner_id, context=env.context)

    def _intent_context(self, conversation_id: str, text: str,
                        *, allowed_spaces=None) -> str:
        """给意图模型的上下文摘要：近期会话历史 + 相关记忆（截断；不含人格禁忌全文）。

        意图模型靠它做合理默认（如地点缺省=用户所在城市/行程目的地）。
        S2：记忆召回按授权空间限域；意图模型只是提议者，不因此获得权限。
        """
        from whale_core.memory.history import load_history
        parts: list[str] = []
        if self.companion.curator is not None:
            # 查询相关记忆 + 稳定事实（空查询只放 stable——owner 所在地等
            # 缺省值来源，与查询词项未必有交集）
            seen: set[str] = set()
            from whale_core.core.companion import _recall
            for hit in list(_recall(self.companion.curator, text,
                                    allowed_spaces, limit=3)) + \
                    list(_recall(self.companion.curator, "",
                                 allowed_spaces, limit=5)):
                if hit.record.key in seen:
                    continue
                seen.add(hit.record.key)
                summary = str(hit.record.content.get("summary", "")).strip()
                if summary:
                    parts.append("记忆：" + summary[:200])
        history = load_history(self.store, conversation_id)
        for msg in history[-4:]:
            role = "用户" if msg["role"] == "user" else "鲸鱼"
            parts.append(f"{role}：{msg['content'][:160]}")
        return "\n".join(parts)[:1500]
