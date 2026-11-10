"""对话编排（issue #30 拆分，G1-A）：统一入口与唯一规划点。

一次请求至多一次规划；慢任务两段式（台账先于 ack，后台执行经同一
``_dispatch_step`` 门控）。模型输出只是提议，核心裁决后才派发。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import replace
from uuid import uuid4

from whale_core.core.intent_plan import IntentPlanError
from whale_core.core.memory_commands import handle_memory_command
from whale_core.core.runtime.context_resolution import _ctx_spaces, _ctx_write_space
from whale_core.core.runtime.envelope import InputEnvelope, TurnResult, _Decision
from whale_core.memory.history import append_turn, conversation_lock


#: 第一条回复的中性知会（实时信息后台补；不编造）。2026-11-06 N100 实测对比
#: 加/不加：不加时「天气怎么样」第一条会说「看不到实时天气、你自己看窗外」，
#: 与随后补来的和风数据自相矛盾；加这句后第一条自然说「还没查、去看看」，
#: 「晚上好」「今天累了」等闲聊不受影响。WHALE_FIRST_REPLY_NOTE 可覆盖（"off" 关闭）。
#: 二次实测（同会话连问）：措辞需限定「本条新消息」并声明之前查到的结果可信，
#: 否则 DS 会把上一轮补来的真实数据当成编的；并去掉「要不要我查」式反问。
#: 三次实测（8 样本）：不点明「你是能查的」时约 1/4 会说「看不到实时天气、你自己看手机」；
#: 加上后 8/8 自然说「我看看/稍等」，闲聊 8/8 不受影响。
DEFAULT_FIRST_REPLY_NOTE = (
    "本条新消息涉及的实时信息（天气、新闻、网页等）还没查——你是能查的，"
    "后台工具会稍后把结果补给你；如果确实要查，先自然说一声正在找就行，"
    "不用问要不要查、也别让对方自己去看，别编具体数据。"
    "之前对话里已经查到说过的结果照常可信。普通聊天照常回。")


# 第一条里「先答应待会儿」的口吻（我去搜/稍等/查到了告诉你…）。
_PROMISE_RE = re.compile(
    r"(稍等|等我|等一下|等下|等会|待会|稍后|马上|一会儿|我去|我看看|我查|我搜|我找|"
    r"去(看|查|搜|找|翻|瞅|瞄|问)|(查|搜|找|看|翻|问)一?(下|查|搜|找|看|翻|问)|瞅一眼|瞄一眼|"
    r"查到了?(再|就)?(告诉|跟你说)|找到了?(再|就)?(告诉|跟你说))")


def reply_promises_followup(reply: str) -> bool:
    """回复模型第一条是否先答应了「待会儿给结果」。"""
    return bool(_PROMISE_RE.search(reply or ""))


def apply_parallel_env(orch, env) -> None:
    """从环境变量配置并行链路（第一条知会 / 意图后台超时）。"""
    raw = (env.get("WHALE_FIRST_REPLY_NOTE") if env is not None else None)
    if raw is None:
        note = DEFAULT_FIRST_REPLY_NOTE
    else:
        note = "" if raw.strip().lower() in ("", "off", "0", "none") else raw.strip()
    orch.FIRST_REPLY_NOTE = note
    try:
        timeout = float((env or {}).get("WHALE_INTENT_BG_TIMEOUT_S") or 30)
        if timeout > 0:
            orch.PARALLEL_INTENT_TIMEOUT_S = timeout
    except (TypeError, ValueError):
        pass
    try:
        grace = float((env or {}).get("WHALE_PROMISE_FALLBACK_S") or 8)
        orch.PROMISE_FALLBACK_S = grace if grace > 0 else 0.0
    except (TypeError, ValueError):
        pass


def intent_brain_max_tokens(env) -> int | None:
    """意图脑输出上限（AgentScope kimi-k3 推理吃预算 → 空 content）；0 = 不传。"""
    try:
        value = int((env or {}).get("WHALE_INTENT_MAX_TOKENS") or 4096)
    except (TypeError, ValueError):
        value = 4096
    return value if value > 0 else None


def intent_brain_timeout(env) -> float:
    """意图脑单次请求超时（秒，默认 15；客户端瞬时错误会再试一次）。"""
    try:
        value = float((env or {}).get("WHALE_INTENT_TIMEOUT_S") or 15)
    except (TypeError, ValueError):
        value = 15.0
    return value if value > 0 else 15.0


def intent_skip_chitchat(env) -> bool:
    return str((env or {}).get("WHALE_INTENT_SKIP_CHITCHAT") or "").strip() == "1"


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
            t0 = time.perf_counter()
            decision = self._decide(env)
            intent_ms = int((time.perf_counter() - t0) * 1000)
            t1 = time.perf_counter()
            result = self._finish_decision(env, decision)
            after_ms = int((time.perf_counter() - t1) * 1000)
            # chat/clarify/memory：after = 回复模型；task：after 含工具+组装（粗粒度 tool_ms）。
            if decision.route == "task":
                tool_ms, reply_ms = after_ms, 0
            else:
                tool_ms, reply_ms = 0, after_ms
            return replace(
                result,
                intent_ms=intent_ms,
                tool_ms=tool_ms,
                reply_ms=reply_ms,
            )

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

    # ---- 2026-11-06：DS 先回 + 意图后台并行 + 需要时跟进第二条 ----

    #: 第一条回复附带的中性系统说明（数据，非用户原话）。None/空 = 不加。
    #: 宿主可按实测效果覆盖（WHALE_FIRST_REPLY_NOTE）。
    FIRST_REPLY_NOTE = ""
    #: 后台意图等待上限（秒）；超时视为不需要工具（记日志）。
    PARALLEL_INTENT_TIMEOUT_S = 30.0
    #: 第一条已「先答应待会儿」而意图模型迟迟不回（AgentScope kimi 实测 5–45s 抖动）：
    #: 等这么多秒后改用只读搜索兑现承诺（意图晚到的结果丢弃）。0 = 关闭。
    PROMISE_FALLBACK_S = 8.0

    def handle_envelope_parallel(self, env: InputEnvelope, *, on_followup=None,
                                 on_settled=None,
                                 first_note: str | None = None) -> TurnResult:
        """第一条回复不等意图：回复模型（DS）立刻回答，意图模型（Kimi）同时后台规划。

        - 意图结果为 chat / clarify / 失败 / 空 / 超时 → 不传任何信息给回复模型，
          本轮结束（``on_followup`` 不会被调用）；
        - 意图为 task → 走既有任务链（台账 → ``_dispatch_step`` 的 ACL/网关/
          Shell 策略门控，裁决与同步路径完全一致）→ 回复模型拿「第一条已说内容
          + 工具结果 + 简短备注」生成跟进第二条，写入会话历史（并入同一轮的
          助手消息），再经 ``on_followup(TurnResult)`` 交宿主投递。

        空消息/显式记忆指令等确定性捷径仍同步处理（不调意图模型）。
        ``on_settled(route, followup_or_None)`` 在后台判定/跟进结束时总会调用一次
        （route ∈ chat/clarify/task/task-error/…），供宿主（眼镜轮询）收口。
        返回第一条的 TurnResult；``reply_ms`` 为第一条耗时。
        """
        import concurrent.futures as _cf
        env = self._ensure_context(env)
        t_start = time.perf_counter()
        shortcut = self._decide_shortcuts(env)
        if shortcut is not None or self.intent_model is None:
            decision = shortcut or _Decision(route="chat")
            with conversation_lock(self.store, env.conversation_id):
                result = self._finish_decision(env, decision)
            if on_settled is not None:
                try:
                    on_settled("shortcut", None)
                except Exception:
                    pass
            return replace(result, reply_ms=int((time.perf_counter() - t_start) * 1000))
        text = (env.text or "").strip()
        box: dict = {}

        def _plan() -> _Decision:
            t0 = time.perf_counter()
            try:
                return self._decide_intent(env)
            finally:
                box["intent_ms"] = int((time.perf_counter() - t0) * 1000)

        fut: _cf.Future = _cf.Future()

        def _plan_thread() -> None:
            try:
                fut.set_result(_plan())
            except BaseException as exc:  # pragma: no cover - _decide_intent 已兜底
                fut.set_exception(exc)

        threading.Thread(target=_plan_thread, daemon=True,
                         name="whale-intent-bg").start()
        note = self.FIRST_REPLY_NOTE if first_note is None else first_note
        t1 = time.perf_counter()
        first = self._first_reply(env, text, note)
        reply_ms = int((time.perf_counter() - t1) * 1000)
        first_result = TurnResult(reply=first, status="answered", reply_ms=reply_ms)
        timeout_s = float(self.PARALLEL_INTENT_TIMEOUT_S)

        def _follow() -> None:
            route, out = "error", None
            try:
                route, out = _follow_inner()
            finally:
                if on_settled is not None:
                    try:
                        on_settled(route, out)
                    except Exception:
                        pass

        promised = reply_promises_followup(first)
        grace = float(self.PROMISE_FALLBACK_S or 0)

        def _follow_inner():
            try:
                if promised and grace > 0 and grace < timeout_s:
                    try:
                        decision = fut.result(timeout=grace)
                    except _cf.TimeoutError:
                        decision = self._promise_fallback_decision(env, text)
                        if decision is None:
                            decision = fut.result(timeout=max(0.0, timeout_s - grace))
                        else:
                            box["intent_ms"] = int((time.perf_counter() - t1) * 1000)
                else:
                    decision = fut.result(timeout=timeout_s)
            except _cf.TimeoutError:
                decision = _Decision(route="chat", receipt="intent:timeout→no-followup")
                box.setdefault("intent_ms", int((time.perf_counter() - t1) * 1000))
            except Exception:
                decision = _Decision(route="chat", receipt="intent:error→no-followup")
            intent_ms = int(box.get("intent_ms", 0) or 0)
            if decision.route != "task" or decision.plan is None:
                # 2026-11-10：heavy chat（复杂创作/长文，无工具）由重模型
                # 跟进第二条——第一条快回复先垫场，全文随后（超长落文件）。
                if decision.route == "chat" and decision.plan is not None \
                        and getattr(decision.plan, "heavy", False) \
                        and getattr(self, "heavy_companion", None) is not None \
                        and on_followup is not None:
                    try:
                        reply2 = self.heavy_companion.chat(
                            text, conversation_id=env.conversation_id,
                            images=env.images, context=env.context,
                            turn_key=decision.plan.intent_id)
                        reply2, file_ids = self._attach_long_file(
                            reply2, decision.plan)
                        fup = TurnResult(reply=reply2, status="answered",
                                         plan=decision.plan,
                                         file_ids=file_ids,
                                         task_id=decision.plan.intent_id)
                        self._log_parallel(env, route="heavy-chat",
                                           reply_ms=reply_ms, intent_ms=intent_ms)
                        on_followup(fup)
                        return "heavy-chat", fup
                    except Exception:
                        self._log_parallel(env, route="heavy-chat-error",
                                           reply_ms=reply_ms, intent_ms=intent_ms)
                        return "heavy-chat-error", None
                self._log_parallel(env, route=decision.route, reply_ms=reply_ms,
                                   intent_ms=intent_ms, receipt=decision.receipt)
                return decision.route, None
            plan = decision.plan
            # 2026-11-10：授权级任务三级制——主人直执；授权会话先审核；其余拒。
            if getattr(plan, "auth_level", "") == "owner" \
                    and not env.owner_id:
                _deny_note = ""
                if not self._task_authz_ok(plan,
                                           conversation_id=env.conversation_id,
                                           owner_id=env.owner_id):
                    _deny_note = ("用户请求的是授权级任务（调研/落文件/高成本操作），"
                                  "这类活儿只在主人本人或主人授权的会话里开工；"
                                  "本次没有执行任何工具。用你的人格简短说明，"
                                  "别声称查过资料。")
                else:
                    concern = self._review_restricted(plan, text)
                    if concern:
                        _deny_note = (f"这个授权级任务经审核没通过：{concern}。"
                                      "本次没有执行任何工具。用你的人格如实简短说明，"
                                      "别硬做。")
                if _deny_note:
                    reply2 = self._voiced(env.conversation_id, text, _deny_note)
                    fup = TurnResult(reply=reply2, status="answered", plan=plan)
                    self._log_parallel(env, route="task-denied", reply_ms=reply_ms,
                                       intent_ms=intent_ms,
                                       receipt="authz:owner-only")
                    try:
                        on_followup(fup)
                    except Exception:
                        pass
                    return "task-denied", fup
            if decision.slow:
                plan = replace(plan, timeout_s=max(plan.timeout_s, 240.0))
            t2 = time.perf_counter()
            try:
                task_id, plan = self._persist_task(
                    plan, conversation_id=env.conversation_id, text=text,
                    source=env.source, channel=env.channel, owner_id=env.owner_id,
                    async_delivery=on_followup is not None, context=env.context)
                self._mark_followup(task_id, first)
                result = self._execute_task(task_id)
            except Exception as exc:
                self._log_parallel(env, route="task-error", reply_ms=reply_ms,
                                   intent_ms=intent_ms, receipt=type(exc).__name__)
                return "task-error", None
            total_ms = int((time.perf_counter() - t2) * 1000)
            followup_ms = int(self._followup_ms.pop(result.task_id, 0) or 0)
            tool_ms = max(0, total_ms - followup_ms)
            result = replace(result, intent_ms=intent_ms, tool_ms=tool_ms,
                             reply_ms=followup_ms)
            self._log_parallel(env, route="task", reply_ms=reply_ms,
                               intent_ms=intent_ms, tool_ms=tool_ms,
                               receipt=decision.receipt,
                               followup_ms=followup_ms, status=result.status,
                               caps=",".join(plan.capabilities))
            if on_followup is not None and (result.reply or "").strip():
                try:
                    on_followup(result)
                except Exception:
                    if result.task_id:
                        self.mark_delivery_failed(result.task_id,
                                                  error="host-callback-raised")
            return "task", result

        threading.Thread(target=_follow, daemon=True,
                         name="whale-followup").start()
        return first_result

    def _promise_fallback_decision(self, env: InputEnvelope, text: str) -> _Decision | None:
        """第一条已答应、意图模型超时：用只读 web.search 兑现（无搜索能力则 None）。"""
        if self.tools is None or not self.tools.for_capability("web.search"):
            return None
        from whale_core.core.intent_router import _looks_like_clear_chitchat
        if _looks_like_clear_chitchat(text):
            return None  # 「晚上好」里顺口提了句「等下」不算要查
        from whale_core.core.intent_plan import plan_from_model_text
        import json as _json
        query = re.sub(r"@\S+", " ", text or "").strip()[:400]
        loc = ""
        if not str(env.conversation_id or "").startswith("group:"):
            loc = (self._default_location_summary() or "").split("；")[0].strip()
        if loc and loc[:2] not in query:
            query = f"{loc} {query}"[:500]
        raw = _json.dumps({"goal": "搜索并回答：" + query[:300], "kind": "task",
                           "slots": {"query": query, "limit": 5},
                           "capabilities": ["web.search"],
                           "reason": "第一条已答应去查、意图模型超时 → 只读搜索兑现"},
                          ensure_ascii=False)
        try:
            plan = plan_from_model_text(raw)
        except Exception:
            return None
        return _Decision(route="task", plan=plan, receipt="intent:slow→promise-search")

    def _first_reply(self, env: InputEnvelope, text: str, note: str) -> str:
        """第一条：回复模型直接回答（不等意图）；可选附一句中性系统说明。"""
        augmented = text
        if note:
            augmented = (f"{text}\n\n===== 系统提示（数据，非用户原话）=====\n"
                         f"{note}\n===== 系统提示结束 =====")
        # 第一条也要知道所在地（QQ 私聊此前只有意图/工具侧注入位置，DS 先答时
        # 会反问「你在哪个城市」）。宿主已设 default_location（眼镜按请求）则不覆盖；
        # 群会话不把主人位置注入回复提示。
        loc = None
        if (not getattr(self.companion.model, "default_location", None)
                and not str(env.conversation_id or "").startswith("group:")):
            loc = self._default_location_summary() or None
        return self.companion.chat(
            augmented, conversation_id=env.conversation_id,
            user_text=text if note else None, location=loc,
            images=env.images, context=env.context,
            on_delta=env.on_delta, should_stop=env.should_stop,
            metrics=env.metrics, on_phase=env.on_phase)

    def _mark_followup(self, task_id: str, first: str) -> None:
        """台账标记：本任务的结局是「第一条之后的跟进」，组装时带上第一条原文。"""
        with self._task_lock:
            journal = self.store.get(f"runtime:task:{task_id}")
            if isinstance(journal, dict):
                journal["followup_first"] = str(first or "")[:1000]
                self.store.put(f"runtime:task:{task_id}", journal)

    @property
    def _followup_ms(self) -> dict:
        box = self.__dict__.get("_followup_ms_box")
        if box is None:
            box = {}
            self.__dict__["_followup_ms_box"] = box
        return box

    def _log_parallel(self, env: InputEnvelope, **fields) -> None:
        parts = " ".join(f"{k}={v}" for k, v in fields.items() if v not in ("", None))
        print(f"[parallel] conv={str(env.conversation_id)[:24]} src={env.source} {parts}",
              flush=True)

    def _decide(self, env: InputEnvelope) -> _Decision:
        """G1-A 唯一规划点：一次请求至多一次意图模型调用。

        顺序（与 roadmap §6.2 一致）：空消息 → 记忆指令捷径（不调模型）→
        无意图模型纯对话 → 意图规划（模型提议、核心裁决，失败回落纯对话）。
        """
        shortcut = self._decide_shortcuts(env)
        if shortcut is not None:
            return shortcut
        if self.intent_model is None:
            return _Decision(route="chat")
        return self._decide_intent(env)

    def _decide_shortcuts(self, env: InputEnvelope) -> _Decision | None:
        """意图前的确定性捷径：空消息 / 显式记忆指令（不调模型）。None = 继续。"""
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
        return None

    def _decide_intent(self, env: InputEnvelope) -> _Decision:
        """意图规划（模型提议、核心裁决；失败/空返回回落纯对话）。"""
        text = (env.text or "").strip()
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
            # 2026-11-10：heavy 计划（长文写作/复杂创作）由重模型陪伴作答；
            # 超长结局落 1 天期附件文件（正文只留引子）
            comp = self.companion
            if plan is not None and getattr(plan, "heavy", False) \
                    and getattr(self, "heavy_companion", None) is not None:
                comp = self.heavy_companion
            reply = comp.chat(text, conversation_id=env.conversation_id,
                              images=env.images, context=env.context,
                              on_delta=env.on_delta,
                              should_stop=env.should_stop,
                              metrics=env.metrics,
                              on_phase=env.on_phase)
            reply, file_ids = self._attach_long_file(reply, plan)
            return TurnResult(
                reply=reply,
                status="answered", plan=plan, receipts=receipts,
                task_id=(plan.task_id or plan.intent_id) if plan else "",
                file_ids=file_ids)
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

    def _default_location_summary(self) -> str:
        """QQ/编排器缺省位置：眼镜最近上报 → WHALE_DEFAULT_CITY 等 env。

        眼镜路径另有请求体包装覆盖本行；QQ 群/私聊此前未注入，导致天气
        无口述城市时 slots 无 location_hint，和风把整句问句当城市名失败。
        """
        import os
        try:
            cached = self.store.get("glasses:last_location")
        except Exception:
            cached = None
        if isinstance(cached, dict):
            summary = str(cached.get("summary") or "").strip()
            if summary and "未知" not in summary:
                return summary[:200]
        for key in ("WHALE_DEFAULT_CITY", "WHALE_HOME_CITY", "WHALE_LOCATION"):
            val = (os.environ.get(key) or "").strip()
            if val:
                return val[:200]
        return ""

    def _intent_context(self, conversation_id: str, text: str,
                        *, allowed_spaces=None) -> str:
        """给意图模型的上下文摘要：近期会话历史 + 相关记忆（截断；不含人格禁忌全文）。

        意图模型靠它做合理默认（如地点缺省=用户所在城市/行程目的地）。
        S2：记忆召回按授权空间限域；意图模型只是提议者，不因此获得权限。
        """
        from whale_core.memory.history import load_history
        from whale_core.core.prompt_composer import format_location_context
        parts: list[str] = []
        # 位置行必须在前：SearchIntentRouter 天气/附近规则读「当前位置：」。
        # 眼镜 wrapper 会再前置请求体位置并覆盖本缺省。
        parts.append(format_location_context(
            location=self._default_location_summary() or None))
        # 群会话：意图上下文并入群时间线（2026-11-01 生产实证：observe 的群消息
        # 不进会话历史，规划者只看历史等于半盲——「说你呢」这类承接丢了目标）。
        if conversation_id.startswith("group:"):
            tl = getattr(self.companion, "timeline", None)
            if tl is not None:
                block = tl.render_for_model(
                    conversation_id, limit=12,
                    self_uid=getattr(self.companion, "self_uid", ""))
                if block:
                    parts.append("本群最近消息：\n" + block)
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
            role = "用户" if msg["role"] == "user" else "Enxi"
            parts.append(f"{role}：{msg['content'][:160]}")
        return "\n".join(parts)[:1500]
